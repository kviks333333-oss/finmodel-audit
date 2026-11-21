"""Escaped display of bounded conditional impact scenarios; no calculation engine."""
from html import escape
import math
import re


_STATUS = {"limited": "Рассчитано с ограничениями", "unsupported": "Нет поддержанных сценариев",
           "not_applicable": "Сценарии не применимы", "partial": "Рассчитано частично", "unavailable": "Расчёт недоступен"}
_CHECK_STATUS = {"calculated": "Рассчитан условный сценарий", "unsupported": "Сценарий не поддерживается", "not_applicable": "Не применим"}
_REASONS = {
    "baseline_does_not_match_saved_output": "Исходный расчёт не совпадает с сохранённым выходом.",
    "baseline_intermediate_cache_mismatch": "Промежуточный расчёт не совпадает с сохранёнными значениями.",
    "missing_or_ambiguous_output_unit": "Подпись единицы выхода отсутствует или неоднозначна.",
    "no_unique_supported_conditional_output_patch": "Для замечания не установлена однозначная условная замена формулы.",
    "no_supported_path_to_labelled_ev_npv": "Не установлен поддерживаемый путь от изменения до EV или NPV.",
    "no_supported_financial_candidates": "Подходящих исходных замечаний для сценариев нет.",
    "unresolved_affected_dependency": "Не все затронутые зависимости разрешены.",
    "overlap_not_established_for_all_selected_flows": "Совпадение состава прибыли подтверждено не для всех выбранных потоков.",
    "overlap_rows_not_one_consistent_series": "Выбранные потоки не образуют один согласованный ряд.",
    "overlap_intervals_not_strictly_increasing": "Последовательность интервалов потоков неоднозначна.",
    "overlap_formula_series_not_consistent": "Формулы выбранного ряда различаются по структуре.",
    "gordon_output_not_unique_single_extra_discount": "Не установлен единственный дополнительный шаг дисконтирования.",
    "gordon_saved_pv_does_not_match_flow_factor": "Сохранённый PV не совпадает с произведением потока и коэффициента.",
    "ambiguous_gordon_numerator_patch": "Замена числителя терминальной стоимости неоднозначна.",
    "missing_or_nonfinite_proof_source": "Исходное значение отсутствует или не является конечным числом.",
    "nonfinite_impact": "Расчёт не дал конечного числового результата.",
    "target_limit": "Достигнут предел числа рассматриваемых выходов.",
    "scenario_limit": "Достигнут предел числа сценариев.",
    "impact_proof_cell_limit": "Полный состав источников превышает установленный предел.",
    "impact_atomic_payload_limit": "Полное доказательство сценария превышает установленный объём.",
}


def _text(value):
    return value if isinstance(value, str) else ""


def _list(value):
    return value if isinstance(value, list) else []


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _ref(value):
    return (isinstance(value, dict) and isinstance(value.get("sheet"), str) and bool(value["sheet"])
            and isinstance(value.get("cell"), str) and re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", value["cell"]) is not None)


def _key(value):
    return value["sheet"], value["cell"]


def _close(a, b, rel=1e-9, absolute=1e-9):
    return _finite(a) and _finite(b) and math.isclose(a, b, rel_tol=rel, abs_tol=absolute)


def _changed(patch):
    return _text(patch.get("changed_formula")) or _text(patch.get("scenario_formula"))


def _scenario_issue(item):
    # This verifies a display contract, never the economic assumption itself.
    if not isinstance(item, dict) or item.get("conditional") is not True or item.get("recalculated_workbook") is not False or item.get("status", "calculated") != "calculated":
        return "Не подтверждены границы условного сценария."
    if not all(_finite(item.get(key)) for key in ("baseline", "saved_output", "result", "delta")):
        return "Числовые результаты сценария неполны или некорректны."
    if not _close(item["result"] - item["baseline"], item["delta"]) or not _close(item["baseline"], item["saved_output"], 1e-7, 1e-6):
        return "Числа сценария не согласованы с исходным расчётом."
    if (item["baseline"] == 0 and item.get("relative_delta", False) is not None or item["baseline"] != 0
            and not _close(item.get("relative_delta"), item["delta"] / abs(item["baseline"]))):
        return "Относительное изменение не согласовано с исходной базой."
    if item.get("baseline_cache_difference") is not None and not _close(item["baseline_cache_difference"], item["baseline"] - item["saved_output"]):
        return "Разница исходного расчёта и сохранённого выхода не согласована."
    if not _ref(item.get("target")) or not _text(item.get("raw_unit")).strip():
        return "Источник выхода или подпись единицы не установлены."
    patches, path, inputs = (_list(item.get(key)) for key in ("patches", "formula_path", "source_inputs"))
    if not patches or any(not _ref(p) or not _text(p.get("original_formula")) or not _changed(p)
                          or _text(p.get("changed_formula")) and _text(p.get("scenario_formula")) and p["changed_formula"] != p["scenario_formula"] for p in patches):
        return "Исходная и условная формулы представлены неполностью."
    if not path or any(not _ref(p) or not _text(p.get("formula")) for p in path) or not inputs or any(not _ref(p) or not _finite(p.get("value")) or p.get("frozen") is not True for p in inputs):
        return "Путь расчёта или зафиксированные входы представлены неполностью."
    sources = {_key(value) for value in _list(item.get("evidence")) if _ref(value)}
    patch_sources = [value for patch in patches for field in ("excluded", "replaced_sources", "replacement_sources") for value in _list(patch.get(field))]
    if any(not _ref(value) or _key(value) not in sources for value in [item["target"], *patches, *path, *inputs, *patch_sources]):
        return "Не все показанные формулы и входы связаны с источниками."
    return None


def _sources(values):
    labels = [escape(f"{value['sheet']}!{value['cell']}") for value in _list(values) if _ref(value)]
    return "<p class='evidence'>Источники: " + ", ".join(labels) + "</p>" if labels else ""


def _limits(values):
    values = [escape(value) for value in _list(values) if isinstance(value, str)]
    return "<ul>" + "".join("<li>" + value + "</li>" for value in values) + "</ul>" if values else ""


def _number(value, signed=False):
    if not _finite(value):
        return "Не установлено"
    return ("+" if signed and value > 0 else "") + format(value, ".12g")


def _details(item):
    parts = ["<details class='check-calculation'><summary>Допущение, изменённые формулы и зафиксированные входы<small style='display:block;margin-top:4px'>" + escape(_text(item.get("title")) or "Условный сценарий") + "</small></summary>",
             "<p>" + escape(_text(item.get("assumption")) or "Допущение не описано.") + "</p>",
             "<p>" + escape(f"Сохранённый выход: {item['saved_output']}. Исходный расчёт: {item['baseline']}. Условный выход: {item['result']}. Изменение: {item['delta']}.") + "</p>"]
    if item["baseline"] == 0:
        parts.append("<p>Относительное изменение не определено: исходная база равна нулю.</p>")
    else:
        percent = item["relative_delta"] * 100
        relative = _number(percent, True) + "%" if _finite(percent) else _number(item["relative_delta"], True) + " (доля)"
        parts.append("<p>Относительное изменение к модулю исходного расчёта: " + relative + ".</p>")
    if item.get("baseline_cache_difference") is not None:
        parts.append("<p>Разница исходного расчёта и сохранённого выхода: " + escape(str(item["baseline_cache_difference"])) + ".</p>")
    parts.append("<h4>Изменённые формулы</h4>")
    for patch in item["patches"]:
        parts.extend([_sources([patch]), "<p>Исходная формула</p><pre>" + escape(patch["original_formula"]) + "</pre>",
                      "<p>Формула условного сценария</p><pre>" + escape(_changed(patch)) + "</pre>",
                      _sources([*_list(patch.get("excluded")), *_list(patch.get("replaced_sources")), *_list(patch.get("replacement_sources"))])])
    parts.append("<h4>Исходный путь к выходу</h4>")
    for value in item["formula_path"]:
        parts.extend([_sources([value]), "<pre>" + escape(value["formula"]) + "</pre>"])
    parts.append("<h4>Зафиксированные входы</h4>")
    for value in item["source_inputs"]:
        parts.extend(["<p>Сохранённое значение: " + escape(str(value["value"])) + " · зафиксировано</p>", _sources([value])])
    parts.extend(["<p>Пересчитан только указанный путь с заменой формул; остальные входы сохранены. Книга Excel целиком не пересчитывалась.</p>",
                  _sources(item.get("evidence")), _limits(item.get("limitations")), "</details>"])
    return "".join(parts)


def _impact_analysis_html(data) -> str:
    if not isinstance(data, dict):
        return ""
    scenarios, checks = _list(data.get("scenarios")), [value for value in _list(data.get("checks")) if isinstance(value, dict)]
    parts = ["<section id='conditional-impact' class='check-panel valuation-panel impact-panel' aria-label='Условное влияние на оценку'><h2>Условное влияние на оценку</h2><small>",
             _STATUS.get(_text(data.get("status")), "Статус не указан"), "</small>",
             "<p>Условные альтернативы, рассчитанные кодом по исходной формуле. Эффекты отдельных сценариев не складываются; финансовая существенность не оценена.</p>",
             "<p>Подписи единиц показаны как в модели. Сохранённые числа не масштабируются по формату Excel.</p>"]
    if scenarios:
        parts.append("<p>На узком экране таблицу можно прокрутить вправо. Пояснения и формулы находятся под таблицей.</p><div class='table-scroll' role='region' aria-label='Таблица условных сценариев' tabindex='0' style='overflow-x:auto'><table class='chart-data' style='width:100%;min-width:640px'><caption>Отдельные условные сценарии</caption><thead><tr>")
        parts.extend("<th scope='col'>" + title + "</th>" for title in ("Сценарий", "Исходный расчёт", "Условный выход", "Изменение", "Подпись единицы", "Целевой выход"))
        parts.append("</tr></thead><tbody>")
        for item in scenarios:
            issue = _scenario_issue(item)
            title = _text(item.get("title")) if isinstance(item, dict) else ""
            parts.append("<tr><th scope='row'>" + escape(title or "Условный сценарий") + "</th>")
            if issue:
                parts.append("<td colspan='5'>Расчёт не показан: " + issue + "</td></tr>")
                continue
            parts.extend("<td>" + _number(item[key], key == "delta") + "</td>" for key in ("baseline", "result", "delta"))
            parts.append("<td>" + escape(item["raw_unit"]) + "</td><td>" + _sources([item["target"]]) + "</td></tr>")
        parts.append("</tbody></table></div>")
        parts.extend(_details(item) for item in scenarios if _scenario_issue(item) is None)
    else:
        parts.append("<p>Поддержанные условные сценарии не представлены.</p>")
    if checks:
        parts.append(f"<details class='check-coverage'><summary>Охват и неподдержанные сценарии · {len(checks)}</summary>")
        for check in checks:
            parts.append("<p><strong>" + _CHECK_STATUS.get(_text(check.get("status")), "Статус не указан") + "</strong>")
            if check.get("status") != "calculated":
                parts.append(" · " + _REASONS.get(_text(check.get("reason")), "Для этой формулы не установлен поддерживаемый путь условного пересчёта."))
            parts.extend(["</p>", _sources([check.get("target"), check.get("source"), *_list(check.get("evidence"))])])
        parts.append("</details>")
    limits = _limits(data.get("limitations"))
    if limits:
        parts.append("<div class='limitations'><h3>Границы сценариев</h3>" + limits + "</div>")
    return "".join(parts) + "</section>"
