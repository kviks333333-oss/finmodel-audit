"""Ordinal conditional priorities, arithmetic replay and no false approval."""
from copy import deepcopy
import json
import math
import unittest

from finmodel_audit.decision import build_decision_assessment
from finmodel_audit.financial import run_financial_checks
from finmodel_audit.impact import build_impact_analysis
from finmodel_audit.workbook import Cell, Sheet, Workbook
from test_impact import fixture


def report_for(workbook):
    return {"financial_checks": run_financial_checks(workbook), "impact_analysis": build_impact_analysis(workbook),
            "stages": [{"id": name, "completion": "submitted"} for name in ("audit", "assumptions", "quality", "business")]}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.wb, self.get, self.add = fixture()
        self.report = report_for(self.wb)

    def test_orders_numeric_effects_without_mutation_or_approval(self):
        original = deepcopy(self.report)
        result = build_decision_assessment(self.report)
        self.assertEqual(self.report, original)
        self.assertEqual(result["issues"], [])
        output = result["outputs"][0]
        rows = output["ranked_findings"]
        effects = [row["max_absolute_delta"] for row in rows]
        self.assertEqual(effects, sorted(effects, reverse=True))
        self.assertEqual([row["rank"] for row in rows], [1, 2, 3])
        self.assertEqual(output["target"], {"sheet": "Other O'Brien", "cell": "C18"})
        self.assertFalse(result["suitability"]["financial_approval"])
        self.assertIsNone(result["policy"]["thresholds"])
        self.assertIn("C18", result["summary"])
        self.assertTrue(all(not row["financial_error_confirmed"] for row in rows))
        json.dumps(result, allow_nan=False)

    def test_two_alternatives_are_one_theme_not_an_added_effect(self):
        workbook, _, _ = fixture(overlap=True)
        result = build_decision_assessment(report_for(workbook))
        self.assertFalse(result["issues"])
        self.assertEqual(len(result["outputs"][0]["ranked_findings"]), 1)
        row = result["outputs"][0]["ranked_findings"][0]
        self.assertEqual(len(row["alternatives"]), 2)
        deltas = [abs(x["delta"]) for x in row["alternatives"]]
        self.assertEqual(row["max_absolute_delta"], max(deltas))
        self.assertNotEqual(row["max_absolute_delta"], sum(deltas))
        unknown = result["unquantified_findings"]
        self.assertEqual([x["code"] for x in unknown], ["irr_on_discounted_cashflows"])
        self.assertIsNone(unknown[0]["rank"])
        self.assertIsNone(unknown[0]["delta"])
        self.assertTrue(any("IRR" in text for text in result["suitability"]["unsubstantiated_uses"]))

    def test_technical_finding_count_never_changes_priorities_or_conclusion(self):
        expected = build_decision_assessment(self.report)
        self.report["structural"] = {"findings": [{"code": "cached_error", "impact": "critical"}] * 9999}
        self.report["supplemental_audit"] = {"findings": [{"severity": "high"}] * 9999}
        self.assertEqual(build_decision_assessment(self.report), expected)

    def test_zero_and_negative_baseline_have_honest_relative_effects(self):
        for baseline in (0, -200):
            with self.subTest(baseline=baseline):
                workbook, get, add = fixture()
                previous = get("C18").value
                add("C20", baseline - previous)
                add("C18", baseline, "=C11+C17+C20")
                result = build_decision_assessment(report_for(workbook))
                self.assertFalse(result["issues"])
                rows = result["outputs"][0]["ranked_findings"]
                self.assertEqual(len(rows), 3)
                for row in rows:
                    if baseline:
                        self.assertAlmostEqual(row["max_absolute_relative_delta"], row["max_absolute_delta"] / abs(baseline))
                    else:
                        self.assertIsNone(row["max_absolute_relative_delta"])
                        self.assertTrue(all(item["relative_delta"] is None for item in row["alternatives"]))

    def test_finite_relative_value_does_not_overflow_when_displayed_as_percent(self):
        from pathlib import Path
        raw = [("A3", "WACC", None), ("C3", 0, None), ("A4", "Discount Period", None),
               ("D4", 1, None), ("E4", 1, "=D4"), ("A5", "Discount Factor", None),
               ("D5", 1, "=1/(1+C3)^D4"), ("E5", 1, "=D5"), ("A7", "FCFF", None),
               ("B7", "RUB M", None), ("D7", -1e305, None), ("E7", -1e305, "=D7"),
               ("A9", "Discounted FCFF", None), ("B9", "RUB M", None),
               ("D9", -1e305, "=D7*D5"), ("E9", -1e305, "=E7*E5"),
               ("C9", -2e305, "=SUM(D9:E9)"), ("C11", 2e305, None), ("C13", .001, None),
               ("A12", "EV", None), ("B12", "RUB M", None), ("C12", .001, "=(C9+C11)+C13")]
        cells = {address: Cell(address, value, formula, "normal" if formula else None) for address, value, formula in raw}
        workbook = Workbook(Path("finite-large.xlsx"), {"S": Sheet("S", cells=cells)}, [], [], {}, [])
        result = build_decision_assessment(report_for(workbook))
        self.assertFalse(result["issues"])
        row = result["outputs"][0]["ranked_findings"][0]
        self.assertTrue(math.isfinite(row["max_absolute_relative_delta"]))
        self.assertFalse(math.isfinite(row["max_absolute_relative_delta"] * 100))
        self.assertIn("в долях", row["reason"])
        self.assertNotIn("inf", row["reason"].lower())
        json.dumps(result, allow_nan=False)

    def test_distinct_outputs_and_units_have_separate_rank_one(self):
        other, _, add = fixture(name="Other entity")
        add("B18", "USD M")
        self.wb.sheets.update(other.sheets)
        result = build_decision_assessment(report_for(self.wb))
        self.assertFalse(result["issues"])
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual({row["raw_unit"] for row in result["outputs"]}, {"RUB M", "USD M"})
        self.assertTrue(all(row["ranked_findings"][0]["rank"] == 1 for row in result["outputs"]))

    def test_consistent_result_delta_tampering_rejected_by_formula_replay(self):
        scenario = self.report["impact_analysis"]["scenarios"][0]
        scenario["result"] += 99
        scenario["delta"] = scenario["result"] - scenario["baseline"]
        scenario["relative_delta"] = scenario["delta"] / abs(scenario["baseline"])
        result = build_decision_assessment(self.report)
        self.assertTrue(any(issue["reason"] == "scenario_replay_mismatch" for issue in result["issues"]))
        self.assertTrue(any(row["code"] == scenario["code"] for row in result["unquantified_findings"]))
        self.assertFalse(any(scenario["id"] in row["scenario_ids"] for out in result["outputs"] for row in out["ranked_findings"]))

    def test_changed_formula_cannot_be_forged_even_with_consistent_result(self):
        scenario = self.report["impact_analysis"]["scenarios"][0]
        scenario["patches"][0]["changed_formula"] = "=1"
        result = build_decision_assessment(self.report)
        self.assertTrue(any(issue.get("reason") == "changed_formula_is_not_declared_exclusion" for issue in result["issues"]))

    def test_declared_exclusion_of_a_different_flow_does_not_borrow_duplicate_proof(self):
        from finmodel_audit.financial import _Checks
        from finmodel_audit.impact import _without, _formula
        scenario = next(s for s in self.report["impact_analysis"]["scenarios"] if s["kind"] == "remove_sum_terms")
        patch = scenario["patches"][0]
        checker = _Checks(self.wb)
        excluded = ("Other O'Brien", "E11")
        patch["excluded"] = [{"sheet": excluded[0], "cell": excluded[1]}]
        patch["changed_formula"] = "=" + _formula(_without(checker.sum_terms(checker.parsed[(patch["sheet"], patch["cell"])]), [excluded]))
        result = build_decision_assessment(self.report)
        self.assertTrue(any(issue.get("id") == scenario["id"] for issue in result["issues"]))
        self.assertFalse(any(scenario["id"] in row["scenario_ids"] for out in result["outputs"] for row in out["ranked_findings"]))

    def test_frozen_boundary_and_formula_path_cannot_be_trimmed_silently(self):
        for field in ("source_inputs", "formula_path", "ignored_sum_inputs"):
            with self.subTest(field=field):
                report = deepcopy(self.report)
                scenario = report["impact_analysis"]["scenarios"][0]
                self.assertTrue(scenario[field])
                scenario[field].pop()
                result = build_decision_assessment(report)
                self.assertTrue(result["issues"])
                self.assertFalse(any(scenario["id"] in row["scenario_ids"] for out in result["outputs"] for row in out["ranked_findings"]))

    def test_tampered_checks_or_stage_shape_remains_a_limited_result(self):
        self.report["stages"] = [{"id": []}]
        self.report["impact_analysis"]["checks"] = [{"status": "unsupported", "finding_ids": None, "reason": "unknown"}]
        result = build_decision_assessment(self.report)
        self.assertEqual(len(result["suitability"]["incomplete_stages"]), 4)
        self.assertFalse(result["suitability"]["financial_approval"])

    def test_missing_proof_unknown_id_wrong_units_nonfinite_and_malformed_rejected(self):
        changes = [lambda s: s.pop("cell_evidence"), lambda s: s.update(finding_ids=["invented"]),
                   lambda s: s.update(raw_unit="USD M"), lambda s: s.update(delta=float("nan")),
                   lambda s: s.update(id=[]), lambda s: s.update(target={"sheet": "Unknown", "cell": "A1"})]
        for change in changes:
            with self.subTest(change=change):
                report = deepcopy(self.report)
                scenario = report["impact_analysis"]["scenarios"][0]
                identity = scenario["id"]
                change(scenario)
                result = build_decision_assessment(report)
                self.assertTrue(result["issues"])
                self.assertFalse(any(identity in row["scenario_ids"] for out in result["outputs"] for row in out["ranked_findings"]))
                json.dumps(result, allow_nan=False)

    def test_conflicting_snapshot_or_duplicate_ids_cannot_choose_convenient_values(self):
        for kind in ("snapshot", "identity"):
            with self.subTest(kind=kind):
                report = deepcopy(self.report)
                first = report["impact_analysis"]["scenarios"][0]
                if kind == "snapshot":
                    proof = next(row for row in first["cell_evidence"] if row["cell"] == "C18")
                    proof["value"] += 100
                else:
                    second = deepcopy(first);second["result"] += 1
                    report["impact_analysis"]["scenarios"].append(second)
                result = build_decision_assessment(report)
                self.assertTrue(result["issues"])
                self.assertFalse(any(first["id"] in row["scenario_ids"] for out in result["outputs"] for row in out["ranked_findings"]))

    def test_empty_malformed_and_incomplete_reports_never_get_a_green_verdict(self):
        for report in (None, {}, {"financial_checks": [1]}, {"stages": [{"id": "audit", "completion": "incomplete"}]}):
            with self.subTest(report=report):
                result = build_decision_assessment(report)
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertFalse(result["suitability"]["financial_approval"])
                self.assertTrue(result["suitability"]["incomplete_stages"])
        self.report["stages"] = []
        result = build_decision_assessment(self.report)
        self.assertEqual(len(result["outputs"]), 1)
        self.assertEqual(len(result["suitability"]["incomplete_stages"]), 4)


if __name__ == "__main__":
    unittest.main()
