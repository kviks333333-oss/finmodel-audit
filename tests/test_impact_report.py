"""Publication boundary for trusted, delivered conditional calculations."""
from copy import deepcopy
import math
import unittest

from finmodel_audit.agent_evidence import _refs
from finmodel_audit.impact import build_impact_analysis
from finmodel_audit.impact_report import ImpactReportError, attach_impact_reports, validated_impact
from test_impact import fixture


def example(*, baseline=None):
    wb, get, add = fixture()
    if baseline is not None:
        add("Z25", baseline-get("C18").value)
        add("C18", baseline, "=C11+C17+Z25")
    scenario = next(s for s in build_impact_analysis(wb)["scenarios"] if s["kind"] == "remove_sum_terms")
    entry = {"scenario": scenario, "cell_evidence": deepcopy(scenario["cell_evidence"])}
    return entry, _refs(scenario)


class ImpactReportTests(unittest.TestCase):
    def test_full_delivered_scenario_preserves_canonical_proof(self):
        entry, retrieved = example()
        result = validated_impact(entry, retrieved)
        self.assertEqual(result, entry["scenario"])
        result["patches"][0]["changed_formula"] = "changed in caller"
        self.assertNotEqual(result, entry["scenario"])

    def test_negative_and_zero_baseline_contract(self):
        for baseline in (-50, 0):
            with self.subTest(baseline=baseline):
                entry, retrieved = example(baseline=baseline)
                result = validated_impact(entry, retrieved)
                self.assertEqual(result["relative_delta"], None if baseline == 0 else result["delta"]/abs(baseline))
                broken = deepcopy(entry)
                broken["scenario"]["relative_delta"] = 0 if baseline == 0 else -result["relative_delta"]
                with self.assertRaises(ImpactReportError):
                    validated_impact(broken, retrieved)

    def test_delta_result_baseline_and_nonfinite_tampering_rejected(self):
        entry, retrieved = example()
        for field, value in (("delta", 999), ("result", 999), ("baseline", 999),
                             ("saved_output", 999), ("relative_delta", 999),
                             ("delta", float("nan")), ("result", math.inf), ("baseline", True)):
            with self.subTest(field=field, value=value):
                broken = deepcopy(entry)
                broken["scenario"][field] = value
                with self.assertRaises(ImpactReportError):
                    validated_impact(broken, retrieved)

    def test_missing_source_record_or_delivery_ref_rejected(self):
        entry, retrieved = example()
        for node in sorted(retrieved):
            broken = deepcopy(entry)
            broken["cell_evidence"] = [r for r in broken["cell_evidence"] if (r["sheet"], r["cell"]) != node]
            with self.assertRaises(ImpactReportError):
                validated_impact(broken, retrieved)
            with self.assertRaises(ImpactReportError):
                validated_impact(entry, retrieved-{node})

    def test_frozen_source_and_original_formula_must_match_delivered_rows(self):
        entry, retrieved = example()
        for field, key, value in (("source_inputs", "value", 999),
                                  ("source_inputs", "frozen", False),
                                  ("formula_path", "formula", "=0"),
                                  ("patches", "original_formula", "=0")):
            broken = deepcopy(entry)
            broken["scenario"][field][0][key] = value
            with self.assertRaises(ImpactReportError):
                validated_impact(broken, retrieved)

    def test_scope_unit_and_required_paths_rejected_if_absent(self):
        entry, retrieved = example()
        for key, value in (("conditional", False), ("recalculated_workbook", True),
                            ("raw_unit", ""), ("patches", []), ("formula_path", []), ("source_inputs", [])):
            broken = deepcopy(entry)
            broken["scenario"][key] = value
            with self.assertRaises(ImpactReportError):
                validated_impact(broken, retrieved)

    def test_attach_keeps_code_proof_and_conditional_scope_once(self):
        entry, retrieved = example()
        report = {"id": "audit", "summary": "Bounded review.", "observations": [], "limitations": []}
        registry = {entry["scenario"]["id"]: entry}
        result = attach_impact_reports(report, registry, retrieved)
        self.assertIs(result, report)
        self.assertEqual(report["impact_calculations"], [entry["scenario"]])
        observation = report["observations"][0]
        self.assertEqual(observation["status"], "not_verified")
        self.assertEqual(observation["origin"], "agent_requested_deterministic_scenario")
        self.assertEqual(observation["conditional_impact"], entry["scenario"])
        self.assertEqual(observation["evidence"], entry["scenario"]["evidence"])
        self.assertIn("не исправленная стоимость", observation["detail"])
        self.assertIn("не складываются", observation["detail"])
        snapshot = deepcopy(report)
        attach_impact_reports(report, registry, retrieved)
        self.assertEqual(report, snapshot)

    def test_attach_empty_selection_has_no_effect_and_invalid_selection_is_atomic(self):
        report = {"summary": "Review", "observations": [], "limitations": []}
        attach_impact_reports(report, {}, set())
        self.assertEqual(report, {"summary": "Review", "observations": [], "limitations": [], "impact_calculations": []})
        entry, retrieved = example()
        fresh = {"summary": "Review", "observations": [], "limitations": []}
        before = deepcopy(fresh)
        with self.assertRaises(ImpactReportError):
            attach_impact_reports(fresh, {entry["scenario"]["id"]: entry}, set())
        self.assertEqual(fresh, before)


if __name__ == "__main__":
    unittest.main()
