"""Offline tests for source-only selection and deterministic financial arithmetic."""
import copy
from html.parser import HTMLParser
import json
import math
import unittest

from finmodel_audit.fact_report import FactReportError, build_fact_report, fact_report_schema, suggest_comparisons
from finmodel_audit.report import render_html


def card(identity, value, period, column, **overrides):
    result = {"id": identity, "source_sheet": "Financials O'Brien", "value_cell": column + "18", "value": value,
              "formula": None, "metric_label": "Revenue, total", "metric_cell": "C18", "metric_key": "revenue",
              "period": period, "period_cell": column + "8", "unit": "RUB M", "unit_cell": "G18",
              "period_type": None, "period_type_cell": None, "binding_status": "bound", "issues": []}
    result.update(overrides)
    return result


def fixtures(start=100, end=400):
    cards = [card("start", start, 2019, "K"), card("end", end, 2031, "W")]
    return {item["id"]: item for item in cards}


def retrieved(registry):
    result = set()
    for item in registry.values():
        for field in ("value_cell", "metric_cell", "period_cell", "unit_cell", "period_type_cell"):
            if item.get(field):
                result.add((item["source_sheet"], item[field]))
        for issue in item["issues"]:
            result.update((item["source_sheet"], source) for source in issue.get("source_cells", []))
    return result


def selection(*kinds, ids=None, stage="business"):
    return {"id": stage, "fact_ids": ["start", "end"] if ids is None else ids,
            "comparisons": [{"start_fact_id": "start", "end_fact_id": "end", "kind": kind} for kind in kinds],
            "limitations": []}


class FactReportTests(unittest.TestCase):
    def build(self, payload=None, registry=None, ledger=None):
        registry = fixtures() if registry is None else registry
        return build_fact_report(selection() if payload is None else payload, (payload or {}).get("id", "business"),
                                 registry, retrieved(registry) if ledger is None else ledger)

    def error(self, code, payload=None, registry=None, ledger=None):
        with self.assertRaises(FactReportError) as caught:
            self.build(payload, registry, ledger)
        self.assertEqual(caught.exception.issues[0]["code"], code)
        return caught.exception.issues[0]

    def test_schema_requires_only_selection_and_known_limitations(self):
        for stage in ("business", "assumptions"):
            schema = fact_report_schema(stage)
            properties = schema["parameters"]["properties"]
            self.assertEqual(schema["name"], "submit_stage_report")
            self.assertEqual(set(properties), {"id", "fact_ids", "comparisons", "limitations"})
            self.assertFalse(schema["parameters"]["additionalProperties"])
            self.assertEqual(properties["fact_ids"]["maxItems"], 24)
            self.assertEqual(properties["comparisons"]["maxItems"], 6)
            self.assertFalse(properties["comparisons"]["items"]["additionalProperties"])
            self.assertEqual(properties["limitations"]["items"]["enum"][0], "no_full_recalculation")
            json.dumps(schema, allow_nan=False)
        with self.assertRaises(FactReportError):
            fact_report_schema("audit")

    def test_suggestions_use_selected_earliest_latest_and_prioritize_business_metrics(self):
        registry = fixtures()
        registry["middle"] = card("middle", 150, 2025, "Q")
        for name, key, row in (("profit", "ebitda", 39), ("flow", "fcff", 27)):
            registry[name + "0"] = card(name + "0", 40, 2019, "K", metric_key=key, metric_label=name,
                                          metric_cell=f"C{row}", value_cell=f"K{row}")
            registry[name + "1"] = card(name + "1", 80, 2031, "W", metric_key=key, metric_label=name,
                                          metric_cell=f"C{row}", value_cell=f"W{row}")
        original = copy.deepcopy(registry)
        suggestions = suggest_comparisons(registry, list(reversed(registry)))
        self.assertEqual([(x["start_fact_id"], x["end_fact_id"], x["kind"]) for x in suggestions], [
            ("start", "end", "total_growth"), ("start", "end", "cagr"),
            ("profit0", "profit1", "total_growth"), ("profit0", "profit1", "cagr")])
        payload = selection(); payload.update(fact_ids=list(registry), comparisons=suggestions)
        self.build(payload, registry)
        self.assertEqual(registry, original)
        self.assertEqual(len(suggest_comparisons(registry, list(registry), limit=3)), 2)
        self.assertEqual(suggest_comparisons(registry, list(registry), limit=1), [])

    def test_suggestions_do_not_use_unselected_cards_or_cross_context(self):
        registry = fixtures()
        self.assertEqual(suggest_comparisons(registry, ["start"]), [])
        for changes in ({"source_sheet": "Other entity"}, {"unit": "USD M"},
                        {"unit": " RUB M"}, {"period": None, "period_cell": None, "binding_status": "partial"},
                        {"period": "2031-Q4"}, {"value": 0}, {"value": -1}, {"value": float("nan")}):
            changed = copy.deepcopy(registry); changed["end"].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(suggest_comparisons(changed, ["start", "end"]), [])
        for changes in ({"metric_key": "wacc"}, {"metric_key": "unknown"}, {"unit": "%"}, {"unit": "x"}):
            changed = copy.deepcopy(registry)
            for value in changed.values(): value.update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(suggest_comparisons(changed, ["start", "end"]), [])

    def test_suggestions_skip_ambiguous_endpoints_and_leave_build_selection_explicit(self):
        registry = fixtures()
        registry["another_end"] = card("another_end", 800, 2031, "V")
        self.assertEqual(suggest_comparisons(registry, list(registry)), [])
        self.assertEqual(len(suggest_comparisons(registry, ["start", "end"])), 2)
        report = self.build(selection(), registry)
        self.assertFalse(any("comparison" in observation for observation in report["observations"]))
        self.assertEqual(report["selection"]["comparisons"], [])
        registry = fixtures(-100, 400)
        registry["middle"] = card("middle", 200, 2025, "Q")
        self.assertEqual(suggest_comparisons(registry, list(registry)), [])

    def test_total_growth_is_distinct_from_cagr_over_twelve_years(self):
        result = self.build(selection("total_growth", "cagr", "difference"))
        growth, cagr, difference = (item["comparison"] for item in result["observations"] if "comparison" in item)
        self.assertEqual(growth["result"], {"value": 3.0, "unit": "fraction"})
        self.assertEqual(growth["display"], {"value": 300.0, "unit": "%"})
        self.assertAlmostEqual(cagr["result"]["value"], 4 ** (1 / 12) - 1, places=14)
        self.assertAlmostEqual(cagr["display"]["value"], (4 ** (1 / 12) - 1) * 100)
        self.assertEqual(cagr["display"]["unit"], "% в год")
        self.assertEqual(cagr["periods"], {"start": 2019, "end": 2031, "years": 12})
        self.assertEqual(difference["result"], {"value": 300, "unit": "RUB M"})
        for calculation in (growth, cagr, difference):
            self.assertFalse(calculation["recalculated"])
            self.assertEqual(calculation["basis"], "saved_values")
            self.assertEqual([item["value"] for item in calculation["inputs"]], [100, 400])
            self.assertEqual({(x["sheet"], x["cell"]) for x in calculation["evidence"]}, retrieved(fixtures()))
        self.assertIn("Общий относительный рост", result["observations"][2]["detail"])
        self.assertIn("Среднегодовой", result["observations"][3]["detail"])

    def test_percent_labels_do_not_rescale_raw_values_or_invent_percentage_points(self):
        registry = fixtures(.087, .04)
        for item in registry.values():
            item.update(unit="%", metric_label="Inflation", metric_key="inflation")
        report = self.build(selection("difference", "total_growth"), registry)
        self.assertIn("сохранённое значение 0.087", report["observations"][0]["detail"])
        difference, growth = report["observations"][2:]
        self.assertAlmostEqual(difference["comparison"]["result"]["value"], -.047)
        self.assertEqual(difference["comparison"]["result"]["unit"], "%")
        self.assertIn("не перевод в процентные пункты", difference["detail"])
        self.assertNotIn("-4.7", difference["detail"])
        self.assertAlmostEqual(growth["comparison"]["display"]["value"], (.04 / .087 - 1) * 100)

    def test_growth_zero_end_is_minus_100_percent_but_cagr_rejects_it(self):
        registry = fixtures(100, 0)
        report = self.build(selection("total_growth"), registry)
        self.assertEqual(report["observations"][-1]["comparison"]["display"]["value"], -100)
        self.error("comparison_nonpositive_value", selection("cagr"), registry)
        for initial, final in ((0, 100), (-1, 100), (100, -1)):
            for kind in ("total_growth", "cagr"):
                with self.subTest(initial=initial, final=final, kind=kind):
                    self.error("comparison_nonpositive_value", selection(kind), fixtures(initial, final))
        report = self.build(selection("difference"), fixtures(-100, 0))
        self.assertEqual(report["observations"][-1]["comparison"]["result"]["value"], 100)

    def test_nonfinite_bool_and_overflow_do_not_enter_reports(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, None, "100"):
            with self.subTest(value=value):
                self.error("unsupported_fact_value", registry=fixtures(value, 100))
        self.error("comparison_numeric_overflow", selection("total_growth"), fixtures(1e-300, 1e300))
        self.error("comparison_numeric_overflow", selection("difference"), fixtures(-1e308, 1e308))
        # Log-domain CAGR is still supported when the endpoint ratio overflows.
        report = self.build(selection("cagr"), fixtures(1e-300, 1e300))
        self.assertTrue(math.isfinite(report["observations"][-1]["comparison"]["result"]["value"]))

    def test_comparison_rejects_wrong_row_entity_or_raw_unit(self):
        for changes, code in (({"source_sheet": "Another company"}, "comparison_metric_mismatch"),
                              ({"metric_cell": "C39"}, "comparison_metric_mismatch"),
                              ({"metric_label": "EBITDA"}, "comparison_metric_mismatch"),
                              ({"unit": "RUB B"}, "comparison_unit_mismatch"),
                              ({"unit": " RUB M"}, "comparison_unit_mismatch")):
            registry = fixtures(); registry["end"].update(changes)
            with self.subTest(changes=changes):
                self.error(code, selection("difference"), registry)

    def test_periods_must_be_bound_chronological_and_years_for_rates(self):
        for end in (2018, 2019):
            registry = fixtures(); registry["end"]["period"] = end
            self.error("comparison_period_order", selection("difference"), registry)
            self.error("comparison_period_order", selection("cagr"), registry)
        registry = fixtures(); registry["end"].update(period=None, period_cell=None, binding_status="partial")
        self.error("comparison_period_unavailable", selection("total_growth"), registry)
        self.error("comparison_period_unavailable", selection("difference"), registry)
        registry = fixtures(); registry["start"]["period"] = "2025-Q1"; registry["end"]["period"] = "2025-Q4"
        self.error("comparison_requires_years", selection("cagr"), registry)
        self.assertEqual(self.build(selection("difference"), registry)["observations"][-1]["comparison"]["periods"]["years"], None)

    def test_partial_cards_are_visible_and_not_verified_without_guessed_context(self):
        registry = fixtures(); registry["start"].update(period=None, period_cell=None, unit=None, unit_cell=None, binding_status="partial")
        result = self.build(selection(ids=["start"]), registry)
        self.assertEqual(result["status"], "not_verified")
        observation = result["observations"][0]
        self.assertEqual(observation["status"], "not_verified")
        self.assertEqual(observation["fact_claims"][0]["verification"], "partial")
        self.assertIn("период: не установлен", observation["detail"])
        self.assertIn("подпись единицы: не установлена", observation["detail"])
        for binding in ("ambiguous", "unsupported"):
            registry["start"]["binding_status"] = binding
            self.error("unsupported_fact_binding", selection(ids=["start"]), registry)

    def test_unread_label_period_unit_and_issue_sources_are_rejected(self):
        registry = fixtures()
        registry["start"]["issues"] = [{"code": "note", "source_cells": ["A99"]}]
        ledger = retrieved(registry)
        for address in ("K18", "C18", "K8", "G18", "A99"):
            with self.subTest(address=address):
                issue = self.error("unread_fact_source", registry=registry, ledger=ledger - {("Financials O'Brien", address)})
                self.assertEqual(issue["cell"], address)

    def test_payload_cannot_forge_values_units_titles_or_free_limitations(self):
        for field in ("summary", "title", "value", "unit", "observations"):
            payload = selection(); payload[field] = "forged"
            self.error("invalid_selection_fields", payload)
        payload = selection("difference"); payload["comparisons"][0]["result"] = 999999
        self.error("invalid_comparison_fields", payload)
        payload = selection(); payload["limitations"] = ["EV is 999999 RUB"]
        self.error("invalid_limitation_codes", payload)
        self.error("unknown_fact_id", selection(ids=["forged"]))
        self.error("invalid_fact_ids", selection(ids=["start", "start"]))
        self.error("invalid_fact_ids", selection(ids=[{}]))
        self.error("no_supported_facts", selection(ids=[]))
        self.error("fact_selection_limit", selection(ids=[str(i) for i in range(25)]))
        payload = selection("difference"); payload["comparisons"] *= 7
        self.error("comparison_limit", payload)
        self.error("duplicate_comparison", selection("difference", "difference"))
        self.error("comparison_endpoint_not_selected", selection("difference", ids=["start"]))

    def test_canonical_output_selection_and_registry_are_not_mutated(self):
        registry = fixtures(); payload = selection("cagr")
        original = copy.deepcopy((registry, payload))
        result = self.build(payload, registry)
        self.assertEqual((registry, payload), original)
        self.assertEqual(result["selection"], payload)
        result["selection"]["fact_ids"].clear()
        self.assertEqual(payload["fact_ids"], ["start", "end"])
        self.assertTrue(all(item["impact"] == "not_assessed" for item in result["observations"]))
        self.assertIn("полный пересчёт Excel не выполнен", result["limitations"][0])
        json.dumps(result, allow_nan=False)

    def test_malicious_source_labels_remain_literal_and_html_renderer_escapes_them(self):
        attack = '<img src=x onerror="alert(1)">'
        registry = fixtures(); registry["start"]["metric_label"] = attack
        result = self.build(selection(ids=["start"]), registry)
        self.assertEqual(result["observations"][0]["title"], attack)
        html = render_html({"schema_version": 2, "source": "synthetic.xlsx", "stages": [result]})
        tags = []
        class Parser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                tags.append(tag)
        Parser().feed(html)
        self.assertNotIn("img", tags)
        self.assertNotIn(attack, html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()
