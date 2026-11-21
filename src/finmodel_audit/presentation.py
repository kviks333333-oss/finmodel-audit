"""Read-only report views. No LLM, workbook access, unit conversion or risk score.

The saved report remains the source of truth. Only supported, selected cards and
previously accepted comparisons can enter the compact view. Original observations
and findings are retained by the renderers, including anything we cannot chart.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any

from .fact_report import FactReportError, _address, _card, _comparison, _sources, _statement

VERSION = "2026-11-04.2"
PERIOD_TYPES = {"actual": "Факт", "forecast": "Прогноз", None: "Факт / прогноз не установлен"}
PARAMETERS = {"discount_rate": "Ставка дисконтирования", "terminal_growth": "Терминальный рост"}
GUIDANCE = {
    "pv_fcff_duplicate_term": (
        "Повторное включение одного потока может менять итоговую оценку; измеренные варианты показаны в разделе условного влияния.",
        "Проверить состав суммы денежных потоков и назначение повторённых слагаемых."),
    "pv_fcff_historical_period": (
        "Поток завершённого периода может не соответствовать дате оценки; назначение включения требует проверки.",
        "Сопоставить дату оценки и границы прогнозного ряда."),
    "gordon_double_discount": (
        "Повторное дисконтирование может менять терминальную стоимость; поддержанные условные варианты оцениваются отдельно.",
        "Проследить путь от исходного FCFF до терминальной стоимости и итоговой оценки."),
    "irr_on_discounted_cashflows": (
        "IRR рассчитан по уже дисконтированным потокам; интерпретация показателя требует проверки.",
        "Проверить назначение IRR и сопоставить его входы с исходными денежными потоками."),
    "fcff_net_profit_nopat_overlap": (
        "Одновременное включение Net Profit и NOPAT создаёт риск повторного учёта прибыли в денежном потоке.",
        "Проверить состав FCFF, знаки и взаимосвязь Net Profit с NOPAT."),
    "datatable_basepoint_cache_mismatch": (
        "Сохранённая точка таблицы не согласуется с выбранным выходом; причина расхождения не установлена.",
        "Проверить входы таблицы чувствительности и актуальность её сохранённых значений."),
}


def _objects(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def format_saved_value(value: Any) -> str:
    """Round display only; retain exact raw values in the source/detail view."""
    if not _number(value):
        return "Не установлено"
    if value == 0:
        return "0"
    if abs(value) >= 1e15 or abs(value) < .0001:
        return f"{value:.6g}".replace(".", ",")
    digits = 2 if abs(value) >= 1 else 6
    shown = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return shown.replace(",", "\u202f").replace(".", ",")


def _equal(a: Any, b: Any) -> bool:
    if _number(a) and _number(b):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _reference(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("sheet"), str)
            and bool(value["sheet"]) and _address(value.get("cell")))


def _refs(items: list[dict]) -> list[dict]:
    return [{"sheet": sheet, "cell": cell} for sheet, cell in sorted(
        {(item["sheet"], item["cell"]) for item in items if _reference(item)})]


def _card_refs(card: dict) -> list[dict]:
    return [{"sheet": sheet, "cell": cell} for sheet, cell in sorted(_sources(card))]


def _identifier(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _stage_cards(report: dict, notices: list[str]) -> dict[str, dict[str, dict]]:
    groups: dict[str, dict[str, dict]] = {}
    conflicts = set()
    rejected = 0
    for card in _objects(report.get("facts")):
        identity, stage = card.get("id"), card.get("stage_id")
        if not isinstance(identity, str) or not isinstance(stage, str) or stage not in {"audit", "assumptions", "quality", "business"}:
            continue
        try:
            _card(identity, {identity: card}, _sources(card))
            if not isinstance(card.get("metric_label"), str) or not card["metric_label"].strip():
                raise ValueError("No metric label")
        except (FactReportError, KeyError, TypeError, ValueError, AttributeError):
            rejected += 1
            continue
        bucket = groups.setdefault(stage, {})
        if identity in bucket and bucket[identity] != card:
            conflicts.add((stage, identity))
        bucket[identity] = card
    for stage, identity in conflicts:
        groups[stage].pop(identity, None)
    if rejected or conflicts:
        notices.append("Часть карточек не вошла в краткий обзор: контекст неоднозначен или данные не поддерживаются. Подробности сохранены в разделах отчёта.")
    for stage in _objects(report.get("stages")):
        selection = stage.get("selection")
        if isinstance(stage.get("id"), str) and isinstance(selection, dict) and isinstance(selection.get("fact_ids"), list):
            selected = {identity for identity in selection["fact_ids"] if isinstance(identity, str)}
            bucket = groups.get(stage.get("id"), {})
            groups[stage.get("id")] = {identity: card for identity, card in bucket.items() if identity in selected}
    return groups


def _duplicate_indexes(stage: dict, cards: dict[str, dict]) -> list[int]:
    indexes = []
    for index, observation in enumerate(stage.get("observations", []) if isinstance(stage.get("observations"), list) else []):
        if not isinstance(observation, dict) or observation.get("narrative_verification") != "source_fact_text_generated" or observation.get("comparison"):
            continue
        claims = observation.get("fact_claims")
        if not isinstance(claims, list) or len(claims) != 1 or not isinstance(claims[0], dict):
            continue
        claim = claims[0]
        if not isinstance(claim.get("fact_id"), str):
            continue
        card = cards.get(claim.get("fact_id"))
        if not card or observation.get("title") != card["metric_label"] or observation.get("detail") != _statement(card):
            continue
        if observation.get("impact") != "not_assessed":
            continue
        if (observation.get("status") != ("observation" if card["binding_status"] == "bound" else "not_verified")
                or claim.get("verification") != ("source_bound" if card["binding_status"] == "bound" else "partial")
                or set(observation) - {"title", "detail", "evidence", "fact_claims", "impact", "status", "narrative_verification"}):
            continue
        if any(claim.get(field) != card.get(field) for field in (
            "metric_label", "period", "unit", "source_sheet", "value", "value_cell", "binding_status")):
            continue
        if _refs(_objects(observation.get("evidence"))) != _card_refs(card):
            continue
        indexes.append(index)
    return indexes


def _metrics(cards: dict[str, dict]) -> list[dict]:
    # Stable editorial selection, never a materiality ranking or perimeter merge.
    priorities = {"npv": 0, "enterprise_value": 1, "revenue": 2, "ebitda": 3,
                  "operating_profit": 4, "net_profit": 5, "fcff": 6, "debt": 7, "irr": 8}
    rows = {}
    for card in cards.values():
        key = card["source_sheet"], card.get("metric_cell"), card["metric_label"], card.get("unit")
        rows.setdefault(key, []).append(card)
    chosen = []
    for group in rows.values():
        # Unknown periods stay explicitly unknown; no inference from column order.
        known = [c for c in group if type(c.get("period")) is int]
        card = max(known, key=lambda c: c["period"]) if known else group[0]
        chosen.append(card)
    chosen.sort(key=lambda c: (priorities.get(c.get("metric_key"), 20), c["source_sheet"], c["metric_label"]))
    return [{"fact_id": c["id"], "label": c["metric_label"], "value": c["value"],
             "value_label": format_saved_value(c["value"]), "unit": c.get("unit"),
             "period": c.get("period"), "period_label": str(c["period"]) if c.get("period") is not None else "Период не установлен",
             "period_type": c.get("period_type"), "period_type_label": PERIOD_TYPES[c.get("period_type")],
             "binding_status": c["binding_status"], "source_sheet": c["source_sheet"],
             "value_cell": c["value_cell"], "evidence": _card_refs(c)} for c in chosen[:6]]


def _comparison_charts(stages: list[dict], groups: dict[str, dict], notices: list[str]) -> list[dict]:
    charts = {}
    rejected = False
    for stage in stages:
        cards = groups.get(stage.get("id"), {}) if isinstance(stage.get("id"), str) else {}
        for item in _objects(stage.get("observations")):
            value = item.get("comparison")
            if not isinstance(value, dict):
                continue
            try:
                specification = {key: value[key] for key in ("start_fact_id", "end_fact_id", "kind")}
                if value["kind"] not in {"total_growth", "cagr", "difference"}:
                    raise ValueError("Unknown comparison")
                expected = _comparison(specification, cards, 0)["comparison"]
                if not _equal(value, expected):
                    raise ValueError("Comparison does not match selected source cards")
                first, last = cards[value["start_fact_id"]], cards[value["end_fact_id"]]
            except (FactReportError, KeyError, TypeError, ValueError, ArithmeticError):
                rejected = True
                continue
            identity = _identifier("comparison_", [stage.get("id"), first["id"], last["id"]])
            if identity not in charts:
                charts[identity] = {"id": identity, "title": first["metric_label"], "metric_label": first["metric_label"],
                    "source_sheet": first["source_sheet"], "unit": first["unit"],
                    "points": [{"label": str(c["period"]), "period": c["period"], "value": c["value"],
                                "period_type": c.get("period_type"), "period_type_label": PERIOD_TYPES[c.get("period_type")],
                                "fact_id": c["id"], "sheet": c["source_sheet"], "cell": c["value_cell"]} for c in (first, last)],
                    "comparisons": [], "evidence": expected["evidence"],
                    "note": "Сравнение двух прочитанных значений; промежуточные периоды не восстановлены. Тип факт / прогноз указан только при наличии источника."}
            if not any(c["kind"] == value["kind"] for c in charts[identity]["comparisons"]):
                charts[identity]["comparisons"].append(deepcopy(value))
    if rejected:
        notices.append("Не все сравнения можно показать на графике: необходимы совместимые выбранные карточки и точное соответствие их значениям. Исходные записи оставлены в подробном отчёте.")
    return list(charts.values())


def _financial_groups(report: dict) -> list[dict]:
    found = report.get("financial_checks")
    if not isinstance(found, dict):
        return []
    groups = {}
    for index, finding in enumerate(_objects(found.get("findings"))):
        code = finding.get("code")
        code = code if isinstance(code, str) and code else "unknown_" + str(index)
        if code not in groups:
            consequence, action = GUIDANCE.get(code, (
                "Наблюдение требует проверки по исходным формулам; финансовая существенность не установлена.",
                "Проверить формулы и источники наблюдения, затем оценить влияние на итоговый показатель."))
            groups[code] = {"id": _identifier("finding_", code), "code": code,
                "title": finding.get("title") or "Финансовое наблюдение", "status": finding.get("status", "candidate"),
                "detail": finding.get("detail", ""), "consequence": consequence, "action": action,
                "count": 0, "findings": [], "evidence": [], "materiality": "not_assessed"}
        group = groups[code]
        group["count"] += 1
        group["findings"].append(deepcopy(finding))
        group["evidence"] = _refs(group["evidence"] + _objects(finding.get("evidence")))
    return list(groups.values())


def _sensitivity_charts(report: dict, notices: list[str]) -> list[dict]:
    block = report.get("valuation_sensitivity")
    if not isinstance(block, dict):
        return []
    groups, rejected, conflicting = {}, False, set()
    for scenario in _objects(block.get("scenarios")):
        target, changed = scenario.get("target"), scenario.get("changed_parameter")
        parameter = scenario.get("parameter")
        numeric = ("baseline", "saved_output", "baseline_cache_difference", "result", "delta")
        if (not _reference(target) or not _reference(changed) or not isinstance(parameter, str) or parameter not in PARAMETERS
                or not all(_number(scenario.get(key)) for key in numeric)
                or not all(_number(changed.get(key)) for key in ("baseline", "change", "effective_value"))
                or changed.get("change_unit") != "absolute_fraction"
                or scenario.get("basis") != "saved_unaffected_inputs_and_original_formula"
                or scenario.get("recalculated_workbook") is not False
                or not isinstance(scenario.get("explicit_formula"), str)
                or (scenario.get("raw_unit") is not None and not isinstance(scenario["raw_unit"], str))
                or not _objects(scenario.get("source_inputs"))
                or not all(_reference(item) and _number(item.get("value")) for item in _objects(scenario.get("source_inputs")))
                or not _equal(scenario["delta"], scenario["result"] - scenario["baseline"])
                or not _equal(changed["effective_value"], changed["baseline"] + changed["change"])
                or not _equal(scenario["baseline"], scenario["saved_output"])
                or not _equal(scenario["baseline_cache_difference"], scenario["baseline"] - scenario["saved_output"])
                or (scenario.get("relative_delta") is not None and
                    (not _number(scenario["relative_delta"]) or scenario["baseline"] == 0
                     or not _equal(scenario["relative_delta"], scenario["delta"] / abs(scenario["baseline"]))))):
            rejected = True
            continue
        signature = {key: scenario.get(key) for key in ("target", "parameter", "baseline", "saved_output", "raw_unit",
                     "explicit_formula", "source_inputs", "frozen_cashflows", "periods")}
        signature["changed_source"] = {key: changed[key] for key in ("sheet", "cell", "baseline")}
        identity = _identifier("sensitivity_", signature)
        evidence = _refs([target, changed] + _objects(scenario.get("source_inputs")) + _objects(scenario.get("formula_path")))
        if identity not in groups:
            groups[identity] = {"id": identity, "title": PARAMETERS[parameter], "target": deepcopy(target),
                "parameter": parameter, "unit": scenario.get("raw_unit"), "baseline": scenario["baseline"],
                "points": [], "scenarios": [], "evidence": evidence,
                "note": "Условный сценарий исходной формулы: денежные потоки и незатронутые входы зафиксированы. Исходные ошибки не исправлены; это не полный пересчёт Excel."}
        group = groups[identity]
        change_pp = changed["change"] * 100
        previous = next((p for p in group["points"] if p["change_pp"] == change_pp), None)
        if previous:
            if not _equal(previous["value"], scenario["result"]):
                rejected = True
                conflicting.add(identity)
            group["scenarios"].append(deepcopy(scenario))
            continue
        group["points"].append({"label": ("+" if change_pp > 0 else "") + format_saved_value(change_pp) + " п.п.",
            "value": scenario["result"], "delta": scenario["delta"], "change_pp": change_pp, "evidence": evidence})
        group["scenarios"].append(deepcopy(scenario))
    if rejected:
        notices.append("Часть сценариев не визуализирована: их входы или арифметические связи не подтверждены для графика. Подробности исходных сценариев сохранены.")
    return [group for identity, group in groups.items() if identity not in conflicting]


def build_presentation(report: dict) -> dict:
    """Build a fresh view and ignore any caller-supplied presentation field."""
    notices: list[str] = []
    stages = _objects(report.get("stages"))
    cards = _stage_cards(report, notices)
    business = cards.get("business", {})
    comparisons = _comparison_charts(stages, cards, notices)
    financial = _financial_groups(report)
    sensitivity = _sensitivity_charts(report, notices)
    incomplete = [stage for stage in stages if stage.get("completion") in {"incomplete", "skipped"}]
    summary = []
    if report.get("mode") == "local":
        summary.append("Выполнены локальные проверки формул и структуры. Агентные разделы не запускались.")
    elif incomplete:
        summary.append("Часть анализа не завершена. Доступные результаты и ограничения сохранены по разделам.")
    else:
        summary.append("Отчёт сформирован по сохранённым данным. Финансовая пригодность модели не установлена.")
    if financial:
        summary.append("Финансовые вопросы требуют проверки: " + "; ".join(group["title"] for group in financial[:3]) + ".")
    else:
        summary.append("Отсутствие представленных финансовых замечаний не подтверждает отсутствие ошибок в модели.")
    if business and all(card.get("period") is None for card in business.values()):
        summary.append("Календарные периоды показателей не установлены: временная динамика не рассчитана.")
    elif comparisons:
        summary.append("Доступно сравнение выбранных показателей по прочитанным периодам. Промежуточная траектория не восстанавливалась.")
    else:
        summary.append("Для графиков динамики нет подтверждённых пар показателей с совместимыми периодами и единицами.")
    next_steps = [{"text": group["action"], "evidence": group["evidence"]} for group in financial[:3]]
    if not next_steps:
        next_steps = [{"text": "Проверить ключевой итоговый показатель, его формулы и предпосылки по источникам из отчёта.", "evidence": []}]
    from .decision import build_decision_assessment
    assessment = build_decision_assessment(report)
    if assessment["outputs"]:
        summary[0] = assessment["summary"]
        next_steps = [{"text": output["label"] + ": " + output["ranked_findings"][0]["action"],
                       "evidence": output["ranked_findings"][0]["evidence"]} for output in assessment["outputs"]]
        next_steps += [{"text": "Влияние не установлено: " + item["action"], "evidence": item["evidence"]}
                       for item in assessment["unquantified_findings"][:2]]
    return {"version": VERSION,
        "overview": {"title": "Результаты и вопросы для проверки", "status": "limited" if financial else "not_verified",
                     "summary": summary, "next_steps": next_steps},
        "metrics": _metrics(business), "financial_groups": financial, "comparison_charts": comparisons,
        "sensitivity_charts": sensitivity, "stage_views": {stage.get("id"): {
            "fact_ids": list(cards.get(stage.get("id"), {})),
            "collapsed_observation_indexes": _duplicate_indexes(stage, cards.get(stage.get("id"), {}))}
            for stage in stages if isinstance(stage.get("id"), str)},
        "notices": notices}
