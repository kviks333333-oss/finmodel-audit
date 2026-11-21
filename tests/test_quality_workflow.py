"""Quality contracts across navigation, actual request, publication and resume."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from finmodel_audit.pipeline import (_stage_navigation, run_analysis, _validate_business_selection,
                                    _evidence_memo, ReportValidationError, _validation_error_identity)
from test_pipeline import FakeTools, FakeClient, make_workbook, function_response


class QualityWorkflowTests(unittest.TestCase):
    def test_business_selection_keeps_outputs_and_endpoints_without_inventing_dates(self):
        registry = {}
        for key, label, cells in (("revenue", "Revenue", ("D3", "H3")), ("ebit", "EBIT", ("D4", "H4")),
                                  ("fcff", "FCFF", ("C5", "H5")), ("npv", "NPV", ("C9",)), ("irr", "IRR", ("C10",))):
            for cell in cells:
                registry[cell] = {"id": cell, "source_sheet": "Renamed", "value_cell": cell,
                    "metric_cell": "A" + cell[1:], "metric_label": label, "metric_key": key,
                    "unit": None, "period": None, "binding_status": "partial"}
        payload = {"fact_ids": ["D3", "D4", "C5"], "comparisons": []}
        with self.assertRaises(ReportValidationError) as raised:
            _validate_business_selection(payload, registry, list(registry))
        missing = {item["fact_id"] for item in raised.exception.issues[0]["required_facts"]}
        self.assertEqual(missing, {"H3", "H4", "H5", "C9", "C10"})
        payload["fact_ids"] += sorted(missing)
        _validate_business_selection(payload, registry, list(registry))
        self.assertEqual(payload["comparisons"], [])
        self.assertTrue(all(card["period"] is None for card in registry.values()))
        registry["secret"] = {**registry["C9"], "binding_status": "ambiguous"}
        _validate_business_selection(payload, registry, list(registry))

    def test_compaction_prefers_distinct_financial_themes_over_repeated_periods(self):
        findings, rows = [], []
        for index, code in enumerate(["rare_irr", "repeated_fcff", "repeated_fcff", "repeated_fcff"], 1):
            findings.append({"id": f"f{index}", "code": code, "scope": "financial", "detail": "x" * 200,
                             "evidence": [{"sheet": "S", "cell": f"A{index}"}]})
            rows.append([f"A{index}", index, "=" + "B1+" * 40 + "B1", "formula_number"])
        entry = {"tool": "get_validation_findings", "arguments": {},
                 "result": {"metadata": {"validation_findings": findings}, "cell_data": {"S": rows}}}
        memo = _evidence_memo([entry], 1500)
        self.assertEqual({item["code"] for item in memo["validation_findings"]}, {"rare_irr", "repeated_fcff"})
        self.assertTrue(memo.get("omitted_finding_selections"))
        self.assertLessEqual(len(json.dumps(memo, ensure_ascii=False, separators=(",", ":"))), 1500)

    def test_operating_profit_label_cannot_substitute_for_revenue(self):
        registry = {}
        for identity, label, key, row in (("revenue", "Выручка", "revenue", 2),
                                          ("profit", "Прибыль от продаж", "operating_profit", 3),
                                          ("cash", "Денежные средства", "cash", 4)):
            registry[identity] = {"id": identity, "source_sheet": "Лист", "value_cell": f"D{row}",
                "metric_cell": f"A{row}", "metric_label": label, "metric_key": key, "unit": "руб.",
                "period": None, "binding_status": "partial"}
        with self.assertRaises(ReportValidationError) as raised:
            _validate_business_selection({"fact_ids": ["profit", "cash"]}, registry, list(registry))
        self.assertEqual([item["fact_id"] for item in raised.exception.issues[0]["required_facts"]], ["revenue"])

    def test_missing_theme_feedback_does_not_merge_distinct_errors(self):
        first = {"code": "missing_candidate_theme", "theme_code": "irr"}
        second = {"code": "missing_candidate_theme", "theme_code": "duplicate"}
        self.assertNotEqual(_validation_error_identity(first, {}), _validation_error_identity(second, {}))
    def test_tax_cards_and_unrelated_trace_cannot_finish_dependency_questions(self):
        active = {"id": "assumptions", "fact_selection": True, "investigation_plan": {"questions": [
            {"id": "rate", "category": "discount_rate_components", "title": "Rate"},
            {"id": "flow", "category": "cashflow_drivers", "title": "Flow"}]},
            "tool_results": [{"tool": "read_fact_cards", "arguments": {}, "result": {"metadata": {"facts": [{"binding_status": "partial"}]}}},
                             {"tool": "trace_dependents", "arguments": {}, "result": {}}]}
        allowed = {"read_rows", "read_fact_cards", "trace_dependents", "inspect_question"}
        name, missing, hint = _stage_navigation(active, allowed)
        self.assertEqual(name, "inspect_question")
        self.assertTrue(missing)
        self.assertIn('"id":"rate"', hint)
        # A failed atomic read is not looped indefinitely or considered checked.
        for identity in ("rate", "flow"):
            active["tool_results"].append({"tool": "inspect_question", "arguments": {"question_id": identity},
                                           "result": {"omitted_investigation_question": identity}})
        name, missing, _ = _stage_navigation(active, allowed)
        self.assertEqual(name, "submit_stage_report")
        self.assertTrue(missing)

    def test_examined_plan_allows_agent_followup_instead_of_forcing_completion(self):
        active = {"id": "assumptions", "fact_selection": True,
                  "investigation_plan": {"questions": [{"id": "rate", "category": "discount_rate_components"}]},
                  "tool_results": [
                      {"tool": "inspect_question", "arguments": {"question_id": "rate"},
                       "result": {"metadata": {"investigation_question": {"id": "rate"}}}},
                      {"tool": "read_fact_cards", "arguments": {}, "result": {"metadata": {"facts": [{"binding_status": "partial"}]}}}]}
        name, missing, hint = _stage_navigation(active, {"read_rows", "read_fact_cards", "inspect_question", "trace_dependents"})
        self.assertIsNone(name)
        self.assertEqual(missing, [])
        self.assertIn("дополнительная", hint)

    def test_real_methodology_submission_and_resume_preserve_full_proof(self):
        class ProofTools(FakeTools):
            def schemas(self):
                return super().schemas() + [{"name": "get_validation_findings", "description": "Test proof",
                    "parameters": {"type": "object", "properties": {"scope": {"type": "string"}, "limit": {"type": "integer"}}}}]

            def call(self, name, arguments):
                if name != "get_validation_findings":
                    return super().call(name, arguments)
                refs = [{"sheet": "Model", "cell": address} for address in ("A1", "B1")]
                return {"validation_findings": [{"id": "financial_test", "scope": "financial", "code": "test",
                    "status": "candidate", "title": "Структурный кандидат", "detail": "Назначение формулы требует проверки.",
                    "evidence": refs, "financial_materiality": "not_assessed"}],
                    "cell_evidence": [{**ref, "value": self.workbook.sheets["Model"].cells[ref["cell"]].value,
                        "formula": self.workbook.sheets["Model"].cells[ref["cell"]].formula, "present": True} for ref in refs]}

        class ProofClient(FakeClient):
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                props = functions[-1]["parameters"]["properties"]
                if "finding_ids" in props:
                    self.calls.append({"messages": messages})
                    stage = props["id"]["enum"][0]
                    return function_response("submit_stage_report", {"id": stage, "finding_ids": ["financial_test"],
                        "hypotheses": [], "limitations": ["limited_source_scope"]})
                return super().chat(messages, functions=functions, function_call=function_call, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "book.xlsx", Path(directory) / "result"
            make_workbook(source)
            module = types.ModuleType("finmodel_audit.tools")
            module.WorkbookTools = ProofTools
            with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
                report = run_analysis(source, output, client=ProofClient())
                replay = ProofClient()
                resumed = run_analysis(source, output, client=replay)
            self.assertFalse(replay.calls)
            self.assertEqual(resumed["checkpoint"]["resumed_stages"], 4)
            audit = report["stages"][0]
            self.assertEqual(audit["selection"]["finding_ids"], ["financial_test"])
            self.assertEqual(audit["observations"][0]["status"], "hypothesis")
            self.assertEqual({ref["cell"] for ref in audit["observations"][0]["evidence"]}, {"A1", "B1"})
            state = json.loads(next((output / "checkpoints").glob("*/state.json")).read_text())
            self.assertEqual(state["finding_registry"]["financial_test"]["cell_evidence"][1]["formula"], "=A1*2")
            state_path = next((output / "checkpoints").glob("*/state.json"))
            state["completed"]["audit"]["observations"][0]["detail"] = "Untrusted stale interpretation"
            state_path.write_text(json.dumps(state))
            rerun = ProofClient()
            with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
                recovered = run_analysis(source, output, client=rerun)
            self.assertTrue(rerun.calls)
            self.assertNotIn("Untrusted stale", recovered["stages"][0]["observations"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
