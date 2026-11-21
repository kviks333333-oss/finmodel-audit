"""Portable presentation contract: real Office objects and source-bound values."""
from copy import deepcopy
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import posixpath
import re
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from finmodel_audit.pptx_export import build_slide_plan, render_pptx, STYLE, MAX_SLIDES
from test_decision import report_for
from test_impact import fixture
from test_presentation import report_with_cards, source_fact

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

_spec = importlib.util.spec_from_file_location("pptx_independent_evaluation", Path(__file__).resolve().parents[1] / "scripts/verify_submission.py")
evaluation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluation)


class PowerPointTests(unittest.TestCase):
    def test_export_does_not_mutate_input_or_trust_supplied_views(self):
        report = report_for(fixture()[0])
        before = deepcopy(report)
        expected = build_slide_plan(report)
        render_pptx(report)
        self.assertEqual(report, before)
        report.update(decision_assessment={"outputs": [{"baseline": 999}]}, presentation={"metrics": [999]})
        self.assertEqual(build_slide_plan(report), expected)

    def test_package_relationships_and_fonts_are_portable_and_closed(self):
        deck = ZipFile(BytesIO(render_pptx(report_for(fixture()[0]))))
        names = set(deck.namelist())
        self.assertFalse(any("vba" in name or name.endswith(".png") for name in names))
        content_types = ET.fromstring(deck.read("[Content_Types].xml"))
        for override in content_types.findall("{http://schemas.openxmlformats.org/package/2006/content-types}Override"):
            self.assertTrue(override.get("PartName", "").startswith("/"))
            target = unquote(override.get("PartName").lstrip("/"))
            self.assertIn(target, names)
            self.assertFalse(target.endswith("/"))
        theme = ET.fromstring(deck.read("ppt/theme/theme1.xml"))
        aliases = {}
        for scheme, short in (("majorFont", "mj"), ("minorFont", "mn")):
            latin = theme.find(f"a:themeElements/a:fontScheme/a:{scheme}/a:latin", NS)
            self.assertEqual(latin.get("typeface"), "Arial")
            for suffix in ("lt", "ea", "cs"):
                aliases[f"+{short}-{suffix}"] = "Arial"
        for name in names:
            if name.endswith((".xml", ".rels")):
                root = ET.fromstring(deck.read(name))
                if name.endswith(".rels"):
                    owner = "" if name == "_rels/.rels" else posixpath.dirname(name).removesuffix("/_rels")
                    for relation in root:
                        self.assertNotEqual(relation.get("TargetMode"), "External")
                        target = relation.attrib["Target"]
                        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(owner, target))
                        self.assertIn(resolved, names)
                for font in root.findall(".//a:latin", NS) + root.findall(".//a:ea", NS) + root.findall(".//a:cs", NS):
                    value = font.attrib["typeface"]
                    if not value:
                        self.assertTrue(name.startswith("ppt/theme/") and font.tag in {"{" + NS["a"] + "}ea", "{" + NS["a"] + "}cs"})
                    else:
                        self.assertEqual(aliases.get(value, value), "Arial")
                if name.startswith(("ppt/slides/", "ppt/charts/")):
                    for run in root.findall(".//a:rPr", NS):
                        self.assertIn(run.get("sz"), {"1000", "1200"})
                if name.startswith("ppt/notesMasters/"):
                    self.assertTrue(all(run.get("sz") == "1000" for run in root.findall(".//a:defRPr", NS)))
                if re.fullmatch(r"ppt/slides/slide\d+.xml", name):
                    for shape in root.findall(".//p:sp", NS):
                        is_title = shape.find("p:nvSpPr/p:cNvPr", NS).get("name") == "Title"
                        self.assertTrue(all(r.get("sz") == ("1200" if is_title else "1000") for r in shape.findall(".//a:rPr", NS)))
        size = ET.fromstring(deck.read("ppt/presentation.xml")).find("p:sldSz", NS)
        self.assertEqual((int(size.get("cx")), int(size.get("cy"))), (STYLE["width_emu"], STYLE["height_emu"]))

    def test_native_charts_cache_workbook_and_verified_scenarios_agree(self):
        report = report_for(fixture(overlap=True)[0])
        plan = build_slide_plan(report)
        charts = [s["chart"] for s in plan["slides"] if s["chart"]]
        self.assertEqual(len(charts), 1)
        scenarios = report["impact_analysis"]["scenarios"]
        self.assertEqual(charts[0]["values"], [scenarios[0]["baseline"]] + [s["result"] for s in scenarios])
        self.assertEqual(len(charts[0]["values"]), 3)  # baseline and two alternatives, no combined scenario
        payload = render_pptx(report)
        deck = ZipFile(BytesIO(payload))
        structure = evaluation.pptx_structure(payload)
        self.assertEqual(structure["issues"], [])
        self.assertEqual(len(structure["charts"]), len(charts))
        for chart, actual in zip(charts, structure["charts"]):
            self.assertEqual(len(actual["series"]), 1)
            series = actual["series"][0]
            categories, values = series["categories"], series["values"]
            self.assertEqual((categories, values), (chart["categories"], chart["values"]))
            embedded = deck.read(actual["embedded_workbook"])
            cells = evaluation._embedded_cells(embedded)
            self.assertEqual(evaluation._range_values(cells, series["category_formula"]), categories)
            self.assertEqual(evaluation._range_values(cells, series["value_formula"]), values)
            with ZipFile(BytesIO(embedded)) as workbook:
                for name in workbook.namelist():
                    if name.endswith(".xml"):
                        self.assertFalse(ET.fromstring(workbook.read(name)).findall(".//s:f", NS))
        slide_xml = [ET.fromstring(deck.read(n)) for n in deck.namelist() if re.fullmatch(r"ppt/slides/slide\d+.xml", n)]
        self.assertGreater(sum(len(s.findall(".//a:tbl", NS)) for s in slide_xml), 0)

    def test_office_incompatible_spacing_text_attribute_and_chart_order_rejected(self):
        """Regression for the three structures found in the failed real deck."""
        data = render_pptx(report_for(fixture(overlap=True)[0]))
        with ZipFile(BytesIO(data)) as deck:
            for name in deck.namelist():
                if name.endswith(".xml"):
                    self.assertEqual(evaluation._schema_contract_issues(ET.fromstring(deck.read(name))), [])
            slide = ET.fromstring(deck.read("ppt/slides/slide1.xml"))
            paragraph = slide.find(".//a:pPr", NS)
            self.assertIsNotNone(paragraph)
            bad_spacing = deepcopy(slide)
            ET.SubElement(bad_spacing.find(".//a:pPr", NS), "{" + NS["a"] + "}spaceAfter")
            self.assertIn("drawingml_paragraph_spacing_element_invalid", evaluation._schema_contract_issues(bad_spacing))
            bad_text = deepcopy(slide)
            bad_text.find(".//a:t", NS).set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            self.assertIn("drawingml_text_xml_space_not_allowed", evaluation._schema_contract_issues(bad_text))
            chart_name = next(name for name in deck.namelist() if re.fullmatch(r"ppt/charts/chart\d+.xml", name))
            bad_chart = ET.fromstring(deck.read(chart_name))
            text_properties = bad_chart.find("c:txPr", NS)
            if text_properties is None:
                text_properties = ET.Element("{" + NS["c"] + "}txPr")
            else:
                bad_chart.remove(text_properties)
            chart = bad_chart.find("c:chart", NS)
            bad_chart.insert(list(bad_chart).index(chart), text_properties)
            self.assertIn("chartspace_text_properties_before_chart", evaluation._schema_contract_issues(bad_chart))

    def test_dangling_content_type_override_is_rejected(self):
        """A declaration is not proof that its slide master exists in the ZIP."""
        payload = render_pptx(report_for(fixture(overlap=True)[0]))
        self.assertNotIn("content_type_override_missing_part", evaluation.pptx_structure(payload)["issues"])
        changed = BytesIO()
        with ZipFile(BytesIO(payload)) as original, ZipFile(changed, "w") as corrupt:
            for name in original.namelist():
                data = original.read(name)
                if name == "[Content_Types].xml":
                    root = ET.fromstring(data)
                    ET.SubElement(root, "{http://schemas.openxmlformats.org/package/2006/content-types}Override", {
                        "PartName": "/ppt/slideMasters/not-present.xml",
                        "ContentType": "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
                    })
                    data = ET.tostring(root)
                corrupt.writestr(name, data)
        self.assertIn("content_type_override_missing_part", evaluation.pptx_structure(changed.getvalue())["issues"])

    def test_consistent_tampering_cannot_reach_a_chart(self):
        report = report_for(fixture()[0])
        scenario = report["impact_analysis"]["scenarios"][0]
        scenario["result"] += 99999
        scenario["delta"] = scenario["result"] - scenario["baseline"]
        scenario["relative_delta"] = scenario["delta"] / abs(scenario["baseline"])
        charts = [s["chart"] for s in build_slide_plan(report)["slides"] if s["chart"]]
        self.assertTrue(charts)
        self.assertNotIn(scenario["result"], [v for c in charts for v in c["values"]])

    def test_unknown_periods_do_not_become_years_or_time_charts(self):
        plan = build_slide_plan(report_with_cards(unknown=True))
        self.assertFalse(any(s["chart"] for s in plan["slides"]))
        rows = [r for s in plan["slides"] if s["section"] == "business" and s["table"] for r in s["table"]["rows"]]
        self.assertTrue(rows)
        self.assertTrue(all(r[3] == "Не установлен" for r in rows))

    def test_raw_percent_label_is_not_scaled_and_exact_value_is_in_notes(self):
        report = report_with_cards(unknown=True)
        report["facts"] = [source_fact("irr", 1.096, None, "B", unit="%", metric_label="IRR", metric_key="irr")]
        report["stages"][0].pop("selection")
        slide = next(s for s in build_slide_plan(report)["slides"] if s["section"] == "business")
        self.assertEqual(slide["table"]["headers"][1:3], ["Число из файла", "Подпись единицы"])
        self.assertEqual(slide["table"]["rows"][0][2], "%")
        self.assertNotIn("%", slide["table"]["rows"][0][1])
        self.assertIn("форматирование Excel не применяются", slide["paragraphs"][0])
        self.assertEqual(json.loads(slide["note"])["facts"][0]["value"], 1.096)

    def test_sources_and_exact_formula_proof_survive_in_notes(self):
        report = report_for(fixture()[0])
        plan = build_slide_plan(report)
        deck = ZipFile(BytesIO(render_pptx(report)))
        for index, slide in enumerate(plan["slides"], 1):
            root = ET.fromstring(deck.read(f"ppt/notesSlides/notesSlide{index}.xml"))
            text = "\n".join(x.text or "" for x in root.findall(".//a:t", NS))
            for reference in slide["sources"]:
                self.assertIn(reference, text)
            if slide["section"] == "impact":
                self.assertIn('"independent_alternatives": true', text)
                self.assertIn('"changed_formula"', text)
                self.assertIn('"cell_evidence"', text)

    def test_empty_report_is_honest_and_still_covers_four_blocks(self):
        plan = build_slide_plan({})
        self.assertTrue({"audit", "assumptions", "quality", "business"}.issubset({s["section"] for s in plan["slides"]}))
        self.assertFalse(any(s["chart"] for s in plan["slides"]))
        self.assertIn("часть стадий не завершена", " ".join(plan["slides"][0]["paragraphs"]))
        self.assertGreater(len(render_pptx({})), 1000)

    def test_zero_negative_values_and_escaped_text_are_preserved(self):
        report = report_with_cards()
        report["facts"][0]["value"] = 0
        report["facts"][1]["value"] = -400
        report["stages"][0].pop("selection")
        report["display_filename"] = "A < B & 'C'\x01.xlsx"
        plan = build_slide_plan(report)
        values = [r[1] for s in plan["slides"] if s["section"] == "business" and s["table"] for r in s["table"]["rows"]]
        self.assertIn("0", values)
        self.assertIn("-400", values)
        deck = ZipFile(BytesIO(render_pptx(report)))
        root = ET.fromstring(deck.read("ppt/slides/slide1.xml"))
        self.assertIn("A < B & 'C'.xlsx", [t.text for t in root.findall(".//a:t", NS)])

    def test_bounded_deck_keeps_critical_sections_and_declares_omissions(self):
        report = report_for(fixture()[0])
        report["facts"] = []
        for stage in ("assumptions", "business"):
            for i in range(96):
                report["facts"].append(source_fact(f"{stage}_{i}", i, None, "B", stage_id=stage,
                    value_cell=f"B{i+1}", metric_cell=f"A{i+1}", metric_key=f"metric_{i}", metric_label=f"Metric {i}"))
        plan = build_slide_plan(report)
        self.assertEqual(len(plan["slides"]), MAX_SLIDES)
        self.assertTrue({"audit", "assumptions", "quality", "business", "limitations"}.issubset({s["section"] for s in plan["slides"]}))
        self.assertTrue(plan["omissions"])
        self.assertIn("24", " ".join(plan["slides"][-1]["paragraphs"]))
        render_pptx(report)

    def test_unsupported_input_rejected_before_serialization(self):
        for report in (None, [], {"bad": float("nan")}, {"bad": {1, 2}}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                render_pptx(report)

    def test_extreme_finite_ratio_never_becomes_infinite_percentage(self):
        from finmodel_audit.decision import build_decision_assessment
        report = report_for(fixture()[0])
        decision = build_decision_assessment(report)
        decision["outputs"][0]["ranked_findings"][0]["max_absolute_relative_delta"] = 1e308
        with patch("finmodel_audit.decision.build_decision_assessment", return_value=decision):
            plan = build_slide_plan(report)
        row = next(s for s in plan["slides"] if s["section"] == "audit")["table"]["rows"][0]
        self.assertIn("(доля)", row[2])
        self.assertNotIn("inf", row[2])
        self.assertNotIn("Не установлено%", row[2])


if __name__ == "__main__":
    unittest.main()
