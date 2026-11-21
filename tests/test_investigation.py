import copy
import json
from pathlib import Path
import re
import unittest

from finmodel_audit.investigation import (build_investigation, inspect_question,
    investigation_coverage, investigation_observations)
from finmodel_audit.workbook import Workbook, Sheet, Cell, _address_parts, _column_letters


def model(bad=False, shift=0, columns=0, name="Unrelated project"):
    cells = {}
    def moved(address):
        col, row = _address_parts(address.replace("$", ""))
        return _column_letters(col + columns) + str(row + shift)
    def add(address, value, formula=None):
        target = moved(address)
        formula = re.sub(r"\$?[A-Z]{1,3}\$?[1-9][0-9]*", lambda m: moved(m[0]), formula) if formula else None
        cells[target] = Cell(target, value, formula, "normal" if formula else None)
    for row, label in ((1,"Revenue"),(2,"Cost"),(3,"FCFF"),(5,"Discount rate"),(6,"Discount Period"),(7,"DCF"),
                       (10,"Period start"),(11,"Period end"),(12,"Period number"),(15,"NPV"),
                       (20,"WACC"),(21,"Equity share"),(22,"Debt share"),(24,"Cost of equity"),(25,"Cost of debt"),
                       (26,"Risk-free rate"),(27,"Equity risk premium"),(28,"Beta"),(29,"Specific risk")):
        add(f"A{row}",label)
    for col, value, period in (("E",100,1),("F",110,2)):
        add(col+"1",value); add(col+"2",value-50); add(col+"3",50,f"={col}1-{col}2")
        add(col+"5",.1,"=C20");add(col+"6",period);add(col+"7",50/(1.1**period),f"={col}3/(1+{col}5)^{col}6")
        add(col+"12",period)
    add("C15",100,"=SUM(E7:F7)")
    add("E10",45000);add("F10",45000 if bad else 45365)
    add("E11",44999 if bad else 45364,"=E10+D50-1" if bad else None)
    add("F11",44999 if bad else 45729,"=E11" if bad else None)
    add("C20",.1,"=C21*C24+C22*C25")
    add("C21",.8);add("C22",.2);add("C24",.12,"=C26+C27*C28+C29")
    add("C25",.1);add("C26",0 if bad else .04,"=D50" if bad else None)
    add("C27",.05);add("C28",1.1);add("C29",.8 if bad else .01)
    wb=Workbook(Path("synthetic.xlsx"),{name:Sheet(name,cells=cells)},[],[],{},[])
    return wb,lambda a:(name,moved(a))


class InvestigationTests(unittest.TestCase):
    def inspected(self, wb):
        plan=build_investigation(wb)
        return plan,[inspect_question(wb,q["id"],plan=plan) for q in plan["questions"]]

    def test_clean_renamed_shifted_models_are_questions_not_false_financial_findings(self):
        for name,rows,cols in (("Unrelated project",0,0),("Проект O'Brien",40,8)):
            wb,ref=model(name=name,shift=rows,columns=cols)
            original=copy.deepcopy(wb)
            plan,results=self.inspected(wb)
            self.assertEqual(len(plan["questions"]),4)
            self.assertEqual({q["category"] for q in plan["questions"]},{"discount_rate_components","cashflow_drivers","operating_assumptions","calendar"})
            self.assertEqual(wb,original)
            self.assertFalse(any(r["investigation_question"]["issues"] for r in results))
            self.assertTrue(all(r["recalculated"] is False for r in results))
            self.assertEqual(investigation_coverage(plan,[])["not_examined"],4)
            self.assertEqual(investigation_coverage(plan,results)["sources_examined"],4)
            self.assertEqual(investigation_coverage(plan,results)["financial_verification"],"not_assessed")
            json.dumps(results,allow_nan=False)

    def test_blank_rate_source_premium_and_calendar_path_survive_layout_changes(self):
        for shift,cols in ((0,0),(90,5)):
            wb,ref=model(bad=True,shift=shift,columns=cols,name="Inputs renamed")
            plan,results=self.inspected(wb)
            rate=next(r for r in results if r["investigation_question"]["category"]=="discount_rate_components")
            self.assertTrue(any((c["source"]["sheet"],c["source"]["cell"])==ref("C29") and c["value"]==.8 for c in rate["investigation_question"]["components"]))
            absent=next(c for c in rate["cell_evidence"] if (c["sheet"],c["cell"])==ref("D50"))
            self.assertFalse(absent["present"])
            self.assertIsNone(absent["value"])
            self.assertIn("blank_dependency",{i["code"] for i in rate["investigation_question"]["issues"]})
            calendar=next(r["investigation_question"] for r in results if r["investigation_question"]["category"]=="calendar")
            self.assertTrue({"blank_dependency","repeated_calendar_values","calendar_end_before_start"}<={i["code"] for i in calendar["issues"]})
            detail=investigation_observations([rate])[0]["detail"]
            self.assertIn("Specific risk: сохранённое значение 0.8",detail)
            self.assertIn(ref("D50")[1],detail)
            self.assertNotIn("80%",detail)

    def test_plan_does_not_authorize_inspection_and_partial_delivery_does_not_pass(self):
        wb,_=model(bad=True)
        plan,results=self.inspected(wb)
        self.assertNotIn("components",plan["questions"][0])
        self.assertFalse(investigation_coverage(plan,[plan])["ready_for_synthesis"])
        cut=copy.deepcopy(results)
        cut[0]["cell_evidence"].pop()
        self.assertEqual(investigation_coverage(plan,cut)["not_examined"],1)
        self.assertFalse(investigation_coverage(plan,results,delivered_cells=set())["ready_for_synthesis"])
        with self.assertRaises(ValueError):inspect_question(wb,"forged-id",plan=plan)

    def test_each_graph_link_and_issue_has_full_cell_evidence(self):
        wb,_=model(bad=True)
        _,results=self.inspected(wb)
        for result in results:
            q=result["investigation_question"]
            sources={(c["sheet"],c["cell"]) for c in result["cell_evidence"]}
            self.assertEqual(sources,{(r["sheet"],r["cell"]) for r in q["evidence"]})
            for item in q["issues"]:
                self.assertTrue({(r["sheet"],r["cell"]) for r in item["evidence"]}<=sources)
            for a,b in q["dependency_links"]:
                self.assertLess(a,len(q["evidence"]));self.assertLess(b,len(q["evidence"]))

    def test_explicit_calendar_step_keeps_raw_time_unit_and_never_infers_a_year(self):
        for label,unit,shift,columns in (("Шаг прогнозирования","мес",0,0),
                                        ("Period length months","months",60,5),
                                        ("Длительность периода","дней",17,2)):
            with self.subTest(label=label,unit=unit):
                wb,ref=model(shift=shift,columns=columns)
                for address,value in (("A13",label),("B13",unit),("C13",12)):
                    sheet,cell=ref(address);wb.sheets[sheet].cells[cell]=Cell(cell,value)
                plan,results=self.inspected(wb)
                calendar=next(r for r in results if r["investigation_question"]["category"]=="calendar")
                sources={(c["sheet"],c["cell"]):c for c in calendar["cell_evidence"]}
                self.assertEqual(sources[ref("C13")]["value"],12)
                self.assertEqual(sources[ref("B13")]["value"],unit)
                self.assertEqual(sources[ref("A13")]["value"],label)
                self.assertFalse(any("year" in c or "period_type" in c for c in calendar["cell_evidence"]))
                self.assertFalse(calendar["investigation_question"]["issues"])
                # An unknown text block still prevents assigning the earlier label.
                wb.sheets[ref("B13")[0]].cells[ref("B13")[1]].value="Unrelated metric"
                _,changed=self.inspected(wb)
                calendar=next(r for r in changed if r["investigation_question"]["category"]=="calendar")
                self.assertNotIn(ref("C13"),{(c["sheet"],c["cell"]) for c in calendar["cell_evidence"]})

    def test_unrecognized_output_and_shadowing_label_are_explicit(self):
        wb,ref=model()
        wb.sheets[ref("C15")[0]].cells[ref("B15")[1]]=Cell(ref("B15")[1],"Unrelated metric")
        plan=build_investigation(wb)
        self.assertEqual(plan["status"],"unsupported")
        self.assertEqual(plan["questions"],[])
        self.assertFalse(investigation_coverage(plan,[])["ready_for_synthesis"])

    def test_limits_nonfinite_and_long_text_do_not_silently_validate(self):
        wb,ref=model()
        wb.sheets[ref("C29")[0]].cells[ref("C29")[1]].value=float("nan")
        plan,results=self.inspected(wb)
        json.dumps(results,allow_nan=False)
        self.assertTrue(any(i["code"]=="unsupported_saved_value" for r in results for i in r["investigation_question"]["issues"]))
        limited=build_investigation(wb,max_questions=1,max_nodes=5,max_depth=1)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["questions"]),1)
        for bad in (True,0,10000):
            with self.assertRaises(ValueError):build_investigation(wb,max_nodes=bad)
        wb,ref=model()
        wb.sheets[ref("C29")[0]].cells[ref("C29")[1]].formula="="+"1+"*2100+"1"
        plan=build_investigation(wb)
        q=next(q for q in plan["questions"] if q["category"]=="discount_rate_components")
        result=inspect_question(wb,q["id"],plan=plan)
        self.assertEqual(result["investigation_question"]["status"],"unsupported")
        self.assertEqual(result["cell_evidence"],[])
        self.assertNotIn("dependency_paths",result["investigation_question"])

    def test_packed_evidence_coverage_counts_absence_but_never_plan_metadata(self):
        from finmodel_audit.pipeline import _pack_tool_result
        wb,_=model(bad=True)
        plan,results=self.inspected(wb)
        packed=[_pack_tool_result(r,14000)[0] for r in results]
        self.assertTrue(all(p["metadata"].get("investigation_question") for p in packed))
        self.assertTrue(investigation_coverage(plan,packed)["ready_for_synthesis"])
        self.assertEqual(len(investigation_observations(packed)),4)


if __name__=="__main__":unittest.main()
