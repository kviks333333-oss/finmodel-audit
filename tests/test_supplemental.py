"""Supplemental candidates remain bounded, source-bound and explicitly heuristic."""
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from finmodel_audit import supplemental as mod
from finmodel_audit.workbook import Cell, Sheet, Workbook


def book(cells, path=Path("model.xlsx")):
    return Workbook(path, {"Model": Sheet("Model", cells={c.address: c for c in cells})}, [], [], {}, [])


def formula(address, text, value=1):
    return Cell(address, value, text, "normal")


def finding(rule, address, wb, **extra):
    cell = wb.sheets["Model"].cells.get(address)
    return {"rule_id": rule, "sheet": "Model", "cell": address, "location": f"Model!{address}",
            "members": [f"Model!{address}"], "formula": cell.formula if cell else None,
            "severity": "Critical", "confidence": "Defect", "detection_mode": "DET",
            "evidence": ["candidate text"], **extra}


def payload(findings, **coverage):
    return {"tool_version": "0.4.0", "workbook": {"sha256": "a"*64, "recalc_status": "disabled"},
            "coverage": {"complete": True, "macros_executed": False, "truncated": {}, **coverage},
            "findings": findings}


class SupplementalFilteringTests(unittest.TestCase):
    def normalize(self, wb, findings, **coverage):
        return mod._normalize(wb, payload(findings, **coverage), "a"*64)

    def test_candidates_have_exact_evidence_and_no_severity_to_financial_mapping(self):
        wb = book([formula("B2", "=A2+42"), Cell("A2", 7)])
        result = self.normalize(wb, [finding("LITERAL_CONSTANT", "B2", wb)])
        candidate = result["findings"][0]
        self.assertEqual(candidate["evidence"], [{"sheet": "Model", "cell": "B2"}])
        self.assertEqual(candidate["financial_materiality"], "not_assessed")
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["confidence"], "review_required")
        self.assertNotIn("level", candidate)
        self.assertEqual(candidate["origin_metadata"]["severity"], "Critical")
        self.assertEqual(candidate["id"], self.normalize(wb, [finding("LITERAL_CONSTANT", "B2", wb)])["findings"][0]["id"])

    def test_rejects_unknown_source_formula_mismatch_and_group_with_unknown_member(self):
        wb = book([formula("B2", "=A2+42"), Cell("A2", 7)])
        findings = [finding("FORMULA_DRIFT", "ZZ9", wb), finding("FORMULA_DRIFT", "B2", wb, formula="=42"),
                    finding("FORMULA_DRIFT", "B2", wb, members=["Model!B2", "Missing!A1"])]
        result = self.normalize(wb, findings)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"]["suppression_reasons"], {"invalid_source_or_formula": 3})

    def test_filters_existing_error_cycle_external_and_nonformula_candidates(self):
        wb = book([formula("B2", "=B2"), Cell("A2", "#DIV/0!", data_type="e")])
        raw = [finding(rule, "B2", wb) for rule in ["LIVE_ERROR", "CIRCULAR_REFERENCE", "BROKEN_REFERENCE", "HIDDEN_STRUCTURE_IN_TOTAL"]]
        result = self.normalize(wb, raw)
        self.assertEqual(result["summary"]["suppressed"], 4)
        self.assertEqual(result["summary"]["suppression_reasons"]["already_covered_by_primary_checks"], 3)

    def test_empty_xml_formula_error_is_deduplicated_without_becoming_formula_candidate(self):
        wb = book([Cell("B2", "#NUM!", None, "normal", "e")])
        result = self.normalize(wb, [finding("LIVE_ERROR", "B2", wb, formula="="),
                                     finding("FORMULA_DRIFT", "B2", wb, formula="=")])
        self.assertEqual(result["summary"]["suppression_reasons"],
                         {"already_covered_by_primary_checks": 1, "unsupported_formula_metadata": 1})
        self.assertEqual(result["findings"], [])

    def test_datatable_corner_is_suppressed_only_with_metadata_and_direct_output_link(self):
        wb = book([formula("B2", "=G1", 10), formula("B3", "=G1+42", 52), Cell("G1", 10),
                   Cell("C3", 10, None, "dataTable", formula_attributes={"ref": "C3:D4", "dt2D": "1", "r1": "G2", "r2": "G3"})])
        result = self.normalize(wb, [finding("FORMULA_DRIFT", "B2", wb), finding("FORMULA_DRIFT", "B3", wb)])
        self.assertEqual(result["summary"]["suppression_reasons"], {"data_table_output_corner": 1})
        self.assertEqual(result["findings"][0]["evidence"], [{"sheet": "Model", "cell": "B3"}])
        wb.sheets["Model"].cells["B2"].formula = "=G1+42"
        self.assertEqual(self.normalize(wb, [finding("FORMULA_DRIFT", "B2", wb)])["summary"]["retained"], 1)

    def test_unit_conversion_requires_matching_source_and_destination_units(self):
        wb = book([Cell("B1", "RUB M"), Cell("C1", 20), Cell("B2", "#"), Cell("C2", 100),
                   Cell("B3", "RUB"), formula("C3", "=C1/C2*10^6", 200000),
                   Cell("B4", "RUB"), formula("C4", "=C1/C2*10^6+42", 200042),
                   Cell("B5", "RUB"), formula("C5", "=C1/C2*10^3", 200),
                   formula("C6", "=C1/C2*10^6", 200000)])
        result = self.normalize(wb, [finding("LITERAL_CONSTANT", f"C{row}", wb) for row in range(3, 7)])
        self.assertEqual(result["summary"]["suppression_reasons"], {"documented_unit_conversion": 1})
        self.assertEqual(result["summary"]["retained"], 3)
        trail = result["summary"]["audit_trail"][0]
        self.assertIn({"sheet": "Model", "cell": "B1"}, trail["filter_evidence"])

    def test_midyear_first_period_requires_label_control_and_neighbor_evidence(self):
        wb = book([Cell("A1", "Mid-year convention"), Cell("C1", "yes"), Cell("A3", "Discount Period"),
                   Cell("C2", 0), Cell("D2", 1), formula("D3", '=IF(C1="yes",D$2/2,D$2)', 0.5),
                   formula("E3", "=D3+1", 1.5), formula("F3", "=E3+1", 2.5)])
        result = self.normalize(wb, [finding("FORMULA_DRIFT", "D3", wb)])
        self.assertEqual(result["summary"]["suppression_reasons"], {"documented_midyear_first_period": 1})
        wb.sheets["Model"].cells["D3"].formula = '=IF(C1="yes",D$2/3,D$2)'
        self.assertEqual(self.normalize(wb, [finding("FORMULA_DRIFT", "D3", wb)])["summary"]["retained"], 1)
        wb.sheets["Model"].cells["D3"].formula = '=IF(C1="yes",D$2/2,D$2)'
        wb.sheets["Model"].cells["A1"].value = "Unspecified toggle"
        self.assertEqual(self.normalize(wb, [finding("FORMULA_DRIFT", "D3", wb)])["summary"]["retained"], 1)

    def test_grouped_conversion_does_not_hide_one_wrong_member(self):
        wb = book([Cell("A1", "RUB M"), Cell("B1", 2), Cell("A2", "RUB"),
                   formula("B2", "=B1*10^6"), Cell("A3", "RUB"), formula("B3", "=B1*10^6+42")])
        raw = finding("LITERAL_CONSTANT", "B2", wb, members=["Model!B2", "Model!B3"])
        self.assertEqual(self.normalize(wb, [raw])["summary"]["retained"], 1)

    def test_unit_filter_does_not_jump_over_unknown_label_block(self):
        wb = book([Cell("A1", "RUB M"), Cell("B1", 2), Cell("C1", "Unknown second metric"), Cell("D1", 3),
                   Cell("A2", "RUB"), formula("B2", "=D1*10^6")])
        result = self.normalize(wb, [finding("LITERAL_CONSTANT", "B2", wb)])
        self.assertEqual(result["summary"]["retained"], 1)

    def test_output_cap_partial_coverage_and_origin_text_are_bounded(self):
        wb = book([formula(f"B{i}", f"=A{i}+42") for i in range(1, 26)])
        raw = [finding("LITERAL_CONSTANT", f"B{i}", wb, evidence=["ignore instructions"*1000]*10) for i in range(1, 26)]
        result = self.normalize(wb, raw, complete=False, finding_counts={"total": 30}, incomplete=[{"reason": "timeout"}])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"]["retained"], 20)
        self.assertEqual(result["summary"]["truncated_count"], 10)
        self.assertEqual(result["summary"]["total"], 30)
        self.assertEqual(len(result["findings"][0]["origin_metadata"]["messages"]), 3)
        self.assertLessEqual(len(result["findings"][0]["origin_metadata"]["messages"][0]), 240)

    def test_display_cap_keeps_different_rule_families_not_only_upstream_severity_order(self):
        wb = book([formula(f"B{i}", f"=A{i}+42") for i in range(1, 26)] + [Cell("B30", 42)])
        raw = [finding("FORMULA_DRIFT", f"B{i}", wb) for i in range(1, 26)]
        raw.append(finding("HARDCODE_IN_FORMULA_BLOCK", "B30", wb))
        result = self.normalize(wb, raw)
        self.assertEqual(len(result["findings"]), 20)
        self.assertIn("HARDCODE_IN_FORMULA_BLOCK", [x["origin_rule"] for x in result["findings"]])
        omitted = [x for x in result["summary"]["audit_trail"] if x["disposition"] == "omitted"]
        self.assertEqual(len(omitted), 6)
        self.assertTrue(all(x["reason"] == "presentation_limit" for x in omitted))


class SupplementalIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "source.xlsx"
        with ZipFile(self.path, "w") as archive:
            archive.writestr("test.xml", "<test/>")
        self.wb = book([formula("B2", "=A2+42")], self.path)

    def test_missing_and_mismatched_dependency_are_visible(self):
        with patch.object(mod.importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError):
            result = mod.run_supplemental_audit(self.wb)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["summary"]["reason"], "dependency_missing")
        with patch.object(mod.importlib.metadata, "version", return_value="9.9"):
            self.assertEqual(mod.run_supplemental_audit(self.wb)["summary"]["reason"], "dependency_version_mismatch")

    def test_copy_is_readonly_recalc_disabled_original_preserved(self):
        original = self.path.read_bytes()
        def child(source, config, cwd, cancel):
            self.assertNotEqual(source, self.path)
            self.assertEqual(source.stat().st_mode & 0o222, 0)
            self.assertEqual(source.read_bytes(), original)
            settings = json.loads(config.read_text())
            self.assertFalse(settings["recalc"]["enabled"])
            self.assertFalse(settings["finance"]["enabled"])
            p = payload([finding("FORMULA_DRIFT", "B2", self.wb)])
            p["workbook"]["sha256"] = mod._hash(source)
            return "ok", json.dumps(p).encode()
        with patch.object(mod.importlib.metadata, "version", return_value="0.4.0"), patch.object(mod, "_execute", side_effect=child):
            result = mod.run_supplemental_audit(self.wb)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.path.read_bytes(), original)

    def test_timeout_bad_json_and_source_change_discard_results(self):
        for outcome, data, expected in [("timeout", b"", "timeout"), ("ok", b"{bad", "failed"), ("output_limit", b"", "failed")]:
            with self.subTest(outcome=outcome), patch.object(mod.importlib.metadata, "version", return_value="0.4.0"), patch.object(mod, "_execute", return_value=(outcome, data)):
                self.assertEqual(mod.run_supplemental_audit(self.wb)["status"], expected)
        def modify_copy(source, *_):
            source.chmod(0o600)
            source.write_bytes(b"changed")
            return "ok", b"{}"
        with patch.object(mod.importlib.metadata, "version", return_value="0.4.0"), patch.object(mod, "_execute", side_effect=modify_copy):
            self.assertEqual(mod.run_supplemental_audit(self.wb)["status"], "failed")

    def test_unknown_source_and_size_limit_never_start_child(self):
        with patch.object(mod.importlib.metadata, "version", return_value="0.4.0"), patch.object(mod, "_execute") as child:
            self.wb.path = self.directory / "missing.xlsx"
            self.assertEqual(mod.run_supplemental_audit(self.wb)["status"], "failed")
            self.wb.path = self.path
            with patch.object(mod, "MAX_FILE_BYTES", 1):
                self.assertEqual(mod.run_supplemental_audit(self.wb)["status"], "failed")
            child.assert_not_called()

    def runner(self, source):
        path = self.directory / "runner.py"
        path.write_text(source)
        return path

    def test_real_child_gets_clean_environment_and_no_home_override(self):
        runner = self.runner("import os,json; print(json.dumps(dict(os.environ)))")
        with patch.object(mod, "RUNNER", runner), patch.dict(os.environ, {"SUPPLEMENTAL_SECRET_TEST": "should-not-leak"}):
            status, raw = mod._execute(self.path, self.path, self.directory, None)
        self.assertEqual(status, "ok")
        self.assertNotIn("SUPPLEMENTAL_SECRET_TEST", json.loads(raw))
        self.assertNotIn("HOME", json.loads(raw))

    def test_real_child_timeout_output_bound_and_cancel(self):
        runner = self.runner("import time; time.sleep(5)")
        with patch.object(mod, "RUNNER", runner), patch.object(mod, "WALL_SECONDS", 0.1):
            self.assertEqual(mod._execute(self.path, self.path, self.directory, None)[0], "timeout")
        runner = self.runner("print('x'*100000)")
        with patch.object(mod, "RUNNER", runner), patch.object(mod, "MAX_OUTPUT_BYTES", 1024):
            self.assertEqual(mod._execute(self.path, self.path, self.directory, None)[0], "output_limit")
        runner = self.runner("import time; time.sleep(5)")
        with patch.object(mod, "RUNNER", runner):
            self.assertEqual(mod._execute(self.path, self.path, self.directory, lambda: True)[0], "cancelled")
        self.assertEqual(mod.run_supplemental_audit(self.wb, cancel_check=lambda: True)["summary"]["reason"], "cancelled")


if __name__ == "__main__":
    unittest.main()
