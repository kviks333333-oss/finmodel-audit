import unittest
from html.parser import HTMLParser
from html import escape
from copy import deepcopy

from finmodel_audit.report import render_html
from finmodel_audit.fact_report import build_fact_report


class AnalysisReportTests(unittest.TestCase):
    def test_valuation_sensitivity_preserves_inputs_pp_changes_and_explicit_refusals(self):
        attack = '<img src=x onerror="alert(1)">'
        report = {"schema_version": 2, "source": "model.xlsx", "stages": [], "valuation_sensitivity": {
            "status": "limited", "scenarios": [{"target": {"sheet": "DCF", "cell": "C39"}, "parameter": "terminal_growth",
                "changed_parameter": {"sheet": "DCF", "cell": "E14", "baseline": .08, "change": .005, "effective_value": .085, "change_unit": "absolute_fraction"},
                "raw_unit": attack, "baseline": 100, "saved_output": 100, "baseline_cache_difference": 0, "result": 120, "delta": 20, "relative_delta": .2,
                "explicit_formula": "=SUM(C29:H29)", "formula_path": [{"sheet": "DCF", "cell": "C39", "formula": attack}],
                "source_inputs": [{"sheet": attack, "cell": "C29", "value": 0, "formula": attack, "frozen": True}],
                "periods": [{"discount_cell": {"sheet": "DCF", "cell": "D28"}, "period_expression": "internal tuple", "value": 1, "evidence": [{"sheet": "DCF", "cell": "D11"}]}],
                "frozen_cashflows": [{"sheet": "DCF", "cell": "C25"}], "effective_parameters": [{"sheet": "DCF", "cell": "E14", "value": .085, "role": "terminal_growth"}],
                "basis": "saved_unaffected_inputs_and_original_formula", "recalculated_workbook": False, "limitations": [attack]}],
            "checks": [{"target": {"sheet": "DCF", "cell": "C50"}, "source_target": {"sheet": "DCF", "cell": "C39"}, "status": "not_applicable", "reason": "alias_of_recognized_output"},
                       {"target": {"sheet": "DCF", "cell": "D50"}, "status": "unsupported", "reason": "ambiguous_or_missing_discount_rate"}],
            "limitations": ["Состав исходных потоков сохранён."]}}
        html = render_html(report)
        for text in ("Чувствительность оценки", "Терминальный рост · +0.5 п.п.", "Выход сценария: <strong>120</strong>", "Изменение выхода: <strong>20</strong>",
                     "=SUM(C29:H29)", "Сохранённое значение: 0 · зафиксировано", "DCF!C25", "DCF!D11", "Период: 1", "ссылка на уже рассмотренный выход",
                     "Ставка дисконтирования отсутствует или неоднозначна", "Книга Excel целиком не пересчитывалась", "&lt;img"):
            self.assertIn(text, html)
        self.assertNotIn(attack, html)
        self.assertNotIn("internal tuple", html)
        report["valuation_sensitivity"] = {"status": "unsupported", "scenarios": [], "checks": [], "limitations": []}
        self.assertIn("Нет поддержанных сценариев", render_html(report))
        del report["valuation_sensitivity"]
        self.assertNotIn("Чувствительность оценки", render_html(report))

    def test_typed_comparison_renders_formula_periods_and_escaped_input_sources(self):
        attack = '<img src=x onerror="alert(1)">'
        report = {"schema_version": 2, "source": "model.xlsx", "stages": [{"id": "business", "title": "Обзор", "summary": "Выбраны факты",
            "observations": [{"title": "Сравнение", "detail": "Разница сохранённых значений", "status": "observation", "impact": "not_assessed", "evidence": [],
                "comparison": {"formula": "end - start", "periods": {"start": 2019, "end": 2031, "years": 12}, "source_unit": "%",
                    "basis": "saved_values", "recalculated": False, "inputs": [{"role": "start", "sheet": attack, "cell": "K18", "value": 0, "period": 2019},
                        {"role": "end", "sheet": "Model", "cell": "W18", "value": .087, "period": 2031}]}}]}]}
        html = render_html(report)
        for text in ("Источники и формула сравнения", "Периоды: 2019 → 2031", "12 лет", "end - start", "Исходная подпись единицы: %",
                     "сохранённое значение: 0", "сохранённое значение: 0.087", "Model!W18", "&lt;img", "Полный пересчёт книги не выполнялся"):
            self.assertIn(text, html)
        self.assertNotIn(attack, html)
        del report["stages"][0]["observations"][0]["comparison"]
        self.assertNotIn("Источники и формула сравнения", render_html(report))

    def test_report_preserves_limitations_and_escapes_every_model_text(self):
        attack = '<script>alert("model")</script>'
        report = {
            "schema_version": 2, "source": attack, "source_sha256": "abc",
            "summary": {"sheet_count": 1, "formula_cells": 2, "cached_errors": 0},
            "coverage": {"retrieved_cells": 3, "total_cells": 4},
            "stages": [{"id": "audit", "title": attack, "status": "limited",
                        "summary": attack, "limitations": [attack],
                        "observations": [{"title": attack, "detail": attack,
                                          "status": "hypothesis", "impact": "not_assessed",
                                          "evidence": [{"sheet": attack, "cell": "B3"}]}]}],
        }
        html = render_html(report)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Гипотеза", html)
        self.assertIn("Не оценено", html)
        self.assertIn("С ограничениями", html)
        self.assertIn("3 из 4", html)
        self.assertIn("!B3", html)

    @staticmethod
    def fact_report():
        return {"schema_version": 2, "source": "model.xlsx", "stages": [
            {"id": "audit", "title": "Аудит расчётов", "summary": "Ранее сохранённый текст"},
            {"id": "business", "title": "Обзор бизнеса", "summary": "Описание бизнеса"}],
            "facts": [{"stage_id": "business", "id": "profit", "source_sheet": "Sample financials",
                       "value_cell": "U37", "value": 1234.5, "formula": "=U18-U30",
                       "metric_label": "Operating profit", "metric_cell": "E37", "metric_key": "operating_profit",
                       "period": 2029, "period_cell": "U8", "unit": "млн руб.", "unit_cell": "E9",
                       "period_type": "forecast", "period_type_cell": "U7", "binding_status": "bound", "issues": []}]}

    def test_fact_cards_keep_source_metric_period_and_stage(self):
        report = self.fact_report()
        report["facts"].append({**report["facts"][0], "stage_id": None, "metric_label": "Orphan card"})
        html = render_html(report)
        self.assertLess(html.index("Обзор бизнеса"), html.index("Operating profit"))
        self.assertEqual(html.count("Данные из модели ·"), 1)
        self.assertNotIn("Orphan card", html)
        for expected in ("Operating profit", "2029", "Прогноз", "млн руб.", "Лист: Sample financials", "Контекст связан", "<th>Сохранённое значение</th>", "<th>Подпись единицы</th>",
                         "Sample financials!U37", "Sample financials!E37", "Sample financials!U8", "Sample financials!E9", "Sample financials!U7"):
            self.assertIn(expected, html)
        self.assertNotIn("EBITDA", html)

    def test_fact_cards_escape_all_raw_fields_and_issue_sources(self):
        report = self.fact_report()
        attack = '<img src=x onerror="alert(1)">'
        fact = report["facts"][0]
        for key in ("source_sheet", "value_cell", "value", "formula", "metric_label", "metric_cell", "period", "period_cell", "unit", "unit_cell", "period_type_cell"):
            fact[key] = attack
        fact["issues"] = [{"code": "unknown", "message": attack, "source_cells": [attack]}]
        html = render_html(report)
        tags = []
        class Tags(HTMLParser):
            def handle_starttag(self, tag, attrs):
                tags.append(tag)
        Tags().feed(html)
        self.assertNotIn("img", tags)
        self.assertNotIn(attack, html)
        self.assertIn("&lt;img", html)
        self.assertIn("&quot;alert(1)&quot;", html)

    def test_missing_and_ambiguous_fact_context_is_not_invented(self):
        report = self.fact_report()
        fact = report["facts"][0]
        fact.update(value=0, period=None, period_cell=None, unit=None, unit_cell=None,
                    period_type=None, period_type_cell=None, binding_status="ambiguous",
                    issues=[{"code": "conflicting_periods", "message": "Two period headers", "source_cells": ["U8", "U9"]}])
        report["facts"].append({**fact, "id": "missing", "value": None, "binding_status": "unsupported", "issues": []})
        html = render_html(report)
        for text in ("Период не определён", "Единица не определена", "Факт/прогноз не определён",
                     "Неоднозначная связь", "Значение не поддерживается", "Нет сохранённого значения", "Sample financials!U9"):
            self.assertIn(text, html)
        self.assertIn("<td>0<small", html)
        self.assertNotIn("2029", html)

    def test_legacy_report_without_facts_has_no_new_empty_section(self):
        report = self.fact_report()
        del report["facts"]
        html = render_html(report)
        self.assertIn("Ранее сохранённый текст", html)
        self.assertNotIn("Данные из модели ·", html)
        self.assertEqual(html, render_html({**report, "facts": []}))
        self.assertNotIn("Финансовые проверки", html)
        self.assertNotIn("Дополнительные проверки Excel", html)

    def test_financial_checks_preserve_candidates_calculations_and_exact_sources(self):
        report = self.fact_report()
        report["financial_checks"] = {"status": "limited", "summary": {"candidate_count": 1, "observation_count": 0, "checked_count": 2},
            "findings": [{"status": "candidate", "title": "Повторное слагаемое", "detail": "Требуется экономическая проверка.",
                "evidence": [{"sheet": "DCF", "cell": "E39"}, {"sheet": "WACC", "cell": "E13"}],
                "calculation": {"label": "Сравнение", "expression": "left-right", "value": 0, "actual": 100, "expected": 100, "difference": 0,
                    "unit": "млн руб.", "basis": "saved_values", "recalculated": False, "inputs": [{"sheet": "DCF", "cell": "E39", "value": 100}], "limitations": ["Смысл потоков не подтверждён."]}}],
            "checks": [{"status": "unsupported", "detail": "Сложное выражение не разобрано."}], "limitations": ["Пересчёта Excel нет."]}
        html = render_html(report)
        for text in ("Финансовые проверки", "Требует проверки", "Выполнены с ограничениями", "Наблюдений: 0", "WACC!E13", "DCF!E39",
                     "Результат выражения: <strong>0</strong>", "Сравниваемое значение", "Смысл потоков не подтверждён.", "Не поддерживается", "Пересчёта Excel нет."):
            self.assertIn(text, html)
        self.assertLess(html.index("id='overview'"), html.index("id='report-section-1'"))
        self.assertLess(html.index("id='report-section-1'"), html.index("id='financial-findings'"))
        self.assertLess(html.index("id='financial-findings'"), html.index("id='evidence-appendix'"))

    @staticmethod
    def endpoint_report():
        cards = [{"id": identity, "source_sheet": "Statement O'Brien", "value_cell": column + "18", "value": value,
                  "formula": None, "metric_label": "Revenue", "metric_cell": "C18", "metric_key": "revenue",
                  "period": period, "period_cell": column + "8", "unit": "RUB M", "unit_cell": "G18",
                  "period_type": None, "period_type_cell": None, "binding_status": "bound", "issues": []}
                 for identity, value, period, column in (("start", 100, 2019, "K"), ("end", 400, 2031, "W"))]
        registry = {c["id"]: c for c in cards}
        ledger = {(c["source_sheet"], c[key]) for c in cards for key in ("value_cell", "metric_cell", "period_cell", "unit_cell")}
        stage = build_fact_report({"id": "business", "fact_ids": ["start", "end"],
                                   "comparisons": [{"start_fact_id": "start", "end_fact_id": "end", "kind": kind}
                                                   for kind in ("total_growth", "cagr")], "limitations": []},
                                  "business", registry, ledger)
        return {"schema_version": 2, "source": "model.xlsx", "stages": [stage],
                "facts": [{**c, "stage_id": "business"} for c in cards]}

    def test_shared_endpoint_chart_is_offline_accessible_and_preserves_full_proof(self):
        report = self.endpoint_report()
        before = deepcopy(report)
        html = render_html(report)
        self.assertEqual(report, before)
        self.assertEqual(html.count("<svg "), 1)  # Two comparisons share the same source pair.
        self.assertEqual(html.count("<rect "), 2)
        self.assertNotIn("<polyline", html)
        self.assertIn("role='img' aria-labelledby=", html)
        self.assertIn("<caption>Данные графика: Revenue</caption>", html)
        self.assertIn("<th scope='row'>2019</th><td>100</td>", html)
        self.assertIn("<th scope='row'>2031</th><td>400</td>", html)
        self.assertIn("Факт/прогноз не определён", html)
        self.assertIn("промежуточные периоды не восстановлены", html)
        self.assertIn("<caption>Точные результаты сравнений</caption>", html)
        main, appendix = html.split("<details id='evidence-appendix'", 1)
        for observation in report["stages"][0]["observations"][:2]:
            self.assertNotIn(escape(observation["detail"]), main)
            self.assertIn(escape(observation["detail"]), appendix)
        for address in ("K18", "W18", "C18", "K8", "W8", "G18"):
            self.assertIn("Statement O&#x27;Brien!" + address, appendix)
        tags, attributes = [], []
        class Inspect(HTMLParser):
            def handle_starttag(self, tag, attrs):
                tags.append(tag)
                attributes.extend(attrs)
        Inspect().feed(html)
        self.assertFalse({"script", "iframe", "object", "embed", "link"} & set(tags))
        self.assertFalse(any(key.lower().startswith("on") for key, value in attributes))
        self.assertFalse(any(key in {"src", "href"} and value and not value.startswith("#") for key, value in attributes))

    def test_input_presentation_is_ignored_and_percent_values_remain_raw(self):
        report = self.fact_report()
        report["facts"][0].update(value=.08, unit="%", metric_label="Raw ratio", metric_key="irr")
        expected = render_html(report)
        report["presentation"] = {"metrics": [{"label": "FORGED", "value": 999}], "overview": {"title": "FORGED"}}
        html = render_html(report)
        self.assertEqual(expected, html)
        self.assertNotIn("FORGED", html)
        self.assertIn("class='metric-number'>0,08</p><p>Подпись единицы: %", html)
        self.assertNotIn("0,08%", html)
        self.assertNotIn("8%", html)

    def test_unknown_periods_have_no_timeline_and_print_appendix_is_opt_in(self):
        report = self.fact_report()
        report["facts"][0].update(period=None, period_cell=None, period_type=None, period_type_cell=None, binding_status="partial")
        html = render_html(report)
        self.assertNotIn("<svg", html)
        self.assertNotIn("CAGR", html)
        self.assertIn("Календарные периоды показателей не установлены", html)
        self.assertIn("<details id='evidence-appendix' class='technical-appendix'>", html)
        inputs = []
        class Inspect(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "input": inputs.append(dict(attrs))
        Inspect().feed(html)
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["id"], "print-full-appendix")
        self.assertEqual(inputs[0]["type"], "checkbox")
        self.assertNotIn("checked", inputs[0])
        self.assertIn("for='print-full-appendix'>Печатать полное приложение с источниками</label>", html)
        self.assertLess(html.index("<input "), html.index("<details id='evidence-appendix'"))
        self.assertIn("@media print", html)
        self.assertIn("#print-full-appendix~.technical-appendix{display:none!important;break-before:page}", html)
        self.assertIn("#print-full-appendix:checked~.technical-appendix{display:block!important}", html)
        self.assertIn("details:not([open])>:not(summary){display:block!important}", html)
        self.assertIn("content-visibility:visible!important", html)

    def test_sensitivity_chart_keeps_baseline_deltas_sources_and_single_pp_conversion(self):
        report = {"schema_version": 2, "source": "model.xlsx", "stages": [], "valuation_sensitivity": {
            "status": "checked", "scenarios": [{"target": {"sheet": "DCF", "cell": "B20"}, "parameter": "discount_rate",
                "changed_parameter": {"sheet": "Rates", "cell": "C3", "baseline": .10, "change": .01, "effective_value": .11, "change_unit": "absolute_fraction"},
                "raw_unit": "USD M", "baseline": 100, "saved_output": 100, "baseline_cache_difference": 0,
                "result": 90, "delta": -10, "relative_delta": -.1, "explicit_formula": "=B10/(1+C3)",
                "source_inputs": [{"sheet": "DCF", "cell": "B10", "value": 110, "frozen": True}],
                "formula_path": [{"sheet": "DCF", "cell": "B20", "formula": "=B10/(1+C3)"}],
                "basis": "saved_unaffected_inputs_and_original_formula", "recalculated_workbook": False}], "checks": [], "limitations": []}}
        html = render_html(report)
        self.assertEqual(html.count("<svg "), 1)
        for expected in ("<th scope='row'>База</th><td>100</td><td>0</td>", "+1 п.п.", "<td>90</td><td>-10</td>",
                         "DCF!B20", "DCF!B10", "Rates!C3", "=B10/(1+C3)"):
            self.assertIn(expected, html)
        self.assertNotIn("+100 п.п.", html)

    def test_legacy_structural_report_still_renders_without_presentation_model(self):
        report = {"source": "old.xlsx", "summary": {}, "findings": [], "limitations": ["Legacy boundary"]}
        html = render_html(report)
        self.assertIn("Структурная проверка финансовой модели", html)
        self.assertIn("Legacy boundary", html)
        self.assertNotIn("id='overview'", html)

    def test_supplemental_failure_partial_and_missing_fields_stay_explicit(self):
        for status, label in (("unavailable", "Недоступны"), ("timeout", "Прерваны по времени"), ("failed", "Не удалось выполнить"),
                              ("skipped", "Не выполнялись"), ("not_applicable", "Не применимы"), ("partial", "Выполнены частично"), ("complete", "Проверки выполнены")):
            with self.subTest(status=status):
                report = self.fact_report()
                report["supplemental_audit"] = {"status": status, "findings": None, "checks": None,
                    "summary": {"retained": 0, "truncated_count": 2, "truncated": True, "upstream_complete": False}, "limitations": ["Проверка ограничена."]}
                html = render_html(report)
                for text in (label, "Дополнительные проверки Excel", "Включено в отчёт: 0", "Не вошло в лимит: 2", "Проверка ограничена.", "Исходная проверка выполнена не полностью."):
                    self.assertIn(text, html)
                self.assertIn("Замечания не представлены.", html)

    def test_deterministic_raw_text_is_escaped_without_rendering_internal_metadata(self):
        report = self.fact_report()
        attack = '<img src=x onerror="alert(1)">'
        report["supplemental_audit"] = {"status": "complete", "limitations": [attack], "findings": [{"status": "candidate", "title": attack, "detail": attack,
            "evidence": [{"sheet": attack, "cell": attack}], "origin_metadata": {"severity": "critical", "internal": "internal-only"},
            "calculation": {"label": attack, "expression": attack, "unit": attack, "inputs": [{"sheet": attack, "cell": attack, "value": attack}], "limitations": [attack], "internal": "internal-only"}}]}
        html = render_html(report)
        self.assertNotIn(attack, html)
        self.assertNotIn("internal-only", html)
        self.assertNotIn("critical", html)
        self.assertIn("&lt;img", html)
        self.assertIn("Требует проверки", html)
        self.assertIn("Финансовая существенность замечаний не оценена.", html)


if __name__ == "__main__":
    unittest.main()
