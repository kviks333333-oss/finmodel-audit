"""Offline integration of the bounded harness with tiny workbooks and a fake LLM."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from finmodel_audit.pipeline import (
    AnalysisCancelled, AnalysisConfig, AnalysisError, BASE_LIMITATIONS,
    ReportValidationError, STAGES, _compact_messages, _pack_tool_result, _stage_navigation, _evidence_memo,
    _register_sent_facts, _data_message, _agent_messages, _json, run_analysis, validate_stage_report,
)
from finmodel_audit.providers import ProviderAdapter, ProviderError
from finmodel_audit.workbook import Cell, load_workbook


def make_workbook(path, value=100):
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets><sheet name="Model" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{main}"><sheetData><row r="1"><c r="A1"><v>{value}</v></c><c r="B1"><f>A1*2</f><v>{value * 2}</v></c></row></sheetData></worksheet>')


def make_fact_workbook(path):
    make_workbook(path, 12345)
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    extra = ('<row r="2"><c r="A2" t="inlineStr"><is><t>Operating profit</t></is></c></row>'
             '<row r="3"><c r="A3"><v>2029</v></c></row>'
             '<row r="4"><c r="A4" t="inlineStr"><is><t>RUB M</t></is></c></row>')
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b"</sheetData>", extra.encode() + b"</sheetData>")
    with ZipFile(path, "w") as archive:
        for name, body in parts.items():
            archive.writestr(name, body)


def bound_fact(card_id="operating-2029", metric="Operating profit", period=2029):
    return {"id": card_id, "source_sheet": "Model", "value_cell": "A1", "value": 12345,
            "formula": None, "metric_label": metric, "metric_cell": "A2", "metric_key": "operating_profit",
            "period": period, "period_cell": "A3", "unit": "RUB M", "unit_cell": "A4",
            "period_type": None, "period_type_cell": None, "binding_status": "bound", "issues": []}


def fact_claim(card):
    return {"fact_id": card["id"], **{key: card[key] for key in ("metric_label", "period", "unit", "source_sheet")}}


class FakeTools:
    def __init__(self, workbook):
        self.workbook = workbook
        self.retrieved_cells = set()

    def schemas(self):
        return [{"name": "read_cells", "description": "Read cells", "parameters": {
            "type": "object", "properties": {"sheet": {"type": "string"}, "cell": {"type": "string"}},
            "required": ["sheet", "cell"],
        }}]

    def call(self, name, arguments):
        if name != "read_cells":
            raise AssertionError("Unknown tools must not execute")
        sheet, address = arguments["sheet"], arguments["cell"]
        cell = self.workbook.sheets[sheet].cells[address]
        self.retrieved_cells.add((sheet, address))
        return {"sheet": sheet, "cell": address, "value": cell.value, "formula": cell.formula}


def stage_report(stage, cell="A1"):
    return {
        "id": stage, "title": dict(STAGES)[stage], "status": "limited",
        "summary": "Прочитано сохранённое значение, финансовая проверка ограничена.",
        "observations": [{
            "title": "Сохранённый показатель", "detail": "В исходной книге есть числовое значение.",
            "evidence": [{"sheet": "Model", "cell": cell}], "status": "observation",
            "impact": "Влияние на итоговые показатели не проверено.",
        }],
        "limitations": ["Внешних данных нет."],
    }


def function_response(name, arguments, model="fake-v1", usage=True):
    result = {"model": model, "choices": [{
        "finish_reason": "function_call",
        "message": {"role": "assistant", "content": "", "functions_state_id": "test-state",
                    "function_call": {"name": name, "arguments": arguments}},
    }]}
    if usage:
        result["usage"] = {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20}
    return result


class FakeClient:
    provider_name = "fake"
    model = "fake"
    model_version = "fake-v1"

    def __init__(self, *, fail_at=None, text_only=False, invalid_first=False, missing_usage=False):
        self.calls = []
        self.fail_at = fail_at
        self.text_only = text_only
        self.invalid_first = invalid_first
        self.missing_usage = missing_usage

    def chat(self, messages, functions=None, function_call=None, *, max_tokens=None, temperature=None, session_id=None):
        self.calls.append(json.loads(json.dumps({"messages": messages, "functions": functions,
            "function_call": function_call, "max_tokens": max_tokens, "temperature": temperature, "session_id": session_id})))
        if self.fail_at and len(self.calls) == self.fail_at:
            raise RuntimeError("private-provider-secret")
        stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
        if self.text_only:
            return {"model": self.model_version, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(stage_report(stage))}}], "usage": {"total_tokens": 20}}
        if self.invalid_first and len(self.calls) == 1:
            return function_response("submit_stage_report", stage_report(stage, "B1"), self.model_version)
        if any(item.get("role") == "function" and item.get("name") == "read_cells" for item in messages):
            return function_response("submit_stage_report", stage_report(stage), self.model_version, usage=not self.missing_usage)
        return function_response("read_cells", {"sheet": "Model", "cell": "A1"}, self.model_version, usage=not self.missing_usage)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "model.xlsx"
        self.output = self.root / "output"
        make_workbook(self.path)
        module = types.ModuleType("finmodel_audit.tools")
        module.WorkbookTools = FakeTools
        fake_tools = patch.dict(sys.modules, {"finmodel_audit.tools": module})
        fake_tools.start()
        self.addCleanup(fake_tools.stop)

    def test_four_stages_use_tools_and_validate_retrieved_evidence(self):
        client = FakeClient()
        progress = []
        original = self.path.read_bytes()
        result = run_analysis(self.path, self.output, client=client, on_progress=progress.append)
        self.assertEqual([s["id"] for s in result["stages"]], [stage for stage, _ in STAGES])
        self.assertEqual(len(client.calls), 8)
        self.assertTrue(all(s["completion"] == "submitted" for s in result["stages"]))
        self.assertEqual(result["usage"]["total_tokens"], 160)
        self.assertEqual(result["coverage"]["retrieved_cells"], 1)
        self.assertFalse(result["verification"]["recalculated"])
        self.assertIn(BASE_LIMITATIONS[1], result["stages"][0]["limitations"])
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(result["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertTrue((self.output / "report.json").is_file())
        self.assertEqual(progress[-1]["progress"], 100)
        self.assertEqual(next(event["tool"] for event in progress if event["event"] == "tool_result"), "read_cells")
        second_request = client.calls[1]
        self.assertEqual(second_request["messages"][-2]["functions_state_id"], "test-state")
        self.assertIn("untrusted_workbook_data", second_request["messages"][-1]["content"])
        actual_function_data = json.loads(second_request["messages"][-1]["content"])["untrusted_workbook_data"]
        self.assertEqual(actual_function_data["cell_data"]["Model"][0][1], 100)
        self.assertNotIn("result_available_in_evidence_memo", actual_function_data)
        self.assertEqual(second_request["max_tokens"], 2500)

    def test_unread_existing_citation_requires_correction_then_tool_read(self):
        client = FakeClient(invalid_first=True)
        result = run_analysis(self.path, self.output, client=client)
        second_call_messages = client.calls[1]["messages"]
        self.assertIn("report_validation_failed", second_call_messages[-1]["content"])
        self.assertEqual(result["stages"][0]["observations"][0]["evidence"][0]["cell"], "A1")
        self.assertEqual(len(client.calls), 9)

    def test_text_or_json_outside_submit_function_is_never_published(self):
        client = FakeClient(text_only=True)
        result = run_analysis(self.path, self.output, client=client,
                              config=AnalysisConfig(max_requests_per_stage=2))
        self.assertEqual(len(client.calls), 8)
        self.assertTrue(all(stage["observations"] == [] for stage in result["stages"]))
        self.assertTrue(all(stage["completion"] == "incomplete" for stage in result["stages"]))
        self.assertTrue(all("лимит" in stage["summary"] for stage in result["stages"]))

    def test_complete_checkpoint_skips_all_stage_calls(self):
        first = run_analysis(self.path, self.output, client=FakeClient())
        second_client = FakeClient()
        second = run_analysis(self.path, self.output, client=second_client)
        self.assertEqual(second_client.calls, [])
        self.assertEqual(second["checkpoint"]["resumed_stages"], 4)
        self.assertEqual(first["stages"], second["stages"])

    def test_checkpoint_identity_changes_for_source_model_and_config(self):
        first = run_analysis(self.path, self.output, client=FakeClient())
        make_workbook(self.path, 200)
        second = run_analysis(self.path, self.output, client=FakeClient())
        self.assertNotEqual(first["checkpoint"]["fingerprint"], second["checkpoint"]["fingerprint"])
        version_client = FakeClient()
        version_client.model_version = "fake-v2"
        third = run_analysis(self.path, self.output, client=version_client)
        fourth = run_analysis(self.path, self.output, client=version_client, config=AnalysisConfig(max_requests_per_stage=7))
        self.assertNotEqual(second["checkpoint"]["fingerprint"], third["checkpoint"]["fingerprint"])
        self.assertNotEqual(third["checkpoint"]["fingerprint"], fourth["checkpoint"]["fingerprint"])

    def test_provider_error_retains_stage_and_safe_retry_state(self):
        with self.assertRaises(AnalysisError) as caught:
            run_analysis(self.path, self.output, client=FakeClient(fail_at=4))
        self.assertNotIn("private-provider-secret", str(caught.exception))
        state_file = next(self.output.glob("checkpoints/*/state.json"))
        state = json.loads(state_file.read_text())
        self.assertEqual(list(state["completed"]), ["audit"])
        self.assertNotIn("private-provider-secret", state_file.read_text())
        self.assertEqual(state["active"]["id"], "assumptions")
        resumed = FakeClient()
        result = run_analysis(self.path, self.output, client=resumed)
        self.assertEqual(result["checkpoint"]["resumed_stages"], 1)
        self.assertEqual(len(resumed.calls), 5)

    def test_cancellation_is_saved_and_resume_continues_after_completed_stage(self):
        cancel = {"requested": False}
        def progress(event):
            if event["event"] == "stage_complete" and event["stage"] == "audit":
                cancel["requested"] = True
        with self.assertRaises(AnalysisCancelled):
            run_analysis(self.path, self.output, client=FakeClient(),
                         on_progress=progress, cancel_check=lambda: cancel["requested"])
        state = json.loads(next(self.output.glob("checkpoints/*/state.json")).read_text())
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(list(state["completed"]), ["audit"])
        result = run_analysis(self.path, self.output, client=FakeClient())
        self.assertEqual(result["checkpoint"]["resumed_stages"], 1)

    def test_cancellation_after_response_does_not_save_an_unanswered_tool_call(self):
        client = FakeClient()
        cancelled = {"value": False}
        original = client.chat
        def cancel_after_response(*args, **kwargs):
            response = original(*args, **kwargs)
            cancelled["value"] = True
            return response
        client.chat = cancel_after_response
        with self.assertRaises(AnalysisCancelled):
            run_analysis(self.path, self.output, client=client, cancel_check=lambda: cancelled["value"])
        state = json.loads(next(self.output.glob("checkpoints/*/state.json")).read_text())
        self.assertEqual(state["active"]["messages"][-1]["role"], "user")
        self.assertEqual(state["usage"]["total_tokens"], 20)
        result = run_analysis(self.path, self.output, client=FakeClient())
        self.assertEqual(len(result["stages"]), 4)

    def test_request_and_token_budgets_produce_explicit_limited_stages(self):
        for config in (AnalysisConfig(max_total_requests=1), AnalysisConfig(max_total_tokens=1)):
            with self.subTest(config=config):
                client = FakeClient()
                result = run_analysis(self.path, self.output, client=client, resume=False, config=config)
                self.assertLessEqual(len(client.calls), 1)
                self.assertTrue(all(s["status"] == "limited" for s in result["stages"]))
                self.assertTrue(all(not s["observations"] for s in result["stages"]))

    def test_missing_usage_is_counted_conservatively(self):
        result = run_analysis(self.path, self.output, client=FakeClient(missing_usage=True),
                              config=AnalysisConfig(max_total_tokens=500_000))
        self.assertEqual(result["usage"]["missing_usage_responses"], 8)
        self.assertGreater(result["usage"]["estimated_tokens"], 0)

    def test_oversized_tool_output_does_not_authorize_omitted_evidence(self):
        class OversizedTools(FakeTools):
            def call(self, name, arguments):
                result = super().call(name, arguments)
                result["huge"] = "x" * 3000
                return result
        module = types.ModuleType("finmodel_audit.tools")
        module.WorkbookTools = OversizedTools
        with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
            result = run_analysis(self.path, self.output, client=FakeClient(),
                                  config=AnalysisConfig(max_tool_result_chars=100, max_requests_per_stage=3))
        self.assertEqual(result["coverage"]["retrieved_cells"], 0)
        self.assertTrue(all(not stage["observations"] for stage in result["stages"]))

    def test_elapsed_time_budget_stops_before_another_request(self):
        clock = {"value": 0.0}
        client = FakeClient()
        original = client.chat
        def slow_request(*args, **kwargs):
            response = original(*args, **kwargs)
            clock["value"] += 15
            return response
        client.chat = slow_request
        with patch("finmodel_audit.pipeline.time.monotonic", side_effect=lambda: clock["value"]):
            result = run_analysis(self.path, self.output, client=client,
                                  config=AnalysisConfig(max_total_seconds=10))
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all("времени" in stage["summary"] for stage in result["stages"]))

    def test_local_mode_does_not_construct_or_call_provider(self):
        with patch("finmodel_audit.pipeline.get_provider", side_effect=AssertionError("No LLM")):
            result = run_analysis(self.path, self.output, client=FakeClient(), mode="local")
        self.assertEqual(result["usage"]["requests"], 0)
        self.assertEqual([s["status"] for s in result["stages"]], ["limited", "skipped", "limited", "skipped"])
        self.assertEqual(result["mode"], "local")
        self.assertTrue(all(stage["completion"] == "skipped" for stage in result["stages"]))

    def test_model_version_change_stops_before_publishing_report(self):
        client = FakeClient()
        original = client.chat
        def changed(*args, **kwargs):
            response = original(*args, **kwargs)
            response["model"] = "changed-version"
            return response
        client.chat = changed
        with self.assertRaisesRegex(AnalysisError, "version changed"):
            run_analysis(self.path, self.output, client=client)
        self.assertFalse((self.output / "report.json").exists())

    def test_unknown_tool_is_not_executed(self):
        client = FakeClient()
        original = client.chat
        def unknown(*args, **kwargs):
            if not client.calls:
                client.calls.append({})
                return function_response("execute_python", {"code": "danger"})
            return original(*args, **kwargs)
        client.chat = unknown
        result = run_analysis(self.path, self.output, client=client)
        self.assertEqual(len(result["stages"]), 4)
        self.assertIn("tool_not_allowlisted", client.calls[1]["messages"][-1]["content"])

    def test_output_alias_cannot_overwrite_workbook(self):
        self.output.mkdir()
        original = self.path.read_bytes()
        (self.output / "report.json").symlink_to(self.path)
        with self.assertRaises(AnalysisError):
            run_analysis(self.path, self.output, mode="local")
        self.assertEqual(self.path.read_bytes(), original)

    def test_validation_rejects_absent_cell_and_unread_cell(self):
        workbook = load_workbook(self.path)
        for cell in ("A1", "Z999"):
            with self.subTest(cell=cell):
                with self.assertRaises(ReportValidationError):
                    validate_stage_report(stage_report("audit", cell), "audit", workbook, set())
        validated = validate_stage_report(stage_report("audit"), "audit", workbook, {("Model", "A1")})
        self.assertIn(BASE_LIMITATIONS[1], validated["limitations"])

    def test_speculative_cause_is_downgraded_and_quality_verdict_is_rejected(self):
        workbook = load_workbook(self.path)
        report = stage_report("audit")
        report["observations"][0]["detail"] = "Вероятная причина ошибки — неизвестные параметры."
        result = validate_stage_report(report, "audit", workbook, {("Model", "A1")})
        self.assertEqual(result["observations"][0]["status"], "hypothesis")
        quality = stage_report("quality")
        quality["summary"] = "Модель пригодна для чернового анализа, поскольку найдено только три ошибки."
        with self.assertRaises(ReportValidationError):
            validate_stage_report(quality, "quality", workbook, {("Model", "A1")})

    def test_tool_compaction_preserves_values_and_deduplicates_context(self):
        shared_label = {"sheet": "Model", "cell": "A1", "value": "Revenue", "present": True}
        raw = {"cells": [{"sheet": "Model", "cell": f"B{i}", "value": i, "formula": "=A1",
                          "row_context": [shared_label], "column_context": [shared_label], "present": True}
                         for i in range(1, 40)], "completeness": {"complete": True}}
        packed, shown = _pack_tool_result(raw, 10_000)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(raw)) / 2)
        self.assertEqual(len(shown), 40)
        self.assertEqual(sum(row[0] == "A1" for row in packed["cell_data"]["Model"]), 1)
        self.assertIn(["B4", 4, "=A1", "formula_number"], packed["cell_data"]["Model"])
        self.assertTrue(packed["metadata"]["completeness"]["complete"])

    def test_numeric_rate_cannot_be_supported_by_label_and_unit_alone(self):
        workbook = load_workbook(self.path)
        workbook.sheets["Model"].cells["A1"].value = "GDP growth rate"
        report = stage_report("assumptions")
        report["observations"][0]["detail"] = "Темп роста составляет 1.30%."
        report["observations"][0]["impact"] = "high"
        with self.assertRaisesRegex(ReportValidationError, "only text"):
            validate_stage_report(report, "assumptions", workbook, {("Model", "A1")})
        workbook.sheets["Model"].cells["B1"].value = 0.013
        report["observations"][0]["evidence"].append({"sheet": "Model", "cell": "B1"})
        accepted = validate_stage_report(report, "assumptions", workbook, {("Model", "A1"), ("Model", "B1")})
        self.assertEqual(accepted["observations"][0]["impact"], "not_assessed")
        workbook.sheets["Model"].cells["B1"].value = 0.015
        with self.assertRaisesRegex(ReportValidationError, "does not match"):
            validate_stage_report(report, "assumptions", workbook, {("Model", "A1"), ("Model", "B1")})

    def test_year_labels_are_not_misclassified_as_financial_amounts(self):
        workbook = load_workbook(self.path)
        workbook.sheets["Model"].cells["A1"].value = "2026F"
        report = stage_report("business")
        report["observations"][0]["detail"] = "Текстовая подпись периода — 2026F."
        validate_stage_report(report, "business", workbook, {("Model", "A1")})

    def test_sensitivity_change_fraction_supports_unicode_negative_percent(self):
        workbook = load_workbook(self.path)
        report = stage_report("assumptions")
        report["observations"][0]["detail"] = "Условный сценарий изменения входа на −10%."
        report["observations"][0]["status"] = "hypothesis"
        calculations = [{"arguments": {"inputs": [{"sheet": "Model", "cell": "A1"}]},
                         "result": {"scenarios": [{"change_fraction": -0.1}]}}]
        validate_stage_report(report, "assumptions", workbook, {("Model", "A1")}, calculations)

    def test_fx_unit_basis_one_usd_is_not_a_financial_amount_claim(self):
        workbook = load_workbook(self.path)
        values = [87.2, 96.8, 101.7, 103.6, 107.3, 109.4, 111.1]
        addresses = [f"{column}14" for column in "QRSTUVW"]
        for address, value in zip(addresses, values):
            workbook.sheets["Model"].cells[address] = Cell(address, value)
        report = stage_report("assumptions")
        observation = report["observations"][0]
        observation["evidence"] = [{"sheet": "Model", "cell": address} for address in addresses]
        for unit in ("RUB за 1 USD", "RUB на 1 USD", "RUB per 1 USD"):
            with self.subTest(unit=unit):
                observation["detail"] = f"Курс в {unit}: 87.2, 96.8, 101.7, 103.6, 107.3, 109.4, 111.1."
                validate_stage_report(report, "assumptions", workbook, {("Model", address) for address in addresses})
        observation["detail"] = "Курс составляет 999.5 RUB."
        with self.assertRaisesRegex(ReportValidationError, "999.5"):
            validate_stage_report(report, "assumptions", workbook, {("Model", address) for address in addresses})

    def test_signed_us_and_russian_formatted_amounts_match_numeric_source(self):
        workbook = load_workbook(self.path)
        workbook.sheets["Model"].cells["A1"].value = -1311.782
        report = stage_report("assumptions")
        for literal in ("-1,311.782", "-1 311,782", "−1\u202f311,782", "-1.311,782"):
            with self.subTest(literal=literal):
                report["observations"][0]["detail"] = f"Сохранённая сумма составляет {literal} RUB."
                validate_stage_report(report, "assumptions", workbook, {("Model", "A1")})
        report["observations"][0]["detail"] = "Сохранённая сумма составляет -1,411.782 RUB."
        with self.assertRaises(ReportValidationError):
            validate_stage_report(report, "assumptions", workbook, {("Model", "A1")})

    def test_exact_cited_formula_constants_are_not_independent_numeric_claims(self):
        workbook = load_workbook(self.path)
        workbook.sheets["Model"].cells["A1"].formula = "=B1+2%"
        report = stage_report("audit")
        for detail in ("В источнике сохранена формула '=B1+2%'.", "Арифметическая проверка B1+2% даёт сохранённый результат."):
            report["observations"][0]["detail"] = detail
            validate_stage_report(report, "audit", workbook, {("Model", "A1")})
        for formula in ("=B1+3%", "=B1+2%+3%", "=B1+2% + 3%", "3% * B1+2%"):
            with self.subTest(formula=formula):
                report["observations"][0]["detail"] = f"В источнике сохранена формула '{formula}'."
                with self.assertRaises(ReportValidationError):
                    validate_stage_report(report, "audit", workbook, {("Model", "A1")})

    def test_fact_claims_bind_value_metric_year_unit_and_sheet(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        card = bound_fact()
        report = stage_report("business")
        report["summary"] = "Неверное числовое резюме: EBITDA за 2025 год."
        obs = report["observations"][0]
        obs["title"], obs["detail"] = "Operating profit", "Operating profit за 2029 год: 12 345 RUB M."
        obs["fact_claims"] = [fact_claim(card)]
        result = validate_stage_report(report, "business", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
        self.assertEqual(result["observations"][0]["fact_claims"][0]["value"], 12345)
        self.assertEqual(result["observations"][0]["fact_claims"][0]["verification"], "source_bound")
        self.assertNotIn("EBITDA", result["summary"])
        self.assertNotIn("2025", result["summary"])
        for field, bad in (("metric_label", "EBITDA"), ("period", 2025), ("unit", "USD"), ("source_sheet", "Other")):
            with self.subTest(field=field):
                original = obs["fact_claims"][0][field]
                obs["fact_claims"][0][field] = bad
                with self.assertRaisesRegex(ReportValidationError, "canonical binding mismatch"):
                    validate_stage_report(report, "business", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
                obs["fact_claims"][0][field] = original

    def test_fact_feedback_identifies_exact_binding_and_missing_value_source(self):
        make_fact_workbook(self.path)
        workbook = load_workbook(self.path)
        card = bound_fact()
        draft = stage_report("business")
        observation = draft["observations"][0]
        observation["fact_claims"] = [{**fact_claim(card), "metric_label": "EBITDA"}]
        with self.assertRaises(ReportValidationError) as caught:
            validate_stage_report(draft, "business", workbook, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
        issue = caught.exception.issues[0]
        self.assertEqual(issue["expected_claim"], fact_claim(card))
        self.assertEqual(issue["required_evidence"], {"sheet": "Model", "cell": "A1"})
        observation["fact_claims"] = [fact_claim(card)]
        observation["evidence"] = [{"sheet": "Model", "cell": "B1"}]
        with self.assertRaises(ReportValidationError) as caught:
            validate_stage_report(draft, "business", workbook, {("Model", "A1"), ("Model", "B1")}, fact_registry={card["id"]: card}, require_facts=True)
        self.assertEqual(caught.exception.issues[0]["code"], "fact_value_evidence_missing")
        self.assertEqual(caught.exception.issues[0]["required_evidence"], {"sheet": "Model", "cell": "A1"})

    def test_financial_finding_compaction_never_retains_claim_without_all_sources(self):
        findings = [{"id": "check", "scope": "financial", "code": "duplicate", "title": "Candidate",
                     "detail": "Inspect both terms", "evidence": [{"sheet": "Model", "cell": "A1"}, {"sheet": "Model", "cell": "B1"}]}]
        source = {"validation_findings": findings, "cell_evidence": [
            {"sheet": "Model", "cell": "A1", "value": 100},
            {"sheet": "Model", "cell": "B1", "value": 200, "formula": "=A1*2"}]}
        packed, shown = _pack_tool_result(source, 2000)
        self.assertEqual(packed["metadata"]["validation_findings"], findings)
        incomplete, _ = _pack_tool_result({**source, "cell_evidence": source["cell_evidence"][:1]}, 2000)
        self.assertEqual(incomplete["metadata"]["validation_findings"], [])
        self.assertTrue(incomplete["truncated"])
        entry = {"tool": "get_validation_findings", "arguments": {"scope": "financial"}, "result": packed}
        for size in (500, 1000, 4000):
            memo = _evidence_memo([entry], size)
            visible = {(sheet, row[0]) for sheet, rows in memo["cell_data"].items() for row in rows}
            for finding in memo["validation_findings"]:
                self.assertTrue({(r["sheet"], r["cell"]) for r in finding["evidence"]} <= visible)
            self.assertLessEqual(len(_json(memo)), size)

    def test_local_report_retains_independent_checks_without_llm_or_fake_evidence(self):
        checks = {"financial_checks": {"version": "test", "status": "complete", "findings": [
            {"id": "test", "code": "test", "title": "Candidate", "detail": "Inspect formula", "status": "candidate",
             "evidence": [{"sheet": "Model", "cell": "B1"}], "financial_materiality": "not_assessed"}], "limitations": []},
            "supplemental_audit": {"version": "test", "status": "unavailable", "findings": [], "limitations": ["Dependency unavailable"]}}
        class CheckTools(FakeTools):
            def validation_reports(inner, cancel_check=None):
                return checks
        module = types.ModuleType("finmodel_audit.tools")
        module.WorkbookTools = CheckTools
        with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
            result = run_analysis(self.path, self.output, mode="local")
        self.assertEqual(result["financial_checks"], checks["financial_checks"])
        self.assertEqual(result["supplemental_audit"]["status"], "unavailable")
        self.assertEqual(result["usage"]["requests"], 0)
        self.assertEqual(result["coverage"]["retrieved_cells"], 0)

    def test_correct_cards_replace_discarded_false_metric_or_year_prose(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        card = bound_fact()
        report = stage_report("assumptions")
        obs = report["observations"][0]
        obs["fact_claims"] = [fact_claim(card)]
        for title, detail in (("EBITDA", "За 2029 год 12 345 RUB M."),
                              ("Operating profit", "За 2025 год 12 345 RUB M.")):
            obs["title"], obs["detail"] = title, detail
            with self.subTest(title=title, detail=detail):
                result = validate_stage_report(report, "assumptions", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
                self.assertNotIn("EBITDA", _json(result))
                self.assertNotIn("2025", _json(result))
                self.assertIn("12345; период: 2029", result["observations"][0]["detail"])
                self.assertEqual(obs["detail"], detail)  # raw diagnostic draft is not mutated
        obs["title"], obs["detail"] = "Operating profit, не EBITDA", "В 2029 году 12 345 RUB M."
        validate_stage_report(report, "assumptions", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
        cash = bound_fact("cash-2029", "Cash and cash equivalents")
        obs["title"], obs["detail"], obs["fact_claims"] = "Net income", "2029: 12 345 RUB M.", [fact_claim(cash)]
        result = validate_stage_report(report, "assumptions", wb, {("Model", "A1")}, fact_registry={cash["id"]: cash}, require_facts=True)
        self.assertNotIn("Net income", _json(result))
        self.assertEqual(result["observations"][0]["title"], "Cash and cash equivalents")

    def test_card_output_drops_uncalculated_margins_and_unknown_draft_period(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        card = bound_fact("cash-flow", "FCFF", period=None)
        card.update(period_cell=None, binding_status="partial")
        for detail in ("FCFF: маржа 20.6%.", "FCFF: маржа 45.9%.", "FCFF за 2031 год: 12 345 RUB M."):
            report = stage_report("business")
            obs = report["observations"][0]
            obs.update(detail=detail, fact_claims=[fact_claim(card)])
            with self.subTest(detail=detail):
                result = validate_stage_report(report, "business", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
                published = _json(result)
                for false_literal in ("20.6", "45.9", "2031"):
                    self.assertNotIn(false_literal, published)
                self.assertIn("12345; период: не установлен", result["observations"][0]["detail"])
                self.assertEqual(result["observations"][0]["status"], "not_verified")
                obs.pop("fact_claims")
                with self.assertRaises(ReportValidationError):
                    validate_stage_report(report, "business", wb, {("Model", "A1")}, require_facts=True)
        obs["fact_claims"] = [fact_claim(card)]
        with self.assertRaisesRegex(ReportValidationError, "Unknown or unsent fact_id"):
            validate_stage_report(report, "business", wb, {("Model", "A1")}, fact_registry={}, require_facts=True)

    def test_numeric_fact_requires_card_but_partial_is_not_verified(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        card = bound_fact()
        report = stage_report("business")
        obs = report["observations"][0]
        obs["detail"] = "Operating profit 12 345 RUB M в 2029 году."
        with self.assertRaisesRegex(ReportValidationError, "require fact_claims"):
            validate_stage_report(report, "business", wb, {("Model", "A1")}, require_facts=True)
        card["binding_status"] = "partial"
        obs["fact_claims"] = [fact_claim(card)]
        result = validate_stage_report(report, "business", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)
        self.assertEqual(result["observations"][0]["status"], "not_verified")
        card["binding_status"] = "ambiguous"
        with self.assertRaisesRegex(ReportValidationError, "ambiguous"):
            validate_stage_report(report, "business", wb, {("Model", "A1")}, fact_registry={card["id"]: card}, require_facts=True)

    def test_card_tuples_replace_swapped_numeric_prose_and_unsupported_impact(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        first, second = bound_fact(), bound_fact("operating-2031", period=2031)
        second.update(value_cell="B1", value=24690)
        report = stage_report("business")
        obs = report["observations"][0]
        obs.update(title="Operating profit", detail="Operating profit: 24690 RUB M в 2029 году и 12345 RUB M в 2031 году.",
                   impact="EBITDA за 2025 год составляет 999999 RUB M.",
                   evidence=[{"sheet": "Model", "cell": "A1"}, {"sheet": "Model", "cell": "B1"}],
                   fact_claims=[fact_claim(first), fact_claim(second)])
        report["limitations"] = ["Cash за 2025 год составляет 999999 RUB M."]
        result = validate_stage_report(report, "business", wb, {("Model", "A1"), ("Model", "B1")},
                                      fact_registry={first["id"]: first, second["id"]: second}, require_facts=True)
        detail = result["observations"][0]["detail"].splitlines()
        self.assertIn("12345; период: 2029", detail[0])
        self.assertIn("24690; период: 2031", detail[1])
        self.assertEqual(result["observations"][0]["impact"], "not_assessed")
        self.assertNotIn("999999", json.dumps(result))

    def test_growth_keyword_does_not_bypass_source_fact_binding(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        report = stage_report("business")
        obs = report["observations"][0]
        obs.update(title="Revenue", detail="Выручка 2025 составила 12345 RUB M; рост 10%.")
        calculations = [{"arguments": {"inputs": [{"name": "x", "sheet": "Model", "cell": "A1"}]},
                         "result": {"result": {"value": 0.1}}}]
        with self.assertRaisesRegex(ReportValidationError, "require fact_claims"):
            validate_stage_report(report, "business", wb, {("Model", "A1")}, calculations, require_facts=True)

    def test_fact_primer_registry_and_generated_reports_resume_without_new_calls(self):
        make_fact_workbook(self.path)
        class FactTools(FakeTools):
            def schemas(inner):
                return super().schemas() + [
                    {"name": "financial_map", "parameters": {"type": "object", "properties": {"stage": {"type": "string"}}}},
                    {"name": "read_fact_cards", "parameters": {"type": "object", "properties": {"sheet": {"type": "string"}, "cells": {"type": "array"}}}}]
            def call(inner, name, arguments):
                if name not in {"financial_map", "read_fact_cards"}:
                    return super().call(name, arguments)
                evidence = [{"sheet": "Model", "cell": addr, "value": inner.workbook.sheets["Model"].cells[addr].value}
                            for addr in ("A1", "A2", "A3", "A4")]
                inner.retrieved_cells.update(("Model", cell["cell"]) for cell in evidence)
                if name == "financial_map":
                    return {"candidates": [{"sheet": "Model", "row": 1, "cells": evidence}]}
                return {"facts": [bound_fact()], "cell_evidence": evidence}
        class FactClient(FakeClient):
            def chat(inner, messages, functions=None, function_call=None, **kwargs):
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                if stage not in {"assumptions", "business"}:
                    return super().chat(messages, functions, function_call, **kwargs)
                inner.calls.append({"messages": messages, **kwargs})
                draft = {"id": stage, "fact_ids": [bound_fact()["id"]], "comparisons": [], "limitations": ["limited_scope"]}
                result = function_response("submit_stage_report", draft)
                result["usage"]["precached_prompt_tokens"] = 7
                return result
        module = types.ModuleType("finmodel_audit.tools")
        module.WorkbookTools = FactTools
        with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
            client = FactClient()
            result = run_analysis(self.path, self.output, client=client)
            self.assertEqual(len(client.calls), 6)
            self.assertEqual([card["stage_id"] for card in result["facts"]], ["assumptions", "business"])
            self.assertEqual(result["usage"]["precached_prompt_tokens"], 14)
            self.assertEqual(result["stages"][1]["selection"]["fact_ids"], [bound_fact()["id"]])
            resumed = FactClient()
            again = run_analysis(self.path, self.output, client=resumed)
            self.assertEqual(again["facts"], result["facts"])
            self.assertEqual(again["checkpoint"]["resumed_stages"], 4)
            self.assertFalse(resumed.calls)

    def test_quality_uses_structural_primer_and_at_most_four_model_requests(self):
        class QualityTools(FakeTools):
            def schemas(inner):
                return super().schemas() + [{"name": "get_structural_findings", "parameters": {"type": "object", "properties": {}}}]
            def call(inner, name, arguments):
                if name == "get_structural_findings":
                    return {"findings": [], "cell_evidence": []}
                return super().call(name, arguments)
        module = types.ModuleType("finmodel_audit.tools")
        module.WorkbookTools = QualityTools
        with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
            result = run_analysis(self.path, self.output, client=FakeClient(text_only=True))
        self.assertEqual(result["usage"]["by_stage"]["quality"]["requests"], 4)
        transcript = json.loads(next(self.output.glob("checkpoints/*/quality-transcript.json")).read_text())
        self.assertEqual(transcript["turns"][0]["tool"], "get_structural_findings")
        self.assertTrue(all(turn["request"]["function_call"] == {"name": "submit_stage_report"} for turn in transcript["turns"] if "request" in turn))

    def test_cancellation_after_automatic_repair_resumes_without_repeating_read(self):
        cancelled = {"value": False}
        def progress(event):
            if event.get("tool") == "read_cells":
                cancelled["value"] = True
        with self.assertRaises(AnalysisCancelled):
            run_analysis(self.path, self.output, client=FakeClient(invalid_first=True), on_progress=progress,
                         cancel_check=lambda: cancelled["value"])
        state = json.loads(next(self.output.glob("checkpoints/*/state.json")).read_text())
        self.assertEqual(state["active"]["repair_rounds"], 1)
        run_analysis(self.path, self.output, client=FakeClient())
        transcript = json.loads(next(self.output.glob("checkpoints/*/audit-transcript.json")).read_text())
        repairs = [turn for turn in transcript["turns"] if turn.get("reason") == "unread_evidence_repair"]
        self.assertEqual(len(repairs), 1)

    def test_fact_registry_requires_all_sources_in_actual_sent_message(self):
        make_fact_workbook(self.path)
        wb = load_workbook(self.path)
        card = bound_fact()
        evidence = [{"sheet": "Model", "cell": addr, "value": wb.sheets["Model"].cells[addr].value} for addr in ("A1", "A2", "A3", "A4")]
        packed, shown = _pack_tool_result({"facts": [card], "cell_evidence": evidence}, 14000)
        registry, stages = {}, {}
        _register_sent_facts([], registry, stages, "business", wb, shown)
        self.assertFalse(registry)
        messages = [{"role": "user", "content": _data_message(_evidence_memo([{"tool": "read_fact_cards", "arguments": {}, "result": packed}], 2000))}]
        _register_sent_facts(messages, registry, stages, "business", wb, shown - {("Model", "A4")})
        self.assertFalse(registry)
        _register_sent_facts(messages, registry, stages, "business", wb, shown)
        self.assertEqual(stages, {"business": [card["id"]]})
        self.assertEqual(registry[card["id"]]["value"], 12345)
        incomplete, _ = _pack_tool_result({"facts": [card], "cell_evidence": evidence[:-1]}, 14000)
        self.assertEqual(incomplete["metadata"]["facts"], [])
        self.assertEqual(incomplete["omitted_fact_count"], 1)

    def test_serialized_envelope_budget_counts_nested_json_escaping(self):
        base = [{"role": "system", "content": "x" * 1200}, {"role": "user", "content": "task"}]
        rows = [[f"B{i}", 'value with "quotation" and \\slash ' * 4, None, "text_label"] for i in range(1, 151)]
        rows += [["A1", 12345, None, "number"], ["A2", "Operating profit", None, "text_label"],
                 ["A3", 2029, None, "number"], ["A4", "RUB M", None, "text_label"]]
        entries = [{"tool": "read_fact_cards", "arguments": {}, "result": {"metadata": {"facts": [bound_fact()]}, "cell_data": {"Model": rows}}}]
        pair = [{"role": "assistant", "content": "", "function_call": {"name": "read_cells", "arguments": {}}, "functions_state_id": "same-state"},
                {"role": "function", "name": "read_cells", "content": _data_message({"note": '"end"\\' * 100})}]
        active = {"id": "audit", "phase": "collection", "base_messages": base, "tool_results": entries,
                  "latest_pair": pair, "latest_pair_has_data": False}
        functions = [{"name": "read_cells", "description": "d" * 7000}]
        config = AnalysisConfig(max_context_chars=20000)
        available = min(18000, max(0, config.max_context_chars - len(_json(base)) - len(_json(functions)) - len(_json(pair)) - 1000))
        legacy_messages = base + [{"role": "user", "content": _data_message(_evidence_memo(entries, available))}] + pair
        self.assertGreater(len(_json({"messages": legacy_messages, "functions": functions})), config.max_context_chars)
        messages = _agent_messages(active, config, functions, {})
        self.assertLessEqual(len(_json({"messages": messages, "functions": functions})), config.max_context_chars)
        self.assertEqual(messages[-2:], pair)
        memo = json.loads(messages[len(base)]["content"])["untrusted_workbook_data"]
        self.assertEqual(memo["facts"], [bound_fact()])
        self.assertTrue({"A1", "A2", "A3", "A4"} <= {row[0] for row in memo["cell_data"]["Model"]})
        self.assertTrue(memo["context_abridged"])

    def test_memo_drops_verbose_outcomes_before_fact_sources_and_keeps_calculation(self):
        card = bound_fact()
        rows = [["A1", 12345, None, "number"], ["A2", "Operating profit", None, "text_label"],
                ["A3", 2029, None, "number"], ["A4", "RUB M", None, "text_label"]]
        calculation = {"tool": "calculate_cells", "arguments": {"expression": "x * 2", "inputs": [
            {"name": "x", "sheet": "Model", "cell": "A1"}]}, "result": {"metadata": {
                "result": {"value": 24690, "recalculated_workbook": False}, "basis": "saved_values",
                "recalculated": False, "assumptions_note": "Local expression only.",
                "evidence": [{"name": "x", "sheet": "Model", "cell": "A1"}],
                "completeness": {"complete": True}}, "cell_data": {}}}
        entries = [{"tool": "read_fact_cards", "arguments": {}, "result": {
            "metadata": {"facts": [card]}, "cell_data": {"Model": rows}}}, calculation]
        entries += [{"tool": "read_rows", "arguments": {"rows": [row]}, "result": {
            "metadata": {"verbose_context": "duplicate label " * 200}, "cell_data": {}}} for row in (1, 2)]
        memo = _evidence_memo(entries, 1800)
        self.assertLessEqual(len(_json(memo)), 1800)
        self.assertEqual(memo["facts"], [card])
        self.assertEqual(memo["cell_data"]["Model"], rows)
        self.assertEqual(memo["omitted_tool_outcome_count"], 3)
        self.assertTrue(memo["tool_outcomes_compacted"])
        self.assertTrue(memo["context_abridged"])
        outcome, = memo["tool_outcomes"]
        self.assertEqual(outcome["arguments"], calculation["arguments"])
        self.assertEqual(outcome["result"]["metadata"]["result"]["value"], 24690)
        self.assertFalse(outcome["result"]["metadata"]["recalculated"])
        self.assertEqual(outcome["result"]["metadata"]["basis"], "saved_values")
        self.assertEqual(outcome["result"]["metadata"]["assumptions_note"], "Local expression only.")

    def test_unfit_fixed_context_is_not_sliced_into_a_false_valid_request(self):
        base = [{"role": "system", "content": "fixed" * 1000}]
        active = {"id": "audit", "phase": "collection", "base_messages": base, "tool_results": []}
        functions = [{"name": "read_cells", "description": "schema" * 1000}]
        config = AnalysisConfig(max_context_chars=4000)
        messages = _agent_messages(active, config, functions, {})
        self.assertEqual(messages[0], base[0])
        self.assertGreater(len(_json({"messages": messages, "functions": functions})), config.max_context_chars)

    def test_all_invalid_references_collected_and_existing_ones_auto_read(self):
        class RepairClient(FakeClient):
            def chat(inner, messages, functions=None, function_call=None, **kwargs):
                if not inner.calls:
                    inner.calls.append({"messages": messages})
                    draft = stage_report("audit")
                    draft["observations"][0]["evidence"] = [{"sheet": "Model", "cell": cell} for cell in ("A1", "B1", "Z999")]
                    return function_response("submit_stage_report", draft)
                return super().chat(messages, functions, function_call, **kwargs)
        client = RepairClient()
        run_analysis(self.path, self.output, client=client)
        transcript = json.loads(next(self.output.glob("checkpoints/*/audit-transcript.json")).read_text())
        issues = transcript["turns"][0]["validation_issues"]
        self.assertEqual([issue["code"] for issue in issues], ["unread_evidence", "unread_evidence", "missing_evidence_cell"])
        repairs = [turn for turn in transcript["turns"] if turn.get("reason") == "unread_evidence_repair"]
        self.assertEqual([turn["arguments"]["cell"] for turn in repairs], ["A1", "B1"])
        self.assertIn("B1", json.dumps(client.calls[1]["messages"]))
        self.assertEqual(transcript["repair_rounds"], 1)

    def test_repeated_invalid_address_stops_even_when_report_prose_changes(self):
        class VaryingClient(FakeClient):
            def chat(inner, messages, functions=None, function_call=None, **kwargs):
                inner.calls.append({})
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                draft = stage_report(stage, "Z999")
                draft["summary"] += str(len(inner.calls))
                return function_response("submit_stage_report", draft)
        client = VaryingClient()
        result = run_analysis(self.path, self.output, client=client)
        self.assertEqual(len(client.calls), 12)
        self.assertTrue(all(stage["completion"] == "incomplete" for stage in result["stages"]))

    def test_session_identifier_is_stable_on_resume_and_separate_for_new_job(self):
        first = FakeClient(fail_at=2)
        with self.assertRaises(AnalysisError):
            run_analysis(self.path, self.output, client=first)
        later = FakeClient()
        run_analysis(self.path, self.output, client=later)
        self.assertEqual(first.calls[0]["session_id"], later.calls[0]["session_id"])
        self.assertNotEqual(later.calls[0]["session_id"], later.calls[-1]["session_id"])
        fresh = FakeClient()
        run_analysis(self.path, self.root / "new-job", client=fresh)
        self.assertNotEqual(first.calls[0]["session_id"], fresh.calls[0]["session_id"])

    def test_reasoning_budget_is_reserved_and_private_text_never_persisted(self):
        baseline = run_analysis(self.path, self.root / "baseline", client=FakeClient(missing_usage=True))
        class ReasoningClient(FakeClient):
            def __init__(self):
                super().__init__(missing_usage=True)
                self.reasoning_calls = []
            def chat(self, messages, functions=None, function_call=None, *, max_tokens=None,
                     temperature=None, reasoning_effort=None, reasoning_max_tokens=None):
                self.reasoning_calls.append((reasoning_effort, reasoning_max_tokens, max_tokens))
                result = super().chat(messages, functions, function_call, max_tokens=max_tokens, temperature=temperature)
                result["reasoning_content"] = "private-reasoning-must-not-be-persisted"
                result["choices"][0]["message"]["reasoning_content"] = "private-reasoning-must-not-be-persisted"
                return result
        client = ReasoningClient()
        result = run_analysis(self.path, self.output, client=client,
                              config=AnalysisConfig(reasoning_effort="high", reasoning_max_tokens=1024))
        self.assertEqual(client.reasoning_calls, [("high", 1024, 2500)] * 8)
        self.assertEqual(result["usage"]["estimated_tokens"] - baseline["usage"]["estimated_tokens"], 8 * 1024)
        state = json.loads(next((self.output / "checkpoints").glob("*/state.json")).read_text())
        self.assertEqual(state["identity"]["config"]["reasoning_effort"], "high")
        self.assertEqual(state["identity"]["config"]["reasoning_max_tokens"], 1024)
        self.assertNotEqual(result["checkpoint"]["fingerprint"], baseline["checkpoint"]["fingerprint"])
        for path in self.output.rglob("*.json"):
            self.assertNotIn("private-reasoning-must-not-be-persisted", path.read_text())

    def test_financial_map_primes_three_stages_once_without_llm_quota(self):
        automatic_calls = []
        class MapTools(FakeTools):
            def schemas(self):
                return super().schemas() + [{"name": "financial_map", "parameters": {
                    "type": "object", "properties": {"stage": {"type": "string"}}, "required": ["stage"]}}]
            def call(self, name, arguments):
                if name != "financial_map":
                    return super().call(name, arguments)
                automatic_calls.append(arguments["stage"])
                self.retrieved_cells.add(("Model", "B1"))
                return {"stage": arguments["stage"], "candidates": [{"category": "revenue", "sheet": "Model", "row": 1,
                    "cells": [{"sheet": "Model", "cell": "B1", "value": 200, "formula": "=A1*2", "present": True}]}]}
        module = types.ModuleType("finmodel_audit.tools")
        module.WorkbookTools = MapTools
        with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
            client = FakeClient()
            result = run_analysis(self.path, self.output, client=client)
            self.assertEqual(automatic_calls, ["audit", "assumptions", "business"])
            self.assertEqual(result["usage"]["requests"], 8)
            self.assertEqual(len(client.calls), 8)
            first_context = json.dumps(client.calls[0]["messages"])
            self.assertIn("financial_map", first_context)
            self.assertIn("B1", first_context)
            replay = FakeClient()
            run_analysis(self.path, self.output, client=replay)
            self.assertEqual(replay.calls, [])
            self.assertEqual(len(automatic_calls), 3)
        state = json.loads(next(self.output.glob("checkpoints/*/state.json")).read_text())
        self.assertEqual(sum(item.get("automatic", False) for item in state["tool_log"]), 3)
        transcript = json.loads(next(self.output.glob("checkpoints/*/audit-transcript.json")).read_text())
        self.assertEqual(transcript["turns"][0]["event"], "automatic_tool")

    def test_metadata_feedback_names_observation_and_clears_rejected_tool_state(self):
        class CorrectsMetadata(FakeClient):
            bad_returned = False
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                if stage == "quality" and self.bad_returned:
                    self.calls.append({"messages": json.loads(json.dumps(messages)), "functions": functions})
                    return function_response("submit_stage_report", stage_report(stage))
                response = super().chat(messages, functions, function_call, **kwargs)
                call = response["choices"][0]["message"].get("function_call", {})
                if stage == "quality" and call.get("name") == "submit_stage_report":
                    call["arguments"]["observations"].append({
                        "title": "Итерационные вычисления включены", "detail": "Это настройка книги.",
                        "evidence": [], "status": "observation", "impact": "not_assessed",
                    })
                    self.bad_returned = True
                return response
        client = CorrectsMetadata()
        result = run_analysis(self.path, self.output, client=client)
        self.assertEqual(result["stages"][2]["completion"], "submitted")
        corrected_call = next(call for call in client.calls if any("Наблюдение №2" in message.get("content", "") for message in call["messages"]))
        feedback = corrected_call["messages"][-1]["content"]
        self.assertIn("Итерационные вычисления включены", feedback)
        self.assertIn("limitations", feedback)
        self.assertIn("not_verified", feedback)
        self.assertIn("Не придумывай ячейку", feedback)
        self.assertFalse(any(message.get("function_call", {}).get("name") == "submit_stage_report" for message in corrected_call["messages"]))
        self.assertFalse(any("functions_state_id" in message for message in corrected_call["messages"]))

    def test_three_identical_rejected_reports_stop_spending_requests(self):
        class RepeatsBadReport(FakeClient):
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                self.calls.append({"messages": messages})
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                return function_response("submit_stage_report", stage_report(stage, "Z999"))
        client = RepeatsBadReport()
        result = run_analysis(self.path, self.output, client=client)
        self.assertEqual(len(client.calls), 12)
        self.assertTrue(all(stage["completion"] == "incomplete" for stage in result["stages"]))
        self.assertTrue(all("трижды" in stage["summary"] for stage in result["stages"]))

    def test_audit_requires_numeric_formula_trace_and_calculation_tools(self):
        available = {"search_cells", "read_rows", "trace_precedents", "calculate_cells"}
        active = {"id": "audit", "tool_results": []}
        self.assertEqual(_stage_navigation(active, available)[0], "search_cells")
        active["tool_results"].append({"tool": "read_rows", "arguments": {"rows": [1]},
            "result": {"cell_data": {"Model": [["A1", 100, None, "number"], ["B1", 200, "=A1*2", "formula_number"]]}}})
        self.assertEqual(_stage_navigation(active, available)[0], "trace_precedents")
        active["tool_results"].append({"tool": "trace_precedents", "arguments": {"sheet": "Model", "cell": "B1"}, "result": {}})
        self.assertEqual(_stage_navigation(active, available)[0], "calculate_cells")
        active["tool_results"].append({"tool": "calculate_cells", "arguments": {}, "result": {}})
        self.assertEqual(_stage_navigation(active, available)[1], [])

    def test_business_navigation_requires_distinct_financial_families_and_caps_growth_calls(self):
        available = {"search_cells", "read_rows", "calculate_cells"}
        def row_entry(number, label):
            return {"tool": "read_rows", "arguments": {"rows": [number]}, "result": {
                "cell_data": {"Model": [[f"A{number}", label, None, "text_label"],
                                       [f"B{number}", number * 100, None, "number"]]}}}
        active = {"id": "business", "tool_results": [row_entry(1, "Revenue"), row_entry(2, "Sales"), row_entry(3, "Выручка") ]}
        next_tool, missing, guidance = _stage_navigation(active, available)
        self.assertEqual(next_tool, "search_cells")
        self.assertIn("profitability", guidance)
        active["tool_results"].append(row_entry(4, "EBITDA"))
        self.assertIn("cash_or_debt", _stage_navigation(active, available)[2])
        active["tool_results"].append(row_entry(5, "Долг"))
        self.assertEqual(_stage_navigation(active, available)[0], "calculate_cells")
        for _ in range(2):
            active["tool_results"].append({"tool": "calculate_cells", "arguments": {}, "result": {}})
        self.assertEqual(_stage_navigation(active, available)[0], "submit_stage_report")

    def test_synthesis_has_exclusive_schema_and_two_correction_attempts(self):
        class NeedsSynthesis(FakeClient):
            def __init__(self):
                super().__init__()
                self.final_attempts = {}
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                self.calls.append({"messages": messages, "functions": functions, "function_call": function_call})
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                if isinstance(function_call, dict):
                    self.assert_exclusive = [f["name"] for f in functions] == ["submit_stage_report"]
                    count = self.final_attempts.get(stage, 0) + 1
                    self.final_attempts[stage] = count
                    result = function_response("submit_stage_report", stage_report(stage, "C99" if count == 1 else "A1"))
                    result["choices"][0]["finish_reason"] = "stop"  # observed-provider variation
                    return result
                return function_response("read_cells", {"sheet": "Model", "cell": "A1"})
        client = NeedsSynthesis()
        result = run_analysis(self.path, self.output, client=client,
                              config=AnalysisConfig(max_requests_per_stage=5, max_total_requests=21))
        self.assertTrue(client.assert_exclusive)
        self.assertTrue(all(count == 2 for count in client.final_attempts.values()))
        self.assertTrue(all(stage["observations"] for stage in result["stages"]))
        transcripts = list(self.output.glob("checkpoints/*/*-transcript.json"))
        self.assertEqual(len(transcripts), 4)
        first = json.loads(next(path for path in transcripts if path.name == "audit-transcript.json").read_text())
        self.assertTrue(any(turn.get("validation_error") for turn in first["turns"]))
        self.assertTrue(any("tool_result_raw" in turn for turn in first["turns"]))

    def test_later_stages_keep_a_token_reserve(self):
        class Expensive(FakeClient):
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                response = super().chat(messages, functions, function_call, **kwargs)
                response["usage"] = {"prompt_tokens": 3000, "completion_tokens": 500, "total_tokens": 3500}
                return response
        result = run_analysis(self.path, self.output, client=Expensive(),
                              config=AnalysisConfig(max_total_tokens=80_000))
        self.assertTrue(all(stage["observations"] for stage in result["stages"]))
        self.assertLess(result["usage"]["total_tokens"], 80_000)

    def test_context_compression_preserves_function_pair_and_warns_about_omissions(self):
        workbook = load_workbook(self.path)
        tools = FakeTools(workbook)
        tools.call("read_cells", {"sheet": "Model", "cell": "A1"})
        messages = [{"role": "system", "content": "trusted instruction"}, {"role": "user", "content": "stage"}]
        messages += [{"role": "assistant", "content": "old" * 4000}]
        messages += [{"role": "assistant", "function_call": {"name": "read_cells", "arguments": {}}}, {"role": "function", "name": "read_cells", "content": "current"}]
        compact = _compact_messages(messages, tools, workbook, 4000)
        self.assertEqual(compact[-2:], messages[-2:])
        self.assertIn("context_compacted", compact[2]["content"])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(messages)))

    def test_version_handshake_is_charged_and_contains_no_workbook_data(self):
        client = FakeClient()
        client.model_version = None
        original = client.chat
        def handshake(messages, functions=None, function_call=None, **kwargs):
            if functions is None:
                client.calls.append({"messages": messages})
                return {"model": "fake:version1", "usage": {"total_tokens": 3}}
            response = original(messages, functions, function_call, **kwargs)
            response["model"] = "fake:version1"
            return response
        client.chat = handshake
        result = run_analysis(self.path, self.output, client=client)
        self.assertEqual(result["usage"]["requests"], 9)
        self.assertEqual(result["usage"]["by_stage"]["provider_check"]["total_tokens"], 3)
        self.assertNotIn("Model", json.dumps(client.calls[0]))
        self.assertEqual(result["provider"]["version"], "fake:version1")


class ProviderAdapterTests(unittest.TestCase):
    def test_optional_reasoning_config_and_env_boundaries(self):
        defaults = AnalysisConfig()
        self.assertIsNone(defaults.reasoning_effort)
        self.assertIsNone(defaults.reasoning_max_tokens)
        self.assertEqual(defaults.reasoning_token_reserve, 0)
        with patch.dict(os.environ, {"FINMODEL_REASONING_EFFORT": "medium", "FINMODEL_REASONING_MAX_TOKENS": "4096"}, clear=True):
            configured = AnalysisConfig.from_env()
        self.assertEqual(configured.reasoning_effort, "medium")
        self.assertEqual(configured.reasoning_token_reserve, 4096)
        self.assertEqual(AnalysisConfig(reasoning_effort="high").reasoning_token_reserve, 8192)
        for value in (1, 8192):
            AnalysisConfig(reasoning_max_tokens=value).validate()
        for options in ({"reasoning_effort": "private-invalid"}, {"reasoning_effort": True},
                        {"reasoning_max_tokens": 0}, {"reasoning_max_tokens": 8193},
                        {"reasoning_max_tokens": True}, {"reasoning_max_tokens": 1.5}):
            with self.subTest(options=options), self.assertRaises(AnalysisError) as caught:
                AnalysisConfig(**options).validate()
            self.assertNotIn("private-invalid", str(caught.exception))
        with patch.dict(os.environ, {"FINMODEL_REASONING_MAX_TOKENS": "private-invalid"}, clear=True):
            with self.assertRaises(AnalysisError) as caught:
                AnalysisConfig.from_env()
            self.assertNotIn("private-invalid", str(caught.exception))

    def test_adapter_optional_reasoning_omitted_by_default_and_requires_support(self):
        class Flexible:
            model = "test:v1"
            def chat(self, messages, **kwargs):
                return {"options": kwargs, "choices": [{"message": {"content": "final", "reasoning_content": "private"}}]}
        adapter = ProviderAdapter(Flexible())
        self.assertNotIn("reasoning_effort", adapter.chat([])["options"])
        self.assertNotIn("reasoning_max_tokens", adapter.chat([])["options"])
        result = adapter.chat([], reasoning_effort="low", reasoning_max_tokens=100)
        self.assertEqual(result["options"]["reasoning_effort"], "low")
        self.assertEqual(result["options"]["reasoning_max_tokens"], 100)
        self.assertNotIn("reasoning_content", result["choices"][0]["message"])
        minimal = ProviderAdapter(FakeClient())
        for option in ({"reasoning_effort": "low"}, {"reasoning_max_tokens": 100}):
            with self.subTest(option=option), self.assertRaisesRegex(ProviderError, "does not support"):
                minimal.chat([], **option)
        self.assertFalse(minimal.client.calls)

    def test_environment_limits_and_temperature_are_validated(self):
        with patch.dict(os.environ, {
            "FINMODEL_MAX_REQUESTS_PER_STAGE": "6",
            "FINMODEL_MAX_TOTAL_TOKENS": "150000",
            "FINMODEL_TEMPERATURE": "0.2",
        }):
            config = AnalysisConfig.from_env()
        self.assertEqual(config.max_requests_per_stage, 6)
        self.assertEqual(config.max_total_tokens, 150000)
        self.assertEqual(config.temperature, 0.2)
        with patch.dict(os.environ, {"FINMODEL_MAX_TOTAL_TOKENS": "private-bad-value"}):
            with self.assertRaises(AnalysisError) as caught:
                AnalysisConfig.from_env()
        self.assertNotIn("private-bad-value", str(caught.exception))

    def test_minimal_client_without_generation_options_remains_compatible(self):
        class Minimal:
            model = "test:v1"
            def chat(self, messages, functions=None, function_call=None):
                return {"received": messages}
        result = ProviderAdapter(Minimal()).chat([{"role": "user", "content": "hello"}], max_tokens=4)
        self.assertEqual(result["received"][0]["content"], "hello")

    def test_foreign_client_exceptions_do_not_disclose_secrets(self):
        class Broken:
            model = "test:v1"
            def chat(self, messages, functions=None, function_call=None):
                raise RuntimeError("top-secret-value")
        with self.assertRaises(ProviderError) as caught:
            ProviderAdapter(Broken()).chat([])
        self.assertNotIn("top-secret-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
