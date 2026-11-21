from pathlib import Path
import unittest

from finmodel_audit.references import DependencyGraph, parse_range
from finmodel_audit.workbook import Cell, Sheet, Workbook


def workbook(sheets, names=None):
    return Workbook(Path("fixture.xlsx"), {name: Sheet(name, cells={cell.address: cell for cell in cells}) for name, cells in sheets.items()}, names or [], [], {}, [])


def references(result):
    return {(node["sheet"], node["cell"]) for node in result["nodes"]}


class DependencyTests(unittest.TestCase):
    def test_quoted_sheet_mixed_references_literals_and_missing_cell(self):
        wb = workbook({"Main": [Cell("A1", 10, '=SUM(B1:B3)+\'O\'\'Brien\'!$A$1+D99+LEN("X9")', "normal"), Cell("B1", 1), Cell("B3", 2)], "O'Brien": [Cell("A1", 7)]})
        graph = DependencyGraph(wb)
        result = graph.precedents("Main", "A1")
        self.assertEqual(references(result), {("Main", "A1"), ("Main", "B1"), ("Main", "B3"), ("Main", "D99"), ("O'Brien", "A1")})
        self.assertTrue(result["completeness"]["complete"])
        absent = next(node for node in result["nodes"] if node["cell"] == "D99")
        self.assertFalse(absent["present"])
        self.assertIsNone(absent["value"])

    def test_entire_columns_are_sparse_but_blank_dependents_are_queryable(self):
        wb = workbook({"Main": [Cell("A1", 1), Cell("A1000000", 2), Cell("B1", 3, "=SUM(A:A)", "normal")]})
        graph = DependencyGraph(wb)
        self.assertEqual(graph.summary()["edge_count"], 2)
        self.assertEqual(graph.summary()["node_count"], 3)
        self.assertEqual(references(graph.dependents("Main", "A500000")), {("Main", "A500000"), ("Main", "B1")})

    def test_local_names_override_global_names_and_names_can_expand(self):
        names = [{"name": "Tax", "value": "Inputs!$A$1"},
                 {"name": "Tax", "value": "$B$1", "localSheetId": "0"},
                 {"name": "DoubleTax", "value": "Tax*2", "localSheetId": "0"}]
        wb = workbook({"Main": [Cell("B1", 0.2), Cell("C1", 0.4, "=DoubleTax", "normal")], "Inputs": [Cell("A1", 0.3), Cell("B1", 0.3, "=Tax", "normal")]}, names)
        graph = DependencyGraph(wb)
        self.assertEqual(references(graph.precedents("Main", "C1")), {("Main", "C1"), ("Main", "B1")})
        self.assertEqual(references(graph.precedents("Inputs", "B1")), {("Inputs", "A1"), ("Inputs", "B1")})
        self.assertTrue(graph.summary()["completeness"]["complete"])

    def test_name_cycle_and_relative_name_report_incompleteness(self):
        names = [{"name": "First", "value": "Second"}, {"name": "Second", "value": "First"},
                 {"name": "Relative", "value": "Main!A1"}, {"name": "NoSheet", "value": "$A$1"}]
        graph = DependencyGraph(workbook({"Main": [Cell("B1", 0, "=First+Relative+NoSheet", "normal")]}, names))
        codes = {issue["code"] for issue in graph.precedents("Main", "B1")["issues"]}
        self.assertEqual(codes, {"name_cycle", "relative_defined_name", "unqualified_global_name"})

    def test_dynamic_external_structured_and_3d_are_not_silently_complete(self):
        formula = '=INDIRECT("A1")+OFFSET(B1,1,0)+SUM(Table1[Value])+\'[2]Other\'!C1+Sheet1:Sheet3!D1'
        graph = DependencyGraph(workbook({"Main": [Cell("E1", 1, formula, "normal"), Cell("B1", 2), Cell("A1", 100)]}))
        result = graph.precedents("Main", "E1")
        self.assertEqual(references(result), {("Main", "E1"), ("Main", "B1")})
        self.assertEqual({issue["code"] for issue in result["issues"]}, {"dynamic_reference", "structured_reference", "external_reference", "three_dimensional_reference"})
        self.assertFalse(result["completeness"]["complete"])

    def test_cycle_components_are_detected_without_recalculation_claims(self):
        graph = DependencyGraph(workbook({"Main": [Cell("A1", 1, "=B1", "normal"), Cell("B1", 1, "=A1", "normal"), Cell("C1", 2, "=C1+1", "normal")]}))
        self.assertEqual(graph.summary()["cycle_group_count"], 2)
        result = graph.precedents("Main", "A1", depth=4)
        self.assertEqual(len(result["cycles"]), 1)
        self.assertFalse(result["truncated"])

    def test_depth_and_nodes_limits_are_explicit(self):
        graph = DependencyGraph(workbook({"Main": [Cell("A1", 1, "=B1", "normal"), Cell("B1", 1, "=C1", "normal"), Cell("C1", 1)]}))
        self.assertIn("depth_limit", graph.precedents("Main", "A1", depth=1)["completeness"]["reasons"])
        result = graph.precedents("Main", "A1", depth=4, max_nodes=1)
        self.assertEqual(len(result["nodes"]), 1)
        self.assertIn("node_limit", result["completeness"]["reasons"])
        with self.assertRaises(ValueError):
            graph.precedents("Main", "A1", depth=True)

    def test_data_table_members_are_incomplete_and_error_text_is_data(self):
        anchor = Cell("A1", "#NUM!", formula_type="dataTable", data_type="e", formula_attributes={"ref": "A1:B2"})
        graph = DependencyGraph(workbook({"Main": [anchor, Cell("B1", "#NUM!", formula_type="normal", data_type="e"), Cell("D1", "OFFSET(A1)", '="OFFSET(A1)"', "normal")]}))
        self.assertFalse(graph.precedents("Main", "B1")["completeness"]["complete"])
        self.assertTrue(graph.precedents("Main", "D1")["completeness"]["complete"])

    def test_invalid_ranges(self):
        for value in ["A0", "XFE1", "A1048577", "B2:A1", "A1,B2", "A1;__import__('os')"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_range(value)

    def test_repeated_name_expansion_is_bounded(self):
        names = [{"name": "NameA", "value": "Main!$A$1"}]
        for index, letter in enumerate("BCDEFGHIJKLMNO", 1):
            previous = chr(ord("A") + index - 1)
            names.append({"name": f"Name{letter}", "value": f"Name{previous}+Name{previous}"})
        graph = DependencyGraph(workbook({"Main": [Cell("A1", 1), Cell("B1", 1, "=NameO", "normal")]}, names))
        result = graph.precedents("Main", "B1")
        self.assertIn("formula_expansion_limit", {issue["code"] for issue in result["issues"]})
        self.assertFalse(result["completeness"]["complete"])

    def test_external_defined_names_and_cell_like_valid_names(self):
        graph = DependencyGraph(workbook({"Main": [Cell("A1", 5), Cell("B1", 5, "=XFE1+[1]!Rate+[2]Tax", "normal")]},
                                         [{"name": "XFE1", "value": "Main!$A$1"}]))
        result = graph.precedents("Main", "B1")
        self.assertEqual(references(result), {("Main", "B1"), ("Main", "A1")})
        self.assertEqual({issue["code"] for issue in result["issues"]}, {"external_reference"})


if __name__ == "__main__":
    unittest.main()
