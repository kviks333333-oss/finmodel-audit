"""Meaningful evidence-preservation tests using tiny generated OOXML packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from finmodel_audit.workbook import (
    FormulaTranslationError,
    WorkbookReadError,
    load_workbook,
    translate_formula,
)


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"


def write_fixture(path: Path, sheet_data: str, *, strings: str = "", external: bool = False) -> None:
    external_ref = '<externalReferences><externalReference r:id="rId3"/></externalReferences>' if external else ""
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'''<workbook xmlns="{MAIN}" xmlns:r="{REL}">
          <sheets><sheet name="Model" sheetId="1" state="hidden" r:id="rId1"/></sheets>
          <definedNames><definedName name="Input" localSheetId="0">Model!$A$1</definedName></definedNames>
          {external_ref}<calcPr iterate="1" iterateCount="100"/>
          </workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", f'''<Relationships xmlns="{PACKAGE}">
          <Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>
          <Relationship Id="rId2" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/>
          <Relationship Id="rId3" Type="{REL}/externalLink" Target="externalLinks/externalLink1.xml"/>
          </Relationships>''')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{MAIN}"><sheetData>{sheet_data}</sheetData></worksheet>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{strings}</sst>')
        if external:
            archive.writestr("xl/externalLinks/_rels/externalLink1.xml.rels", f'''<Relationships xmlns="{PACKAGE}">
              <Relationship Id="rId1" Type="{REL}/externalLinkPath" Target="file:///missing.xlsx" TargetMode="External"/>
              </Relationships>''')


class SharedFormulaTranslationTests(unittest.TestCase):
    def test_relative_absolute_quoted_sheets_and_strings(self):
        source = '=IF(A1="A1 ""B2""",\'O\'\'Brien\'!$A1+Q1!A$1+$A$1,"C3")'
        expected = '=IF(C2="A1 ""B2""",\'O\'\'Brien\'!$A2+Q1!C$1+$A$1,"C3")'
        self.assertEqual(translate_formula(source, "B2", "D3"), expected)

    def test_ranges_external_3d_function_names_and_dynamic_references(self):
        source = '=LOG10(A1)+SUM(A1:B4,$A2:C$5,A:C,1:3,$A:$C,$1:$3)+\'[Book A.xlsx]Q1\'!A1+Sheet1:Sheet3!B2+[1]Q1!A1+A1#+@B2+INDIRECT("A1")'
        expected = '=LOG10(C2)+SUM(C2:D5,$A3:E$5,C:E,2:4,$A:$C,$1:$3)+\'[Book A.xlsx]Q1\'!C2+Sheet1:Sheet3!D3+[1]Q1!C2+C2#+@D3+INDIRECT("A1")'
        self.assertEqual(translate_formula(source, "B2", "D3"), expected)

    def test_names_and_scientific_literals_are_not_cell_references(self):
        self.assertEqual(translate_formula('=1E10+name.A1+abc_A1+LOG10(A1)', "B2", "D3"), '=1E10+name.A1+abc_A1+LOG10(C2)')

    def test_unsupported_structured_reference_is_not_silently_changed(self):
        with self.assertRaisesRegex(FormulaTranslationError, "structured"):
            translate_formula("=SUM(Table1[A1])+B2", "B2", "C2")

    def test_translation_outside_grid_is_explicit(self):
        with self.assertRaises(FormulaTranslationError):
            translate_formula("=A1", "B2", "A1")


class WorkbookReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "model.xlsx"

    def test_preserves_shared_formulas_caches_and_source_bytes(self):
        write_fixture(self.path, '''<row r="1">
          <c r="A1" t="s"><v>0</v></c><c r="B1" s="3"><f t="shared" si="0" ref="B1:C1">$A1+B$2</f><v>12.5</v></c>
          <c r="C1"><f t="shared" si="0"/><v>99</v></c><c r="D1" s="22"/>
          <c r="E1" t="b"><v>0</v></c><c r="F1" t="inlineStr"><is><r><t>Inline</t></r><r><t> text</t></r></is></c>
          </row>''', strings='<si><r><t>Revenue</t></r><r><t> total</t></r><rPh><t>phonetic</t></rPh></si>')
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        workbook = load_workbook(self.path)
        cells = workbook.sheets["Model"].cells
        self.assertEqual(cells["A1"].value, "Revenue total")
        self.assertEqual(cells["B1"].formula, "=$A1+B$2")
        self.assertEqual(cells["B1"].value, 12.5)
        self.assertEqual(cells["C1"].formula, "=$A1+C$2")
        self.assertEqual(cells["C1"].value, 99)  # The saved cache is retained, not recomputed.
        self.assertNotIn("D1", cells)
        self.assertIs(cells["E1"].value, False)
        self.assertEqual(cells["F1"].value, "Inline text")
        self.assertEqual(cells["B1"].style_id, 3)
        self.assertEqual(workbook.sheets["Model"].state, "hidden")
        self.assertEqual(workbook.summary()["formula_cells"], 2)
        self.assertEqual(workbook.summary()["actual_nonempty_cells"], 5)
        self.assertEqual(workbook.calculation["iterate"], "1")
        self.assertEqual(workbook.defined_names[0]["value"], "Model!$A$1")
        json.dumps(workbook.summary(), allow_nan=False)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_errors_data_tables_and_external_targets_are_evidence(self):
        write_fixture(self.path, '''<row r="1">
          <c r="A1" t="e"><f>A2/0</f><v>#DIV/0!</v></c>
          <c r="B1" t="e"><f t="dataTable" ref="B1:C2" dt2D="1" r1="A2" r2="A3"/><v>#NUM!</v></c>
          <c r="C1" t="e"><v>#NUM!</v></c><c r="D1"><f>SUM(A2:A4)</f></c>
          </row>''', external=True)
        workbook = load_workbook(self.path)
        self.assertEqual(workbook.summary()["cached_errors"], 3)
        self.assertEqual(workbook.summary()["formula_cells"], 3)
        self.assertEqual(workbook.summary()["missing_formula_caches"], 1)
        table = workbook.sheets["Model"].cells["B1"]
        self.assertIsNone(table.formula)
        self.assertEqual(table.formula_type, "dataTable")
        self.assertEqual(table.formula_attributes["ref"], "B1:C2")
        self.assertEqual(workbook.external_links[0]["targets"], ["file:///missing.xlsx"])

    def test_table_result_markers_and_cached_empty_strings_are_valid(self):
        write_fixture(self.path, '''<row r="1">
          <c r="A1"><f t="dataTable" ref="A1:B2" r1="D1"/><v>1</v></c>
          <c r="B1"><f/><v>2</v></c>
          <c r="C1" t="str"><f>IF(A1=1,"","value")</f><v/></c>
          <c r="D1" t="str"><f>IF(A1=1,"","value")</f></c>
          </row>''')
        workbook = load_workbook(self.path)
        self.assertEqual(workbook.warnings, [])
        self.assertEqual(workbook.sheets["Model"].cells["C1"].value, "")
        self.assertIsNone(workbook.sheets["Model"].cells["D1"].value)
        self.assertEqual(workbook.summary()["missing_formula_caches"], 1)

    def test_missing_or_unsupported_shared_formulas_warn(self):
        write_fixture(self.path, '''<row r="1">
          <c r="A1"><f t="shared" si="3"/><v>2</v></c>
          <c r="B1"><f t="shared" si="4" ref="B1:C1">Table1[A1]</f><v>4</v></c>
          <c r="C1"><f t="shared" si="4"/><v>4</v></c></row>''')
        workbook = load_workbook(self.path)
        self.assertIsNone(workbook.sheets["Model"].cells["A1"].formula)
        self.assertIsNone(workbook.sheets["Model"].cells["C1"].formula)
        self.assertEqual(len(workbook.warnings), 2)
        self.assertIn("anchor", workbook.warnings[0])
        self.assertIn("structured", workbook.warnings[1])

    def test_shared_anchor_can_follow_its_dependant(self):
        write_fixture(self.path, '<row r="1"><c r="A1"><f t="shared" si="1"/><v>1</v></c><c r="B1"><f t="shared" si="1">B2</f><v>1</v></c></row>')
        self.assertEqual(load_workbook(self.path).sheets["Model"].cells["A1"].formula, "=A2")

    def test_zip_limits_and_dtd_are_rejected(self):
        write_fixture(self.path, '<row r="1"><c r="A1"><v>1</v></c></row>')
        with patch("finmodel_audit.workbook.MAX_UNCOMPRESSED_BYTES", 10):
            with self.assertRaisesRegex(WorkbookReadError, "uncompressed"):
                load_workbook(self.path)
        with patch("finmodel_audit.workbook.MAX_ZIP_ENTRIES", 1):
            with self.assertRaisesRegex(WorkbookReadError, "ZIP entries"):
                load_workbook(self.path)
        with ZipFile(self.path, "w") as archive:
            archive.writestr("xl/workbook.xml", '<!DOCTYPE workbook [<!ENTITY x "unsafe">]><workbook/>')
        with self.assertRaisesRegex(WorkbookReadError, "DTD"):
            load_workbook(self.path)
        with ZipFile(self.path, "w") as archive:
            archive.writestr("xl/workbook.xml", '<!DOCTYPE workbook [<!ENTITY x "unsafe">]><workbook/>'.encode("utf-16"))
        with self.assertRaisesRegex(WorkbookReadError, "DTD"):
            load_workbook(self.path)


if __name__ == "__main__":
    unittest.main()
