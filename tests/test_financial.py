"""Offline structural tests; fixture-specific addresses occur only in tests."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import unittest

from finmodel_audit.financial import _Checks, _Parser, run_financial_checks
from finmodel_audit.workbook import Cell, Sheet, Workbook, _address_parts, _column_letters, load_workbook


def make_model(*, bad=True, sheet_name="Forecast O'Brien", row_shift=0, column_shift=0):
    cells = {}
    def moved(text):
        def change(match):
            column, row = _address_parts(match.group())
            return f"{_column_letters(column + column_shift)}{row + row_shift}"
        return re.sub(r"\b[A-Z]{1,3}[1-9][0-9]*\b", change, text)
    def add(address, value, formula=None, attrs=None):
        target = moved(address)
        attrs = {key: moved(value) if key in {"ref", "r1", "r2"} else value for key, value in (attrs or {}).items()}
        cells[target] = Cell(target, value, moved(formula) if formula else None,
                             "dataTable" if attrs else "normal" if formula else None,
                             formula_attributes=attrs)
    add("D1", 2024); add("E1", 2025); add("F1", 2026); add("H1", "TV")
    add("A3", "Valuation date"); add("B3", "2025-06-30")
    add("A4", "WACC"); add("B4", 0.1)
    add("A5", "LTGR"); add("B5", 0.03)
    add("A7", "FCFF"); add("D7", 10); add("E7", 12); add("F7", 14); add("H7", 14, "=F7")
    add("A9", "Discount Period"); add("D9", 0); add("E9", 0.5); add("F9", 1.5); add("H9", 1.5, "=F9")
    add("A10", "Discount Factor")
    for column, period in (("D", 0), ("E", .5), ("F", 1.5)):
        add(column + "10", 1 / 1.1 ** period, f"=1/(1+B4)^{column}9")
    add("H10", 1 / 1.1 ** 1.5, "=F10")
    add("A11", "Discounted FCFF")
    pv = {}
    for column, flow, period in (("D", 10, 0), ("E", 12, .5), ("F", 14, 1.5), ("H", 14, 1.5)):
        pv[column] = flow / 1.1 ** period
        add(column + "11", pv[column], f"={column}7*{column}10")
    total = sum(pv.values()) if bad else pv["E"] + pv["F"]
    add("B11", total, "=SUM(D11:H11)" if bad else "=SUM(E11:F11)")
    add("A14", "GGM")
    gordon = (pv["H"] if bad else 14) * 1.03 / .07
    add("B14", gordon, "=H11*(1+B5)/(B4-B5)" if bad else "=H7*(1+B5)/(B4-B5)")
    add("B15", 100); add("C14", 1, "=100%-C15"); add("C15", 0)
    add("A16", "TV"); add("B16", gordon, "=B14*C14+B15*C15")
    add("A17", "Discounted TV"); add("B17", gordon / 1.1 ** 1.5, "=B16*F10")
    ev = total + gordon / 1.1 ** 1.5
    add("A18", "EV"); add("B18", ev, "=B11+B17")
    add("C20", ev, "=B18"); add("D20", .03); add("E20", .04)
    add("C21", .1); add("C22", .11)
    add("D21", ev * (.6 if bad else 1), attrs={"ref": "D21:E22", "dt2D": "1", "dtr": "1", "r1": "B5", "r2": "B4"})
    add("E21", ev * 1.1); add("D22", ev * .9); add("E22", ev)
    workbook = Workbook(Path("synthetic.xlsx"), {sheet_name: Sheet(sheet_name, cells=cells)}, [], [], {}, [])
    return workbook, lambda address: (sheet_name, moved(address))


def get(wb, node):
    return wb.sheets[node[0]].cells[node[1]]


def codes(result):
    return {item["code"] for item in result["findings"]}


class FinancialChecksTests(unittest.TestCase):
    ALL = {"pv_fcff_duplicate_term", "pv_fcff_historical_period", "gordon_double_discount", "datatable_basepoint_cache_mismatch"}

    def assert_valid_evidence(self, wb, result):
        json.dumps(result, allow_nan=False)
        for item in result["findings"]:
            self.assertTrue(item["evidence"])
            self.assertEqual(item["financial_materiality"], "not_assessed")
            for ref in item["evidence"]:
                self.assertIn(ref["cell"], wb.sheets[ref["sheet"]].cells)
            if "calculation" in item:
                self.assertFalse(item["calculation"]["recalculated"])
                self.assertEqual(item["calculation"]["basis"], "saved_values")
                self.assertTrue({(x["sheet"], x["cell"]) for x in item["calculation"]["inputs"]}
                                <= {(x["sheet"], x["cell"]) for x in item["evidence"]})

    def test_bad_model_all_four_findings_shifted_and_renamed(self):
        for name, rows, columns in (("Unrelated valuation", 0, 0), ("Модель Другая", 30, 7), ("Plan O'Brien", 400, 30)):
            with self.subTest(name=name):
                wb, ref = make_model(sheet_name=name, row_shift=rows, column_shift=columns)
                before = copy.deepcopy(wb)
                result = run_financial_checks(wb)
                self.assertEqual(codes(result), self.ALL)
                self.assertEqual(wb, before)
                self.assertEqual(result["summary"]["candidate_count"], 3)
                self.assert_valid_evidence(wb, result)
                duplicate = next(x for x in result["findings"] if x["code"] == "pv_fcff_duplicate_term")
                self.assertTrue({ref("F11"), ref("H11")} <= {(x["sheet"], x["cell"]) for x in duplicate["evidence"]})

    def test_known_good_model_has_no_findings(self):
        wb, _ = make_model(bad=False, row_shift=12, column_shift=3)
        result = run_financial_checks(wb)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "limited")
        self.assertTrue(result["limitations"])

    def test_equal_values_distinct_cashflows_are_not_duplicates(self):
        wb, ref = make_model()
        get(wb, ref("H7")).formula = None
        get(wb, ref("H7")).formula_type = None
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_duplicate_term", codes(result))
        self.assertIn("gordon_double_discount", codes(result))

    def test_independent_constant_formulas_are_distinct_cashflow_sources(self):
        wb, ref = make_model()
        get(wb, ref("F7")).formula = "=7+7"
        get(wb, ref("H7")).formula = "=7+7"
        self.assertNotIn("pv_fcff_duplicate_term", codes(run_financial_checks(wb)))

    def test_excel_unary_minus_and_power_precedence(self):
        wb, ref = make_model()
        checker = _Checks(wb)
        for expression, expected in (("-1^2", 1), ("2^3^2", 64), ("2^-2", .25), ("-(1^2)", -1)):
            with self.subTest(expression=expression):
                self.assertEqual(checker.number(_Parser(expression, ref("A1")[0]).parse(), set()), expected)

    def test_component_aliases_prove_structural_duplicate_without_value_equality(self):
        wb, ref = make_model()
        sheet = wb.sheets[ref("A1")[0]]
        for addr, value, formula in (("F6", 9, None), ("F8", 5, None), ("H6", 9, "=F6"), ("H8", 5, "=F8")):
            sheet.cells[addr] = Cell(addr, value, formula, "normal" if formula else None)
        get(wb, ref("F7")).formula = "=F6+F8"
        get(wb, ref("H7")).formula = "=H6+H8"
        # Stale saved values must not be the structural equivalence criterion.
        get(wb, ref("H7")).value = 15
        result = run_financial_checks(wb)
        self.assertIn("pv_fcff_duplicate_term", codes(result))

    def test_quoted_cross_sheet_alias_is_followed(self):
        wb, ref = make_model()
        other = "Inputs O'Brien"
        wb.sheets[other] = Sheet(other, cells={"A1": Cell("A1", 14, "='Forecast O''Brien'!F7", "normal")})
        get(wb, ref("H7")).formula = "='Inputs O''Brien'!A1"
        result = run_financial_checks(wb)
        self.assertIn("pv_fcff_duplicate_term", codes(result))
        self.assert_valid_evidence(wb, result)

    def test_disabled_gordon_weight_not_reported_as_active_double_discount(self):
        wb, ref = make_model()
        get(wb, ref("C14")).value = 0
        get(wb, ref("C15")).value = 1
        self.assertNotIn("gordon_double_discount", codes(run_financial_checks(wb)))

    def test_fractional_gordon_weight_still_exposes_repeated_discount_branch(self):
        wb, ref = make_model()
        get(wb, ref("C14")).value = .4
        get(wb, ref("C15")).value = .6
        self.assertIn("gordon_double_discount", codes(run_financial_checks(wb)))

    def test_complement_weight_proof_keeps_its_source_when_moved_and_renamed(self):
        for name, rows, columns in (("Forecast O'Brien", 0, 0), ("Переименованная модель", 40, 7), ("Different plan", 300, 31)):
            with self.subTest(name=name):
                wb, ref = make_model(sheet_name=name, row_shift=rows, column_shift=columns)
                before = copy.deepcopy(wb)
                result = run_financial_checks(wb)
                finding = next(item for item in result["findings"] if item["code"] == "gordon_double_discount")
                sources = {(item["sheet"], item["cell"]) for item in finding["evidence"]}
                self.assertTrue({ref("C14"), ref("C15"), ref("B14"), ref("B16"), ref("B17")} <= sources)
                self.assertEqual(get(wb, ref("C15")).value, 0)
                self.assertAlmostEqual(finding["calculation"]["value"], (1 / 1.1 ** 1.5) ** 2)
                self.assertEqual(codes(result), self.ALL)
                self.assertEqual(wb, before)
                self.assert_valid_evidence(wb, result)

    def test_complement_weight_alias_keeps_cross_sheet_source_without_recalculating_cache(self):
        wb, ref = make_model()
        other = "Choice O'Brien"
        wb.sheets[other] = Sheet(other, cells={"J8": Cell("J8", .6)})
        get(wb, ref("C15")).formula = "='Choice O''Brien'!J8"
        get(wb, ref("C15")).formula_type = "normal"
        get(wb, ref("C15")).value = .6
        get(wb, ref("C14")).formula = "=1-C15"
        # Deliberately stale cache: detection still uses the original saved
        # weight, rather than silently recalculating 1-.6 and enabling a branch.
        get(wb, ref("C14")).value = 0
        self.assertNotIn("gordon_double_discount", codes(run_financial_checks(wb)))
        get(wb, ref("C14")).value = .4
        result = run_financial_checks(wb)
        finding = next(item for item in result["findings"] if item["code"] == "gordon_double_discount")
        sources = {(item["sheet"], item["cell"]) for item in finding["evidence"]}
        self.assertTrue({ref("C14"), ref("C15"), (other, "J8")} <= sources)
        self.assertAlmostEqual(finding["calculation"]["value"], (1 / 1.1 ** 1.5) ** 2)

    def test_downstream_division_by_discount_base_is_supported(self):
        wb, ref = make_model()
        get(wb, ref("B17")).formula = "=B16/(1+B4)^F9"
        self.assertIn("gordon_double_discount", codes(run_financial_checks(wb)))

    def test_cancelled_discount_factors_are_not_double_discounting(self):
        for formula in ("=B16*F10/F10", "=B16*F10*(1/F10)", "=B16*F10*F10^-1"):
            with self.subTest(formula=formula):
                wb, ref = make_model()
                get(wb, ref("B17")).formula = formula
                self.assertNotIn("gordon_double_discount", codes(run_financial_checks(wb)))

    def test_equal_factor_formulas_with_different_caches_do_not_invent_square(self):
        wb, ref = make_model()
        get(wb, ref("H10")).value = .5
        get(wb, ref("F10")).value = .7
        result = run_financial_checks(wb)
        self.assertNotIn("gordon_double_discount", codes(result))
        self.assertTrue(any(x["detail"] == "inconsistent_saved_discount_factor_caches" for x in result["checks"]))

    def test_ambiguous_dates_do_not_produce_historical_claim(self):
        wb, ref = make_model()
        wb.sheets[ref("A1")[0]].cells.update({"J3": Cell("J3", "Valuation date"), "K3": Cell("K3", "2023-12-31")})
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_historical_period", codes(result))
        self.assertTrue(any(x["status"] == "ambiguous" for x in result["checks"]))

    def test_isolated_year_like_number_is_not_a_period_header(self):
        wb, ref = make_model()
        get(wb, ref("E1")).value = "no year"
        get(wb, ref("F1")).value = "no year"
        self.assertNotIn("pv_fcff_historical_period", codes(run_financial_checks(wb)))

    def test_sequential_amounts_under_unknown_metric_are_not_years(self):
        wb, ref = make_model()
        sheet = wb.sheets[ref("A1")[0]]
        sheet.cells["A1"] = Cell("A1", "Custom operating cost")
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_historical_period", codes(result))

    def test_explicit_calendar_header_can_skip_years_with_complete_sources(self):
        for label, rows, columns in (("Year", 0, 0), ("Calendar years", 14, 5), ("Календарный год", 150, 28)):
            with self.subTest(label=label):
                wb, ref = make_model(sheet_name="Different calendar layout", row_shift=rows, column_shift=columns)
                sheet = wb.sheets[ref("A1")[0]]
                sheet.cells[ref("A1")[1]] = Cell(ref("A1")[1], label)
                get(wb, ref("D1")).value = 2023  # 2024 is deliberately absent.
                before = copy.deepcopy(wb)
                result = run_financial_checks(wb)
                historical = [f for f in result["findings"] if f["code"] == "pv_fcff_historical_period"]
                self.assertEqual(len(historical), 1)
                self.assertIn("2023", historical[0]["detail"])
                required = {ref(a) for a in ("A1", "D1", "E1", "F1", "A3", "B3", "B11", "D11", "D7")}
                self.assertTrue(required <= {(r["sheet"], r["cell"]) for r in historical[0]["evidence"]})
                self.assert_valid_evidence(wb, result)
                self.assertEqual(wb, before)

    def test_skipped_years_without_explicit_calendar_label_remain_unsupported(self):
        for label in (None, "Units", "Actual", "Forecast"):
            with self.subTest(label=label):
                wb, ref = make_model()
                if label:
                    wb.sheets[ref("A1")[0]].cells["A1"] = Cell("A1", label)
                get(wb, ref("D1")).value = 2023
                result = run_financial_checks(wb)
                self.assertNotIn("pv_fcff_historical_period", codes(result))
                self.assertTrue(any(c["code"] == "pv_fcff_historical_period" and c["status"] == "unsupported"
                                    for c in result["checks"]))

    def test_competing_year_headers_fail_closed_even_with_calendar_labels(self):
        wb, ref = make_model(bad=False)
        sheet = wb.sheets[ref("A1")[0]]
        get(wb, ref("B11")).formula = "=SUM(D11:F11)"
        sheet.cells.update({"A1": Cell("A1", "Year"), "A2": Cell("A2", "Year"),
                            "D2": Cell("D2", 2026), "E2": Cell("E2", 2028), "F2": Cell("F2", 2029)})
        result = run_financial_checks(wb)
        self.assertEqual(result["findings"], [])
        self.assertTrue(any(c["detail"] == "ambiguous_period_headers" for c in result["checks"]))

    def test_nonchronological_year_header_is_not_resolved_by_one_matching_neighbor(self):
        for years in ((2024, 2025, 2023), (2024, 2025, 2025)):
            with self.subTest(years=years):
                wb, ref = make_model(bad=False)
                sheet = wb.sheets[ref("A1")[0]]
                sheet.cells["A1"] = Cell("A1", "Year")
                get(wb, ref("B11")).formula = "=SUM(D11:F11)"
                for address, value in zip(("D1", "E1", "F1"), years):
                    get(wb, ref(address)).value = value
                result = run_financial_checks(wb)
                self.assertEqual(result["findings"], [])
                self.assertTrue(any(c["detail"] == "ambiguous_period_headers" for c in result["checks"]))

    def test_unit_bearing_year_like_amounts_are_not_calendar_headers(self):
        for unit in ("RUB M", "%", "months"):
            with self.subTest(unit=unit):
                wb, ref = make_model(bad=False)
                sheet = wb.sheets[ref("A1")[0]]
                sheet.cells.update({"A1": Cell("A1", "Year"), "B1": Cell("B1", unit)})
                get(wb, ref("B11")).formula = "=SUM(D11:F11)"
                self.assertEqual(run_financial_checks(wb)["findings"], [])

    def test_dates_and_period_counters_are_not_inferred_as_calendar_years(self):
        for label, values in (("Period", (0, 1, 2)), ("Period", (2023, 2024, 2025)),
                              ("Date", (2023, 2024, 2025)), ("Date", (45292, 45658, 46023)),
                              ("Date", ("2023-12-31", "2024-12-31", "2025-12-31")),
                              ("Year", (0, 1, 2))):
            with self.subTest(label=label, values=values):
                wb, ref = make_model(bad=False)
                sheet = wb.sheets[ref("A1")[0]]
                sheet.cells["A1"] = Cell("A1", label)
                get(wb, ref("B11")).formula = "=SUM(D11:F11)"
                for address, value in zip(("D1", "E1", "F1"), values):
                    get(wb, ref(address)).value = value
                result = run_financial_checks(wb)
                self.assertEqual(result["findings"], [])
                self.assertTrue(any(c["code"] == "pv_fcff_historical_period" and c["status"] == "unsupported"
                                    for c in result["checks"]))

    def test_later_metric_label_is_a_boundary_not_an_fcff_alias(self):
        wb, ref = make_model()
        wb.sheets[ref("A1")[0]].cells["G7"] = Cell("G7", "Custom operating cost")
        # H7 aliases F7: following a real alias establishes source identity,
        # so make the later independent formula a distinct labelled input.
        get(wb, ref("H7")).formula = "=F7+0"
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_duplicate_term", codes(result))

    def test_missing_or_error_cache_on_alias_remains_unsupported(self):
        for value in (None, True, "#NUM!"):
            with self.subTest(value=value):
                wb, ref = make_model()
                get(wb, ref("H7")).value = value
                result = run_financial_checks(wb)
                self.assertNotIn("pv_fcff_duplicate_term", codes(result))
                self.assertGreater(result["summary"]["unsupported_count"], 0)

    def test_absent_valuation_date_is_explicitly_unsupported(self):
        wb, ref = make_model()
        get(wb, ref("B3")).value = 45838  # no style/date-system contract: do not guess
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_historical_period", codes(result))
        self.assertTrue(any(x["code"] == "pv_fcff_historical_period" and x["status"] == "unsupported" for x in result["checks"]))

    def test_unsupported_and_huge_sum_do_not_execute_or_expand(self):
        for formula in ('=SUM(OFFSET(D11,0,0,1,5))', '=SUM(A1:XFD1048576)', '=SUM(', '=SUM(D11:H11)+__import__("os")'):
            with self.subTest(formula=formula):
                wb, ref = make_model()
                get(wb, ref("B11")).formula = formula
                result = run_financial_checks(wb)
                self.assertNotIn("pv_fcff_duplicate_term", codes(result))
                self.assertTrue(any(x["code"] == "pv_fcff_sum" and x["status"] == "unsupported" for x in result["checks"]))
                self.assert_valid_evidence(wb, result)

    def test_alias_cycle_is_bounded(self):
        wb, ref = make_model()
        get(wb, ref("H7")).formula = "=H8"
        wb.sheets[ref("A1")[0]].cells["H8"] = Cell("H8", 14, "=H7", "normal")
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_duplicate_term", codes(result))
        self.assertTrue(any("cycle" in x["detail"] for x in result["checks"]))

    def test_nonfinite_saved_values_never_escape_json(self):
        for address, value in (("H7", math.nan), ("B4", math.inf), ("D21", -math.inf)):
            with self.subTest(address=address):
                wb, ref = make_model()
                get(wb, ref(address)).value = value
                result = run_financial_checks(wb)
                self.assert_valid_evidence(wb, result)
                self.assertGreater(result["summary"]["unsupported_count"], 0)

    def test_overflow_in_candidate_calculation_is_explicit(self):
        wb, ref = make_model()
        get(wb, ref("F11")).value = 1e308
        get(wb, ref("H11")).value = 1e308
        result = run_financial_checks(wb)
        self.assertNotIn("pv_fcff_duplicate_term", codes(result))
        self.assertTrue(any(x["detail"] == "nonfinite_calculation_output" for x in result["checks"]))
        json.dumps(result, allow_nan=False)

    def test_datatable_unsupported_orientation_is_not_guessed(self):
        wb, ref = make_model()
        get(wb, ref("D21")).formula_attributes["dt2D"] = "0"
        result = run_financial_checks(wb)
        self.assertNotIn("datatable_basepoint_cache_mismatch", codes(result))
        self.assertTrue(any(x["detail"] == "unsupported_datatable_orientation" for x in result["checks"]))

    def test_datatable_multiple_near_axes_are_ambiguous(self):
        wb, ref = make_model()
        get(wb, ref("E20")).value = .03
        result = run_financial_checks(wb)
        self.assertNotIn("datatable_basepoint_cache_mismatch", codes(result))
        self.assertTrue(any(x["detail"] == "ambiguous_basepoint_axes" for x in result["checks"]))

    def test_datatable_error_cache_has_no_guessed_financial_cause(self):
        wb, ref = make_model()
        get(wb, ref("D21")).value = "#NUM!"
        result = run_financial_checks(wb)
        self.assertNotIn("datatable_basepoint_cache_mismatch", codes(result))
        self.assertTrue(any(x["code"] == "datatable_basepoint_cache_mismatch" and x["status"] == "unsupported" for x in result["checks"]))

    def test_empty_workbook_is_limited_not_clean_verdict(self):
        wb = Workbook(Path("empty.xlsx"), {}, [], [], {}, [])
        result = run_financial_checks(wb)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "limited")
        self.assertTrue(all(x["status"] == "not_applicable" for x in result["checks"]))

    def test_reviewed_example_generic_detection_and_source_unchanged(self):
        path = Path(__file__).resolve().parents[1] / "examples/Fin_Model_for_test.xlsx"
        if not path.is_file():
            self.skipTest("Local evaluation workbook is not distributed with the code")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        wb = load_workbook(path)
        result = run_financial_checks(wb)
        self.assertEqual(codes(result), self.ALL)
        self.assert_valid_evidence(wb, result)
        required = {
            "pv_fcff_duplicate_term": {("DCF", "E39"), ("DCF", "N39"), ("DCF", "P39")},
            "pv_fcff_historical_period": {("DCF", "H9"), ("DCF", "H39"), ("DCF", "E33")},
            "gordon_double_discount": {("DCF", "E45"), ("DCF", "E44"), ("DCF", "E41"), ("DCF", "N37"), ("DCF", "F45"), ("DCF", "F46")},
            "datatable_basepoint_cache_mismatch": {("DCF", "N73"), ("DCF", "E55"), ("DCF", "N70"), ("DCF", "I73")},
        }
        for finding in result["findings"]:
            self.assertTrue(required[finding["code"]] <= {(x["sheet"], x["cell"]) for x in finding["evidence"]})
        self.assertNotIn("144748", json.dumps(result))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
