"""Conditional effects, independent arithmetic and failure boundaries; no API."""
import copy
import json
import math
from pathlib import Path
import re
import unittest

from finmodel_audit.impact import build_impact_analysis, MAX_TOOL_CHARS, _transport_size
from finmodel_audit.workbook import Cell, Sheet, Workbook, _address_parts, _column_letters


def fixture(*, overlap=False, rows=0, columns=0, name="Other O'Brien"):
    cells = {}
    def moved(text):
        def change(m):
            c, r = _address_parts(m.group())
            return f"{_column_letters(c+columns)}{r+rows}"
        return re.sub(r"\b[A-Z]+[1-9][0-9]*\b", change, text)
    def add(address, value, formula=None):
        address = moved(address)
        cells[address] = Cell(address, value, moved(formula) if formula else None, "normal" if formula else None)
    add("A1", "Year"); add("D1", 2024); add("E1", 2025); add("F1", 2026)
    add("A3", "Valuation date"); add("C3", "2025-06-30")
    add("A4", "WACC"); add("B4", "%"); add("C4", .1)
    add("A5", "LTGR"); add("B5", "%"); add("C5", .03)
    add("A7", "FCFF"); add("B7", "RUB M")
    add("A9", "Discount Period"); add("A10", "Discount Factor")
    add("A11", "Discounted FCFF"); add("B11", "RUB M")
    for col, t, flow in (("D", 0, 10), ("E", .5, 12), ("F", 1.5, 14), ("H", 1.5, 14)):
        add(col+"7", flow, "=F7" if col == "H" else None)
        add(col+"9", t, "=F9" if col == "H" else None)
        add(col+"10", 1/1.1**t, f"=1/(1+C4)^{col}9")
        add(col+"11", flow/1.1**t, f"={col}7*{col}10")
    pv = 10+12/1.1**.5+28/1.1**1.5
    add("C11", pv, "=SUM(D11:H11)")
    gordon = 14/1.1**1.5*1.03/.07
    add("A14", "GGM"); add("C14", gordon, "=H11*(1+C5)/(C4-C5)")
    add("D14", .4, "=1-D15"); add("D15", .6); add("C15", 100)
    tv = gordon*.4+100*.6
    add("A16", "TV"); add("C16", tv, "=C14*D14+C15*D15")
    add("A17", "Discounted TV"); add("C17", tv/1.1**1.5, "=C16*F10")
    add("A18", "EV"); add("B18", "RUB M"); add("C18", pv+tv/1.1**1.5, "=C11+C17")
    if overlap:
        cells.clear()
        add("A3", "Net Profit"); add("B3", "RUB M")
        add("A4", "NOPAT"); add("B4", "RUB M")
        add("A5", "D&A"); add("A6", "FCFF"); add("B6", "RUB M")
        add("A7", "Discount Period"); add("A8", "Discount Factor")
        add("A9", "DCF"); add("B9", "RUB M")
        add("A12", "WACC"); add("B12", "%"); add("C12", .1)
        add("C6", -100); add("C9", -100, "=C6*1")
        total = -100
        for col, t, ebit in (("D", 1, 80), ("E", 2, 100)):
            add(col+"1", ebit); add(col+"2", -10); add(col+"13", -5)
            add(col+"3", ebit-15, f"={col}1+{col}2+{col}13")
            add(col+"4", ebit-10, f"={col}1+{col}2")
            add(col+"5", 8); add(col+"6", 2*ebit-17, f"=SUM({col}3:{col}5)")
            add(col+"7", t); add(col+"8", 1/1.1**t, f"=1/(1+C12)^{col}7")
            add(col+"9", (2*ebit-17)/1.1**t, f"={col}6*{col}8")
            total += (2*ebit-17)/1.1**t
        add("A18", "NPV"); add("B18", "RUB M"); add("C18", total, "=SUM(C9:E9)")
        add("A19", "IRR"); add("B19", "%"); add("C19", .4, "=IRR(C9:E9)")
    wb = Workbook(Path("test.xlsx"), {name: Sheet(name, cells=cells)}, [], [], {}, [])
    return wb, lambda address: cells[moved(address)], add


class ImpactTests(unittest.TestCase):
    def by_kind(self, report):
        return {x["kind"]: x for x in report["scenarios"]}

    def assert_proof(self, wb, scenario):
        proof = {(x["sheet"], x["cell"]): x for x in scenario["cell_evidence"]}
        self.assertEqual(set(proof), {(x["sheet"], x["cell"]) for x in scenario["evidence"]})
        for node, row in proof.items():
            cell = wb.sheets[node[0]].cells.get(node[1])
            if cell is None:
                self.assertIs(row["present"], False)
                self.assertIsNone(row["value"])
                continue
            self.assertEqual(row["value"], cell.value)
            self.assertEqual(row.get("formula"), cell.formula)
        self.assertTrue(scenario["conditional"])
        self.assertFalse(scenario["recalculated_workbook"])
        self.assertEqual(scenario["financial_materiality"], "not_assessed")
        self.assertEqual(scenario["raw_unit"], "RUB M")
        json.dumps(scenario, allow_nan=False)
        self.assertLessEqual(_transport_size(scenario), MAX_TOOL_CHARS)
        for group in ("source_inputs", "formula_path", "patches"):
            self.assertTrue({(x["sheet"], x["cell"]) for x in scenario[group]} <= set(proof))

    def test_shifted_renamed_independent_scenarios_and_no_mutation(self):
        for rows, cols, name in ((0, 0, "Other O'Brien"), (70, 9, "План иной")):
            wb, get, _ = fixture(rows=rows, columns=cols, name=name)
            before = copy.deepcopy(wb)
            report = build_impact_analysis(wb)
            self.assertEqual(wb, before)
            scenarios = self.by_kind(report)
            self.assertEqual(set(scenarios), {"remove_sum_terms", "remove_historical_sum_term", "replace_gordon_numerator"})
            base = get("C18").value
            self.assertAlmostEqual(scenarios["remove_sum_terms"]["result"], base-14/1.1**1.5)
            self.assertAlmostEqual(scenarios["remove_historical_sum_term"]["result"], base-10)
            effect = (14-14/1.1**1.5)*1.03/.07*.4/1.1**1.5
            self.assertAlmostEqual(scenarios["replace_gordon_numerator"]["result"], base+effect)
            # Removing a sum occurrence leaves its other consumer (Gordon) intact.
            self.assertEqual(len(scenarios["remove_sum_terms"]["patches"]), 1)
            for s in scenarios.values():
                self.assert_proof(wb, s)
                self.assertAlmostEqual(s["baseline"], base)

    def test_overlap_two_alternatives_all_periods(self):
        for rows, cols in ((0, 0), (20, 4)):
            wb, get, _ = fixture(overlap=True, rows=rows, columns=cols)
            before = copy.deepcopy(wb)
            report = build_impact_analysis(wb)
            scenarios = self.by_kind(report)
            self.assertEqual(set(scenarios), {"remove_net_profit", "remove_nopat"})
            self.assertAlmostEqual(scenarios["remove_net_profit"]["result"], -100+78/1.1+98/1.1**2)
            self.assertAlmostEqual(scenarios["remove_nopat"]["result"], -100+73/1.1+93/1.1**2)
            for s in scenarios.values():
                self.assertEqual(len(s["patches"]), 2)
                self.assertEqual([p["source_discount_interval"] for p in s["patches"]], [1, 2])
                self.assert_proof(wb, s)
            self.assertEqual(wb, before)
            self.assertTrue(any(x.get("code") == "irr_on_discounted_cashflows" and x["status"] == "unsupported" for x in report["checks"]))

    def test_negative_and_zero_baseline(self):
        for base in (-50, 0):
            wb, get, add = fixture()
            offset = base - get("C18").value
            add("Z25", offset)
            add("C18", base, "=C11+C17+Z25")
            for s in build_impact_analysis(wb)["scenarios"]:
                self.assertAlmostEqual(s["baseline"], base)
                self.assertEqual(s["relative_delta"], None if not base else s["delta"]/abs(base))

    def test_equal_distinct_flow_is_not_omitted(self):
        wb, get, _ = fixture()
        get("H7").formula = None
        self.assertNotIn("remove_sum_terms", self.by_kind(build_impact_analysis(wb)))

    def test_missing_calendar_does_not_guess_historical_effect(self):
        wb, get, _ = fixture()
        get("C3").value = "unresolved"
        report = build_impact_analysis(wb)
        self.assertNotIn("remove_historical_sum_term", self.by_kind(report))
        self.assertTrue(any(x.get("reason") == "calendar_not_established_for_historical_omission" for x in report["checks"]))

    def test_known_good_formulas_have_no_effect_candidates(self):
        wb, get, add = fixture()
        pv = 12/1.1**.5+14/1.1**1.5
        add("C11", pv, "=SUM(E11:F11)")
        gordon = 14*1.03/.07
        add("C14", gordon, "=H7*(1+C5)/(C4-C5)")
        tv = gordon*.4+100*.6
        add("C16", tv, "=C14*D14+C15*D15")
        add("C17", tv/1.1**1.5, "=C16*F10")
        add("C18", pv+tv/1.1**1.5, "=C11+C17")
        self.assertEqual(build_impact_analysis(wb)["scenarios"], [])

    def test_atomic_packing_keeps_full_sources_including_blank(self):
        from finmodel_audit.pipeline import _pack_tool_result
        from finmodel_audit.agent_evidence import _refs
        wb, _, _ = fixture()
        report = build_impact_analysis(wb)
        for scenario in report["scenarios"]:
            public = {k:v for k,v in scenario.items() if k != "cell_evidence"}
            packed, shown = _pack_tool_result({"impact_scenarios": [public], "cell_evidence": scenario["cell_evidence"]}, 14000)
            self.assertEqual(len(packed.get("metadata", {}).get("impact_scenarios", [])), 1)
            self.assertTrue(_refs(public) <= shown)
        duplicate = self.by_kind(report)["remove_sum_terms"]
        self.assertTrue(any(x.get("present") is False for x in duplicate["cell_evidence"]))

    def test_baseline_mismatch_and_unsupported_downstream_refuse(self):
        for formula, adjustment in (("=C11+C17", 1), ("=IF(TRUE,C11+C17,0)", 0)):
            wb, get, _ = fixture()
            get("C18").formula = formula
            get("C18").value += adjustment
            report = build_impact_analysis(wb)
            self.assertFalse(report["scenarios"])
            self.assertTrue(any(x["status"] == "unsupported" for x in report["checks"]))

    def test_stale_gordon_pv_refuses_replacement(self):
        wb, get, _ = fixture()
        get("H11").value += 1
        self.assertNotIn("replace_gordon_numerator", self.by_kind(build_impact_analysis(wb)))

    def test_no_candidates_and_forged_report_cannot_inject_patch(self):
        wb, _, _ = fixture()
        report = build_impact_analysis(wb, {"findings": [{"id": "forged", "code": "gordon_double_discount"}]})
        self.assertEqual(report["status"], "not_applicable")
        self.assertEqual(report["scenarios"], [])
        blank = Workbook(Path("blank.xlsx"), {}, [], [], {}, [])
        self.assertEqual(build_impact_analysis(blank)["status"], "not_applicable")

    def test_nonfinite_input_and_unknown_unit_fail_closed(self):
        for address, value in (("C18", math.inf), ("B18", "unexplained units")):
            wb, get, _ = fixture()
            get(address).value = value
            report = build_impact_analysis(wb)
            # No nonfinite outputs or hidden unit conversion reach the report.
            json.dumps(report, allow_nan=False)
            self.assertFalse(report["scenarios"])
        wb, get, _ = fixture()
        get("D15").value = float("nan")
        report = build_impact_analysis(wb)
        self.assertNotIn("replace_gordon_numerator", self.by_kind(report))
        json.dumps(report, allow_nan=False)

    def test_overlap_inconsistent_periods_refused(self):
        wb, get, _ = fixture(overlap=True)
        get("E7").value = 1
        report = build_impact_analysis(wb)
        self.assertFalse(report["scenarios"])
        self.assertTrue(any("intervals" in x.get("reason", "") for x in report["checks"]))

    def test_ids_repeat_deterministically(self):
        wb, _, _ = fixture()
        self.assertEqual(build_impact_analysis(wb), build_impact_analysis(wb))


if __name__ == "__main__":
    unittest.main()
