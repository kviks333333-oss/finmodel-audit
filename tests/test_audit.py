from pathlib import Path
import unittest

from finmodel_audit.audit import audit_workbook
from finmodel_audit.report import render_html
from finmodel_audit.workbook import Cell, Sheet, Workbook


class AuditTests(unittest.TestCase):
    def book(self, cells, **kwargs):
        return Workbook(path=Path("model.xlsx"), sheets={"DCF": Sheet("DCF", cells=cells)},
                        defined_names=[], external_links=[], calculation=kwargs.get("calculation", {}), warnings=[])

    def test_saved_errors_grouped_without_financial_severity(self):
        book = self.book({
            "A1": Cell("A1", "#DIV/0!", "=1/0", "normal", "e"),
            "A2": Cell("A2", "#DIV/0!", "=1/0", "normal", "e"),
        })
        report = audit_workbook(book)
        errors = [f for f in report["findings"] if f["code"] == "cached_excel_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["cells"], ["A1", "A2"])
        self.assertEqual(errors[0]["financial_materiality"], "not_assessed")
        self.assertFalse(report["recalculated"])

    def test_literal_text_not_mistaken_for_external_or_broken_reference(self):
        book = self.book({
            "A1": Cell("A1", "[2]#REF!", '="[2]#REF!"', "normal"),
            "A2": Cell("A2", 10, "='[2]Source'!A1", "normal"),
        })
        findings = audit_workbook(book)["findings"]
        self.assertEqual([(f["code"], f["cells"]) for f in findings], [("external_formula", ["A2"])])

    def test_iteration_is_information_not_a_confirmed_error(self):
        findings = audit_workbook(self.book({}, calculation={"iterate": "1"}))["findings"]
        self.assertEqual(findings[0]["code"], "iterative_calculation")
        self.assertEqual(findings[0]["level"], "info")

    def test_table_columns_and_quoted_sheet_names_are_not_reference_errors(self):
        book = self.book({
            "A1": Cell("A1", 5, "=SUM(Table1[2024])", "normal"),
            "A2": Cell("A2", 5, "='Cost #REF!'!B1", "normal"),
            "A3": Cell("A3", 5, "=Table1[#REF!]", "normal"),
            "A4": Cell("A4", 5, "=[1]Source!B2", "normal"),
            "A5": Cell("A5", 5, "='[2]Source'!B2", "normal"),
            "A6": Cell("A6", 5, "=#REF!B2", "normal"),
        })
        findings = audit_workbook(book)["findings"]
        self.assertEqual([(f["code"], f["cells"]) for f in findings], [
            ("external_formula", ["A4"]), ("external_formula", ["A5"]),
            ("broken_reference", ["A6"]),
        ])

    def test_report_escapes_workbook_content(self):
        book = self.book({"A1": Cell("A1", "#VALUE!", '="<script>alert(1)</script>"', "normal", "e")})
        html = render_html(audit_workbook(book))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
