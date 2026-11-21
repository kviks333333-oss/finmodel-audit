"""Standalone, escaped HTML report for the local structural check."""

from html import escape
import json
import math

from .impact_html import _impact_analysis_html


_CHECK_STATUS = {"complete": "Проверки выполнены", "completed": "Проверки выполнены", "partial": "Выполнены частично",
                 "limited": "Выполнены с ограничениями", "unavailable": "Недоступны", "not_applicable": "Не применимы",
                 "timeout": "Прерваны по времени", "failed": "Не удалось выполнить", "skipped": "Не выполнялись"}
_CHECK_ITEM_STATUS = {"checked": "Выполнена", "completed": "Выполнена", "not_applicable": "Не применима",
                      "unsupported": "Не поддерживается", "ambiguous": "Неоднозначный контекст"}
_CHECK_COUNTS = {"candidate_count": "Требуют проверки", "observation_count": "Наблюдений", "checked_count": "Проверок выполнено",
                 "unsupported_count": "Не поддерживается", "ambiguous_count": "Неоднозначный контекст", "total": "Всего замечаний",
                 "eligible": "После отбора", "retained": "Включено в отчёт", "suppressed": "Отсеяно", "truncated_count": "Не вошло в лимит"}


def _check_text(value) -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _check_limits(value) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _check_evidence_html(value) -> str:
    refs = [f"{item['sheet']}!{item['cell']}" for item in value
            if isinstance(item, dict) and isinstance(item.get("sheet"), str) and isinstance(item.get("cell"), str)] if isinstance(value, list) else []
    return "<p class='evidence'>Источники: " + escape(", ".join(refs)) + "</p>" if refs else ""


def _check_calculation_html(data) -> str:
    if not isinstance(data, dict):
        return ""
    parts = []
    if data.get("basis") == "saved_values":
        parts.append("<p>На основе сохранённых значений.</p>")
    if _check_text(data.get("expression")):
        parts.append("<pre>" + escape(_check_text(data["expression"])) + "</pre>")
    for key, label in (("value", "Результат выражения"), ("actual", "В модели"), ("expected", "Сравниваемое значение"), ("difference", "Разница")):
        value = data.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            parts.append("<p>" + label + ": <strong>" + escape(str(value)) + "</strong></p>")
    if _check_text(data.get("unit")):
        parts.append("<p>Подпись единицы: " + escape(_check_text(data["unit"])) + "</p>")
    for item in data.get("inputs", []) if isinstance(data.get("inputs"), list) else []:
        if isinstance(item, dict):
            text = _check_text(item.get("value")) if item.get("value") is not None else "Нет сохранённого значения"
            parts.append("<div>" + escape(text) + _check_evidence_html([item]) + "</div>")
    if data.get("recalculated") is False:
        parts.append("<p>Полный пересчёт книги не выполнялся.</p>")
    limits = _check_limits(data.get("limitations"))
    if limits:
        parts.append("<ul>" + "".join("<li>" + escape(text) + "</li>" for text in limits) + "</ul>")
    return "<details class='check-calculation'><summary>" + escape(_check_text(data.get("label")) or "Расчёт к замечанию") + "</summary>" + "".join(parts) + "</details>"


def _comparison_html(data) -> str:
    if not isinstance(data, dict):
        return ""
    periods = data.get("periods") if isinstance(data.get("periods"), dict) else {}
    interval = f" · интервал: {periods['years']} лет" if type(periods.get("years")) is int and periods["years"] > 0 else ""
    parts = ["<p>Периоды: " + escape(_check_text(periods.get("start")) or "не установлен") + " → " +
             escape(_check_text(periods.get("end")) or "не установлен") + escape(interval) + ".</p>"]
    if _check_text(data.get("formula")):
        parts.append("<pre>" + escape(_check_text(data["formula"])) + "</pre>")
    if _check_text(data.get("source_unit")):
        parts.append("<p>Исходная подпись единицы: " + escape(_check_text(data["source_unit"])) + "</p>")
    for item in data.get("inputs", []) if isinstance(data.get("inputs"), list) else []:
        if not isinstance(item, dict):
            continue
        role = {"start": "Начало", "end": "Конец"}.get(_check_text(item.get("role")), "Вход")
        value = _check_text(item.get("value")) if item.get("value") is not None else "не установлено"
        text = role + " · " + (_check_text(item.get("period")) or "период не установлен") + " · сохранённое значение: " + value
        parts.append("<div>" + escape(text) + _check_evidence_html([item]) + "</div>")
    if data.get("basis") == "saved_values":
        parts.append("<p>Расчёт по сохранённым значениям выбранных источников.</p>")
    if data.get("recalculated") is False:
        parts.append("<p>Полный пересчёт книги не выполнялся.</p>")
    return "<details class='check-calculation comparison-details'><summary>Источники и формула сравнения</summary>" + "".join(parts) + "</details>"


_SENSITIVITY_PARAMETERS = {"discount_rate": "Ставка дисконтирования", "terminal_growth": "Терминальный рост"}
_SENSITIVITY_REASONS = {
    "alias_of_recognized_output": "Отдельный сценарий не строился: ссылка на уже рассмотренный выход.",
    "original_formula_arithmetic_only": "Поддержана локальная арифметика исходной формулы.",
    "rate_only_no_supported_terminal_growth_branch": "Поддержано изменение ставки; подходящая ветвь терминального роста не найдена.",
    "ambiguous_or_missing_discount_rate": "Ставка дисконтирования отсутствует или неоднозначна.",
    "ambiguous_terminal_growth": "Терминальный рост неоднозначен.",
    "baseline_does_not_match_saved_output": "Расчёт при исходных входах не совпадает с сохранённым выходом.",
    "missing_or_ambiguous_output_unit": "Подпись единицы выхода отсутствует или неоднозначна.",
    "scenario_rate_not_above_growth": "В сценарии ставка дисконтирования не выше терминального роста.",
    "invalid_effective_parameters": "Исходные эффективные параметры не позволяют построить сценарий.",
    "unresolved_dependency_in_output_sources": "Не все зависимости выхода разрешены.",
    "discount_rate_not_explicit_cell": "Не определена отдельная ячейка ставки дисконтирования.",
    "terminal_growth_not_explicit_cell": "Не определена отдельная ячейка терминального роста.",
    "terminal_growth_label_not_established": "Подпись терминального роста не подтверждена.",
}


def _finite_display(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _valuation_sensitivity_html(data) -> str:
    if not isinstance(data, dict):
        return ""
    scenarios = [item for item in data.get("scenarios", []) if isinstance(item, dict)] if isinstance(data.get("scenarios"), list) else []
    checks = [item for item in data.get("checks", []) if isinstance(item, dict)] if isinstance(data.get("checks"), list) else []
    status = {"limited": "Рассчитана с ограничениями", "unsupported": "Нет поддержанных сценариев"}.get(_check_text(data.get("status")), "Статус не указан")
    parts = ["<section class='check-panel valuation-panel'><h2>Чувствительность оценки</h2><small>" + status + "</small>",
             "<p>Сценарии исходной формулы при фиксированных незатронутых входах и FCFF. Полный пересчёт Excel не выполнялся.</p>",
             f"<p>Сценариев: {len(scenarios)}</p>"]
    for item in scenarios:
        parameter = item.get("changed_parameter") if isinstance(item.get("changed_parameter"), dict) else {}
        delta = parameter.get("change")
        change = (f"{delta * 100:+.12g} п.п." if parameter.get("change_unit") == "absolute_fraction" and _finite_display(delta)
                  else "изменение не установлено")
        label = _SENSITIVITY_PARAMETERS.get(_check_text(item.get("parameter")), "Параметр")
        parts.append("<article class='sensitivity-scenario'><h3>" + label + " · " + escape(change) + "</h3>")
        parts.append("<p>Целевой выход</p>" + _check_evidence_html([item.get("target")]))
        parts.append("<p>Изменяемый параметр</p>" + _check_evidence_html([parameter]))
        initial = str(parameter["baseline"]) if _finite_display(parameter.get("baseline")) else "не установлен"
        effective = str(parameter["effective_value"]) if _finite_display(parameter.get("effective_value")) else "не установлен"
        parts.append("<p>Параметр (доля): " + escape(initial) + " → " + escape(effective) + ".</p>")
        for key, title in (("saved_output", "Сохранённый выход"), ("baseline", "Выход при исходных входах"), ("result", "Выход сценария"), ("delta", "Изменение выхода")):
            if _finite_display(item.get(key)):
                parts.append("<p>" + title + ": <strong>" + escape(str(item[key])) + "</strong></p>")
        if _check_text(item.get("raw_unit")):
            parts.append("<p>Подпись единицы выхода: " + escape(_check_text(item["raw_unit"])) + "</p>")
        if _finite_display(item.get("relative_delta")):
            parts.append(f"<p>Относительное изменение выхода: {item['relative_delta'] * 100:.10g}%.</p>")
        parts.append("<details class='check-calculation'><summary>Формулы, входы и периоды сценария</summary>")
        if _check_text(item.get("explicit_formula")):
            parts.append("<pre>" + escape(_check_text(item["explicit_formula"])) + "</pre>")
        if item.get("basis") == "saved_unaffected_inputs_and_original_formula":
            parts.append("<p>Исходная формула сохранена; незатронутые входы зафиксированы на сохранённом уровне.</p>")
        if _finite_display(item.get("baseline_cache_difference")):
            parts.append("<p>Отличие исходного расчёта от сохранённого выхода: " + escape(str(item["baseline_cache_difference"])) + ".</p>")
        effective_parameters = item.get("effective_parameters")
        if isinstance(effective_parameters, list) and effective_parameters:
            parts.append("<h4>Эффективные параметры (доли)</h4>")
            for value in effective_parameters:
                if isinstance(value, dict):
                    parts.append("<p>" + _SENSITIVITY_PARAMETERS.get(_check_text(value.get("role")), "Параметр") + ": " + escape(_check_text(value.get("value"))) + "</p>" + _check_evidence_html([value]))
        if isinstance(item.get("frozen_cashflows"), list) and item["frozen_cashflows"]:
            parts.append("<h4>FCFF зафиксированы на сохранённом уровне</h4>" + _check_evidence_html(item["frozen_cashflows"]))
        formulas = item.get("formula_path")
        if isinstance(formulas, list) and formulas:
            parts.append("<h4>Формулы пути к выходу</h4>")
            for value in formulas:
                if isinstance(value, dict):
                    parts.append(_check_evidence_html([value]) + "<pre>" + escape(_check_text(value.get("formula"))) + "</pre>")
        inputs = item.get("source_inputs")
        if isinstance(inputs, list) and inputs:
            parts.append("<h4>Исходные входы</h4>")
            for value in inputs:
                if isinstance(value, dict):
                    label = " · зафиксировано" if value.get("frozen") is True else " · изменяемый вход" if value.get("frozen") is False else ""
                    text = _check_text(value.get("value")) if value.get("value") is not None else "не установлено"
                    parts.append("<p>Сохранённое значение: " + escape(text + label) + "</p>" + _check_evidence_html([value]))
                    if _check_text(value.get("formula")):
                        parts.append("<pre>" + escape(_check_text(value["formula"])) + "</pre>")
        periods = item.get("periods")
        if isinstance(periods, list) and periods:
            parts.append("<h4>Периоды дисконтирования</h4>")
            for value in periods:
                if isinstance(value, dict):
                    evidence = value.get("evidence") if isinstance(value.get("evidence"), list) else []
                    parts.append("<p>Период: " + escape(_check_text(value.get("value")) or "не установлен") + "</p>" + _check_evidence_html([value.get("discount_cell"), *evidence]))
        if item.get("recalculated_workbook") is False:
            parts.append("<p>Книга Excel целиком не пересчитывалась.</p>")
        limits = _check_limits(item.get("limitations"))
        if limits:
            parts.append("<ul>" + "".join("<li>" + escape(text) + "</li>" for text in limits) + "</ul>")
        parts.append("</details></article>")
    if not scenarios:
        parts.append("<p>Поддержанные сценарии не представлены.</p>")
    if checks:
        parts.append(f"<details class='check-coverage'><summary>Охват и пропущенные выходы · {len(checks)}</summary>")
        for item in checks:
            status = _CHECK_ITEM_STATUS.get(_check_text(item.get("status")), "Статус не указан")
            reason = _SENSITIVITY_REASONS.get(_check_text(item.get("reason")), "Расчёт не поддерживается для этого выхода.")
            parts.append("<p><strong>" + status + "</strong> · " + reason + "</p>" + _check_evidence_html([item.get("target"), item.get("source_target")]))
        parts.append("</details>")
    limits = _check_limits(data.get("limitations"))
    if limits:
        parts.append("<div class='limits'><h3>Границы сценариев</h3><ul>" + "".join("<li>" + escape(text) + "</li>" for text in limits) + "</ul></div>")
    return "".join(parts) + "</section>"


def _checks_html(title: str, data) -> str:
    if not isinstance(data, dict):
        return ""
    findings = [item for item in data.get("findings", []) if isinstance(item, dict)] if isinstance(data.get("findings"), list) else []
    checks = [item for item in data.get("checks", []) if isinstance(item, dict)] if isinstance(data.get("checks"), list) else []
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    counts = [f"Замечаний в отчёте: {len(findings)}"]
    counts += [f"{label}: {summary[key]}" for key, label in _CHECK_COUNTS.items() if type(summary.get(key)) is int and summary[key] >= 0]
    parts = ["<section class='check-panel'><div class='section-top'><h2>" + escape(title) + "</h2><small>" +
             _CHECK_STATUS.get(_check_text(data.get("status")), "Статус не указан") + "</small></div>",
             "<p class='meta'>Финансовая существенность замечаний не оценена.</p>",
             "<p class='meta'>" + escape(" · ".join(counts)) + "</p>"]
    if summary.get("truncated") is True:
        parts.append("<p class='meta'>Показана часть замечаний.</p>")
    if summary.get("upstream_complete") is False:
        parts.append("<p class='meta'>Исходная проверка выполнена не полностью.</p>")
    for item in findings:
        label = {"candidate": "Требует проверки", "observation": "Наблюдение"}.get(_check_text(item.get("status")), "Статус не указан")
        parts.append("<article><small>" + label + "</small><h3>" + escape(_check_text(item.get("title")) or "Замечание без названия") +
                     "</h3><p>" + escape(_check_text(item.get("detail"))) + "</p>" + _check_evidence_html(item.get("evidence")) +
                     _check_calculation_html(item.get("calculation")) + "</article>")
    if not findings:
        parts.append("<p>Замечания не представлены.</p>")
    if checks:
        parts.append(f"<details><summary>Охват проверок · {len(checks)}</summary><ul>")
        for index, item in enumerate(checks, 1):
            label = _CHECK_ITEM_STATUS.get(_check_text(item.get("status")), "Статус не указан")
            detail = _check_text(item.get("detail")) or _check_text(item.get("title")) or f"Проверка {index}"
            parts.append("<li><small>" + label + "</small><p>" + escape(detail) + "</p>" + _check_evidence_html(item.get("evidence")) + "</li>")
        parts.append("</ul></details>")
    limits = _check_limits(data.get("limitations"))
    if limits:
        parts.append("<div class='limits'><h3>Границы проверки</h3><ul>" + "".join("<li>" + escape(text) + "</li>" for text in limits) + "</ul></div>")
    return "".join(parts) + "</section>"


def _facts_html(items: list[dict]) -> str:
    """Render source bindings as data, without inferring a financial perimeter."""
    if not items:
        return ""
    statuses = {"bound": "Контекст связан", "partial": "Контекст неполный",
                "ambiguous": "Неоднозначная связь", "unknown": "Связь не установлена",
                "unsupported": "Значение не поддерживается"}
    period_types = {"actual": "Факт", "forecast": "Прогноз"}
    rows = []
    for fact in items:
        sheet = str(fact.get("source_sheet") or "не указан")

        def source(field: str, cell_field: str, fallback: str) -> str:
            value = fact.get(field)
            text = fallback if value is None or value == "" else str(value)
            address = fact.get(cell_field)
            reference = ("<small class='fact-ref'>" + escape(f"{sheet}!{address}") + "</small>") if address else ""
            return escape(text) + reference

        metric = source("metric_label", "metric_cell", "Подпись не определена")
        metric += "<small class='fact-sheet'>Лист: " + escape(sheet) + "</small>"
        if fact.get("formula"):
            metric += "<details><summary>Формула</summary><code>" + escape(str(fact["formula"])) + "</code></details>"
        period = source("period", "period_cell", "Период не определён")
        period += "<small class='fact-period-type'>" + escape(period_types.get(fact.get("period_type"), "Факт/прогноз не определён"))
        if fact.get("period_type_cell"):
            period += "<span class='fact-ref'>" + escape(f"{sheet}!{fact['period_type_cell']}") + "</span>"
        period += "</small>"
        issues = []
        for issue in fact.get("issues") or []:
            if isinstance(issue, dict):
                text = str(issue.get("message") or issue.get("code") or "Контекст требует проверки")
                refs = ", ".join(f"{sheet}!{cell}" for cell in issue.get("source_cells", []) if isinstance(cell, str))
                if refs:
                    text += " · " + refs
            else:
                text = str(issue)
            issues.append("<li>" + escape(text) + "</li>")
        binding = escape(statuses.get(fact.get("binding_status"), statuses["unknown"]))
        if issues:
            binding += "<ul class='fact-issues'>" + "".join(issues) + "</ul>"
        rows.append("<tr><td>" + metric + "</td><td>" + source("value", "value_cell", "Нет сохранённого значения")
                    + "</td><td>" + period + "</td><td>" + source("unit", "unit_cell", "Единица не определена")
                    + "</td><td>" + binding + "</td></tr>")
    return ("<div class='fact-panel'><h3>Данные из модели · " + str(len(items)) + "</h3>"
            "<p class='fact-note'>Связь с источниками не подтверждает финансовый вывод. Название листа указывает источник, а не периметр компании.</p>"
            "<div class='table-scroll'><table class='fact-table'><thead><tr><th>Показатель</th><th>Сохранённое значение</th><th>Период</th><th>Подпись единицы</th><th>Связь с источниками</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div></div>")


def _calculations_html(items: list[dict]) -> str:
    if not items:
        return ""
    fragments = []
    def value(number):
        return escape(str(number)) if number is not None else "Не рассчитано"
    for item in items:
        data = item.get("result", {})
        expression = escape(str(data.get("expression", "")))
        if "scenarios" in data:
            base = data.get("baseline", {})
            rows = f"<tr><td>Базовое значение</td><td>{value(base.get('input_value'))}</td><td>{value(base.get('result',{}).get('value'))}</td><td>—</td></tr>"
            for row in data["scenarios"]:
                rows += f"<tr><td>{value(row.get('change_fraction',0)*100)}%</td><td>{value(row.get('input_value'))}</td><td>{value(row.get('result',{}).get('value'))}</td><td>{value(row.get('absolute_output_change'))}</td></tr>"
            body = "<table><thead><tr><th>Изменение входа</th><th>Значение входа</th><th>Результат</th><th>Изменение результата</th></tr></thead><tbody>" + rows + "</tbody></table>"
        else:
            body = "<p>Результат: " + value(data.get("result", {}).get("value")) + "</p>"
        inputs = "".join("<li>" + escape(f"{e.get('name','')} = {e.get('value')} · {e.get('sheet')}!{e.get('cell')}") + "</li>" for e in data.get("evidence", []))
        fragments.append(f"<article><code>{expression}</code>{body}<ul>{inputs}</ul></article>")
    return "<details><summary>Дополнительные расчёты</summary><p>Только локальные выражения при фиксированных остальных входах. Полный пересчёт книги не выполняется.</p>" + "".join(fragments) + "</details>"


def render_html(report: dict) -> str:
    if report.get("schema_version") == 2:
        return render_analysis_html(report)
    rows = []
    levels = {"error": "Ошибка в файле", "warning": "Нужна проверка", "info": "Особенность"}
    for item in report["findings"]:
        refs = ", ".join(item["cells"])
        location = f"{item['sheet']}!{refs}" if item["sheet"] else "Книга"
        details = json.dumps(item["evidence"], ensure_ascii=False, indent=2)
        rows.append(
            "<tr><td>" + escape(levels[item["level"]]) + "</td><td>"
            + escape(location) + "</td><td><strong>" + escape(item["title"])
            + "</strong><p>" + escape(item["impact"]) + "</p>"
            + "<details><summary>Доказательство</summary><pre>" + escape(details)
            + "</pre></details></td></tr>"
        )
    limitations = "".join(f"<li>{escape(t)}</li>" for t in report["limitations"])
    summary = escape(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return f"""<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Структурная проверка финансовой модели</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;color:#182230;background:#fff;max-width:1200px;margin:40px auto;padding:0 24px}}
h1{{font-size:28px}}p{{margin:.6em 0}}table{{width:100%;border-collapse:collapse;margin:24px 0}}
th,td{{text-align:left;vertical-align:top;padding:14px;border-bottom:1px solid #d7dde5}}
th{{background:#edf1f6}}td:nth-child(1){{width:140px}}td:nth-child(2){{width:240px;overflow-wrap:anywhere}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}}summary{{cursor:pointer}}
@media(max-width:650px){{body{{margin:20px auto;padding:0 12px}}th,td{{padding:8px;font-size:14px}}}}
</style><main>
<h1>Структурная проверка финансовой модели</h1>
<p>Источник: {escape(report['source'])}</p>
<p>Это первый этап проверки. Финансовая существенность находок ещё не оценена.</p>
<ul>{limitations}</ul>
<details><summary>Состав книги</summary><pre>{summary}</pre></details>
{_impact_analysis_html(report.get('impact_analysis'))}
<table><thead><tr><th>Тип</th><th>Расположение</th><th>Наблюдение</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</main></html>"""


def _observation_html(item: dict) -> str:
    labels = {"observation": "Наблюдение", "hypothesis": "Гипотеза", "not_verified": "Не проверено"}
    impact = "Не оценено" if item.get("impact") == "not_assessed" else str(item.get("impact") or "Не оценено")
    evidence = _check_evidence_html(item.get("evidence")) or "<p class='evidence'>Источники: не установлены</p>"
    return ("<article class='stage-observation'><small>" + escape(labels.get(item.get("status"), "Наблюдение")) +
            "</small><h3>" + escape(str(item.get("title") or "Наблюдение")) + "</h3><p>" +
            escape(str(item.get("detail") or "")) + "</p>" + _comparison_html(item.get("comparison")) +
            "<p class='impact'><b>Влияние:</b> " + escape(impact) + "</p>" + evidence + "</article>")


def _stage_fact_snapshot(facts: list[dict]) -> str:
    if not facts:
        return ""
    rows = []
    for fact in facts:
        period = str(fact["period"]) if fact.get("period") is not None else "Не установлен"
        period_type = {"actual": "Факт", "forecast": "Прогноз"}.get(fact.get("period_type"), "Факт/прогноз не определён")
        binding = "Контекст связан" if fact.get("binding_status") == "bound" else "Контекст связан частично"
        unit = str(fact.get("unit") or "Не установлена")
        rows.append("<tr><th scope='row'>" + escape(str(fact.get("metric_label") or "Подпись не определена")) +
                    "<small class='fact-note'>" + binding + "</small></th><td>" + escape(str(fact.get("value"))) + "</td><td>" + escape(period) +
                    "<small class='fact-period-type'>" + period_type + "</small></td><td>" +
                    escape(unit) + "</td><td>" + escape(str(fact.get("source_sheet") or "")) + "!" +
                    escape(str(fact.get("value_cell") or "")) + "</td></tr>")
    return ("<div class='table-scroll'><table class='selected-facts'><caption>Выбранные исходные показатели</caption>"
            "<thead><tr><th scope='col'>Показатель</th><th scope='col'>Сохранённое значение</th><th scope='col'>Период</th>"
            "<th scope='col'>Подпись единицы</th><th scope='col'>Источник</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def render_analysis_html(report: dict) -> str:
    """Offline report with a reader print view and opt-in full evidence appendix."""
    from .presentation import build_presentation
    from .report_presentation import CSS, overview, metrics, financial_groups, chart, text
    from .decision import build_decision_assessment
    from .decision_html import decision_html

    # Never trust a client-supplied presentation. Derive it from saved evidence.
    view = build_presentation(report)
    assessment = build_decision_assessment(report)
    stages = [x for x in report.get("stages", []) if isinstance(x, dict)]
    all_facts = [x for x in report.get("facts", []) if isinstance(x, dict)]
    status_labels = {"limited": "С ограничениями", "not_verified": "Не проверено", "skipped": "Не выполнялся", "completed": "Завершён"}
    sections, appendix = [], []
    nav = ["<a href='#overview'>Главное</a>"]
    if view.get("metrics"):
        nav.append("<a href='#key-metrics'>Показатели</a>")
    for number, stage in enumerate(stages, 1):
        anchor = f"report-section-{number}"
        title = str(stage.get("title") or stage.get("id") or f"Раздел {number}")
        nav.append(f"<a href='#{anchor}'>" + text(title) + "</a>")
        stage_view = view.get("stage_views", {}).get(stage.get("id"), {})
        hidden = set(stage_view.get("collapsed_observation_indexes", []))
        observations = [x for x in stage.get("observations", []) if isinstance(x, dict)]
        stage_facts = [x for x in all_facts if x.get("stage_id") == stage.get("id")]
        selected_ids = set(stage_view.get("fact_ids", []))
        selected = [x for x in stage_facts if x.get("id") in selected_ids]
        limits = "".join("<li>" + text(x) + "</li>" for x in stage.get("limitations", []))
        stage_charts = ""
        if stage.get("id") == "quality":
            stage_charts = decision_html(assessment, conclusion_only=True)
        elif stage.get("id") == "audit":
            stage_charts = decision_html(assessment)
        if stage.get("id") == "business":
            stage_charts = "".join(chart(c, i) for i, c in enumerate(view.get("comparison_charts", []), 1))
            if stage_charts:
                stage_charts = "<div class='chart-grid'>" + stage_charts + "</div>"
        status = "Не завершён" if stage.get("completion") == "incomplete" else status_labels.get(stage.get("status"), str(stage.get("status") or "Статус не указан"))
        sections.append(f"<section id='{anchor}' class='report-stage'><div class='section-top'><span class='stage-number'>{number:02d} / РАЗДЕЛ</span><span class='status'>" +
                        text(status) + "</span></div><h2>" + text(title) + "</h2><p class='stage-summary'>" + text(stage.get("summary") or "Резюме не представлено.") +
                        "</p>" + stage_charts + _stage_fact_snapshot(selected) +
                        "".join(_observation_html(item) for i, item in enumerate(observations) if i not in hidden) +
                        ("<p class='fine-print'>Повторяющие таблицу исходные наблюдения сохранены полностью в приложении с источниками.</p>" if hidden else "") +
                        ("<div class='limits'><h3>Границы проверки</h3><ul>" + limits + "</ul></div>" if limits else "") + "</section>")
        calculations = _calculations_html([x for x in report.get("calculations", []) if isinstance(x, dict) and x.get("stage_id") == stage.get("id")])
        appendix.append("<section class='full-stage-observations'><h2>" + text(title) + " · полные источники</h2>" +
                        _facts_html(stage_facts) + "".join(_observation_html(item) for item in observations) + calculations + "</section>")
    sensitivity_charts = "".join(chart(c, i, sensitivity=True) for i, c in enumerate(view.get("sensitivity_charts", []), 1))
    sensitivity = ("<section id='valuation-scenarios'><p class='eyebrow'>ЛОКАЛЬНЫЕ СЦЕНАРИИ</p><h2>Чувствительность оценки</h2>"
                   "<p>Исходная формула и сохранённые остальные входы. Сценарии не исправляют модель и не заменяют полный пересчёт Excel.</p>"
                   "<div class='chart-grid'>" + sensitivity_charts + "</div></section>") if sensitivity_charts else ""
    groups = financial_groups(view)
    impact = _impact_analysis_html(report.get("impact_analysis"))
    if groups:
        nav.append("<a href='#financial-findings'>Финансовые вопросы</a>")
    if impact:
        nav.append("<a href='#conditional-impact'>Условное влияние</a>")
    if sensitivity:
        nav.append("<a href='#valuation-scenarios'>Сценарии</a>")
    nav.append("<a href='#evidence-appendix'>Источники и детали</a>")
    structural = report.get("structural") if isinstance(report.get("structural"), dict) else {}
    rows = []
    for item in structural.get("findings", []):
        if not isinstance(item, dict):
            continue
        rows.append("<tr><td>" + text(item.get("title") or "Наблюдение") + "</td><td>" + text(item.get("sheet") or "Книга") +
                    " " + text(", ".join(str(x) for x in item.get("cells", []))) + "</td><td>" + text(item.get("impact") or "Не оценено") + "</td></tr>")
    technical_checks = (_checks_html("Финансовые проверки · полное доказательство", report.get("financial_checks")) +
                        _checks_html("Дополнительные проверки Excel", report.get("supplemental_audit")) +
                        _valuation_sensitivity_html(report.get("valuation_sensitivity")))
    usage = report.get("usage") if isinstance(report.get("usage"), dict) else {}
    stats = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    unfinished = [str(x.get("title") or x.get("id")) for x in stages if x.get("completion") == "incomplete"]
    completion_note = ("<p class='completion-note'><strong>Частичный отчёт.</strong> Не завершены разделы: " + text(", ".join(unfinished)) + ". Причины указаны ниже.</p>") if unfinished else ""
    global_limits = _check_limits(report.get("limitations"))
    limits = ("<section><h2>Общие границы анализа</h2><ul>" + "".join("<li>" + text(x) + "</li>" for x in global_limits) + "</ul></section>") if global_limits else ""
    return ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Анализ финансовой модели · " + text(report.get("source") or "Excel") + "</title><style>" + CSS + "</style></head><body><main>"
            "<header class='report-title'><p class='overline'>АНАЛИЗ ФИНАНСОВОЙ МОДЕЛИ</p><h1>" + text(report.get("source") or "Финансовая модель") + "</h1>" + completion_note +
            "<p class='meta'>Отчёт по формулам и сохранённым значениям. Источники проверяемы; пригодность модели и финансовое влияние требуют отдельного обоснования.</p>"
            "<nav class='section-nav' aria-label='Разделы отчёта'>" + "".join(nav) + "</nav></header>"
            "<input type='checkbox' id='print-full-appendix' class='print-option' aria-describedby='print-option-help'>"
            "<label class='print-option-label' for='print-full-appendix'>Печатать полное приложение с источниками</label>"
            "<p id='print-option-help' class='print-help'>По умолчанию печатается читательский обзор с точными таблицами графиков. "
            "Отметьте поле перед печатью, чтобы добавить полные доказательства и технические детали. В HTML приложение доступно всегда.</p>"
            "<p class='print-summary-note'>Читательская версия. Полное приложение с источниками сохранено в исходном HTML и может быть включено при печати.</p>" + overview(view) + metrics(view) +
            "".join(sections) + groups + impact + sensitivity +
            "<details id='evidence-appendix' class='technical-appendix'><summary>Источники, полные доказательства и технические детали</summary><div class='appendix-content'>"
            "<p class='meta'>Приложение содержит все сохранённые наблюдения и исходные таблицы, включая свёрнутые повторы. "
            "Для включения в печатную версию отметьте «Печатать полное приложение с источниками».</p>" +
            "<p class='meta'>Листов: " + text(stats.get("sheet_count", 0)) + " · Формульных ячеек: " + text(stats.get("formula_cells", 0)) +
            " · Сохранённых ошибок: " + text(stats.get("cached_errors", 0)) + "</p><p class='meta'>Инструментами прочитано и включено в подготовленные ответы " +
            text(coverage.get("retrieved_cells", 0)) + " из " + text(coverage.get("total_cells", 0)) +
            " ячеек. Это не гарантия отправки каждой ячейки модели и не доля проверенной финансовой логики.</p>" +
            technical_checks + "".join(appendix) + "<section><h2>Структурные наблюдения</h2><table><thead><tr><th>Наблюдение</th><th>Адрес</th><th>Граница проверки</th></tr></thead><tbody>" +
            "".join(rows) + "</tbody></table></section>" + limits + "</div></details>"
            "<footer class='report-footer'><p>SHA-256 исходника: " + text(report.get("source_sha256", "")) + "</p><p>Запросов к LLM: " + text(usage.get("requests", 0)) +
            ". Total tokens по usage: " + text(usage.get("total_tokens", 0)) + ". Отдельно precached prompt tokens: " + text(usage.get("precached_prompt_tokens", 0)) +
            ".</p><p>Исходный Excel не пересохранялся. Отчёт работает без JavaScript, CDN и внешних шрифтов.</p></footer></main></body></html>")
