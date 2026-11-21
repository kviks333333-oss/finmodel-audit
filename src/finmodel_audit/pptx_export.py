"""Portable read-only PowerPoint export with native tables and editable charts.

The verified slide plan is rendered by pinned PptxGenJS in a separate process.
Charts carry an embedded value-only workbook, with the same values in the cache.
This is a renderer of saved verified report structures, never an LLM call or an
Excel recalculation. It does not read the source workbook or a filesystem path.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re

from .presentation import build_presentation, _stage_cards, format_saved_value
from .impact_report import validated_impact, ImpactReportError
from .agent_evidence import _refs

PPTX_VERSION = "2026-11-04.3"
STYLE = {"font": "Arial", "body_pt": 10, "title_pt": 12,
         "width_emu": 12192000, "height_emu": 6858000,
         "accent": "408D7A", "dark": "176E59", "light": "E6F0EC", "ink": "202925"}
MAX_SLIDES = 24
MAX_REPORT_CHARS = 12_000_000
MAX_CARDS = 96
EMU = 914400


def _objects(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _text(value, limit=220):
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value if value is not None else "Не установлено"))
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _ref_text(ref):
    return f"{ref['sheet']}!{ref['cell']}" if isinstance(ref, dict) and ref.get("sheet") and ref.get("cell") else "Источник не установлен"


def _sources(values):
    return sorted({_ref_text(x) for x in _objects(values) if x.get("sheet") and x.get("cell")})


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _chunks(values, count):
    return [values[i:i + count] for i in range(0, len(values), count)]


def _card_row(card):
    return [_text(card.get("metric_label"), 90), format_saved_value(card["value"]),
            _text(card.get("unit") or "Не установлена", 45),
            str(card["period"]) if card.get("period") is not None else "Не установлен",
            _text(_ref_text({"sheet": card["source_sheet"], "cell": card["value_cell"]}), 80)]


def _impact_scenarios(report):
    good = []
    for item in _objects((report.get("impact_analysis") or {}).get("scenarios"))[:24]:
        try:
            full = deepcopy(item)
            rows = full.pop("cell_evidence", [])
            value = validated_impact({"scenario": full, "cell_evidence": rows}, _refs(full))
            value["cell_evidence"] = rows
            good.append(value)
        except (ImpactReportError, TypeError, KeyError, ValueError, ArithmeticError):
            continue
    return good


def build_slide_plan(report: dict) -> dict:
    """Create an auditable bounded presentation plan; never trust a supplied view."""
    if not isinstance(report, dict):
        raise ValueError("Report must be an object")
    try:
        if len(json.dumps(report, ensure_ascii=False, allow_nan=False)) > MAX_REPORT_CHARS:
            raise ValueError("Report exceeds PowerPoint export size limit")
    except (TypeError, OverflowError):
        raise ValueError("Report is not a supported JSON object") from None
    from .decision import build_decision_assessment
    decision = build_decision_assessment(report)
    view = build_presentation(report)
    notices = []
    groups = _stage_cards(report, notices)
    slides, omissions = [], []
    source = _text(report.get("display_filename") or report.get("source") or "Финансовая модель", 140)
    stages = {x.get("id"): x for x in _objects(report.get("stages"))}
    incomplete = [name for name in ("audit", "assumptions", "quality", "business")
                  if stages.get(name, {}).get("completion") != "submitted"]

    def add(title, *, paragraphs=None, table=None, chart=None, sources=None, note=None, section="analysis"):
        paragraphs = [_text(x, 420) for x in paragraphs or []]
        pages = [table]
        if table:
            start_y = .98 + sum(_paragraph_height(p) + .12 for p in paragraphs) + (2.88 if chart else 0)
            available = 6.88 - start_y - _row_height(table["headers"], table["widths"])
            chunks, chunk, height = [], [], 0
            for row in table["rows"]:
                row_height = _row_height(row, table["widths"])
                if row_height > available:
                    raise ValueError("PowerPoint table row exceeds bounded slide layout")
                if chunk and height + row_height > available:
                    chunks.append(chunk); chunk, height = [], 0
                chunk.append(row); height += row_height
            if chunk:
                chunks.append(chunk)
            pages = [{**table, "rows": rows} for rows in chunks] or [table]
        for index, page in enumerate(pages):
            suffix = f" · {index + 1}/{len(pages)}" if len(pages) > 1 else ""
            slides.append({"title": _text(title, 115) + suffix, "paragraphs": paragraphs,
                           "table": page, "chart": chart, "sources": list(sources or []), "note": note or "", "section": section})

    suitability = decision.get("suitability", {})
    add("Анализ финансовой модели", paragraphs=[source, "Расчёты, предпосылки, качество и финансовые показатели.",
        "Отчёт по сохранённым данным. Исходный Excel не изменён.",
        "Анализ ограничен: часть стадий не завершена." if incomplete else "Четыре блока анализа завершены. Ограничения проверок приведены отдельно."],
        note="Источник: " + source + "\nSHA-256: " + _text(report.get("source_sha256"), 100), section="overview")

    for output in _objects(decision.get("outputs"))[:6]:
        ranked = _objects(output.get("ranked_findings"))
        for chunk in _chunks(ranked, 5):
            rows = []
            for finding in chunk:
                rel = finding.get("max_absolute_relative_delta")
                effect = format_saved_value(finding.get("max_absolute_delta")) + " " + str(output.get("raw_unit") or "")
                if _finite(rel):
                    percent = 100 * rel
                    effect += "\n" + (format_saved_value(percent) + "% к модулю базы" if _finite(percent)
                                        else format_saved_value(rel) + " к модулю базы (доля)")
                else:
                    effect += "\nОтносительный эффект не определён"
                rows.append([str(finding.get("rank", "")), _text(finding.get("title"), 110), effect, _text(finding.get("action"), 210)])
            add("Приоритет проверки: " + _ref_text(output.get("target")),
                paragraphs=["Порядок по максимальному абсолютному условному эффекту для одного выхода. Альтернативы не складываются."],
                table={"headers": ["№", "Вопрос", "Измеренное влияние", "Следующий шаг"], "rows": rows, "widths": [.5, 3.6, 2.3, 6.0]},
                sources=_sources([output.get("target")] + [r for f in chunk for r in _objects(f.get("evidence"))]),
                note=json.dumps({"target": output.get("target"), "ranking_basis": output.get("ranking_basis"), "findings": chunk}, ensure_ascii=False), section="audit")

    unknown = _objects(decision.get("unquantified_findings"))
    for chunk in _chunks(unknown[:16], 4):
        add("Аудит: влияние пока не измерено", paragraphs=["Неизвестное влияние не означает низкий риск. Эти вопросы не включены в числовой рейтинг."],
            table={"headers": ["Вопрос", "Что проверить", "Источники"], "widths": [4.0, 5.4, 3.0],
                   "rows": [[_text(x.get("title"), 130), _text(x.get("action") or x.get("reason"), 230),
                              _text("; ".join(_sources(x.get("evidence"))), 110)] for x in chunk]},
            sources=_sources([r for x in chunk for r in _objects(x.get("evidence"))]), note=json.dumps(chunk, ensure_ascii=False), section="unquantified")
    if len(unknown) > 16:
        omissions.append(f"Неизмеренные вопросы: показаны 16 из {len(unknown)}. Полный список доступен в HTML/JSON.")
    if not decision.get("outputs") and not unknown:
        add("Аудит финансовой модели", paragraphs=["Поддержанные расчётные приоритеты не сформированы.",
            "Отсутствие обнаруженных кандидатов не подтверждает отсутствие ошибок. Нужна адресная проверка формул и ключевых выходов."],
            note="Полные наблюдения: раздел audit исходного отчёта.", section="audit")

    scenario_groups = {}
    accepted_scenarios = {identity for output in _objects(decision.get("outputs"))
                          for finding in _objects(output.get("ranked_findings")) for identity in finding.get("scenario_ids", [])}
    for scenario in _impact_scenarios(report):
        if scenario.get("id") not in accepted_scenarios:
            continue
        key = scenario["target"]["sheet"], scenario["target"]["cell"], scenario["raw_unit"], scenario["baseline"]
        scenario_groups.setdefault(key, []).append(scenario)
    short = {"remove_sum_terms": "Без повтора PV", "remove_historical_sum_term": "Без исторического потока",
             "replace_gordon_numerator": "Gordon: исходный FCFF", "remove_net_profit": "FCFF без Net Profit", "remove_nopat": "FCFF без NOPAT"}
    for (sheet, cell, unit, baseline), scenarios in list(scenario_groups.items())[:6]:
        for chunk in _chunks(scenarios, 4):
            add("Условные варианты: " + sheet + "!" + cell,
                paragraphs=["Единица источника: " + unit + ". Остальные входы зафиксированы. Каждый вариант рассматривается отдельно."],
                chart={"title": "Условные выходы", "unit": unit,
                       "categories": ["Исходный расчёт"] + [short.get(x.get("kind"), "Вариант " + str(i + 1)) for i, x in enumerate(chunk)],
                       "values": [baseline] + [x["result"] for x in chunk], "kind": "bar"},
                table={"headers": ["Вариант", "Результат", "Изменение к базе"], "widths": [6.2, 3.1, 3.1],
                       "rows": [["Исходный расчёт", format_saved_value(baseline), "—"]] +
                               [[short.get(x.get("kind"), _text(x["title"], 70)), format_saved_value(x["result"]), format_saved_value(x["delta"])] for x in chunk]},
                sources=[sheet + "!" + cell],
                note=json.dumps({"independent_alternatives": True, "scenarios": chunk}, ensure_ascii=False), section="impact")

    for stage_id, label in (("assumptions", "Ключевые предпосылки"), ("business", "Бизнес и финансовые показатели")):
        cards = list(groups.get(stage_id, {}).values())
        if len(cards) > MAX_CARDS:
            omissions.append(f"{label}: показаны {MAX_CARDS} из {len(cards)} карточек. Полный список доступен в HTML/JSON.")
        for index, chunk in enumerate(_chunks(cards[:MAX_CARDS], 8)):
            add(label + (f" ({index + 1})" if len(cards) > 8 else ""),
                paragraphs=["Числа и подписи единиц взяты из файла отдельно. Масштабирование и процентное форматирование Excel не применяются. Неустановленные периоды остаются неизвестными."],
                table={"headers": ["Показатель", "Число из файла", "Подпись единицы", "Период", "Источник"],
                       "widths": [3.2, 1.7, 1.7, 1.4, 4.4], "rows": [_card_row(c) for c in chunk]},
                sources=_sources([r for c in chunk for r in [{"sheet": c["source_sheet"], "cell": c["value_cell"]}]]),
                note=json.dumps({"stage": stage_id, "facts": chunk}, ensure_ascii=False), section=stage_id)
        if not cards:
            add(label, paragraphs=["Пригодные выбранные карточки отсутствуют или стадия не завершена.",
                "Числа, периоды и сведения о бизнесе не дополняются предположениями."], section=stage_id)
        if stage_id == "assumptions":
            questions = _objects((stages.get(stage_id, {}).get("investigation_plan") or {}).get("questions"))
            if questions:
                from .investigation import investigation_coverage
                details = _objects(stages[stage_id].get("investigation_details"))
                coverage = investigation_coverage(stages[stage_id]["investigation_plan"], details)
                statuses = {q["id"]: q["status"] for q in coverage["questions"]}
                delivered = {(d.get("metadata", {}).get("investigation_question") or {}).get("id"):
                             d.get("metadata", {}).get("investigation_question", {}) for d in details}
                categories = {"discount_rate_components": "Ставка и её компоненты", "cashflow_drivers": "Денежный поток",
                              "operating_assumptions": "Выручка и операционные входы", "calendar": "Календарь"}
                rows, references = [], []
                for q in questions[:6]:
                    actual = delivered.get(q.get("id"), {})
                    if statuses.get(q.get("id")) != "sources_examined":
                        detail = "Полные источники вопроса не исследованы. Нужна дополнительная проверка."
                    else:
                        issues = list(dict.fromkeys(x.get("detail") for x in _objects(actual.get("issues")) if isinstance(x.get("detail"), str)))
                        detail = "Источники прочитаны. " + (" ".join(issues[:2]) if issues else "В ограниченном обходе явных структурных препятствий не отмечено. Полнота и реалистичность не подтверждены.")
                    refs = [actual.get("subject") or q.get("subject")] + [r for issue in _objects(actual.get("issues")) for r in _objects(issue.get("evidence"))]
                    references += [r for r in refs if isinstance(r, dict)]
                    rows.append([categories.get(q.get("category"), "Предпосылки"), _text(detail, 360), _text("; ".join(_sources(refs)), 120)])
                add("Предпосылки: результаты исследования", paragraphs=["Исследование внутренних зависимостей не подтверждает рыночную реалистичность входов."],
                    table={"headers": ["Область", "Результат и ограничения", "Источники"], "widths": [2.1, 7.3, 3.0], "rows": rows},
                    sources=_sources(references),
                    note=json.dumps({"investigation": stages[stage_id].get("investigation"), "questions": questions, "details": details}, ensure_ascii=False), section="investigation")

    charts = _objects(view.get("comparison_charts"))
    for data in charts[:3]:
        points = _objects(data.get("points"))
        if len(points) != 2 or not all(_finite(p.get("value")) for p in points):
            continue
        add("Динамика: " + str(data.get("title") or "Показатель"),
            paragraphs=["Единица источника: " + str(data.get("unit")) + ". " + str(data.get("source_sheet")),
                        "Показаны две прочитанные точки. Промежуточная траектория не восстановлена. Факт / прогноз: по источникам отчёта."],
            chart={"title": str(data.get("title")), "unit": str(data.get("unit")),
                   "categories": [str(p["label"]) for p in points], "values": [p["value"] for p in points], "kind": "bar"},
            table={"headers": ["Период", "Значение", "Тип", "Источник"], "widths": [1.4, 2.4, 4.0, 4.6],
                   "rows": [[str(p["label"]), format_saved_value(p["value"]), str(p.get("period_type_label")), _ref_text(p)] for p in points]},
            sources=_sources(data.get("evidence")), note=json.dumps(data, ensure_ascii=False), section="business_chart")
    if len(charts) > 3:
        omissions.append(f"Графики динамики: показаны 3 из {len(charts)}. Остальные доступны в HTML/JSON.")

    supported = suitability.get("supported_uses", [])
    unsupported = suitability.get("unsubstantiated_uses", [])
    def sentences(items):
        return [_text(x.get("text") or x.get("description") or x.get("title") or x, 280) if isinstance(x, dict) else _text(x, 280) for x in items if isinstance(x, (str, dict))]
    add("Заключение о применении модели", paragraphs=[_text(suitability.get("summary") or decision.get("summary"), 420),
        "Поддерживаемое применение", *sentences(supported)[:2],
        "Решения, которые пока не обоснованы", *sentences(unsupported)[:2]],
        note=json.dumps(suitability, ensure_ascii=False), section="quality")
    checks = suitability.get("required_checks", [])
    if checks:
        for index, chunk in enumerate(_chunks(sentences(checks)[:12], 6)):
            add("Действия перед использованием результатов" + (f" ({index + 1})" if index else ""), paragraphs=chunk,
                note=json.dumps(checks, ensure_ascii=False), section="actions")
    add("Границы анализа", paragraphs=[
        "Полного пересчёта Excel и проверки внешних рыночных источников нет. Незатронутые входы сценариев зафиксированы.",
        "Условные изменения показывают зависимость результата от конкретного допущения. Экономическая правильность корректировки требует проверки.",
        "Неизвестное влияние не равно нулю. Альтернативные сценарии не складываются.",
        "Детальные формулы, источники и точные числа находятся в заметках слайдов и исходном HTML/JSON отчёте."],
        note=json.dumps({"limitations": report.get("limitations", []), "decision_limitations": decision.get("limitations", []), "source_sha256": report.get("source_sha256")}, ensure_ascii=False), section="limitations")
    if len(slides) > MAX_SLIDES:
        omissions.append(f"Краткая презентация ограничена {MAX_SLIDES} слайдами. Полный анализ доступен в HTML/JSON.")
        # Keep every main report section, including unknown effects and quality,
        # before retaining additional pages. Truncation is visible, never silent.
        seen, keep = set(), set()
        for index, slide in enumerate(slides):
            if slide["section"] not in seen:
                keep.add(index)
                seen.add(slide["section"])
        for index in range(len(slides)):
            if len(keep) >= MAX_SLIDES:
                break
            keep.add(index)
        slides = [slide for index, slide in enumerate(slides) if index in keep]
    if omissions or notices:
        slides[-1]["paragraphs"].extend([_text(x, 300) for x in (omissions + notices)[:3]])
    return {"version": PPTX_VERSION, "style": deepcopy(STYLE), "source": source, "slides": slides, "omissions": omissions}


def _paragraph_height(text):
    return max(.3, math.ceil(len(text) / 165) * .2 + .08)


def _row_height(values, widths):
    lines = max(max(1, math.ceil(len(str(v)) / max(8, int(width * 12)))) + str(v).count('\n') for v, width in zip(values, widths))
    return max(.34, lines * .16 + .13)


def render_pptx(report: dict) -> bytes:
    """Render a fresh plan through the packaged library, without provider calls."""
    from .pptx_backend import render_plan
    return render_plan(build_slide_plan(report))
