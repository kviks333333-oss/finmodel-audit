import copy
import json
import math
from pathlib import Path
import re
import unittest

from finmodel_audit.financial import run_financial_checks
from finmodel_audit.valuation_sensitivity import build_valuation_sensitivity
from finmodel_audit.workbook import Workbook, Sheet, Cell, _address_parts, _column_letters


def project(*, shift=0, columns=0, name="Unrelated O'Brien", terminal=False):
    cells = {}
    def move(value):
        def replace(match):
            col, row = _address_parts(match.group())
            return f"{_column_letters(col + columns)}{row + shift}"
        return re.sub(r"\b[A-Z]+[1-9][0-9]*\b", replace, value)
    def add(address, value, formula=None):
        a = move(address)
        cells[a] = Cell(a, value, move(formula) if formula else None, "normal" if formula else None)
    add("A6", "Discount rate"); add("B6", "%"); add("C6", .1)
    add("A7", "LTGR"); add("B7", "%"); add("C7", .03)
    add("A8", "Period")
    add("A10", "FCFF"); add("B10", "тыс. руб.")
    add("A12", "Discount Factor"); add("A13", "DCF"); add("B13", "тыс. руб.")
    for col, period, flow in (("C", 0, -100), ("D", 1, 70), ("E", 2, 80)):
        add(col+"8", period); add(col+"10", flow)
        add(col+"12", 1/1.1**period, f"=1/(1+C6)^{col}8")
        add(col+"13", flow/1.1**period, f"={col}10*{col}12")
    baseline = -100 + 70/1.1 + 80/1.1**2
    add("A20", "NPV"); add("B20", "тыс. руб.")
    if terminal:
        tv = 80*1.03/.07
        add("A24", "Gordon"); add("C24", tv, "=E10*(1+C7)/(C6-C7)")
        add("C25", tv/1.1**2, "=C24*E12")
        add("C20", baseline+tv/1.1**2, "=SUM(C13:E13)+C25")
    else:
        add("C20", baseline, "=SUM(C13:E13)")
    add("A21", "IRR"); add("B21", "%"); add("C21", .2, "=IRR(C13:E13)")
    wb = Workbook(Path("synthetic.xlsx"), {name: Sheet(name, cells=cells)}, [], [], {}, [])
    return wb, lambda address: (name, move(address)), add


def cell(wb, node):
    return wb.sheets[node[0]].cells[node[1]]


class SensitivityTests(unittest.TestCase):
    def test_shifted_renamed_project_rate_only_and_read_only(self):
        for shift, columns, name in ((0, 0, "x"), (61, 8, "Другая модель")):
            wb, ref, _ = project(shift=shift, columns=columns, name=name)
            before = copy.deepcopy(wb)
            report = build_valuation_sensitivity(wb)
            self.assertEqual(wb, before)
            self.assertEqual(len(report["scenarios"]), 2)
            json.dumps(report, allow_nan=False)
            for s in report["scenarios"]:
                rate = s["changed_parameter"]["effective_value"]
                self.assertAlmostEqual(s["result"], -100 + 70/(1+rate) + 80/(1+rate)**2)
                self.assertEqual(s["raw_unit"], "тыс. руб.")
                self.assertEqual(s["target"], {"sheet": name, "cell": ref("C20")[1]})
                self.assertEqual(s["parameter"], "discount_rate")
                self.assertFalse(s["recalculated_workbook"])

    def test_gordon_four_scenarios_keep_original_timing(self):
        wb, ref, _ = project(terminal=True)
        report = build_valuation_sensitivity(wb)
        self.assertEqual(len(report["scenarios"]), 4)
        for s in report["scenarios"]:
            p = {x["role"]:x["value"] for x in s["effective_parameters"]}
            r,g = p["discount_rate"],p["terminal_growth"]
            expected = -100+70/(1+r)+80/(1+r)**2 + 80*(1+g)/(r-g)/(1+r)**2
            self.assertAlmostEqual(s["result"], expected)
            self.assertTrue(s["formula_path"])

    def test_historical_duplicate_and_squared_discount_are_not_corrected(self):
        wb, ref, add = project(terminal=True)
        add("C24", (80/1.1**2)*1.03/.07, "=E13*(1+C7)/(C6-C7)")
        add("C25", cell(wb, ref("C24")).value/1.1**2, "=C24*E12")
        add("C20", -100+70/1.1+160/1.1**2+cell(wb,ref("C25")).value, "=SUM(C13:E13)+E13+C25")
        scenarios = build_valuation_sensitivity(wb)["scenarios"]
        self.assertEqual(len(scenarios), 4)
        for s in scenarios:
            p={x["role"]:x["value"] for x in s["effective_parameters"]};r,g=p["discount_rate"],p["terminal_growth"]
            self.assertAlmostEqual(s["result"], -100+70/(1+r)+160/(1+r)**2+80*(1+g)/(r-g)/(1+r)**4)

    def test_midyear_periods_preserved(self):
        wb, ref, add = project()
        for col,t in (("D", .5), ("E", 1.5)):
            add(col+"8", t);add(col+"12",1/1.1**t,f"=1/(1+C6)^{col}8")
            add(col+"13",cell(wb,ref(col+"10")).value/1.1**t,f"={col}10*{col}12")
        add("C20",-100+70/1.1**.5+80/1.1**1.5,"=SUM(C13:E13)")
        for s in build_valuation_sensitivity(wb)["scenarios"]:
            r=s["changed_parameter"]["effective_value"]
            self.assertAlmostEqual(s["result"],-100+70/(1+r)**.5+80/(1+r)**1.5)
            self.assertEqual(sorted(x["value"] for x in s["periods"]),[0,.5,1.5])

    def test_unsupported_affected_function_rejected(self):
        wb, ref, _ = project()
        cell(wb,ref("C20")).formula="=IF(C6>0,SUM(C13:E13),0)"
        report=build_valuation_sensitivity(wb)
        self.assertEqual(report["scenarios"],[])
        self.assertEqual(report["checks"][0]["reason"],"unsupported_affected_formula")

    def test_upstream_array_in_explicit_frozen_fcff_is_not_recalculated(self):
        wb,ref,_=project()
        cell(wb,ref("D10")).formula="=UNKNOWN(C6)"
        cell(wb,ref("D10")).formula_type="array"
        report=build_valuation_sensitivity(wb)
        self.assertEqual(len(report["scenarios"]),2)
        self.assertIn({"sheet":ref("D10")[0],"cell":ref("D10")[1]},report["scenarios"][0]["frozen_cashflows"])

    def test_bad_cache_nonfinite_ambiguous_unit_refused(self):
        for mode in ("cache", "nonfinite", "unit"):
            wb,ref,_=project()
            if mode=="cache":cell(wb,ref("C20")).value+=100
            elif mode=="nonfinite":cell(wb,ref("D10")).value=float("nan")
            else:cell(wb,ref("B20")).value="unspecified"
            report=build_valuation_sensitivity(wb)
            self.assertEqual(report["scenarios"],[],mode)
            json.dumps(report,allow_nan=False)

    def test_multiple_rates_refused(self):
        wb,ref,add=project();add("D6",.1)
        cell(wb,ref("E12")).formula="=1/(1+D6)^E8"
        result=build_valuation_sensitivity(wb)
        self.assertEqual(result["scenarios"],[])
        self.assertEqual(result["checks"][0]["reason"],"ambiguous_or_missing_discount_rate")

    def test_affected_cycle_or_overflow_refused(self):
        for formula in ("=C6+D12", "=C6^9999"):
            wb,ref,_=project();cell(wb,ref("D12")).formula=formula
            result=build_valuation_sensitivity(wb)
            self.assertEqual(result["scenarios"],[])
            self.assertTrue(any(x["status"]=="unsupported" for x in result["checks"]))

    def test_alias_output_is_not_duplicate_scenarios(self):
        wb,ref,add=project();add("A22","NPV");add("B22","тыс. руб.");add("C22",cell(wb,ref("C20")).value,"=C20")
        report=build_valuation_sensitivity(wb)
        self.assertEqual(len(report["scenarios"]),2)
        self.assertTrue(any(x["reason"]=="alias_of_recognized_output" for x in report["checks"]))

    def test_growth_crossing_rate_is_explicitly_refused(self):
        wb,ref,add=project(terminal=True);add("C7",.099)
        tv=80*1.099/.001;add("C24",tv,"=E10*(1+C7)/(C6-C7)");add("C25",tv/1.1**2,"=C24*E12")
        add("C20",-100+70/1.1+80/1.1**2+tv/1.1**2,"=SUM(C13:E13)+C25")
        report=build_valuation_sensitivity(wb)
        self.assertEqual(len(report["scenarios"]),2)
        self.assertEqual(sum(x.get("reason")=="scenario_rate_not_above_growth" for x in report["checks"]),2)

    def test_unknown_growth_label_cannot_bypass_gordon_domain_guard(self):
        wb,ref,_=project(terminal=True)
        cell(wb,ref("A7")).value="Growth assumption"
        report=build_valuation_sensitivity(wb)
        self.assertEqual(report["scenarios"],[])
        self.assertEqual(report["checks"][0]["reason"],"terminal_growth_label_not_established")

    def test_literal_gordon_growth_refuses_unbound_parameter(self):
        wb,ref,_=project(terminal=True)
        cell(wb,ref("C24")).formula="=E10*(1+0.03)/(C6-0.03)"
        report=build_valuation_sensitivity(wb)
        self.assertEqual(report["scenarios"],[])
        self.assertEqual(report["checks"][0]["reason"],"terminal_growth_not_explicit_cell")


class AdditionalFinancialTests(unittest.TestCase):
    def test_irr_discounted_renamed_shifted_and_raw_negative(self):
        for shift,columns in ((0,0),(44,6)):
            wb,ref,_=project(shift=shift,columns=columns)
            result=run_financial_checks(wb)
            self.assertIn("irr_on_discounted_cashflows",{f["code"] for f in result["findings"]})
            cell(wb,ref("C21")).formula=f"=IRR({ref('C10')[1]}:{ref('E10')[1]})"
            self.assertNotIn("irr_on_discounted_cashflows",{f["code"] for f in run_financial_checks(wb)["findings"]})

    def test_irr_initial_blank_rate_zero_period(self):
        wb,ref,add=project();cell(wb,ref("C12")).formula="=1/(1+C5)^C8"
        self.assertIn("irr_on_discounted_cashflows",{x["code"] for x in run_financial_checks(wb)["findings"]})

    def test_irr_invalid_inputs_are_explicit_unsupported(self):
        for formula in ("=IRR(C13:E14)","=IRR(C13:XFD13)","=IRR(C13:E13,C6)"):
            wb,ref,_=project();cell(wb,ref("C21")).formula=formula
            report=run_financial_checks(wb)
            self.assertNotIn("irr_on_discounted_cashflows",{x["code"] for x in report["findings"]})
            self.assertTrue(any(x["code"]=="irr_on_discounted_cashflows" and x["status"]=="unsupported" for x in report["checks"]))

    def test_strict_profit_overlap_and_independent_equal_values_negative(self):
        wb,ref,add=project()
        for address,value,formula in (("A30","Net Profit",None),("A31","NOPAT",None),("A32","FCFF",None),("B32","тыс.руб.",None),
              ("D26",100,None),("D27",-20,None),("D28",-5,None),("D30",75,"=SUM(D26:D28)"),("D31",80,"=D26+D27"),("D32",155,"=SUM(D30:D31)")):
            add(address,value,formula)
        code="fcff_net_profit_nopat_overlap"
        self.assertIn(code,{x["code"] for x in run_financial_checks(wb)["findings"]})
        add("E26",100);add("E27",-20);cell(wb,ref("D31")).formula="=E26+E27"
        self.assertNotIn(code,{x["code"] for x in run_financial_checks(wb)["findings"]})
        cell(wb,ref("D31")).formula="=D26+D27";cell(wb,ref("A30")).value="Adjusted margin"
        self.assertNotIn(code,{x["code"] for x in run_financial_checks(wb)["findings"]})


if __name__ == "__main__":
    unittest.main()
