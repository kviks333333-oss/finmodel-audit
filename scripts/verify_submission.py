#!/usr/bin/env python3
"""Independent finite acceptance for decision priorities and editable PPTX.

Reads reports/decks only. No provider requests or original workbook writes.
The frozen .9 arithmetic precedes the implementation under review.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests/submission_cases/gold.json"
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def near(a, b):
    return finite(a) and finite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def references(items):
    return {(x.get("sheet"), x.get("cell")) for x in items if isinstance(x, dict)
            and isinstance(x.get("sheet"), str) and isinstance(x.get("cell"), str)}


def numeric_scenario_issues(scenario):
    issues = []
    baseline, result, delta = (scenario.get(k) for k in ("baseline", "result", "delta"))
    if not all(finite(x) for x in (baseline, result, delta)) or not near(delta, result - baseline):
        issues.append("invalid_scenario_arithmetic")
    elif baseline == 0:
        if scenario.get("relative_delta") is not None:
            issues.append("zero_baseline_percentage")
    elif not near(scenario.get("relative_delta"), delta / abs(baseline)):
        issues.append("invalid_relative_delta")
    if scenario.get("conditional") is not True or scenario.get("recalculated_workbook") is not False:
        issues.append("invalid_scenario_scope")
    proof = {(x.get("sheet"), x.get("cell")): x for x in scenario.get("cell_evidence", []) if isinstance(x, dict)}
    target = scenario.get("target", {})
    target_row = proof.get((target.get("sheet"), target.get("cell")), {})
    if not near(target_row.get("value"), scenario.get("saved_output")) or not near(scenario.get("saved_output"), baseline):
        issues.append("saved_target_proof_mismatch")
    if not references(scenario.get("evidence", [])) or not references(scenario["evidence"]) <= set(proof):
        issues.append("scenario_evidence_incomplete")
    for field in ("patches", "formula_path", "source_inputs"):
        if not scenario.get(field):
            issues.append("scenario_path_or_inputs_missing")
        for item in scenario.get(field, []):
            row = proof.get((item.get("sheet"), item.get("cell")), {})
            if field == "source_inputs":
                if item.get("frozen") is not True or not near(item.get("value"), row.get("value")):
                    issues.append("frozen_source_proof_mismatch")
            elif item.get("original_formula" if field == "patches" else "formula") != row.get("formula") or not row.get("formula"):
                issues.append("formula_source_proof_mismatch")
            if field == "patches" and (not isinstance(item.get("changed_formula"), str) or not item["changed_formula"].startswith("=")):
                issues.append("conditional_formula_missing")
    return issues


def decision_checks(report, assessment, expected=None):
    """Inspect published quantities/order independently of decision.py helpers."""
    issues = []
    if not isinstance(assessment, dict):
        return ["decision_assessment_missing"]
    scenarios = {s.get("id"): s for s in report.get("impact_analysis", {}).get("scenarios", []) if isinstance(s, dict)}
    findings = {f.get("id"): f for f in report.get("financial_checks", {}).get("findings", []) if isinstance(f, dict)}
    measured_codes, seen_scenarios = [], set()
    output_keys = set()
    ranked_count = 0
    for output in assessment.get("outputs", []):
        target = output.get("target", {})
        key = (target.get("sheet"), target.get("cell"), output.get("raw_unit"), output.get("baseline"))
        if key in output_keys:
            issues.append("duplicate_output_scope")
        output_keys.add(key)
        previous_magnitude = math.inf
        for group in output.get("ranked_findings", []):
            ranked_count += 1
            measured_codes.append(group.get("code"))
            selected = []
            alternative_ids = []
            for alternative in group.get("alternatives", []):
                identity = alternative.get("id")
                alternative_ids.append(identity)
                if identity in seen_scenarios:
                    issues.append("scenario_ranked_more_than_once")
                seen_scenarios.add(identity)
                scenario = scenarios.get(identity)
                if scenario is None:
                    issues.append("unknown_ranked_scenario")
                    continue
                issues.extend(numeric_scenario_issues(scenario))
                if scenario.get("target") != target or scenario.get("raw_unit") != output.get("raw_unit") or not near(scenario.get("baseline"), output.get("baseline")):
                    issues.append("mixed_output_unit_or_baseline")
                if scenario.get("code") != group.get("code"):
                    issues.append("scenario_bound_to_wrong_theme")
                for field in ("result", "delta", "relative_delta"):
                    want = scenario.get(field)
                    if (want is None and alternative.get(field) is not None) or (want is not None and not near(want, alternative.get(field))):
                        issues.append("alternative_value_changed")
                selected.append(scenario)
            if not selected:
                issues.append("ranked_group_without_scenarios")
                continue
            if set(alternative_ids) != set(group.get("scenario_ids", [])) or len(alternative_ids) != len(set(alternative_ids)):
                issues.append("alternative_id_list_mismatch")
            actual_finding_ids = {fid for s in selected for fid in s.get("finding_ids", [])}
            if actual_finding_ids != set(group.get("finding_ids", [])):
                issues.append("finding_binding_changed")
            if any(fid not in findings or findings[fid].get("code") != group.get("code") for fid in actual_finding_ids):
                issues.append("unproven_finding_link")
            absolute = max(abs(s["delta"]) for s in selected)
            relative = max(abs(s["relative_delta"]) for s in selected) if output.get("baseline") else None
            if not near(group.get("max_absolute_delta"), absolute):
                issues.append("alternatives_summed_or_max_mismatch")
            if (relative is None and group.get("max_absolute_relative_delta") is not None) or (relative is not None and not near(group.get("max_absolute_relative_delta"), relative)):
                issues.append("relative_magnitude_mismatch")
            magnitude = relative if relative is not None else absolute
            if magnitude > previous_magnitude + 1e-10:
                issues.append("priority_not_descending_within_output")
            previous_magnitude = magnitude
            if not isinstance(group.get("reason"), str) or not group["reason"].strip() or not isinstance(group.get("action"), str) or not group["action"].strip():
                issues.append("priority_lacks_reason_or_action")
            if not references(group.get("evidence", [])):
                issues.append("priority_lacks_sources")
    unknown = assessment.get("unquantified_findings", [])
    unknown_codes = {x.get("code") for x in unknown}
    for item in unknown:
        if item.get("rank") is not None or item.get("delta") is not None or item.get("relative_delta") is not None:
            issues.append("unknown_impact_given_numeric_priority")
    suitability = assessment.get("suitability", {})
    if suitability.get("financial_approval") is not False:
        issues.append("unsupported_financial_approval")
    if not suitability.get("summary") or not suitability.get("supported_uses") or not suitability.get("unsubstantiated_uses"):
        issues.append("suitability_not_actionable")
    if (measured_codes or unknown) and not suitability.get("required_checks"):
        issues.append("unresolved_findings_without_next_checks")
    if expected:
        if ranked_count != expected["expected_ranked_groups"]:
            issues.append("gold_ranked_group_count")
        if measured_codes != expected["expected_theme_order"]:
            issues.append("gold_theme_order")
        if not set(expected["expected_unknown_themes"]) <= unknown_codes:
            issues.append("gold_unknown_theme_missing")
        want = {s["id"]: s for s in expected["scenarios"]}
        if seen_scenarios != set(want):
            issues.append("gold_scenario_coverage")
        for identity, scenario in scenarios.items():
            if identity not in want:
                continue
            for field in ("baseline", "result", "delta", "relative_delta"):
                if not near(scenario.get(field), want[identity][field]):
                    issues.append("frozen_gold_value_changed")
    return sorted(set(issues))


def _relationships(archive, part):
    path = "_rels/.rels" if not part else str(PurePosixPath(part).parent / "_rels" / (PurePosixPath(part).name + ".rels"))
    if path not in archive.namelist():
        return {}
    records = {}
    for rel in ET.fromstring(archive.read(path)):
        if rel.get("TargetMode") == "External":
            records[rel.get("Id")] = {"external": True, "target": rel.get("Target"), "type": rel.get("Type")}
        else:
            target = rel.get("Target", "")
            resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(str(PurePosixPath(part).parent / target))
            records[rel.get("Id")] = {"external": False, "target": resolved, "type": rel.get("Type")}
    return records


def _schema_contract_issues(root):
    """Reject the three Office-incompatible structures from the first exporter.

    This is a targeted regression contract, not a replacement for the external
    ECMA XSD validator. Inspect namespaced elements and parent/child order, not
    serialized prefixes or string substrings.
    """
    issues = []
    a, c = NS["a"], NS["c"]
    if any(node.tag == "{" + a + "}spaceAfter" for node in root.iter()):
        issues.append("drawingml_paragraph_spacing_element_invalid")
    if any("{http://www.w3.org/XML/1998/namespace}space" in node.attrib
           for node in root.iter("{" + a + "}t")):
        issues.append("drawingml_text_xml_space_not_allowed")
    for chart in root.iter("{" + c + "}chartSpace"):
        names = [node.tag for node in chart]
        if "{" + c + "}txPr" in names:
            text = names.index("{" + c + "}txPr")
            chart_positions = [i for i, name in enumerate(names) if name == "{" + c + "}chart"]
            if len(chart_positions) != 1 or text < chart_positions[0]:
                issues.append("chartspace_text_properties_before_chart")
    return issues


def _embedded_cells(content):
    """Read numeric/text caches without a spreadsheet library or recalculation."""
    spreadsheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns = {"s": spreadsheet, "r": NS["r"]}
    with ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        package_links = _relationships(archive, "")
        workbook_links = [link for link in package_links.values() if (link.get("type") or "").endswith("/officeDocument")]
        if package_links:
            if len(workbook_links) != 1 or workbook_links[0].get("external"):
                raise ValueError("Chart workbook root relationship is invalid")
            workbook_part = workbook_links[0]["target"]
        else:
            # Minimal independent test packages can omit OPC metadata; real
            # Office packages use the declared officeDocument relationship.
            workbook_part = "xl/workbook.xml"
        book = ET.fromstring(archive.read(workbook_part))
        rels = _relationships(archive, workbook_part)
        string_links = [link for link in rels.values() if (link.get("type") or "").endswith("/sharedStrings")]
        strings = []
        if string_links:
            if len(string_links) != 1 or string_links[0].get("external"):
                raise ValueError("External or duplicate chart shared-string table")
            strings = ["".join(x.text or "" for x in si.findall(".//s:t", ns))
                       for si in ET.fromstring(archive.read(string_links[0]["target"])).findall("s:si", ns)]
        result = {}
        for sheet in book.findall("s:sheets/s:sheet", ns):
            link = rels[sheet.get("{" + NS["r"] + "}id")]
            if link.get("external"):
                raise ValueError("External chart workbook sheet")
            xml = ET.fromstring(archive.read(link["target"]))
            cells = {}
            for cell in xml.findall(".//s:c", ns):
                if cell.find("s:f", ns) is not None:
                    raise ValueError("Chart workbook must contain values, not formulas")
                value = cell.find("s:v", ns)
                if cell.get("t") == "inlineStr":
                    parsed = "".join(t.text or "" for t in cell.findall("s:is//s:t", ns))
                elif cell.get("t") == "s":
                    parsed = strings[int(value.text)]
                elif value is not None:
                    parsed = float(value.text)
                else:
                    parsed = None
                cells[cell.get("r")] = parsed
            result[sheet.get("name")] = cells
        return result


def _range_values(cells, formula):
    match = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^'!]+))!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)", formula or "")
    if not match or match[3] != match[5]:
        raise ValueError("Unsupported chart range")
    sheet = (match[1] or match[2]).replace("''", "'")
    first, last = int(match[4]), int(match[6])
    if not 0 < first <= last <= first + 200:
        raise ValueError("Unbounded chart range")
    return [cells[sheet][match[3] + str(row)] for row in range(first, last + 1)]


def pptx_structure(content):
    """Native/editable OOXML and numeric caches. Rendering is separately tested."""
    issues = []
    slides, chart_data = [], []
    try:
        archive = ZipFile(io.BytesIO(content))
    except BadZipFile:
        return {"issues": ["invalid_pptx_zip"], "slides": [], "charts": []}
    with archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            issues.append("duplicate_zip_parts")
        if any("vbaproject" in name.lower() for name in names):
            issues.append("unexpected_macro_content")
        if "[Content_Types].xml" in names:
            content_types = ET.fromstring(archive.read("[Content_Types].xml"))
            for override in content_types.findall("{http://schemas.openxmlformats.org/package/2006/content-types}Override"):
                part_name = override.get("PartName", "")
                target = unquote(part_name.lstrip("/"))
                if not part_name.startswith("/") or target not in names or target.endswith("/"):
                    issues.append("content_type_override_missing_part")
        if "ppt/presentation.xml" not in names:
            return {"issues": ["missing_presentation_part"], "slides": [], "charts": []}
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        for name in names:
            if name.endswith(".xml"):
                issues.extend(_schema_contract_issues(ET.fromstring(archive.read(name))))
        rels = _relationships(archive, "ppt/presentation.xml")
        size = presentation.find("p:sldSz", NS)
        geometry = {key: int(size.get(key, 0)) for key in ("cx", "cy")} if size is not None else {}
        for record in presentation.findall("p:sldIdLst/p:sldId", NS):
            link = rels.get(record.get("{" + NS["r"] + "}id"), {})
            part = link.get("target")
            if part not in names or link.get("external"):
                issues.append("missing_or_external_slide")
                continue
            xml = ET.fromstring(archive.read(part))
            text = [node.text or "" for node in xml.findall(".//a:t", NS)]
            font_names = {node.get("typeface") for node in xml.findall(".//a:latin", NS)}
            sizes = {int(node.get("sz")) for node in xml.iter() if node.tag in {"{" + NS["a"] + "}" + k for k in ("rPr", "defRPr", "endParaRPr")} and node.get("sz")}
            if font_names != {"Arial"}:
                issues.append("visible_slide_font_not_explicit_arial")
            if not sizes or not sizes <= {1000, 1200}:
                issues.append("visible_slide_size_outside_10_12")
            slide_rels = _relationships(archive, part)
            notes = []
            for relation in slide_rels.values():
                if (relation.get("type") or "").endswith("/notesSlide"):
                    if relation.get("external") or relation.get("target") not in names:
                        issues.append("missing_or_external_notes")
                    else:
                        notes.extend(x.text or "" for x in ET.fromstring(archive.read(relation["target"])).findall(".//a:t", NS))
            charts = []
            for node in xml.findall(".//c:chart", NS):
                relationship = slide_rels.get(node.get("{" + NS["r"] + "}id"), {})
                chart_part = relationship.get("target")
                if chart_part not in names or relationship.get("external"):
                    issues.append("missing_or_external_native_chart")
                    continue
                cxml = ET.fromstring(archive.read(chart_part))
                chart_fonts = {node.get("typeface") for node in cxml.findall(".//a:latin", NS)}
                if chart_fonts != {"Arial"}:
                    issues.append("chart_font_not_explicit_arial")
                chart = {"part": chart_part, "series": []}
                for series in cxml.findall(".//c:ser", NS):
                    category = series.find("c:cat", NS)
                    values = series.find("c:val", NS)
                    labels = [x.text or "" for x in category.findall(".//c:pt/c:v", NS)] if category is not None else []
                    try:
                        numbers = [float(x.text) for x in values.findall(".//c:pt/c:v", NS)] if values is not None else []
                    except (TypeError, ValueError):
                        issues.append("non_numeric_chart_cache")
                        numbers = []
                    if not labels or len(labels) != len(numbers) or not all(finite(x) for x in numbers):
                        issues.append("chart_categories_values_mismatch")
                    chart["series"].append({"categories": labels, "values": numbers,
                        "category_formula": category.findtext(".//c:f", default="", namespaces=NS) if category is not None else "",
                        "value_formula": values.findtext(".//c:f", default="", namespaces=NS) if values is not None else ""})
                external_data = cxml.find("c:externalData", NS)
                chart_rels = _relationships(archive, chart_part)
                relation = chart_rels.get(external_data.get("{" + NS["r"] + "}id"), {}) if external_data is not None else {}
                embedded = relation.get("target")
                if not embedded or embedded not in names or relation.get("external") or not embedded.endswith(".xlsx"):
                    issues.append("native_chart_has_no_embedded_workbook")
                else:
                    try:
                        cells = _embedded_cells(archive.read(embedded))
                        for series in chart["series"]:
                            labels = _range_values(cells, series["category_formula"])
                            numbers = _range_values(cells, series["value_formula"])
                            if labels != series["categories"] or len(numbers) != len(series["values"]) or any(not near(a, b) for a, b in zip(numbers, series["values"])):
                                issues.append("chart_cache_disagrees_with_embedded_workbook")
                    except (BadZipFile, KeyError, ValueError, TypeError, ET.ParseError):
                        issues.append("embedded_workbook_unreadable_or_unbounded")
                chart["embedded_workbook"] = embedded
                charts.append(chart_part)
                chart_data.append(chart)
            slides.append({"part": part, "text": text, "notes": notes, "tables": len(xml.findall(".//a:tbl", NS)), "charts": charts,
                           "fonts": sorted(font_names), "font_sizes": sorted(sizes)})
        if not slides or len(slides) > 24:
            issues.append("slide_count_outside_bound")
        if not sum(slide["tables"] for slide in slides):
            issues.append("no_editable_tables")
        if not chart_data:
            issues.append("no_editable_charts")
    return {"issues": sorted(set(issues)), "geometry": geometry, "slides": slides, "charts": chart_data}


def _normalized(value):
    return " ".join(str(value).split())


def pptx_content_checks(structure, plan, report):
    """Match native output to the plan, then bind chart inputs to report facts."""
    issues = []
    slides = plan.get("slides", [])
    if len(slides) != len(structure.get("slides", [])):
        return ["plan_slide_count_mismatch"]
    chart_lookup = {item["part"]: item for item in structure.get("charts", [])}
    canonical = {item.get("id"): item for item in report.get("impact_analysis", {}).get("scenarios", [])}
    cards = {item.get("id"): item for item in report.get("facts", [])}
    chosen = set(next((s.get("selection", {}).get("fact_ids", []) for s in report.get("stages", []) if s.get("id") == "business"), []))
    represented_scenarios = set()
    for slide, actual in zip(slides, structure["slides"]):
        visible = _normalized(" ".join(actual["text"]))
        expected_strings = [slide.get("title", ""), *slide.get("paragraphs", [])]
        table = slide.get("table") or {}
        expected_strings += table.get("headers", [])
        expected_strings += [cell for row in table.get("rows", []) for cell in row]
        if any(_normalized(value) not in visible for value in expected_strings if value):
            issues.append("plan_text_or_table_not_preserved")
        notes = _normalized(" ".join(actual.get("notes", [])))
        if slide.get("note") and _normalized(slide["note"]) not in notes:
            issues.append("plan_source_notes_not_preserved")
        chart = slide.get("chart")
        if not chart:
            if actual.get("charts"):
                issues.append("unplanned_native_chart")
            continue
        if len(actual.get("charts", [])) != 1:
            issues.append("planned_chart_not_native_or_duplicated")
            continue
        series = chart_lookup[actual["charts"][0]]["series"]
        if len(series) != 1 or series[0]["categories"] != chart.get("categories") or len(series[0]["values"]) != len(chart.get("values", [])) or any(not near(a, b) for a, b in zip(series[0]["values"], chart.get("values", []))):
            issues.append("native_chart_differs_from_plan")
        if _normalized(chart.get("unit", "")) not in visible:
            issues.append("chart_unit_not_visible")
        try:
            basis = json.loads(slide.get("note", ""))
        except (TypeError, ValueError):
            issues.append("chart_source_basis_missing")
            continue
        if isinstance(basis.get("scenarios"), list):
            source_scenarios = basis["scenarios"]
            if basis.get("independent_alternatives") is not True or not source_scenarios:
                issues.append("conditional_alternative_scope_missing")
                continue
            target, unit, baseline = (source_scenarios[0].get(k) for k in ("target", "raw_unit", "baseline"))
            for scenario in source_scenarios:
                identity = scenario.get("id")
                represented_scenarios.add(identity)
                expected = canonical.get(identity, {})
                if scenario != expected:
                    issues.append("conditional_notes_differ_from_canonical_source")
                if scenario.get("target") != target or scenario.get("raw_unit") != unit or not near(scenario.get("baseline"), baseline):
                    issues.append("conditional_chart_mixes_scopes")
            expected_values = [baseline] + [s.get("result") for s in source_scenarios]
            if unit != chart.get("unit") or len(expected_values) != len(chart.get("values", [])) or any(not near(a, b) for a, b in zip(expected_values, chart.get("values", []))):
                issues.append("conditional_chart_has_invented_or_combined_values")
            if f"{target['sheet']}!{target['cell']}" not in slide.get("sources", []):
                issues.append("conditional_chart_target_not_cited")
        elif isinstance(basis.get("points"), list):
            points = basis["points"]
            if len(points) != len(chart.get("values", [])):
                issues.append("comparison_chart_point_count")
                continue
            identities = []
            for index, point in enumerate(points):
                identity = point.get("fact_id")
                identities.append(identity)
                card = cards.get(identity, {})
                if identity not in chosen or card.get("binding_status") != "bound":
                    issues.append("comparison_chart_not_selected_bound_fact")
                if (not near(card.get("value"), chart["values"][index]) or point.get("period") != card.get("period")
                        or str(card.get("period")) != chart["categories"][index] or card.get("unit") != chart.get("unit")
                        or card.get("source_sheet") != point.get("sheet") or card.get("value_cell") != point.get("cell")):
                    issues.append("comparison_chart_changes_value_period_unit_or_source")
            if len({(cards.get(identity, {}).get("source_sheet"), cards.get(identity, {}).get("metric_cell")) for identity in identities}) != 1:
                issues.append("comparison_chart_mixes_metrics_or_perimeters")
        else:
            issues.append("unsupported_chart_source_basis")
    if represented_scenarios != set(canonical):
        issues.append("presentation_conditional_scenario_coverage")
    return sorted(set(issues))


def runtime_probes(report, build):
    """Metamorphic checks with frozen expectations; never write the input report."""
    encoded = json.dumps(report, sort_keys=True, allow_nan=False)
    baseline = build(deepcopy(report))
    checks = []

    def record(name, passed):
        checks.append({"name": name, "passed": bool(passed)})

    def selected(assessment):
        return {identity for output in assessment.get("outputs", []) for row in output.get("ranked_findings", [])
                for identity in row.get("scenario_ids", [])}

    extra = deepcopy(report)
    extra.setdefault("structural", {}).setdefault("findings", []).extend([
        {"code": "technical_review_note", "title": "A technical observation", "sheet": "Unrelated", "cells": ["A1"]}
        for _ in range(100)])
    changed = build(extra)
    record("technical_counts_do_not_downgrade_or_reorder", baseline.get("outputs") == changed.get("outputs") and baseline.get("suitability") == changed.get("suitability"))
    incomplete = deepcopy(report)
    quality = next((stage for stage in incomplete.get("stages", []) if stage.get("id") == "quality"), None)
    if quality is not None:
        quality["completion"] = "incomplete"
        result = build(incomplete).get("suitability", {})
        record("incomplete_stage_is_explicit_not_approved", result.get("financial_approval") is False and "quality" in result.get("incomplete_stages", []))
    no_impact = deepcopy(report)
    no_impact["impact_analysis"] = {"scenarios": []}
    result = build(no_impact)
    codes = {x.get("code") for x in report.get("financial_checks", {}).get("findings", [])}
    record("no_calculation_means_unknown_not_low", not result.get("outputs") and codes <= {x.get("code") for x in result.get("unquantified_findings", [])}
           and result.get("suitability", {}).get("financial_approval") is False)
    scenarios = report.get("impact_analysis", {}).get("scenarios", [])
    if scenarios:
        identity = scenarios[0]["id"]
        for name, mutate in (
            ("changed_delta_is_not_ranked", lambda scenario: scenario.update(delta=scenario["delta"] + 123)),
            ("missing_proof_is_not_ranked", lambda scenario: scenario.update(cell_evidence=[])),
            ("changed_unit_is_not_ranked", lambda scenario: scenario.update(raw_unit="unproven currency")),
        ):
            altered = deepcopy(report)
            mutate(altered["impact_analysis"]["scenarios"][0])
            record(name, identity not in selected(build(altered)))
    record("input_report_unchanged", encoded == json.dumps(report, sort_keys=True, allow_nan=False))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", default=[], help="original=PATH or second=PATH enriched report")
    parser.add_argument("--pptx", action="append", default=[], help="original=PATH or second=PATH exported deck")
    parser.add_argument("--slide-plan", action="append", default=[], help="original=PATH or second=PATH exported plan")
    parser.add_argument("--runtime-probes", action="store_true", help="Run finite mutation checks on fresh copies through decision.py, without API")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output_dir.exists():
        raise FileExistsError("Use a new evaluation output directory")
    gold = json.loads(GOLD.read_text())
    reports = dict(item.split("=", 1) for item in args.report)
    decks = dict(item.split("=", 1) for item in args.pptx)
    plans = dict(item.split("=", 1) for item in args.slide_plan)
    loaded_reports = {}
    result = {"gold_sha256": digest(GOLD), "reports": {}, "presentations": {}, "limitations": [
        "Finite published ranking/provenance acceptance, not universal financial correctness.",
        "OOXML editable objects and font metadata do not prove a native PowerPoint visual review."]}
    for name, path in reports.items():
        report = json.loads(Path(path).read_text())
        loaded_reports[name] = report
        expected = gold["reports"][name]
        issues = decision_checks(report, report.get("decision_assessment"), expected)
        if report.get("source_sha256") != expected["source_sha256"]:
            issues.append("wrong_source_workbook")
        result["reports"][name] = {"path": path, "sha256": digest(path), "issues": issues, "passed": not issues}
        if args.runtime_probes:
            from finmodel_audit.decision import build_decision_assessment
            probes = runtime_probes(report, build_decision_assessment)
            result["reports"][name]["runtime_probes"] = probes
            if not all(probe["passed"] for probe in probes):
                result["reports"][name]["passed"] = False
                result["reports"][name]["issues"].append("runtime_probe_failed")
    for name, path in decks.items():
        check = pptx_structure(Path(path).read_bytes())
        if name in plans and name in loaded_reports:
            check["content_issues"] = pptx_content_checks(check, json.loads(Path(plans[name]).read_text()), loaded_reports[name])
            check["issues"] = sorted(set(check["issues"] + check["content_issues"]))
        else:
            check["issues"].append("source_plan_or_report_not_supplied")
        result["presentations"][name] = {"path": path, "sha256": digest(path), **check, "passed": not check["issues"]}
    result["passed"] = bool(result["reports"]) and all(x["passed"] for area in ("reports", "presentations") for x in result[area].values())
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": result["passed"], "reports": {k: v["issues"] for k, v in result["reports"].items()},
                      "presentations": {k: v["issues"] for k, v in result["presentations"].items()}}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
