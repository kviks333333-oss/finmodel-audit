"""Offline provenance/scope contract; no workbook, network or provider needed."""
from copy import deepcopy
import json
import unittest

from finmodel_audit.methodology import MethodologyError, build_methodology_report, methodology_schema, validate_candidate_coverage


def ref(cell, sheet="Model"):
    return {"sheet": sheet, "cell": cell}


def source(cell, value, formula=None, *, present=True, sheet="Model"):
    return {**ref(cell, sheet), "value": value, "formula": formula, "present": present}


def finding_registry():
    finding = {"id": "financial_repeat", "scope": "financial", "code": "pv_fcff_duplicate_term",
               "status": "candidate", "title": "Повторённое слагаемое", "detail": "В исходной сумме повторён один поток.",
               "target": ref("D4"), "evidence": [ref("D4"), ref("D2"), ref("D3")],
               "source_features": [{**ref("D4"), "formula_type": "array", "formula_attributes": {"ref": "D4:D5"}}],
               "calculation": {"label": "Сохранённые слагаемые", "expression": "x + x", "value": 20,
                               "inputs": [ref("D2")], "basis": "saved_values", "recalculated": False,
                               "limitations": ["Не финансовое влияние."]}, "financial_materiality": "not_assessed"}
    return {finding["id"]: {"finding": finding,
                           "cell_evidence": [source("D4", 40, "=D2+D3+D2"), source("D2", 10), source("D3", 20)],
                           "limitations": ["Проверено только данное выражение."]}}


def payload(stage="audit", findings=None, hypotheses=None):
    return {"id": stage, "finding_ids": ["financial_repeat"] if findings is None else findings,
            "hypotheses": hypotheses or [], "limitations": []}


def hypothesis(**changes):
    return {"title": "Состав денежного потока", "detail": "Возможно повторное включение компонента.",
            "target": ref("D4"), "tool_evidence_ids": ["trace-proof", "calc-proof"], "limitations": [], **changes}


def tool_registry():
    columns = ["cell", "saved_value", "formula", "kind"]
    return {
        "trace-proof": {"tool": "trace_precedents", "arguments": {**ref("D4"), "depth": 1}, "result": {
            "metadata": {"evidence_id": "trace-proof", "root": ref("D4"), "direction": "precedents", "depth": 1,
                         "edges": [{"from": ref("D4"), "to": ref("D2")}, {"from": ref("D4"), "to": ref("D3")}],
                         "completeness": {"complete": True, "scope": "static_explicit_populated_references", "reasons": []}},
            "cell_columns": columns, "cell_data": {"Model": [["D4", 40, "=D2+D3+D2", "formula_number"],
                                                              ["D2", 10, None, "number"], ["D3", 20, None, "number"]]}, "truncated": False}},
        "calc-proof": {"tool": "calculate_cells", "arguments": {"expression": "x + x", "inputs": [{"name": "x", **ref("D2")}]}, "result": {
            "metadata": {"evidence_id": "calc-proof", "expression": "x + x", "result": {"value": 20},
                         "evidence": [{"name": "x", **ref("D2")}], "basis": "saved_values", "recalculated": False,
                         "completeness": {"complete": True, "reasons": []}},
            "cell_columns": columns, "cell_data": {"Model": [["D2", 10, None, "number"]]}, "truncated": False}},
    }


class MethodologyTests(unittest.TestCase):
    def setUp(self):
        self.retrieved = {("Model", cell) for cell in ("D2", "D3", "D4")}

    def assert_code(self, expected, action):
        with self.assertRaises(MethodologyError) as caught:
            action()
        self.assertEqual(caught.exception.issues[0]["code"], expected)

    def test_schema_has_no_summary_status_or_free_units_and_describes_every_property(self):
        schema = methodology_schema("audit")
        self.assertEqual(schema["name"], "submit_stage_report")
        self.assertEqual(set(schema["parameters"]["properties"]), {"id", "finding_ids", "hypotheses", "limitations"})
        def walk(value):
            if isinstance(value, dict):
                for prop in value.get("properties", {}).values():
                    self.assertTrue(prop.get("description"), prop)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(schema)
        self.assert_code("unsupported_stage", lambda: methodology_schema("business"))

    def test_finding_preserves_target_formula_all_sources_calculation_and_provenance(self):
        registry = finding_registry()
        before = deepcopy(registry)
        report = build_methodology_report(payload(), "audit", registry, self.retrieved)
        item = report["observations"][0]
        self.assertEqual(item["status"], "hypothesis")
        self.assertEqual(item["impact"], "not_assessed")
        self.assertEqual({(x["sheet"], x["cell"]) for x in item["evidence"]}, self.retrieved)
        proof = item["methodology"]
        self.assertEqual(proof["original_finding"], registry["financial_repeat"]["finding"])
        self.assertEqual(proof["source_cells"][0]["formula"], "=D2+D3+D2")
        self.assertEqual(proof["calculation"]["inputs"][0]["value"], 10)
        self.assertEqual(proof["calculation"]["value"], 20)
        self.assertFalse(proof["scope"]["all_periods"])
        self.assertEqual(proof["origin"], "deterministic_finding")
        self.assertIn("не самостоятельное обнаружение", item["detail"])
        self.assertEqual(registry, before)
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)

    def test_omitted_target_formula_cell_and_unread_calculation_source_are_rejected(self):
        registry = finding_registry()
        registry["financial_repeat"]["cell_evidence"] = registry["financial_repeat"]["cell_evidence"][1:]
        self.assert_code("missing_proof_source", lambda: build_methodology_report(payload(), "audit", registry, self.retrieved))
        self.assert_code("unread_source", lambda: build_methodology_report(payload(), "audit", finding_registry(), self.retrieved - {("Model", "D2")}))
        registry = finding_registry()
        registry["financial_repeat"]["cell_evidence"][0]["formula_truncated"] = True
        self.assert_code("truncated_source", lambda: build_methodology_report(payload(), "audit", registry, self.retrieved))

    def test_detector_status_cannot_be_overridden_and_quality_never_declares_suitability(self):
        for illegal in ({"summary": "Модель пригодна"}, {"status": "observation"}, {"units": "%"}):
            self.assert_code("invalid_methodology_selection", lambda: build_methodology_report({**payload(), **illegal}, "audit", finding_registry(), self.retrieved))
        report = build_methodology_report(payload("quality"), "quality", finding_registry(), self.retrieved)
        self.assertEqual(report["status"], "not_verified")
        self.assertIn("Пригодность модели для принятия решений не установлена", report["summary"])
        registry = finding_registry()
        registry["financial_repeat"]["finding"]["status"] = "observation"
        self.assertEqual(build_methodology_report(payload(), "audit", registry, self.retrieved)["observations"][0]["status"], "observation")

    def test_unknown_duplicate_oversized_ids_and_unknown_limits_are_rejected(self):
        cases = [(payload(findings=["not-delivered"]), "unknown_finding_id"),
                 (payload(findings=["financial_repeat", "financial_repeat"]), "invalid_selection"),
                 (payload(findings=[str(i) for i in range(13)]), "invalid_selection"),
                 ({**payload(), "limitations": ["all_periods_verified"]}, "unknown_limitation")]
        for value, code in cases:
            with self.subTest(code=code):
                self.assert_code(code, lambda: build_methodology_report(value, "audit", finding_registry(), self.retrieved))

    def test_scope_qualified_collisions_are_explicit_and_cannot_duplicate_one_finding(self):
        registry = finding_registry()
        first = deepcopy(registry["financial_repeat"])
        other = deepcopy(first)
        other["finding"]["scope"] = "supplemental"
        other["finding"]["detail"] = "Отдельный источник кандидата."
        registry.update({"financial:financial_repeat": first, "supplemental:financial_repeat": other})
        self.assert_code("ambiguous_finding_id", lambda: build_methodology_report(payload(), "audit", registry, self.retrieved))
        report = build_methodology_report(payload(findings=["financial:financial_repeat"]), "audit", registry, self.retrieved)
        self.assertEqual(report["observations"][0]["finding_id"], "financial:financial_repeat")
        registry.pop("supplemental:financial_repeat")
        self.assert_code("duplicate_finding", lambda: build_methodology_report(payload(findings=["financial_repeat", "financial:financial_repeat"]), "audit", registry, self.retrieved))

    def test_finite_calculation_matches_source_cache_and_does_not_accept_metadata_as_number(self):
        registry = finding_registry()
        registry["financial_repeat"]["finding"]["calculation"]["inputs"][0]["value"] = 999
        self.assert_code("calculation_source_mismatch", lambda: build_methodology_report(payload(), "audit", registry, self.retrieved))
        registry = finding_registry()
        registry["financial_repeat"]["finding"]["calculation"]["value"] = float("nan")
        self.assert_code("invalid_registry_entry", lambda: build_methodology_report(payload(), "audit", registry, self.retrieved))

    def test_own_hypothesis_links_formula_trace_and_calculation_but_stays_hypothesis(self):
        registry = tool_registry()
        before = deepcopy(registry)
        report = build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved, tool_registry=registry)
        item = report["observations"][0]
        proof = item["methodology"]
        self.assertEqual(item["status"], "hypothesis")
        self.assertEqual(proof["verification"], "tool_backed_hypothesis")
        self.assertEqual(proof["missing_evidence"], [])
        self.assertEqual(proof["scope"]["target"], ref("D4"))
        self.assertEqual(len(proof["tool_evidence"]), 2)
        self.assertFalse(proof["scope"]["all_periods"])
        self.assertIn("Непроверенное предположение агента", item["detail"])
        self.assertNotIn(hypothesis()["detail"], report["summary"])
        self.assertEqual(registry, before)

    def test_no_proof_or_incomplete_trace_is_not_verified_with_exact_missing_reasons(self):
        item = build_methodology_report(payload(findings=[], hypotheses=[hypothesis(tool_evidence_ids=[])]), "audit", {}, self.retrieved)["observations"][0]
        self.assertEqual(item["status"], "not_verified")
        self.assertEqual(set(item["methodology"]["missing_evidence"]), {"target_formula_not_delivered", "target_precedents_not_traced", "linked_calculation_not_delivered"})
        registry = tool_registry()
        registry["trace-proof"]["result"]["metadata"]["completeness"] = {"complete": False, "reasons": ["depth_limit"]}
        item = build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved, tool_registry=registry)["observations"][0]
        self.assertEqual(item["status"], "not_verified")
        self.assertIn("tool_scope_incomplete", item["methodology"]["missing_evidence"])
        self.assertEqual(item["methodology"]["tool_evidence"][0]["result"]["metadata"]["completeness"]["reasons"], ["depth_limit"])

    def test_unrelated_calculation_does_not_substitute_for_target_dependency_evidence(self):
        registry = tool_registry()
        registry["calc-proof"]["arguments"]["inputs"][0]["cell"] = "J8"
        registry["calc-proof"]["result"]["metadata"]["evidence"][0]["cell"] = "J8"
        registry["calc-proof"]["result"]["cell_data"]["Model"][0][0] = "J8"
        item = build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved | {("Model", "J8")}, tool_registry=registry)["observations"][0]
        self.assertEqual(item["status"], "not_verified")
        self.assertIn("calculation_outside_target_dependencies", item["methodology"]["missing_evidence"])

    def test_undisclosed_tool_ids_missing_rows_and_forged_expression_are_rejected(self):
        self.assert_code("unknown_tool_evidence_id", lambda: build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved))
        registry = tool_registry()
        registry["trace-proof"]["result"]["cell_data"]["Model"].pop()
        self.assert_code("missing_proof_source", lambda: build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved, tool_registry=registry))
        registry = tool_registry()
        registry["calc-proof"]["arguments"]["expression"] = "x * imaginary"
        self.assert_code("invalid_calculation_proof", lambda: build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved, tool_registry=registry))

    def test_hypothesis_cannot_forge_status_or_put_numbers_in_free_prose(self):
        for changes, code in [({"status": "observation"}, "invalid_hypothesis"),
                              ({"detail": "Эффект равен 9999."}, "numeric_hypothesis_text"),
                              ({"title": "Ошибка в D4"}, "numeric_hypothesis_text"),
                              ({"detail": "Прогноз вырос на ²."}, "numeric_hypothesis_text")]:
            with self.subTest(changes=changes):
                self.assert_code(code, lambda: build_methodology_report(payload(findings=[], hypotheses=[hypothesis(**changes)]), "audit", {}, self.retrieved, tool_registry=tool_registry()))
        claim = hypothesis(detail="Все периоды модели корректны.")
        report = build_methodology_report(payload(findings=[], hypotheses=[claim]), "audit", {}, self.retrieved, tool_registry=tool_registry())
        item = report["observations"][0]
        self.assertEqual(item["status"], "hypothesis")
        self.assertNotIn(claim["detail"], report["summary"])
        self.assertFalse(item["methodology"]["scope"]["all_periods"])
        self.assertIn("Это текст гипотезы, а не установленный финансовый вывод", item["detail"])

    def test_source_injection_remains_literal_data_and_empty_selection_is_not_verified(self):
        registry = finding_registry()
        attack = '<script>alert("source")</script>'
        registry["financial_repeat"]["finding"]["title"] = attack
        registry["financial_repeat"]["cell_evidence"][0]["formula"] = "=" + attack
        item = build_methodology_report(payload(), "audit", registry, self.retrieved)["observations"][0]
        self.assertEqual(item["title"], attack)
        self.assertEqual(item["methodology"]["source_cells"][0]["formula"], "=" + attack)
        report = build_methodology_report(payload(findings=[]), "audit", {}, set())
        self.assertEqual(report["status"], "not_verified")
        self.assertEqual(report["observations"], [])

    def test_mixed_proofs_cannot_splice_different_source_snapshots(self):
        registry = tool_registry()
        registry["calc-proof"]["result"]["cell_data"]["Model"][0][1] = 999
        self.assert_code("conflicting_source", lambda: build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved, tool_registry=registry))

    def test_tool_argument_a1_normalization_matches_tools_without_changing_source_names(self):
        registry = tool_registry()
        registry["trace-proof"]["arguments"]["cell"] = "$d$4"
        registry["calc-proof"]["arguments"]["inputs"][0]["cell"] = "d2"
        item = build_methodology_report(payload(findings=[], hypotheses=[hypothesis()]), "audit", {}, self.retrieved, tool_registry=registry)["observations"][0]
        self.assertEqual(item["status"], "hypothesis")
        self.assertEqual(item["methodology"]["scope"]["target"], ref("D4"))
        self.assertEqual(item["methodology"]["tool_evidence"][0]["arguments"]["cell"], "$d$4")

    def test_candidate_coverage_requires_themes_not_duplicate_finding_count(self):
        registry = finding_registry()
        for identity, code in (("financial_repeat_other", "pv_fcff_duplicate_term"),
                               ("financial_irr", "irr_on_discounted_cashflows")):
            registry[identity] = deepcopy(registry["financial_repeat"])
            registry[identity]["finding"].update(id=identity, code=code)
        before = deepcopy(registry)
        chosen = payload(findings=["financial_repeat", "financial_repeat_other"])
        with self.assertRaises(MethodologyError) as caught:
            validate_candidate_coverage(chosen, registry)
        self.assertEqual(len(caught.exception.issues), 1)
        issue = caught.exception.issues[0]
        self.assertEqual(issue["code"], "missing_candidate_theme")
        self.assertEqual(issue["theme_code"], "irr_on_discounted_cashflows")
        self.assertEqual(issue["candidate_ids"], ["financial_irr"])
        self.assertIn("title", issue)
        self.assertNotIn("evidence", issue)
        self.assertNotIn("calculation", issue)
        result = validate_candidate_coverage(payload(findings=["financial_repeat_other", "financial_irr"]), registry)
        self.assertEqual(result["required_theme_count"], 2)
        self.assertEqual(result["selected_theme_count"], 2)
        self.assertFalse(result["financial_verification"])
        self.assertEqual(registry, before)

    def test_candidate_coverage_ignores_supplemental_conflicts_and_uncoded_entries(self):
        registry = finding_registry()
        for identity, fields in (("extra", {"scope": "supplemental", "code": "supplemental_only"}),
                                 ("uncoded", {"code": None}), ("emptycode", {"code": ""})):
            registry[identity] = deepcopy(registry["financial_repeat"])
            registry[identity]["finding"].update(id=identity, **fields)
        registry["collision"] = {"conflict": True}
        registry["also_collision"] = {"conflict": True, "finding": {"id": "also_collision", "scope": "financial", "code": "unusable_theme"}}
        result = validate_candidate_coverage(payload(), registry)
        self.assertEqual(result["required_codes"], ["pv_fcff_duplicate_term"])
        self.assertEqual(result["selected_codes"], result["required_codes"])

    def test_candidate_coverage_requires_only_delivered_themes_and_accepts_one_per_code(self):
        registry = finding_registry()
        # No global detector catalogue or unseen IRR finding is consulted.
        result = validate_candidate_coverage(payload(), registry)
        self.assertEqual(result["required_theme_count"], 1)
        empty = validate_candidate_coverage(payload(findings=[]), {})
        self.assertEqual(empty["required_theme_count"], 0)
        registry["financial:financial_repeat"] = registry.pop("financial_repeat")
        result = validate_candidate_coverage(payload(findings=["financial:financial_repeat"]), registry)
        self.assertEqual(result["selected_theme_count"], 1)
        self.assert_code("missing_candidate_theme", lambda: validate_candidate_coverage(payload(findings=["pv_fcff_duplicate_term"]), registry))

    def test_candidate_coverage_is_opt_in_and_does_not_change_direct_report_selection(self):
        registry = finding_registry()
        registry["other_theme"] = deepcopy(registry["financial_repeat"])
        registry["other_theme"]["finding"].update(id="other_theme", code="gordon_double_discount")
        selected = payload(findings=["financial_repeat"])
        report = build_methodology_report(selected, "audit", registry, self.retrieved)
        self.assertEqual(len(report["observations"]), 1)
        self.assertEqual(report["observations"][0]["status"], "hypothesis")
        self.assertEqual(report["selection"], selected)
        self.assert_code("missing_candidate_theme", lambda: validate_candidate_coverage(selected, registry))


if __name__ == "__main__":
    unittest.main()
