"""Conservative, same-sheet bindings between saved values and their context.

These cards describe source evidence. They neither calculate Excel nor certify
financial meaning, company perimeter, dates inferred from formatting or freshness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .references import normalize_cell
from .workbook import Cell, Workbook, _address_parts


MAX_FACT_CARDS = 8
MAX_FACT_RESPONSE_CHARS = 40_000
MAX_CONTEXT_CANDIDATES = 8
MAX_TEXT_CHARS = 300
MAX_FORMULA_CHARS = 1000

_METRICS = {
    "revenue": {"revenue", "revenue total", "total revenue", "net revenue", "выручка"},
    "operating_profit": {"operating profit", "operating income", "операционная прибыль", "прибыль от продаж"},
    "ebit": {"ebit"},
    "ebitda": {"ebitda"},
    "adjusted_ebitda": {"adjusted ebitda", "скорректированная ebitda"},
    "net_income": {"net income", "net profit", "profit for the year", "чистая прибыль"},
    "cash": {"cash", "cash and cash equivalents", "cash equivalents", "cash bop", "cash eop", "денежные средства", "денежные средства и их эквиваленты"},
    "enterprise_value": {"ev", "enterprise value", "total ev", "стоимость предприятия"},
    "equity_value": {"equity value", "стоимость собственного капитала"},
    "npv": {"npv"},
    "irr": {"irr"},
    "wacc": {"wacc", "weighted average cost of capital"},
    "fcff": {"fcff", "free cash flow to firm", "free cash flow to the firm"},
    "free_cash_flow": {"fcf", "free cash flow", "свободный денежный поток"},
    "discounted_fcff": {"discounted fcff", "pv of fcff"},
    "terminal_value": {"tv", "terminal value", "терминальная стоимость"},
    "discounted_terminal_value": {"discounted tv", "discounted terminal value"},
    "total_debt": {"debt", "total debt", "total debt liabilities bop", "total debt liabilities eop", "долг", "совокупный долг"},
    "net_debt": {"net debt", "чистый долг"},
    "loans_and_borrowings": {"loans and borrowings", "кредиты и займы"},
    "growth_rate": {"growth rate", "ltgr", "terminal growth", "terminal growth rate", "long term growth rate", "темп роста"},
    "inflation_rate": {"inflation", "inflation rate", "инфляция"},
    "tax_rate": {"tax rate", "income tax rate", "terminal stage income tax rate", "ставка налога"},
    "interest_rate": {"interest rate", "key rate", "risk free rate", "ключевая ставка"},
    "ebitda_margin": {"ebitda margin"},
    "operating_margin": {"operating margin", "operating profit margin"},
    "net_margin": {"net margin", "net profit margin"},
}
_HEADER_WORDS = {"year", "years", "fiscal year", "period", "periods", "date", "год", "годы", "период", "дата", "units", "единицы"}
_PERIOD_TYPES = {"actual": "actual", "actuals": "actual", "fact": "actual", "факт": "actual",
                 "forecast": "forecast", "budget": "forecast", "plan": "forecast", "прогноз": "forecast", "план": "forecast"}
_UNIT_WORDS = {"%", "percent", "percentage", "процент", "проценты", "п.п.", "pp", "bps", "bp",
               "x", "times", "ratio", "шт", "шт.", "pcs", "shares", "акций", "акции", "чел", "чел.",
               "year", "years", "год", "лет", "days", "дни"}
_CURRENCY_UNIT = re.compile(r"(?:RUB|USD|EUR|GBP|CNY|JPY|руб\.?|₽|\$|€)(?:\s*(?:M|MM|B|BN|K|mln|million|millions|billion|billions|тыс\.?|млн\.?|млрд\.?))?", re.IGNORECASE)
# Explicit scale-before-currency spelling; a separator is required, and raw text is retained.
_RUBLE_PREFIX_UNIT = re.compile(r"(?:тыс|млн|млрд)(?:\.\s*|\s+)руб\.?", re.IGNORECASE)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[,()\-]", " ", text.strip().casefold()).replace("&", "and")).strip()


def metric_key(label: str | None) -> str:
    """Use explicit aliases only; never equate operating profit, EBITDA and cash."""
    if not isinstance(label, str):
        return "unknown"
    normalized = _normalized(label)
    for key, aliases in _METRICS.items():
        if normalized in aliases:
            return key
    if re.fullmatch(r"usd/rub(?: .*)?", normalized):
        return "exchange_rate"
    if re.fullmatch(r"key rate(?: of .*)?", normalized):
        return "interest_rate"
    if re.fullmatch(r"(?:customer|client) retention(?: rate)?", normalized):
        return "retention_rate"
    return "unknown"


def _is_unit(value: Any) -> bool:
    return isinstance(value, str) and (value.strip().casefold() in _UNIT_WORDS
        or _CURRENCY_UNIT.fullmatch(value.strip()) is not None
        or _RUBLE_PREFIX_UNIT.fullmatch(value.strip()) is not None)


def _period(value: Any) -> tuple[int | str, str | None] | None:
    if type(value) in (int, float) and math.isfinite(value) and value == int(value) and 1900 <= value <= 2200:
        return int(value), None
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.fullmatch(r"(?:FY\s*)?((?:19|20|21|22)\d{2})(?:\s*(F|A|Forecast|Actual|Прогноз|Факт))?", text, re.IGNORECASE)
    if match and 1900 <= int(match[1]) <= 2200:
        suffix = (match[2] or "").casefold()
        kind = "forecast" if suffix in {"f", "forecast", "прогноз"} else "actual" if suffix in {"a", "actual", "факт"} else None
        return int(match[1]), kind
    match = re.fullmatch(r"Q([1-4])\s*[- ]?\s*((?:19|20|21)\d{2})|((?:19|20|21)\d{2})\s*[- ]\s*Q([1-4])", text, re.IGNORECASE)
    if match:
        return f"{match[2] or match[3]}-Q{match[1] or match[4]}", None
    return None


class FactCardReader:
    def __init__(self, workbook: Workbook):
        self.workbook = workbook
        self.rows: dict[tuple[str, int], list[str]] = {}
        self.columns: dict[tuple[str, int], list[tuple[int, str]]] = {}
        self._header_cache: dict[tuple[str, str], bool] = {}
        for sheet_name, sheet in workbook.sheets.items():
            for address in sheet.cells:
                column, row = _address_parts(address)
                self.rows.setdefault((sheet_name, row), []).append(address)
                self.columns.setdefault((sheet_name, column), []).append((row, address))
        for addresses in self.rows.values():
            addresses.sort(key=lambda address: _address_parts(address)[0])
        for addresses in self.columns.values():
            addresses.sort()

    def _cell(self, sheet: str, address: str) -> Cell | None:
        return self.workbook.sheets[sheet].cells.get(address)

    def _text(self, sheet: str, address: str) -> str | None:
        cell = self._cell(sheet, address)
        if cell and isinstance(cell.value, str) and cell.value.strip() and cell.data_type != "e":
            return cell.value
        return None

    def _labels(self, sheet: str, row: int) -> list[str]:
        labels = []
        for address in self.rows.get((sheet, row), []):
            value = self._text(sheet, address)
            if value and not _is_unit(value) and _period(value) is None and _normalized(value) not in _HEADER_WORDS and value.strip().casefold() not in _PERIOD_TYPES:
                labels.append(address)
        return labels

    def _is_period_header(self, sheet: str, address: str) -> bool:
        key = sheet, address
        if key in self._header_cache:
            return self._header_cache[key]
        cell = self._cell(sheet, address)
        column, row = _address_parts(address)
        parsed = _period(cell.value) if cell else None
        if parsed is None:
            return False
        # A numeric financial amount that happens to be 2029 is not a header.
        labels = self._labels(sheet, row)
        if any(metric_key(self._text(sheet, item)) != "unknown" for item in labels):
            self._header_cache[key] = False
            return False
        same_row = self.rows.get((sheet, row), [])
        explicit = isinstance(cell.value, str) and re.search(r"FY|[AF]$|Forecast|Actual|Прогноз|Факт|Q[1-4]", cell.value, re.IGNORECASE) is not None
        anchor = any((_normalized(self._text(sheet, item) or "") in _HEADER_WORDS)
                     for item in same_row if _address_parts(item)[0] < column)
        peers = [item for item in same_row if item != address and abs(_address_parts(item)[0] - column) <= 4
                 and _period(self._cell(sheet, item).value) is not None]
        result = explicit or anchor or (bool(peers) and not labels) or (row <= 12 and not labels)
        self._header_cache[key] = result
        return result

    def _data_between(self, sheet: str, column: int, start: int, end: int) -> bool:
        if start > end:
            start, end = end, start
        for row, address in self.columns.get((sheet, column), []):
            if row <= start:
                continue
            if row >= end:
                break
            cell = self._cell(sheet, address)
            if type(cell.value) in (int, float) and self._labels(sheet, row) and not self._is_period_header(sheet, address):
                return True
        return False

    def _source(self, sheet: str, address: str) -> dict[str, Any]:
        cell = self._cell(sheet, address)
        value = cell.value if cell else None
        formula = cell.formula if cell else None
        value_cut = isinstance(value, str) and len(value) > MAX_TEXT_CHARS
        formula_cut = isinstance(formula, str) and len(formula) > MAX_FORMULA_CHARS
        nonfinite = type(value) is float and not math.isfinite(value)
        return {"sheet": sheet, "cell": address, "value": None if nonfinite else value[:MAX_TEXT_CHARS] if value_cut else value,
                "formula": formula[:MAX_FORMULA_CHARS] if formula_cut else formula,
                "formula_type": cell.formula_type if cell else None, "data_type": cell.data_type if cell else None,
                "present": cell is not None, "value_truncated": value_cut, "formula_truncated": formula_cut,
                "nonfinite_value": nonfinite}

    def _build(self, sheet: str, address: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        column, row = _address_parts(address)
        source = self._source(sheet, address)
        cell = self._cell(sheet, address)
        facts = {"source_sheet": sheet, "value_cell": address, "value": source["value"], "formula": source["formula"],
                 "metric_label": None, "metric_cell": None, "metric_key": "unknown",
                 "period": None, "period_cell": None, "unit": None, "unit_cell": None,
                 "period_type": None, "period_type_cell": None, "binding_status": "partial", "issues": []}
        evidence = {address: source}
        ambiguous = False
        unsupported = False

        def issue(code: str, message: str, addresses: list[str] | None = None) -> None:
            refs = list(dict.fromkeys(addresses or []))[:MAX_CONTEXT_CANDIDATES]
            facts["issues"].append({"code": code, "message": message, "source_cells": refs})
            for reference in refs:
                evidence[reference] = self._source(sheet, reference)

        if cell is None:
            unsupported = True
            issue("missing_cell", "The requested cell is absent from the saved workbook.", [address])
        elif cell.data_type == "e":
            unsupported = True
            issue("cached_error", "The saved value is an Excel error, not a numeric fact.", [address])
        elif cell.value is None:
            unsupported = True
            issue("missing_formula_cache" if cell.formula_type else "empty_value", "A numeric saved value is unavailable.", [address])
        elif type(cell.value) is bool:
            unsupported = True
            issue("boolean_value", "Boolean values are not numeric financial facts.", [address])
        elif type(cell.value) not in (int, float) or not math.isfinite(cell.value):
            unsupported = True
            issue("non_numeric_value", "The source is not a finite numeric value.", [address])
        if source["value_truncated"] or source["formula_truncated"]:
            issue("source_text_truncated", "Long source text or formula is clipped in this response.", [address])

        all_labels = self._labels(sheet, row)
        labels_left = [item for item in all_labels if _address_parts(item)[0] < column]
        # An intervening saved number separates side-by-side metric blocks,
        # including blocks whose labels are not in our vocabulary.
        block_labels = []
        for label in reversed(labels_left):
            if block_labels:
                left_col = _address_parts(label)[0]
                right_col = _address_parts(block_labels[-1])[0]
                if any(left_col < _address_parts(item)[0] < right_col and type(self._cell(sheet, item).value) in (int, float)
                       for item in self.rows.get((sheet, row), [])):
                    break
            block_labels.append(label)
        labels_left = list(reversed(block_labels))
        known = [item for item in labels_left if metric_key(self._text(sheet, item)) != "unknown"]
        metric_candidates = known if known else labels_left
        selected_label = metric_candidates[-1] if metric_candidates else None
        if selected_label:
            selected_col, _ = _address_parts(selected_label)
            competing = []
            for previous in reversed(metric_candidates[:-1]):
                previous_col, _ = _address_parts(previous)
                has_value_between = any(previous_col < _address_parts(item)[0] < selected_col and type(self._cell(sheet, item).value) in (int, float)
                                        for item in self.rows.get((sheet, row), []))
                if has_value_between:
                    break
                competing.append(previous)
            if competing:
                ambiguous = True
                issue("ambiguous_metric", "Multiple adjacent labels can describe the value; no metric was selected.", competing + [selected_label])
            else:
                raw = self._text(sheet, selected_label)
                facts.update(metric_label=raw[:MAX_TEXT_CHARS], metric_cell=selected_label, metric_key=metric_key(raw))
                evidence[selected_label] = self._source(sheet, selected_label)
                if facts["metric_key"] == "unknown":
                    issue("unknown_metric", "The raw label is retained, but no supported metric identity was inferred.", [selected_label])
                if len(raw) > MAX_TEXT_CHARS:
                    issue("source_text_truncated", "The metric label is clipped.", [selected_label])
            later_labels = [item for item in all_labels if _address_parts(item)[0] > column]
            block_end = min((_address_parts(item)[0] for item in later_labels), default=16385)
            units = [item for item in self.rows.get((sheet, row), [])
                     if selected_col < _address_parts(item)[0] < block_end and _is_unit(self._text(sheet, item))]
            unit_values = {self._text(sheet, item).strip().casefold() for item in units}
            if len(unit_values) > 1:
                ambiguous = True
                issue("ambiguous_unit", "The row block contains conflicting explicit units.", units)
            elif units:
                unit_cell = min(units, key=lambda item: abs(_address_parts(item)[0] - column))
                facts.update(unit=self._text(sheet, unit_cell), unit_cell=unit_cell)
                evidence[unit_cell] = self._source(sheet, unit_cell)
            else:
                issue("unit_not_found", "No recognized explicit unit was found in the metric's row block.")
        else:
            issue("metric_not_found", "No same-row label to the left of the value was found.")
            issue("unit_not_found", "A unit cannot be assigned without a row label block.")

        before = [(r, item) for r, item in self.columns.get((sheet, column), []) if r < row]
        headers = [(r, item) for r, item in before if self._is_period_header(sheet, item)]
        band = []
        if headers:
            nearest_row = headers[-1][0]
            for header_row, header in reversed(headers):
                if self._data_between(sheet, column, header_row, nearest_row):
                    break
                band.append((header_row, header))
                if len(band) > MAX_CONTEXT_CANDIDATES:
                    ambiguous = True
                    issue("header_candidate_limit", "Too many competing period headers; context is incomplete.", [item for _, item in band[:MAX_CONTEXT_CANDIDATES]])
                    band = band[:MAX_CONTEXT_CANDIDATES]
                    break
            periods = {_period(self._cell(sheet, item).value)[0] for _, item in band}
            if len(periods) == 1:
                period_cell = band[0][1]
                facts.update(period=next(iter(periods)), period_cell=period_cell)
                evidence[period_cell] = self._source(sheet, period_cell)
            else:
                ambiguous = True
                issue("ambiguous_period", "Same-column headers in the current header band disagree; no period was selected.", [item for _, item in band])
        else:
            issue("period_not_found", "No supported same-column year or explicit year-quarter header was found.")

        type_candidates = []
        if facts["period_cell"]:
            header_row = _address_parts(facts["period_cell"])[1]
            for _, item in band:
                parsed = _period(self._cell(sheet, item).value)
                if parsed[1]:
                    type_candidates.append((parsed[1], item))
            for candidate_row, item in before:
                raw = self._text(sheet, item)
                kind = _PERIOD_TYPES.get(raw.strip().casefold()) if raw else None
                if kind and abs(candidate_row - header_row) <= 4 and not self._data_between(sheet, column, candidate_row, header_row):
                    type_candidates.append((kind, item))
        types = {kind for kind, _ in type_candidates}
        if len(types) > 1:
            ambiguous = True
            issue("ambiguous_period_type", "Actual/forecast markers conflict; no period type was selected.", [item for _, item in type_candidates])
        elif type_candidates:
            kind, type_cell = min(type_candidates, key=lambda item: abs(_address_parts(item[1])[1] - _address_parts(facts["period_cell"])[1]))
            facts.update(period_type=kind, period_type_cell=type_cell)
            evidence[type_cell] = self._source(sheet, type_cell)
        else:
            issue("period_type_not_found", "Actual/forecast status is not established by the source context.")

        if unsupported:
            facts["binding_status"] = "unsupported"
        elif ambiguous:
            facts["binding_status"] = "ambiguous"
        elif all(facts[field] is not None for field in ("metric_label", "period", "unit")) and facts["metric_key"] != "unknown" and not any(item["code"] == "source_text_truncated" for item in facts["issues"]):
            facts["binding_status"] = "bound"
        # Content identity excludes the local path and query order.
        identity_sources = [(item, repr(self._cell(sheet, item).value) if self._cell(sheet, item) and type(self._cell(sheet, item).value) is float and not math.isfinite(self._cell(sheet, item).value) else self._cell(sheet, item).value if self._cell(sheet, item) else None,
                             self._cell(sheet, item).formula if self._cell(sheet, item) else None) for item in sorted(evidence)]
        identity = json.dumps({"version": 1, "card": facts, "sources": identity_sources}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        facts = {"id": "fact_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24], **facts}
        return facts, list(evidence.values())

    def read(self, sheet: str, cells: list[str]) -> dict[str, Any]:
        if not isinstance(sheet, str) or sheet not in self.workbook.sheets:
            raise ValueError("Unknown worksheet")
        if not isinstance(cells, list) or not 1 <= len(cells) <= MAX_FACT_CARDS:
            raise ValueError("cells must contain between 1 and 8 A1 addresses")
        addresses = [normalize_cell(address) for address in cells]
        if len(set(addresses)) != len(addresses):
            raise ValueError("cells must not contain duplicate addresses")
        cards = []
        evidence = {}
        truncated = False
        for address in addresses:
            card, sources = self._build(sheet, address)
            proposed = dict(evidence)
            proposed.update({source["cell"]: source for source in sources})
            if len(json.dumps({"facts": cards + [card], "cell_evidence": list(proposed.values())}, ensure_ascii=False, allow_nan=False)) > MAX_FACT_RESPONSE_CHARS - 1500:
                truncated = True
                break
            cards.append(card)
            evidence = proposed
            truncated |= any(source["value_truncated"] or source["formula_truncated"] for source in sources)
        return {"facts": cards, "cell_evidence": list(evidence.values()), "requested_cards": len(addresses),
                "returned_cards": len(cards), "truncated": truncated,
                "completeness": {"complete": not truncated and all(card["binding_status"] == "bound" for card in cards),
                                 "scope": "same_sheet_core_value_metric_period_unit_bindings",
                                 "reasons": (["response_or_text_limit"] if truncated else []) + (["unresolved_bindings"] if any(card["binding_status"] != "bound" for card in cards) else [])},
                "basis": "saved_values", "recalculated": False,
                "limitations": ["Bindings use explicit same-row labels/units and same-column headers only; ambiguous evidence is not guessed.",
                                "source_sheet is provenance, not a verified company or consolidation perimeter.",
                                "bound means core context was linked, not that financial correctness or freshness was verified.",
                                "Actual/forecast status remains null unless explicitly supported; styles and cross-sheet conventions are not inferred.",
                                "Excel serial dates are not converted into periods without supported date-style and workbook-epoch context; repeated serials do not establish a forecast horizon."]}


def read_fact_cards(workbook: Workbook, sheet: str, cells: list[str]) -> dict[str, Any]:
    return FactCardReader(workbook).read(sheet, cells)
