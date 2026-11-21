"""Compile a bounded selection of source facts into a report, without LLM prose.

No workbook evaluation, company inference, Excel formatting or arbitrary
expressions occur here. The caller owns registration of actually delivered
cards; every referenced source must also be in its retrieved evidence ledger.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any


STAGE_TITLES = {"assumptions": "Анализ ключевых предпосылок", "business": "Обзор бизнеса и финансовых результатов"}
LIMITATIONS = {
    "no_full_recalculation": "Используются сохранённые значения; полный пересчёт Excel не выполнен.",
    "external_facts_unverified": "Внешние факты и реалистичность предпосылок независимо не проверены.",
    "limited_scope": "Выбор исходных показателей не подтверждает полноту финансового анализа.",
    "uncertain_periods": "Для части источников период или статус факт/прогноз не установлен.",
    "sensitivity_not_checked": "Чувствительность всех зависимостей модели не проверена; поддержанные локальные сценарии показаны отдельно.",
}
KINDS = ("total_growth", "cagr", "difference")
SOURCE_FIELDS = ("value_cell", "metric_cell", "period_cell", "unit_cell", "period_type_cell")


class FactReportError(ValueError):
    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or [{"code": "invalid_fact_selection", "message": message}]


def _fail(code: str, message: str, **details) -> None:
    raise FactReportError(message, [{"code": code, "message": message, **details}])


def fact_report_schema(stage_id: str) -> dict:
    if stage_id not in STAGE_TITLES:
        _fail("unsupported_stage", "Fact selection is supported only for assumptions and business.")
    return {
        "name": "submit_stage_report",
        "description": "Выбери полученные fact_id и допустимые сравнения. Текст, единицы и вычисления сформирует код; не добавляй свободные числовые утверждения.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "enum": [stage_id], "description": "Текущая стадия анализа."},
                "fact_ids": {"type": "array", "minItems": 1, "maxItems": 24, "uniqueItems": True,
                             "items": {"type": "string", "minLength": 1, "maxLength": 200},
                             "description": "Уникальные id реально полученных карточек bound/partial; включи оба endpoint каждого сравнения."},
                "comparisons": {"type": "array", "maxItems": 6, "uniqueItems": True,
                                "description": "Необязательные сравнения одного показателя/строки/листа и одинаковой исходной единицы.",
                                "items": {"type": "object", "additionalProperties": False,
                                          "properties": {
                                              "start_fact_id": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Начальная карточка из fact_ids."},
                                              "end_fact_id": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Конечная карточка из fact_ids."},
                                              "kind": {"type": "string", "enum": list(KINDS), "description": "total_growth — общий относительный рост; cagr — среднегодовой; difference — разница сохранённых значений."},
                                          }, "required": ["start_fact_id", "end_fact_id", "kind"]}},
                "limitations": {"type": "array", "maxItems": len(LIMITATIONS), "uniqueItems": True,
                                "items": {"type": "string", "enum": list(LIMITATIONS)},
                                "description": "Только коды ограничений; свободный текст не допускается. Отсутствие пересчёта и внешней проверки указываются всегда."},
            }, "required": ["id", "fact_ids", "comparisons", "limitations"],
        },
    }


def _finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _address(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", value):
        return False
    letters = value.rstrip("0123456789")
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - 64
    return column <= 16384 and int(value[len(letters):]) <= 1048576


def _period_key(value: Any) -> tuple[str, int] | None:
    if type(value) is int and 1900 <= value <= 2199:
        return "year", value
    if isinstance(value, str) and re.fullmatch(r"(?:19|20|21)\d{2}-Q[1-4]", value):
        return "quarter", int(value[:4]) * 4 + int(value[-1])
    return None


def _sources(card: dict) -> set[tuple[str, str]]:
    sheet, identity = card.get("source_sheet"), card.get("id")
    if not isinstance(sheet, str) or not sheet:
        _fail("invalid_registered_fact", "Registered fact has no source worksheet.", fact_id=identity)
    addresses = []
    for field in SOURCE_FIELDS:
        address = card.get(field)
        if address is not None:
            if not _address(address):
                _fail("invalid_registered_fact", "Registered fact contains an invalid source address.", fact_id=identity, field=field)
            addresses.append(address)
    issues = card.get("issues", [])
    if not isinstance(issues, list):
        _fail("invalid_registered_fact", "Registered fact issues must be a list.", fact_id=identity)
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("source_cells", []), list):
            _fail("invalid_registered_fact", "Registered fact has invalid issue sources.", fact_id=identity)
        for address in issue.get("source_cells", []):
            if not _address(address):
                _fail("invalid_registered_fact", "Registered fact has an invalid issue source address.", fact_id=identity)
            addresses.append(address)
    return {(sheet, address) for address in addresses}


def _card(identity: str, registry: dict, retrieved: set[tuple[str, str]]) -> dict:
    card = registry.get(identity)
    if not isinstance(card, dict) or card.get("id") != identity:
        _fail("unknown_fact_id", "Select an exact id from registered fact cards.", fact_id=identity)
    if card.get("binding_status") not in {"bound", "partial"}:
        _fail("unsupported_fact_binding", "Only bound or partial cards can be selected.", fact_id=identity)
    if not _finite(card.get("value")) or not _address(card.get("value_cell")):
        _fail("unsupported_fact_value", "A fact needs a finite numeric saved value and its exact source cell.", fact_id=identity)
    for field in ("metric_label", "unit"):
        if card.get(field) is not None and not isinstance(card[field], str):
            _fail("invalid_registered_fact", "Registered label and unit must be raw strings or null.", fact_id=identity, field=field)
    if card.get("period") is not None and _period_key(card["period"]) is None:
        _fail("invalid_registered_fact", "Registered period is not a supported year or quarter.", fact_id=identity)
    for field, source in (("metric_label", "metric_cell"), ("unit", "unit_cell"), ("period", "period_cell"), ("period_type", "period_type_cell")):
        if card.get(field) is not None and not card.get(source):
            _fail("invalid_registered_fact", "Context value has no source address.", fact_id=identity, field=field)
    if card.get("period_type") not in {None, "actual", "forecast"}:
        _fail("invalid_registered_fact", "Registered period type is unsupported.", fact_id=identity)
    if card["binding_status"] == "bound" and any(card.get(field) is None or card.get(field) == "" for field in ("metric_label", "unit", "period")):
        _fail("invalid_registered_fact", "A bound card is missing its core context.", fact_id=identity)
    sources = _sources(card)
    unread = sorted(sources - retrieved)
    if unread:
        sheet, cell = unread[0]
        _fail("unread_fact_source", "Every fact source, including context and issue sources, must be retrieved.",
              fact_id=identity, sheet=sheet, cell=cell, unread_source_count=len(unread))
    return card


def _refs(sources: set[tuple[str, str]]) -> list[dict]:
    return [{"sheet": sheet, "cell": cell} for sheet, cell in sorted(sources)]


def _claim(card: dict) -> dict:
    return {"fact_id": card["id"], **{field: card.get(field) for field in ("metric_label", "period", "unit", "source_sheet", "value", "value_cell", "binding_status")},
            "verification": "source_bound" if card["binding_status"] == "bound" else "partial"}


def _statement(card: dict) -> str:
    period = str(card["period"]) if card.get("period") is not None else "не установлен"
    return (f"{card.get('metric_label') or 'Показатель без установленной подписи'}: сохранённое значение {card['value']}; "
            f"период: {period}; подпись единицы: {card.get('unit') or 'не установлена'}; "
            f"источник: {card['source_sheet']}!{card['value_cell']}.")


def _comparison(specification: dict, cards: dict[str, dict], index: int) -> dict:
    start, end = cards[specification["start_fact_id"]], cards[specification["end_fact_id"]]
    details = {"comparison": index, **specification}
    if (start["source_sheet"] != end["source_sheet"] or not start.get("metric_cell")
            or start.get("metric_cell") != end.get("metric_cell") or not start.get("metric_label")
            or start.get("metric_label") != end.get("metric_label")):
        _fail("comparison_metric_mismatch", "Endpoints must share the same worksheet, source metric cell and raw metric label.", **details)
    if not start.get("unit") or not start["unit"].strip() or start["unit"] != end.get("unit"):
        _fail("comparison_unit_mismatch", "Endpoints need identical nonempty raw unit labels; units are never converted.", **details)
    first, last = _period_key(start.get("period")), _period_key(end.get("period"))
    if first is None or last is None or not start.get("period_cell") or not end.get("period_cell"):
        _fail("comparison_period_unavailable", "Both endpoints need source-bound periods; missing periods are not inferred.", **details)
    if first[0] != last[0] or last[1] <= first[1]:
        _fail("comparison_period_order", "Endpoints need comparable periods with end strictly after start.", **details)
    kind = specification["kind"]
    if kind != "difference" and first[0] != "year":
        _fail("comparison_requires_years", "Growth and CAGR require integer chronological years, not quarter labels.", **details)
    initial, final = start["value"], end["value"]
    years = last[1] - first[1] if first[0] == "year" else None
    if kind != "difference" and (initial <= 0 or final < 0 or (kind == "cagr" and final == 0)):
        _fail("comparison_nonpositive_value", "Growth requires start>0 and end>=0; CAGR requires both endpoints>0.", **details)
    try:
        if kind == "difference":
            value, formula, name = final - initial, "end - start", "Разница сохранённых значений"
            unit, display_value, display_unit = start["unit"], value, start["unit"]
        elif kind == "total_growth":
            value, formula, name = final / initial - 1, "end / start - 1", "Общий относительный рост"
            unit, display_value, display_unit = "fraction", value * 100, "%"
        else:
            value = math.expm1((math.log(final) - math.log(initial)) / years)
            formula, name = "(end / start) ** (1 / years) - 1", "Среднегодовой темп роста (CAGR)"
            unit, display_value, display_unit = "fraction", value * 100, "% в год"
    except (ArithmeticError, ValueError):
        _fail("comparison_numeric_overflow", "Comparison is outside finite arithmetic limits.", **details)
    if not _finite(value) or not _finite(display_value):
        _fail("comparison_numeric_overflow", "Comparison produced a nonfinite result.", **details)
    sources = _sources(start) | _sources(end)
    comparison = {**specification, "formula": formula,
                  "inputs": [{"role": role, "fact_id": card["id"], "sheet": card["source_sheet"], "cell": card["value_cell"],
                              "value": card["value"], "period": card["period"]} for role, card in (("start", start), ("end", end))],
                  "periods": {"start": start["period"], "end": end["period"], "years": years},
                  "source_unit": start["unit"], "result": {"value": value, "unit": unit},
                  "display": {"value": display_value, "unit": display_unit},
                  "basis": "saved_values", "recalculated": False, "evidence": _refs(sources)}
    shown = (f"{display_value:.10g}; подпись исходной единицы: {display_unit}"
             if kind == "difference" else f"{display_value:.10g} {display_unit}")
    detail = f"{name} за {start['period']}–{end['period']}: {shown}. Формула: {formula}. "
    if kind == "difference":
        detail += "Разница рассчитана без Excel-форматирования и масштабирования; подпись единицы сохранена буквально."
        if start["unit"].strip() == "%":
            detail += " Это разница сохранённых чисел, не перевод в процентные пункты."
    else:
        detail += "Это относительное изменение, не процентные пункты и не финансовое влияние."
    return {"title": f"{name}: {start['metric_label']}", "detail": detail,
            "evidence": _refs(sources), "fact_claims": [_claim(start), _claim(end)],
            "status": "not_verified" if any(card["binding_status"] == "partial" for card in (start, end)) else "observation",
            "impact": "not_assessed", "comparison": comparison, "narrative_verification": "source_comparison_text_generated"}


def suggest_comparisons(registry: dict, fact_ids: list[str], limit: int = 4) -> list[dict]:
    """Suggest at most two endpoint pairs from explicitly selected source facts.

    Each pair contributes total growth AND CAGR; an odd limit is rounded down.
    This is a conservative selection helper, not proof of retrieval or company
    perimeter. build_fact_report still validates the actual evidence ledger.
    """
    if type(limit) is not int or limit < 0:
        _fail("invalid_comparison_limit", "Comparison limit must be a nonnegative integer.")
    pair_limit = min(limit, 4) // 2
    if not pair_limit or not isinstance(registry, dict) or not isinstance(fact_ids, list):
        return []
    priorities = {"revenue": 0, "ebitda": 1, "operating_profit": 2, "net_income": 3,
                  "ebit": 4, "adjusted_ebitda": 5, "fcff": 6, "free_cash_flow": 7}
    rate_units = {"%", "x", "×", "ratio", "times", "доля", "раз", "п.п.", "pp", "bps", "b.p."}
    groups: dict[tuple, list[dict]] = {}
    seen = set()
    for identity in fact_ids:
        if not isinstance(identity, str) or identity in seen:
            continue
        seen.add(identity)
        card = registry.get(identity)
        if not isinstance(card, dict) or card.get("metric_key") not in priorities:
            continue
        try:
            # Validate the registered structure only. Do not claim that these
            # synthesized references were sent; the caller owns that ledger.
            card = _card(identity, registry, _sources(card))
        except FactReportError:
            continue
        unit, period = card.get("unit"), card.get("period")
        if (not isinstance(unit, str) or not unit.strip() or unit.strip().casefold() in rate_units
                or type(period) is not int or not card.get("metric_cell")):
            continue
        key = (priorities[card["metric_key"]], card["source_sheet"], card["metric_cell"], unit)
        groups.setdefault(key, []).append(card)
    suggestions = []
    for key in sorted(groups):
        cards = groups[key]
        first_year, last_year = min(card["period"] for card in cards), max(card["period"] for card in cards)
        first = [card for card in cards if card["period"] == first_year]
        last = [card for card in cards if card["period"] == last_year]
        if (first_year == last_year or len(first) != 1 or len(last) != 1
                or first[0]["value"] <= 0 or last[0]["value"] <= 0):
            continue
        proposed = [{"start_fact_id": first[0]["id"], "end_fact_id": last[0]["id"], "kind": kind}
                    for kind in ("total_growth", "cagr")]
        try:
            for specification in proposed:
                _comparison(specification, registry, 1)
        except FactReportError:
            continue
        suggestions.extend(proposed)
        if len(suggestions) >= pair_limit * 2:
            break
    return suggestions


def build_fact_report(payload: Any, stage_id: str, registry: dict, retrieved: set[tuple[str, str]]) -> dict:
    fact_report_schema(stage_id)  # Reject unsupported stages consistently.
    if not isinstance(payload, dict) or set(payload) != {"id", "fact_ids", "comparisons", "limitations"} or payload.get("id") != stage_id:
        _fail("invalid_selection_fields", "Supply only id, fact_ids, comparisons and limitation codes for the current stage.")
    ids, comparisons, limitations = payload["fact_ids"], payload["comparisons"], payload["limitations"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 24:
        _fail("no_supported_facts" if ids == [] else "fact_selection_limit", "Select between 1 and 24 registered fact ids.")
    if any(not isinstance(identity, str) or not 1 <= len(identity) <= 200 for identity in ids) or len(set(ids)) != len(ids):
        _fail("invalid_fact_ids", "Fact ids must be unique nonempty bounded strings.")
    if not isinstance(comparisons, list) or len(comparisons) > 6:
        _fail("comparison_limit", "At most six comparisons are supported.")
    seen = set()
    for index, comparison in enumerate(comparisons, 1):
        if not isinstance(comparison, dict) or set(comparison) != {"start_fact_id", "end_fact_id", "kind"}:
            _fail("invalid_comparison_fields", "Comparison allows only endpoint ids and kind.", comparison=index)
        if any(not isinstance(comparison[key], str) for key in comparison) or comparison["kind"] not in KINDS:
            _fail("unsupported_comparison", "Comparison kind or endpoint ids are invalid.", comparison=index)
        if comparison["start_fact_id"] not in ids or comparison["end_fact_id"] not in ids:
            _fail("comparison_endpoint_not_selected", "Both comparison endpoints must be present in fact_ids.", comparison=index)
        identity = (comparison["start_fact_id"], comparison["end_fact_id"], comparison["kind"])
        if identity in seen:
            _fail("duplicate_comparison", "Do not repeat an identical comparison.", comparison=index)
        seen.add(identity)
    if (not isinstance(limitations, list) or len(limitations) > len(LIMITATIONS)
            or any(not isinstance(code, str) or code not in LIMITATIONS for code in limitations)
            or len(set(limitations)) != len(limitations)):
        _fail("invalid_limitation_codes", "Use unique supported limitation codes; free-text limitations are not accepted.")
    cards = {identity: _card(identity, registry, retrieved) for identity in ids}
    observations = [{"title": card.get("metric_label") or "Показатель без установленной подписи", "detail": _statement(card),
                     "evidence": _refs(_sources(card)), "fact_claims": [_claim(card)], "impact": "not_assessed",
                     "status": "observation" if card["binding_status"] == "bound" else "not_verified",
                     "narrative_verification": "source_fact_text_generated"} for card in cards.values()]
    observations.extend(_comparison(specification, cards, index) for index, specification in enumerate(comparisons, 1))
    codes = list(dict.fromkeys(["no_full_recalculation", "external_facts_unverified", "limited_scope", *limitations]))
    if any(card.get("period") is None or card.get("period_type") is None for card in cards.values()) and "uncertain_periods" not in codes:
        codes.append("uncertain_periods")
    return {"id": stage_id, "title": STAGE_TITLES[stage_id],
            "status": "not_verified" if all(card["binding_status"] == "partial" for card in cards.values()) else "limited",
            "summary": f"Выбрано исходных показателей: {len(cards)}; сравнений по сохранённым значениям: {len(comparisons)}. "
                       "Подписи, периоды, значения и источники приведены в наблюдениях. Финансовая существенность не оценена.",
            "observations": observations, "limitations": [LIMITATIONS[code] for code in codes],
            "selection": deepcopy(payload)}
