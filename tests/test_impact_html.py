import copy
from html.parser import HTMLParser
import math
import unittest

from finmodel_audit.impact_html import _impact_analysis_html, _scenario_issue
from finmodel_audit.report import render_html


def ref(cell, sheet="DCF"):
    return {"sheet": sheet, "cell": cell}


def scenario(**overrides):
    result = {
        "id": "conditional-1", "title": "Убрать одно повторное слагаемое", "assumption": "Одно вхождение исключено только из суммы.",
        "target": ref("C9"), "raw_unit": "RUB M", "baseline": 100, "saved_output": 100,
        "baseline_cache_difference": 0, "result": 70, "delta": -30, "relative_delta": -.3,
        "patches": [{**ref("C8"), "original_formula": "=A1+B1+B1", "changed_formula": "=A1+B1", "excluded": [ref("B1")]}],
        "formula_path": [{**ref("C8"), "formula": "=A1+B1+B1"}, {**ref("C9"), "formula": "=C8"}],
        "source_inputs": [{**ref("A1"), "value": 40, "frozen": True}, {**ref("B1"), "value": 30, "frozen": True}],
        "evidence": [ref(value) for value in ("A1", "B1", "C8", "C9")], "conditional": True,
        "recalculated_workbook": False, "status": "calculated", "limitations": ["Другие вхождения потока сохранены."],
    }
    result.update(overrides)
    return result


class ParsedHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags, self.text, self.attrs = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


class ImpactHTMLTests(unittest.TestCase):
    def test_conditional_table_has_exact_formula_sources_and_boundaries(self):
        item = scenario()
        before = copy.deepcopy(item)
        html = _impact_analysis_html({"status": "limited", "scenarios": [item], "limitations": ["Раздельные сценарии."]})
        self.assertIn("<td>100</td><td>70</td><td>-30</td><td>RUB M</td>", html)
        for value in ("=A1+B1", "DCF!C9", "DCF!A1", "-30%", "Эффекты отдельных сценариев не складываются", "Книга Excel целиком не пересчитывалась"):
            self.assertIn(value, html)
        self.assertIn("<caption>Отдельные условные сценарии</caption>", html)
        self.assertIn("scope='col'", html)
        self.assertIn("scope='row'", html)
        self.assertIn("tabindex='0'", html)
        self.assertGreater(html.index("<details class='check-calculation'>"), html.index("</table></div>"))
        self.assertIn("<small style='display:block;margin-top:4px'>" + item["title"], html)
        self.assertEqual(item, before)

    def test_negative_base_absolute_denominator_and_zero_base(self):
        negative = _impact_analysis_html({"scenarios": [scenario(baseline=-100, saved_output=-100, result=-70, delta=30, relative_delta=.3)]})
        self.assertIn("<td>+30</td>", negative)
        self.assertIn("+30%", negative)
        zero = _impact_analysis_html({"scenarios": [scenario(baseline=0, saved_output=0, result=-10, delta=-10, relative_delta=None)]})
        self.assertIn("исходная база равна нулю", zero)
        self.assertNotIn("0%", "".join(ParsedHTML(zero).text))
        self.assertIn("<td>0</td><td>-10</td><td>-10</td>", zero)

    def test_raw_percent_unit_does_not_scale_numbers(self):
        html = _impact_analysis_html({"scenarios": [scenario(raw_unit="%", baseline=.08, saved_output=.08, result=.06, delta=-.02, relative_delta=-.25)]})
        self.assertIn("<td>0.08</td><td>0.06</td><td>-0.02</td><td>%</td>", html)

    def test_invalid_arithmetic_and_missing_proof_hide_numbers(self):
        variants = [{"delta": 999999}, {"relative_delta": .8}, {"result": math.inf}, {"result": math.nan}, {"saved_output": None},
                    {"baseline_cache_difference": 7}, {"baseline": True}, {"conditional": False}, {"recalculated_workbook": True},
                    {"source_inputs": []}, {"target": None}, {"evidence": [ref("C9")]},
                    {"patches": [{**ref("C8"), "original_formula": "=A1+B1", "changed_formula": "=A1", "excluded": [ref("Z99")]}]},
                    {"patches": [{**ref("C8"), "original_formula": "=A1+B1", "changed_formula": "=A1", "scenario_formula": "=B1"}]}]
        for override in variants:
            with self.subTest(override=override):
                item = scenario(**override)
                self.assertIsNotNone(_scenario_issue(item))
                html = _impact_analysis_html({"scenarios": [item]})
                self.assertIn("colspan='5'>Расчёт не показан", html)
                self.assertNotIn("999999", html)
                self.assertNotIn("<pre>", html)
        self.assertIsNotNone(_scenario_issue(scenario(baseline=0, saved_output=0, result=0, delta=0, relative_delta=0)))

    def test_injection_escaped_in_all_public_text_without_metadata_dump(self):
        attack = '<img src=x onerror="alert(1)">'
        item = scenario(title=attack, assumption=attack, raw_unit=attack, limitations=[attack], private_debug="DO NOT DISPLAY")
        for value in [item["target"], *item["patches"], *item["formula_path"], *item["source_inputs"], *item["evidence"], *item["patches"][0]["excluded"]]:
            value["sheet"] = attack
        item["patches"][0]["changed_formula"] = attack
        item["formula_path"][0]["formula"] = attack
        html = _impact_analysis_html({"scenarios": [item], "limitations": [attack]})
        parsed = ParsedHTML(html)
        self.assertNotIn("img", parsed.tags)
        self.assertNotIn("script", parsed.tags)
        self.assertIn(attack, "".join(parsed.text))
        self.assertFalse(any("onerror" in attrs for attrs in parsed.attrs))
        self.assertNotIn("DO NOT DISPLAY", html)
        self.assertNotIn("Расчёт не показан", html)

    def test_unsupported_is_readable_and_legacy_absence_is_empty(self):
        html = _impact_analysis_html({"status": "unsupported", "scenarios": [], "checks": [
            {"status": "unsupported", "reason": "baseline_intermediate_cache_mismatch", "target": ref("C9")},
            {"status": "unsupported", "reason": "internal_unknown_code"}], "limitations": ["Нет надёжной базы."]})
        self.assertIn("Промежуточный расчёт не совпадает с сохранёнными значениями", html)
        self.assertIn("Поддержанные условные сценарии не представлены", html)
        self.assertNotIn("internal_unknown_code", html)
        self.assertNotIn("<table", html)
        self.assertEqual(_impact_analysis_html(None), "")
        self.assertEqual(_impact_analysis_html([]), "")

    def test_export_integration_is_visible_outside_evidence_appendix_and_legacy_optional(self):
        report = {"schema_version": 2, "source": "Example.xlsx", "stages": [], "summary": {},
                  "impact_analysis": {"status": "limited", "scenarios": [scenario()]}}
        html = render_html(report)
        self.assertEqual(html.count("id='conditional-impact'"), 1)
        self.assertIn("href='#conditional-impact'", html)
        self.assertLess(html.index("<section id='conditional-impact'"), html.index("<details id='evidence-appendix'"))
        old = {"source": "Old.xlsx", "summary": {}, "findings": [], "limitations": []}
        self.assertNotIn("Условное влияние на оценку", render_html(old))
        old["impact_analysis"] = report["impact_analysis"]
        self.assertIn("id='conditional-impact'", render_html(old))


if __name__ == "__main__":
    unittest.main()
