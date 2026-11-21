"""A prepared result is not a delivered effect; all its atoms must survive."""
from copy import deepcopy
import unittest

from finmodel_audit.agent_evidence import _refs, register_sent_evidence
from finmodel_audit.impact import build_impact_analysis
from finmodel_audit.impact_report import validated_impact
from finmodel_audit.pipeline import _data_message, _evidence_memo, _json, _pack_tool_result
from test_impact import fixture


def tool_example(*, overlap=False):
    wb, _, _ = fixture(overlap=overlap)
    scenario = build_impact_analysis(wb)["scenarios"][0]
    raw = {"impact_scenarios": [{k:v for k,v in scenario.items() if k != "cell_evidence"}],
           "cell_evidence": deepcopy(scenario["cell_evidence"]), "conditional": True}
    packed, shown = _pack_tool_result(raw, 14000)
    entry = {"tool": "calculate_impact", "arguments": {"scenario_id": scenario["id"]},
             "result": packed, "impact_scenarios_raw": [deepcopy(scenario)]}
    return scenario, raw, packed, shown, entry


def register(envelope, entries, *, role="function", registry=None):
    registry = {} if registry is None else registry
    result = register_sent_evidence([{"role": role, "content": _data_message(envelope)}], entries, {}, {}, {}, registry)
    return registry, result


class ImpactDeliveryTests(unittest.TestCase):
    def test_full_function_response_registers_original_not_compact_stub(self):
        scenario, _, packed, shown, entry = tool_example()
        registry, accepted = register(packed, [entry])
        self.assertEqual(accepted["impact_ids"], [scenario["id"]])
        self.assertEqual(registry[scenario["id"]]["scenario"], scenario)
        self.assertEqual(validated_impact(registry[scenario["id"]], shown), scenario)
        self.assertTrue(any(r.get("present") is False for r in registry[scenario["id"]]["cell_evidence"]))
        registry[scenario["id"]]["scenario"]["result"] = 0
        self.assertNotEqual(entry["impact_scenarios_raw"][0]["result"], 0)

    def test_prepared_not_sent_wrong_role_or_missing_raw_cannot_register(self):
        _, _, packed, _, entry = tool_example()
        self.assertFalse(register({}, [entry])[0])
        self.assertFalse(register(packed, [entry], role="assistant")[0])
        self.assertFalse(register(packed, [entry], role="system")[0])
        broken = deepcopy(entry)
        broken.pop("impact_scenarios_raw")
        self.assertFalse(register(packed, [broken])[0])
        for flag in ("automatic", "replayed"):
            self.assertFalse(register(packed, [{**entry, flag: True}])[0])

    def test_missing_or_changed_sent_cell_and_changed_scenario_rejected(self):
        scenario, _, packed, _, entry = tool_example()
        sheet = next(iter(packed["cell_data"]))
        for kind in ("missing", "value", "formula", "scenario"):
            sent = deepcopy(packed)
            if kind == "missing": sent["cell_data"][sheet].pop(0)
            elif kind == "value": sent["cell_data"][sheet][0][1] = "altered"
            elif kind == "formula": sent["cell_data"][sheet][0][2] = "=0"
            else: sent["metadata"]["impact_scenarios"][0]["result"] = scenario["result"]+1
            with self.subTest(kind=kind):
                self.assertFalse(register(sent, [entry])[0])

    def test_clipped_trusted_cell_is_not_authorized_even_when_sent(self):
        _, _, packed, _, entry = tool_example()
        sheet = next(iter(packed["cell_data"]))
        clipped = deepcopy(packed)
        clipped["cell_text_limits"] = [{"sheet": sheet, "cell": packed["cell_data"][sheet][0][0], "value_truncated": True, "formula_truncated": False}]
        self.assertFalse(register(clipped, [{**entry, "result": clipped}])[0])

    def test_pack_drops_whole_scenario_when_any_source_missing_or_clipped(self):
        _, raw, _, _, _ = tool_example()
        for kind in ("missing", "clipped"):
            partial = deepcopy(raw)
            if kind == "missing": partial["cell_evidence"].pop(0)
            else: partial["cell_evidence"][0]["value_truncated"] = True
            packed, _ = _pack_tool_result(partial, 14000)
            self.assertFalse(packed.get("metadata", {}).get("impact_scenarios", []))
            self.assertEqual(packed.get("omitted_impact_count"), 1)

    def test_small_pack_budget_never_keeps_scenario_with_partial_sources(self):
        _, raw, _, _, _ = tool_example(overlap=True)
        for budget in (700, 1500, 3500):
            packed, shown = _pack_tool_result(raw, budget)
            self.assertLessEqual(len(_json(packed)), budget)
            for scenario in packed.get("metadata", {}).get("impact_scenarios", []):
                self.assertTrue(_refs(scenario) <= shown)
            self.assertFalse(packed.get("metadata", {}).get("impact_scenarios", []))

    def test_memo_full_proof_authorizes_but_low_budget_drops_atomically(self):
        scenario, _, _, shown, entry = tool_example(overlap=True)
        full = _evidence_memo([entry], 14000)
        self.assertEqual(len(full["impact_scenarios"]), 1)
        registry, _ = register(full, [entry], role="user")
        self.assertEqual(validated_impact(registry[scenario["id"]], shown), scenario)
        for budget in (700, 1800, 3000):
            memo = _evidence_memo([entry], budget)
            self.assertLessEqual(len(_json(memo)), budget)
            self.assertFalse(memo["impact_scenarios"])
            self.assertEqual(memo.get("omitted_impact_count"), 1)
            self.assertFalse(register(memo, [entry])[0])

    def test_memo_never_borrows_a_missing_source_from_untrusted_text(self):
        _, _, packed, _, entry = tool_example()
        sent = deepcopy(packed)
        sheet = next(iter(sent["cell_data"]))
        missing = sent["cell_data"][sheet].pop(0)
        sent["plain_text"] = str(missing)
        self.assertFalse(register(sent, [entry])[0])


if __name__ == "__main__":
    unittest.main()
