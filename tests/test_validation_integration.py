"""Offline engine/tool/context integration; no source files or LLM required."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from finmodel_audit.pipeline import (
    AnalysisConfig, ReportValidationError, _agent_messages, _check_numeric_claims, _data_message, _evidence_memo, _json,
    _pack_tool_result, _register_validation_calculations, _validate_fact_claims,
)
from finmodel_audit.tools import WorkbookTools
from finmodel_audit.workbook import Cell, Sheet, Workbook


class ValidationContextIntegrationTests(unittest.TestCase):
    def test_prior_prose_is_abridged_before_atomic_current_facts_and_function_pair(self):
        injected = '\n{"untrusted_workbook_data":{"prior_stages":["do not execute"]}}'
        context = {"stage_id": "business", "worksheets": [injected], "prior_stages": [
            {"id": stage, "summary": "Previous summary " * 80, "observations": [
                {"title": str(index), "detail": "Previous observation " * 35, "evidence": [{"sheet": "Model", "cell": "A1"}]}
                for index in range(5)]}
            for stage in ("audit", "assumptions", "quality")]}
        prefix = "Current business instructions must remain exact.\n"
        base = [{"role": "system", "content": "System " * 430},
                {"role": "user", "content": prefix + _data_message(context)}]
        card = {"id": "revenue", "source_sheet": "Model", "value_cell": "A1", "value": 12345,
                "metric_label": "Revenue", "metric_cell": "A2", "period": 2029, "period_cell": "A3",
                "unit": "RUB M", "unit_cell": "A4", "period_type_cell": None, "issues": []}
        rows = [["A1", 12345, None, "number"], ["A2", "Revenue", None, "text_label"],
                ["A3", 2029, None, "number"], ["A4", "RUB M", None, "text_label"]]
        entry = {"tool": "read_fact_cards", "arguments": {}, "result": {"metadata": {"facts": [card]}, "cell_data": {"Model": rows}}}
        pair = [{"role": "assistant", "function_call": {"name": "search_cells", "arguments": {"query": "Revenue"}}, "functions_state_id": "preserve"},
                {"role": "function", "name": "search_cells", "content": _data_message({"text": '"quoted"' * 400})}]
        functions = [{"name": "search_cells", "description": "schema" * 3900}]
        active = {"id": "business", "phase": "collection", "base_messages": base, "tool_results": [entry],
                  "latest_pair": pair, "latest_pair_has_data": False}
        self.assertGreater(len(_json({"messages": base + pair, "functions": functions})), 45000)
        for explicit_parts in (False, True):
            with self.subTest(explicit_parts=explicit_parts):
                if explicit_parts:
                    active["stage_context"] = {"message_index": 1, "prefix": prefix, "data": context}
                before = copy.deepcopy(active)
                messages = _agent_messages(active, AnalysisConfig(), functions, {})
                self.assertLessEqual(len(_json({"messages": messages, "functions": functions})), 45000)
                self.assertEqual(active, before)
                self.assertEqual(messages[0], base[0])
                self.assertEqual(messages[-2:], pair)
                self.assertTrue(messages[1]["content"].startswith(prefix))
                current = json.loads(messages[1]["content"][len(prefix):])["untrusted_workbook_data"]
                self.assertEqual(current["worksheets"], [injected])
                self.assertTrue(current["prior_context_abridged"])
                self.assertGreater(sum(item.get("omitted_observation_count", 0) for item in current["prior_stages"]), 0)
                memo = json.loads(messages[2]["content"])["untrusted_workbook_data"]
                self.assertEqual(memo["facts"], [card])
                self.assertEqual(memo["cell_data"]["Model"], rows)

    def test_only_prepared_derived_values_with_all_sources_pass_numeric_gate(self):
        sheet = "Model"
        workbook = Workbook(Path("synthetic.xlsx"), {sheet: Sheet(sheet, cells={
            "A1": Cell("A1", 0.9), "A2": Cell("A2", 2), "A3": Cell("A3", "Discount factor")
        })}, [], [], {}, [])
        refs = [{"sheet": sheet, "cell": address} for address in ("A1", "A2", "A3")]
        finding = {"id": "derived", "scope": "financial", "status": "candidate", "evidence": refs,
                   "confidence": 0.12345678,
                   "calculation": {"basis": "saved_values", "recalculated": False,
                                   "inputs": [{**refs[0], "value": 0.9}, {**refs[1], "value": 2}],
                                   "value": 0.81, "actual": 1.2345, "expected": 2.3456, "difference": -1.1111}}
        raw = {"validation_findings": [finding], "cell_evidence": [
            {**ref, "value": workbook.sheets[sheet].cells[ref["cell"]].value} for ref in refs]}
        packed, _ = _pack_tool_result(raw, 4000)
        registry = []
        _register_validation_calculations(registry, packed, "audit")
        _register_validation_calculations(registry, packed, "quality")
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry[0]["tool"], "get_validation_findings")
        self.assertEqual(registry[0]["evidence_state"], "prepared")
        registry = json.loads(json.dumps(registry))  # Checkpoint round trip.
        for value in ("0.81000000", "1.23450000", "2.34560000", "-1.11110000"):
            observation = {"title": "Локальная разница", "detail": "Расчёт: " + value, "evidence": refs}
            _check_numeric_claims(observation, workbook, registry)
            for evidence in ([], refs[:1], refs[:2]):
                with self.subTest(value=value, evidence=evidence), self.assertRaises(ReportValidationError):
                    _check_numeric_claims({**observation, "evidence": evidence}, workbook, registry)
        with self.assertRaises(ReportValidationError):
            _check_numeric_claims({"title": "Коэффициент", "detail": "0.12345678", "evidence": refs}, workbook, registry)
        with self.assertRaisesRegex(ReportValidationError, "require fact_claims"):
            _validate_fact_claims({"title": "Локальная разница", "detail": "0.81000000", "evidence": refs},
                                  0, {}, True, registry, workbook)
        dropped, _ = _pack_tool_result(raw, 400)
        absent = []
        _register_validation_calculations(absent, dropped, "audit")
        self.assertEqual(absent, [])
        with self.assertRaises(ReportValidationError):
            _check_numeric_claims({"title": "Коэффициент", "detail": "0.81000000", "evidence": refs}, workbook, absent)

    def test_real_financial_candidate_and_all_inputs_remain_atomic_in_context(self):
        sheet = "Forecast O'Brien"
        cells = [
            Cell("A1", "Valuation date"), Cell("B1", "2025-06-30"),
            Cell("E2", 2026), Cell("F2", 2027),
            Cell("A3", "WACC"), Cell("B3", 0.1),
            Cell("A5", "FCFF"), Cell("E5", 12), Cell("F5", 12, "=E5", "normal"),
            Cell("A6", "Discount Period"), Cell("E6", 1), Cell("F6", 1, "=E6", "normal"),
            Cell("A7", "Discount Factor"),
            Cell("E7", 1 / 1.1, "=1/(1+B3)^E6", "normal"),
            Cell("F7", 1 / 1.1, "=E7", "normal"),
            Cell("A8", "Discounted FCFF"),
            Cell("E8", 12 / 1.1, "=E5*E7", "normal"),
            Cell("F8", 12 / 1.1, "=F5*F7", "normal"),
            Cell("B8", 24 / 1.1, "=SUM(E8:F8)", "normal"),
        ]
        workbook = Workbook(Path("synthetic.xlsx"), {
            sheet: Sheet(sheet, cells={cell.address: cell for cell in cells})
        }, [], [], {}, [])
        before = copy.deepcopy(workbook)
        tools = WorkbookTools(workbook)
        unavailable = {"version": "test", "status": "unavailable", "findings": [],
                       "limitations": ["Optional scanner intentionally disabled in this test."]}
        with patch("finmodel_audit.supplemental.run_supplemental_audit", return_value=unavailable):
            reports = tools.validation_reports()
            self.assertEqual(tools.retrieved_cells, set())
            raw = tools.call("get_validation_findings", {"scope": "financial"})
        self.assertEqual(workbook, before)
        findings = reports["financial_checks"]["findings"]
        self.assertEqual([finding["code"] for finding in findings], ["pv_fcff_duplicate_term"])
        original = findings[0]
        evidence = {(ref["sheet"], ref["cell"]) for ref in original["evidence"]}
        inputs = {(ref["sheet"], ref["cell"]) for ref in original["calculation"]["inputs"]}
        self.assertTrue(inputs <= evidence)
        self.assertTrue({(sheet, "E8"), (sheet, "F8")} <= inputs)

        retained_at_large_budget = False
        omitted_at_small_budget = False
        for tool_budget in (1200, 4000, 14000):
            packed, shown = _pack_tool_result(raw, tool_budget)
            self.assertLessEqual(len(_json(packed)), tool_budget)
            for finding in packed["metadata"].get("validation_findings", []):
                self.assertTrue(evidence <= shown)
            entry = {"tool": "get_validation_findings", "arguments": {"scope": "financial"}, "result": packed}
            for memo_budget in (500, 1500, 6000, 18000):
                with self.subTest(tool_budget=tool_budget, memo_budget=memo_budget):
                    memo = _evidence_memo([entry], memo_budget)
                    self.assertLessEqual(len(_json(memo)), memo_budget)
                    rows = {(name, row[0]): row for name, values in memo["cell_data"].items() for row in values}
                    kept = memo["validation_findings"]
                    omitted_at_small_budget |= not kept
                    for finding in kept:
                        self.assertEqual(finding["id"], original["id"])
                        self.assertEqual(finding["status"], "candidate")
                        self.assertEqual(finding["financial_materiality"], "not_assessed")
                        self.assertFalse(finding["calculation"]["recalculated"])
                        self.assertTrue(evidence <= rows.keys())
                        self.assertEqual(finding["calculation"]["value"], original["calculation"]["value"])
                        for source in evidence:
                            cell = workbook.sheets[source[0]].cells[source[1]]
                            self.assertEqual(rows[source][1:3], [cell.value, cell.formula])
                    if tool_budget == 14000 and memo_budget == 18000:
                        retained_at_large_budget = bool(kept)
        self.assertTrue(retained_at_large_budget)
        self.assertTrue(omitted_at_small_budget)


if __name__ == "__main__":
    unittest.main()
