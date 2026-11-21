"""Independent acceptance rejects misleading ranks and corrupted native charts."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("submission_evaluation", ROOT / "scripts/verify_submission.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def inputs():
    ref = {"sheet": "S", "cell": "C1"}
    source = {"sheet": "S", "cell": "A1"}
    scenario = {"id": "one", "code": "theme", "target": ref, "raw_unit": "RUB M", "baseline": 100,
                "saved_output": 100, "result": 120, "delta": 20, "relative_delta": .2,
                "finding_ids": ["finding"], "conditional": True, "recalculated_workbook": False,
                "patches": [{**ref, "original_formula": "=A1*10", "changed_formula": "=A1*12"}],
                "formula_path": [{**ref, "formula": "=A1*10"}], "source_inputs": [{**source, "value": 10, "frozen": True}],
                "evidence": [ref, source], "cell_evidence": [{**ref, "value": 100, "formula": "=A1*10"}, {**source, "value": 10}]}
    report = {"financial_checks": {"findings": [{"id": "finding", "code": "theme"}]},
              "impact_analysis": {"scenarios": [scenario]}}
    assessment = {"outputs": [{"target": ref, "raw_unit": "RUB M", "baseline": 100, "ranked_findings": [
        {"rank": 1, "code": "theme", "finding_ids": ["finding"], "scenario_ids": ["one"],
         "max_absolute_delta": 20, "max_absolute_relative_delta": .2,
         "alternatives": [{"id": "one", "result": 120, "delta": 20, "relative_delta": .2}],
         "evidence": [ref], "reason": "Largest measured conditional effect", "action": "Check formula"}]}],
        "unquantified_findings": [{"code": "unknown", "rank": None, "delta": None}],
        "suitability": {"financial_approval": False, "summary": "Review is required.", "supported_uses": ["Read saved figures"],
                        "unsubstantiated_uses": ["Approved valuation"], "required_checks": ["Check formula"]}}
    return report, assessment


def package(*, cached=10, embedded=10, font="Arial", include_embedded=True):
    r = evaluation.NS["r"]
    a = evaluation.NS["a"]
    p = evaluation.NS["p"]
    c = evaluation.NS["c"]
    rel = evaluation.NS["rel"]
    sheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    parts = {
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{rel}"><Relationship Id="s1" Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:c="{c}" xmlns:r="{r}"><p:cSld><p:spTree><p:sp><a:r><a:rPr sz="1200"><a:latin typeface="{font}"/></a:rPr><a:t>Title</a:t></a:r></p:sp><a:tbl/><c:chart r:id="chart1"/></p:spTree></p:cSld></p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": f'<Relationships xmlns="{rel}"><Relationship Id="chart1" Target="../charts/chart1.xml"/></Relationships>',
        "ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="{c}" xmlns:a="{a}" xmlns:r="{r}"><a:latin typeface="Arial"/><c:chart><c:plotArea><c:barChart><c:ser><c:cat><c:strRef><c:f>Data!$A$2:$A$2</c:f><c:strCache><c:pt idx="0"><c:v>Baseline</c:v></c:pt></c:strCache></c:strRef></c:cat><c:val><c:numRef><c:f>Data!$B$2:$B$2</c:f><c:numCache><c:pt idx="0"><c:v>{cached}</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart><c:externalData r:id="book"/></c:chartSpace>',
        "ppt/charts/_rels/chart1.xml.rels": f'<Relationships xmlns="{rel}"><Relationship Id="book" Target="../embeddings/book.xlsx"/></Relationships>',
    }
    book = io.BytesIO()
    with ZipFile(book, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{sheet}" xmlns:r="{r}"><sheets><sheet name="Data" r:id="sheet1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{rel}"><Relationship Id="sheet1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{sheet}"><sheetData><row r="2"><c r="A2" t="inlineStr"><is><t>Baseline</t></is></c><c r="B2"><v>{embedded}</v></c></row></sheetData></worksheet>')
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
        if include_embedded:
            archive.writestr("ppt/embeddings/book.xlsx", book.getvalue())
    return output.getvalue()


class SubmissionEvaluationTests(unittest.TestCase):
    def test_frozen_priorities_are_conditional_and_alternatives_not_added(self):
        gold = json.loads(evaluation.GOLD.read_text())["reports"]
        self.assertEqual(gold["original"]["expected_theme_order"], ["gordon_double_discount", "pv_fcff_duplicate_term", "pv_fcff_historical_period"])
        scenarios = gold["second"]["scenarios"]
        self.assertEqual(gold["second"]["expected_ranked_groups"], 1)
        self.assertGreater(max(abs(s["delta"]) for s in scenarios), 139000)
        self.assertLess(max(abs(s["delta"]) for s in scenarios), sum(abs(s["delta"]) for s in scenarios))

    def test_complete_finite_decision_contract_is_accepted(self):
        report, assessment = inputs()
        self.assertEqual(evaluation.decision_checks(report, assessment), [])

    def test_wrong_relative_sign_and_zero_denominator_are_rejected(self):
        report, assessment = inputs()
        report["impact_analysis"]["scenarios"][0]["relative_delta"] = -.2
        self.assertIn("invalid_relative_delta", evaluation.decision_checks(report, assessment))
        scenario = report["impact_analysis"]["scenarios"][0]
        scenario.update(baseline=0, result=20, delta=20)
        self.assertIn("zero_baseline_percentage", evaluation.numeric_scenario_issues(scenario))

    def test_unknown_impact_cannot_receive_low_numeric_rank(self):
        report, assessment = inputs()
        assessment["unquantified_findings"][0].update(rank=9, delta=0)
        self.assertIn("unknown_impact_given_numeric_priority", evaluation.decision_checks(report, assessment))

    def test_alternatives_sum_cannot_replace_maximum_individual_effect(self):
        report, assessment = inputs()
        assessment["outputs"][0]["ranked_findings"][0]["max_absolute_delta"] = 40
        self.assertIn("alternatives_summed_or_max_mismatch", evaluation.decision_checks(report, assessment))

    def test_missing_or_changed_source_proof_is_not_measured_evidence(self):
        report, assessment = inputs()
        report["impact_analysis"]["scenarios"][0]["cell_evidence"] = []
        issues = evaluation.decision_checks(report, assessment)
        self.assertIn("scenario_evidence_incomplete", issues)
        self.assertIn("saved_target_proof_mismatch", issues)

    def test_mixed_units_foreign_findings_and_approval_are_rejected(self):
        report, assessment = inputs()
        assessment["outputs"][0]["raw_unit"] = "тыс. руб."
        assessment["suitability"]["financial_approval"] = True
        report["financial_checks"]["findings"][0]["code"] = "other"
        issues = evaluation.decision_checks(report, assessment)
        self.assertIn("mixed_output_unit_or_baseline", issues)
        self.assertIn("unproven_finding_link", issues)
        self.assertIn("unsupported_financial_approval", issues)

    def test_native_chart_has_exact_editable_embedded_data(self):
        result = evaluation.pptx_structure(package())
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["charts"][0]["series"][0]["values"], [10])
        self.assertEqual(result["slides"][0]["tables"], 1)

    def test_chart_cache_and_embedded_data_mismatch_is_caught(self):
        result = evaluation.pptx_structure(package(cached=10, embedded=999))
        self.assertIn("chart_cache_disagrees_with_embedded_workbook", result["issues"])

    def test_missing_editable_workbook_and_wrong_visible_font_are_caught(self):
        result = evaluation.pptx_structure(package(font="Calibri", include_embedded=False))
        self.assertIn("visible_slide_font_not_explicit_arial", result["issues"])
        self.assertIn("native_chart_has_no_embedded_workbook", result["issues"])

    def test_slide_plan_cannot_hide_missing_text(self):
        report, _ = inputs()
        plan = {"slides": [{"title": "Missing title", "paragraphs": [], "table": None, "chart": None}]}
        result = evaluation.pptx_structure(package())
        issues = evaluation.pptx_content_checks(result, plan, report)
        self.assertIn("plan_text_or_table_not_preserved", issues)
        self.assertIn("unplanned_native_chart", issues)

    def test_conditional_notes_preserve_full_source_cells(self):
        report, _ = inputs()
        scenario = report["impact_analysis"]["scenarios"][0]
        basis = {"scenarios": [deepcopy(scenario)], "independent_alternatives": True}
        slide = {"title": "Result", "note": json.dumps(basis), "sources": ["S!C1"],
                 "chart": {"categories": ["Baseline", "Alternative"], "values": [100, 120], "unit": "RUB M"}}
        structure = {"slides": [{"text": ["Result", "RUB M"], "notes": [slide["note"]], "charts": ["chart1"]}],
                     "charts": [{"part": "chart1", "series": [{"categories": ["Baseline", "Alternative"], "values": [100, 120]}]}]}
        self.assertEqual(evaluation.pptx_content_checks(structure, {"slides": [slide]}, report), [])
        basis["scenarios"][0]["cell_evidence"][0]["value"] = 999
        slide["note"] = json.dumps(basis)
        structure["slides"][0]["notes"] = [slide["note"]]
        self.assertIn("conditional_notes_differ_from_canonical_source", evaluation.pptx_content_checks(structure, {"slides": [slide]}, report))

    def test_existing_evaluation_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                evaluation.main(["--output-dir", directory])


if __name__ == "__main__":
    unittest.main()
