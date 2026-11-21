"""A model-requested scenario survives publication and checkpoint validation."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finmodel_audit.pipeline import AnalysisConfig, run_analysis, _stage_navigation
from test_pipeline import FakeTools, make_workbook, function_response, stage_report


def conditional_result():
    target = {"sheet": "Model", "cell": "B1"}
    source = {"sheet": "Model", "cell": "A1"}
    return {"impact_scenarios": [{"id": "impact-test", "conditional": True, "recalculated_workbook": False,
        "title": "Conditional test", "assumption": "One factor removed", "raw_unit": "RUB M",
        "target": target, "baseline": 200, "saved_output": 200, "result": 100, "delta": -100,
        "relative_delta": -.5, "baseline_cache_difference": 0,
        "patches": [{**target, "original_formula": "A1*2", "changed_formula": "=A1"}],
        "formula_path": [{**target, "formula": "A1*2"}],
        "source_inputs": [{**source, "value": 100, "frozen": True}], "evidence": [target, source]}],
        "cell_evidence": [{**target, "value": 200, "formula": "A1*2"}, {**source, "value": 100}]}


class ImpactTools(FakeTools):
    def schemas(self):
        return super().schemas() + [{"name": name, "parameters": {"type": "object", "properties": {}}}
            for name in ("list_impact_scenarios", "calculate_impact", "get_validation_findings")]

    def call(self, name, arguments):
        if name == "get_validation_findings":
            return {"validation_findings": [], "cell_evidence": []}
        if name == "list_impact_scenarios":
            return {"impact_catalog": [{"id": "impact-test"}]}
        if name == "calculate_impact":
            self.retrieved_cells.update({("Model", "A1"), ("Model", "B1")})
            return conditional_result()
        return super().call(name, arguments)


class ImpactClient:
    provider_name = model = "fake"
    model_version = "fake-v1"

    def __init__(self):
        self.calls = []
        self.stages = set()

    def chat(self, messages, functions=None, function_call=None, **kwargs):
        self.calls.append(deepcopy(messages))
        stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
        if stage not in self.stages:
            self.stages.add(stage)
            if stage == "audit":
                return function_response("calculate_impact", {"scenario_id": "impact-test"})
            if stage not in {"quality"}:
                return function_response("read_cells", {"sheet": "Model", "cell": "A1"})
        payload = ({"id": stage, "finding_ids": [], "hypotheses": [], "limitations": []}
                   if stage in {"audit", "quality"} else stage_report(stage))
        return function_response("submit_stage_report", payload)


class ImpactPipelineTests(unittest.TestCase):
    def test_actual_delivery_published_once_and_resumed_without_api(self):
        with tempfile.TemporaryDirectory() as directory, patch("finmodel_audit.tools.WorkbookTools", ImpactTools):
            path, output = Path(directory) / "model.xlsx", Path(directory) / "output"
            make_workbook(path)
            report = run_analysis(path, output, client=ImpactClient())
            audit = report["stages"][0]
            self.assertEqual(audit["impact_coverage"]["delivered"], 1)
            self.assertEqual(len(audit["impact_calculations"]), 1)
            self.assertEqual(audit["impact_calculations"][0]["delta"], -100)
            self.assertEqual(len([x for x in audit["observations"] if x.get("conditional_impact")]), 1)
            client = ImpactClient()
            resumed = run_analysis(path, output, client=client)
            self.assertEqual(client.calls, [])
            self.assertEqual(resumed["stages"], report["stages"])
            state_path = next((output / "checkpoints").glob("*/state.json"))
            state = json.loads(state_path.read_text())
            state["completed"]["audit"]["impact_calculations"][0]["result"] = 999
            state_path.write_text(json.dumps(state))
            client = ImpactClient()
            restored = run_analysis(path, output, client=client)
            self.assertTrue(client.calls)
            self.assertEqual(restored["stages"][0]["impact_calculations"][0]["result"], 100)

    def test_missing_delivery_marks_scenario_incomplete(self):
        class SkippingClient(ImpactClient):
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                if stage == "audit":
                    return function_response("submit_stage_report", {"id": stage, "finding_ids": [], "hypotheses": [], "limitations": []})
                return super().chat(messages, functions, function_call, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch("finmodel_audit.tools.WorkbookTools", ImpactTools):
            path = Path(directory) / "model.xlsx"
            make_workbook(path)
            report = run_analysis(path, Path(directory) / "out", client=SkippingClient())
            self.assertEqual(report["stages"][0]["completion"], "incomplete")
            self.assertEqual(report["stages"][0]["impact_calculations"], [])

    def test_navigation_requests_each_scenario_once_after_basic_checks(self):
        active = {"id": "audit", "impact_catalog": [{"id": "one"}, {"id": "two"}], "tool_results": [
            {"tool": "read_rows", "arguments": {"rows": [1]}, "result": {"cell_data": {"S": [["A1", 2, "=1+1", "formula_number"]]}}},
            {"tool": "trace_precedents", "arguments": {}, "result": {}},
            {"tool": "calculate_cells", "arguments": {}, "result": {}}]}
        allowed = {"read_rows", "trace_precedents", "calculate_cells", "calculate_impact"}
        self.assertEqual(_stage_navigation(active, allowed)[0], "calculate_impact")
        active["tool_results"].append({"tool": "calculate_impact", "arguments": {"scenario_id": "one"},
            "result": {"metadata": {"impact_scenarios": [{"id": "one"}]}}})
        self.assertIn('"two"', _stage_navigation(active, allowed)[2])
        active["tool_results"].append({"tool": "calculate_impact", "arguments": {"scenario_id": "two"}, "result": {"error": "too_large"}})
        next_tool, missing, _ = _stage_navigation(active, allowed)
        self.assertEqual(next_tool, "submit_stage_report")
        self.assertTrue(missing)

    def test_partial_impact_with_methodology_selection_resumes_without_new_calls(self):
        class TwoScenarioTools(ImpactTools):
            def call(self, name, arguments):
                if name == "list_impact_scenarios":
                    return {"impact_catalog": [{"id": "impact-test"}, {"id": "impact-unrequested"}]}
                return super().call(name, arguments)
        with tempfile.TemporaryDirectory() as directory, patch("finmodel_audit.tools.WorkbookTools", TwoScenarioTools):
            path, output = Path(directory) / "model.xlsx", Path(directory) / "output"
            make_workbook(path)
            report = run_analysis(path, output, client=ImpactClient())
            audit = report["stages"][0]
            self.assertEqual(audit["completion"], "incomplete")
            self.assertEqual(audit["impact_coverage"], {"planned": 2, "delivered": 1,
                "missing_ids": ["impact-unrequested"], "scope": "conditional_calculations_only"})
            self.assertEqual(len(audit["impact_calculations"]), 1)
            client = ImpactClient()
            resumed = run_analysis(path, output, client=client)
            self.assertEqual(client.calls, [])
            self.assertEqual(resumed["checkpoint"]["resumed_stages"], 4)
            self.assertEqual(resumed["stages"], report["stages"])

    def test_partial_impact_coverage_and_extra_limitations_are_not_whitelisted(self):
        class CanonicalTwoScenarioTools(ImpactTools):
            def validation_reports(self, cancel_check=None):
                return {"impact_analysis": {"version": "test", "status": "limited",
                    "scenarios": [{"id": "impact-test"}, {"id": "impact-unrequested"}]}}

            def call(self, name, arguments):
                if name == "list_impact_scenarios":
                    return {"impact_catalog": [{"id": "impact-test"}, {"id": "impact-unrequested"}]}
                return super().call(name, arguments)
        with tempfile.TemporaryDirectory() as directory, patch("finmodel_audit.tools.WorkbookTools", CanonicalTwoScenarioTools):
            path, output = Path(directory) / "model.xlsx", Path(directory) / "output"
            make_workbook(path)
            report = run_analysis(path, output, client=ImpactClient())
            state_path = next((output / "checkpoints").glob("*/state.json"))
            original = json.loads(state_path.read_text())
            first_resume = ImpactClient()
            run_analysis(path, output, client=first_resume)
            self.assertEqual(first_resume.calls, [])
            for kind in ("unknown_missing_id", "wrong_count", "boolean_count", "extra_limitation", "claimed_complete", "erased_ledger"):
                with self.subTest(kind=kind):
                    changed = deepcopy(original)
                    audit = changed["completed"]["audit"]
                    if kind == "unknown_missing_id": audit["impact_coverage"]["missing_ids"] = ["not-in-canonical-analysis"]
                    elif kind == "wrong_count": audit["impact_coverage"]["planned"] = 50
                    elif kind == "boolean_count": audit["impact_coverage"]["delivered"] = True
                    elif kind == "extra_limitation": audit["limitations"].append("A checkpoint cannot authorize arbitrary narrative.")
                    elif kind == "claimed_complete": audit["completion"] = "submitted"
                    else: changed["stage_impact_ids"]["audit"] = []
                    state_path.write_text(json.dumps(changed))
                    client = ImpactClient()
                    restored = run_analysis(path, output, client=client)
                    self.assertTrue(client.calls)
                    self.assertEqual(restored["stages"][0]["impact_coverage"], report["stages"][0]["impact_coverage"])
                    self.assertNotIn("A checkpoint cannot authorize arbitrary narrative.", restored["stages"][0]["limitations"])

    def test_zero_canonical_scenarios_allow_empty_coverage_on_resume(self):
        class EmptyTools(ImpactTools):
            def validation_reports(self, cancel_check=None):
                return {"impact_analysis": {"version": "test", "status": "not_applicable", "scenarios": []}}

            def call(self, name, arguments):
                if name == "list_impact_scenarios":
                    return {"impact_catalog": []}
                return super().call(name, arguments)
        class EmptyClient(ImpactClient):
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                if stage == "audit":
                    self.calls.append(deepcopy(messages))
                    return function_response("submit_stage_report", {"id": stage, "finding_ids": [], "hypotheses": [], "limitations": []})
                return super().chat(messages, functions, function_call, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch("finmodel_audit.tools.WorkbookTools", EmptyTools):
            path, output = Path(directory) / "model.xlsx", Path(directory) / "output"
            make_workbook(path)
            report = run_analysis(path, output, client=EmptyClient())
            self.assertEqual(report["stages"][0]["impact_coverage"]["planned"], 0)
            state_path = next((output / "checkpoints").glob("*/state.json"))
            state = json.loads(state_path.read_text())
            del state["completed"]["audit"]["impact_coverage"]
            state_path.write_text(json.dumps(state))
            client = EmptyClient()
            resumed = run_analysis(path, output, client=client)
            self.assertEqual(client.calls, [])
            self.assertEqual(resumed["checkpoint"]["resumed_stages"], 4)


if __name__ == "__main__":
    unittest.main()
