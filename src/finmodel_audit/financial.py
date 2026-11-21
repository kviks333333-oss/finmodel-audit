"""Conservative, read-only financial formula checks on saved workbook evidence.

This is a bounded structural analyser, not an Excel evaluator or a valuation
opinion. Arithmetic and contracts are original; no FinRobot source is vendored.
Only an explicit arithmetic/SUM/A1 subset is parsed. Unsupported expressions,
ambiguous periods and non-finite saved inputs never become verified findings.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import date, datetime
import hashlib
import json
import math
import re
from typing import Any

from .references import normalize_cell, parse_range
from .workbook import Workbook, _address_parts, _column_letters

VERSION = "2026-11-04.4"
MAX_FORMULA = 4096
MAX_TOKENS = 512
MAX_EXPANSIONS = 400
MAX_RANGE_CELLS = 256
MAX_FORMULAS = 40_000
MAX_FINDINGS = 100
MAX_CHECKS = 300
MAX_DEPTH = 12
Node = tuple[str, str]
Expr = tuple

_TOKEN = re.compile(
    r"\s*(?:(?P<ref>(?:'(?:[^']|'')+'!|[^\s+*/^&=<>(),;{}\"'\[\]!:]+!)?"
    r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)?)"
    r"(?![\w.])|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z_0-9.]*)|(?P<op>[+\-*/^(),;%]))"
)
_FCF = re.compile(r"\bfcff?\b|free\s+cash\s*flow|свободн\w*\s+денежн\w*\s+поток", re.I)
_PV = re.compile(r"discount|present\s+value|\bpv\b|\bnpv\b|дисконт", re.I)
_VALUATION_DATE = re.compile(r"valuation\s+date|date\s+of\s+valuation|дата\s+оценки", re.I)
_UNIT_LABEL = re.compile(r"^(?:[%x×]|(?:RUB|USD|EUR|GBP|CNY|руб\.?)(?:\s*(?:M|mn|mln|million|bn|billion|тыс\.?|млн\.?|млрд\.?))?|(?:тыс\.?|млн\.?|млрд\.?)\s*руб\.?|units?|value|comment|единицы|значение|комментарий)$", re.I)
_YEAR_HEADER_LABEL = re.compile(r"^(?:units?|value|comment|years?|periods?|dates?|calendar|actual|forecast|historical|fiscal\s+years?|forecast\s+years?|год(?:ы)?|период(?:ы)?|дата|факт|прогноз|единицы|значение|комментарий)$", re.I)
_CALENDAR_YEAR_LABEL = re.compile(r"^(?:(?:calendar|fiscal|forecast)\s+years?|years?|calendar|год(?:ы)?|календарн(?:ый|ые)\s+год(?:ы)?)$", re.I)
_NON_YEAR_TIME_LABEL = re.compile(r"^(?:periods?|dates?|период(?:ы)?|дат(?:а|ы))$", re.I)


class Unsupported(ValueError):
    pass


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _ref(node: Node) -> dict:
    return {"sheet": node[0], "cell": node[1]}


def _split_reference(text: str, context: str) -> tuple[str, str]:
    if "!" in text:
        sheet, text = text.rsplit("!", 1)
        if sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    else:
        sheet = context
    if any(c in sheet for c in "[]:"):
        raise Unsupported("external_or_three_dimensional_reference")
    return sheet, text


class _Parser:
    def __init__(self, formula: str, sheet: str):
        if len(formula) > MAX_FORMULA:
            raise Unsupported("formula_size_limit")
        self.sheet, self.tokens, self.i = sheet, [], 0
        text, pos = formula.lstrip("=").strip(), 0
        while pos < len(text):
            match = _TOKEN.match(text, pos)
            if not match or len(self.tokens) >= MAX_TOKENS:
                raise Unsupported("unsupported_syntax_or_token_limit")
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            pos = match.end()
        self.tokens.append(("end", ""))

    def take(self, value: str) -> bool:
        if self.tokens[self.i][1] == value:
            self.i += 1
            return True
        return False

    def expression(self, minimum: int = 0, depth: int = 0) -> Expr:
        if depth > 40:
            raise Unsupported("expression_depth_limit")
        kind, value = self.tokens[self.i]
        self.i += 1
        if kind == "number":
            number = float(value)
            if not math.isfinite(number):
                raise Unsupported("nonfinite_literal")
            left = ("number", number)
        elif kind == "ref":
            sheet, address = _split_reference(value, self.sheet)
            if ":" in address:
                bounds = parse_range(address)
                left = ("range", sheet, bounds)
            else:
                left = ("ref", sheet, normalize_cell(address))
        elif value == "(":
            left = self.expression(0, depth + 1)
            if not self.take(")"):
                raise Unsupported("unbalanced_parentheses")
        elif value in ("+", "-"):
            # Excel unary negation binds before exponentiation (-1^2 == 1).
            term = self.expression(31, depth + 1)
            left = term if value == "+" else ("*", ("number", -1.0), term)
        elif kind == "name" and value.upper() == "SUM" and self.take("("):
            terms = [self.expression(0, depth + 1)]
            while self.take(",") or self.take(";"):
                terms.append(self.expression(0, depth + 1))
            if not self.take(")"):
                raise Unsupported("unsupported_sum")
            left = ("sum", tuple(terms))
        else:
            raise Unsupported("unsupported_function_name_or_operand")
        while True:
            if self.take("%"):
                left = ("/", left, ("number", 100.0))
                continue
            op = self.tokens[self.i][1]
            power = {"+": 10, "-": 10, "*": 20, "/": 20, "^": 30}.get(op, -1)
            if power < minimum:
                break
            self.i += 1
            # Excel exponentiation is left associative.
            left = (op, left, self.expression(power + 1, depth + 1))
        return left

    def parse(self) -> Expr:
        result = self.expression()
        if self.tokens[self.i][0] != "end":
            raise Unsupported("trailing_formula_syntax")
        return result


class _Checks:
    def __init__(self, workbook: Workbook):
        self.wb = workbook
        self.parsed: dict[Node, Expr] = {}
        self.parse_errors: dict[Node, str] = {}
        self.reverse: dict[Node, set[Node]] = defaultdict(set)
        self.positions: dict[str, dict[tuple[int, int], str]] = defaultdict(dict)
        self.rows: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
        self.findings: list[dict] = []
        self.checks: list[dict] = []
        self.limits: set[str] = set()
        self.formulas_seen = 0
        for sheet, data in workbook.sheets.items():
            for address, cell in data.cells.items():
                try:
                    column, row = _address_parts(address)
                except ValueError:
                    self.limits.add("Некорректные адреса ячеек пропущены.")
                    continue
                self.positions[sheet][column, row] = address
                self.rows[sheet, row].append((column, address))
                if not cell.formula or cell.formula_type in {"array", "dataTable"}:
                    continue
                self.formulas_seen += 1
                if self.formulas_seen > MAX_FORMULAS:
                    self.limits.add("Достигнут лимит разбора формул.")
                    continue
                node = sheet, address
                try:
                    expression = _Parser(cell.formula, sheet).parse()
                    references = self.references(expression)
                    self.parsed[node] = expression
                    for source in references:
                        self.reverse[source].add(node)
                except (Unsupported, ValueError, RecursionError) as exc:
                    self.parse_errors[node] = str(exc)[:100]
        for items in self.rows.values():
            items.sort()

    def cell(self, node: Node):
        sheet = self.wb.sheets.get(node[0])
        return sheet.cells.get(node[1]) if sheet else None

    def value(self, node: Node) -> float:
        cell = self.cell(node)
        if cell is None or not _finite(cell.value):
            raise Unsupported("missing_or_nonfinite_saved_number")
        return float(cell.value)

    def references(self, expr: Expr) -> set[Node]:
        if expr[0] == "ref":
            return {(expr[1], expr[2])}
        if expr[0] == "range":
            sheet, (c1, r1, c2, r2) = expr[1:]
            if (c2 - c1 + 1) * (r2 - r1 + 1) > MAX_RANGE_CELLS:
                raise Unsupported("range_size_limit")
            return {(sheet, f"{_column_letters(c)}{r}") for c in range(c1, c2 + 1) for r in range(r1, r2 + 1)}
        if expr[0] == "number":
            return set()
        children = expr[1] if expr[0] == "sum" else expr[1:]
        return set().union(*(self.references(child) for child in children))

    def evidence(self, nodes) -> list[dict]:
        unique = sorted({node for node in nodes if self.cell(node) is not None})
        if len(unique) > 100:
            raise Unsupported("evidence_size_limit")
        return [_ref(node) for node in unique]

    def check(self, code: str, status: str, detail: str, nodes=()):
        if len(self.checks) >= MAX_CHECKS:
            self.limits.add("Достигнут лимит подробных проверок.")
            return
        self.checks.append({"code": code, "status": status, "detail": detail,
                            "evidence": self.evidence(nodes)})

    def finding(self, code: str, title: str, detail: str, nodes, calculation=None, status="candidate"):
        if len(self.findings) >= MAX_FINDINGS:
            self.limits.add("Достигнут лимит находок.")
            return
        evidence = self.evidence(nodes)
        identity = json.dumps([code, evidence], sort_keys=True, ensure_ascii=False).encode()
        item = {"id": "financial_" + hashlib.sha256(identity).hexdigest()[:16],
                "code": code, "status": status, "title": title, "detail": detail,
                "evidence": evidence, "financial_materiality": "not_assessed"}
        if calculation is not None:
            item["calculation"] = calculation
        if not any(old["id"] == item["id"] for old in self.findings):
            self.findings.append(item)

    def calculation(self, label: str, expression: str, nodes, **numbers) -> dict:
        if any(key != "unit" and not _finite(value) for key, value in numbers.items()):
            raise Unsupported("nonfinite_calculation_output")
        return {"label": label, "expression": expression, **numbers,
                "basis": "saved_values", "recalculated": False,
                "inputs": [{**_ref(node), "value": self.value(node)} for node in sorted(set(nodes))],
                "limitations": ["Локальная арифметика сохранённых входов; не пересчёт Excel и не установленное финансовое влияние."]}

    def labels(self, node: Node) -> list[Node]:
        column, row = _address_parts(node[1])
        # Labels may be far left of a long forecast row; never take a label
        # from another row or silently assign an entity from its sheet name.
        labels = [(node[0], address) for c, address in self.rows[node[0], row]
                if c < column and isinstance(self.cell((node[0], address)).value, str)
                and not self.cell((node[0], address)).formula][:12]
        # A later, different metric starts a separate horizontal block. Unit
        # labels do not replace the metric, but unknown labels are boundaries.
        boundaries = [i for i, ref in enumerate(labels)
                      if not _UNIT_LABEL.fullmatch(self.cell(ref).value.strip())]
        return labels[boundaries[-1]:] if boundaries else labels

    def has_label(self, node: Node, pattern) -> list[Node]:
        return [ref for ref in self.labels(node) if pattern.search(self.cell(ref).value)]

    def alias(self, expr: Expr, evidence: set[Node], seen=None) -> Expr:
        seen = set() if seen is None else set(seen)
        while expr[0] == "ref":
            node = expr[1], expr[2]
            if node in seen or len(seen) >= MAX_DEPTH:
                raise Unsupported("alias_cycle_or_depth_limit")
            if self.cell(node) is None:
                raise Unsupported("missing_reference")
            if not _finite(self.cell(node).value):
                raise Unsupported("missing_or_nonnumeric_alias_cache")
            seen.add(node)
            evidence.add(node)
            other = self.parsed.get(node)
            if not other or other[0] != "ref":
                break
            expr = other
        return expr

    def expanded(self, expr: Expr, evidence: set[Node]) -> Expr:
        expr = self.alias(expr, evidence)
        return self.parsed.get((expr[1], expr[2]), expr) if expr[0] == "ref" else expr

    def signature(self, expr: Expr, evidence: set[Node], depth=0, seen=frozenset(), budget=None) -> Expr:
        budget = [0] if budget is None else budget
        budget[0] += 1
        if budget[0] > MAX_EXPANSIONS:
            raise Unsupported("structural_expansion_limit")
        expr = self.alias(expr, evidence)
        if expr[0] == "ref":
            node = expr[1], expr[2]
            if node in seen:
                raise Unsupported("formula_cycle")
            other = self.parsed.get(node)
            # Opaque leaves retain their identity, NEVER their saved value.
            # Independent constant-only formulas (=100, =50+50) likewise
            # remain distinct sources even when their arithmetic is identical.
            if other is None or depth >= 3 or not self.references(other):
                return expr
            return self.signature(other, evidence, depth + 1, seen | {node}, budget)
        if expr[0] in {"number", "range"}:
            return expr
        children = expr[1] if expr[0] == "sum" else expr[1:]
        signatures = tuple(self.signature(x, evidence, depth, seen, budget) for x in children)
        if expr[0] in {"+", "*", "sum"}:
            signatures = tuple(sorted(signatures, key=repr))
        return expr[0], signatures

    def number(self, expr: Expr, evidence: set[Node], depth=0) -> float:
        if depth > MAX_DEPTH:
            raise Unsupported("arithmetic_depth_limit")
        if expr[0] == "number":
            return expr[1]
        if expr[0] == "ref":
            node = expr[1], expr[2]
            evidence.add(node)
            return self.value(node)
        if expr[0] not in {"+", "-", "*", "/", "^"}:
            raise Unsupported("unsupported_local_arithmetic")
        a, b = (self.number(x, evidence, depth + 1) for x in expr[1:])
        try:
            if expr[0] == "+": value = a + b
            elif expr[0] == "-": value = a - b
            elif expr[0] == "*": value = a * b
            elif expr[0] == "/": value = a / b
            else:
                if abs(b) > 1000:
                    raise Unsupported("power_limit")
                value = a ** b
        except (ArithmeticError, ValueError):
            raise Unsupported("invalid_local_arithmetic") from None
        if not _finite(value):
            raise Unsupported("nonfinite_local_arithmetic")
        return float(value)

    def discount(self, expr: Expr, evidence: set[Node]):
        expanded = self.expanded(expr, evidence)
        if expanded[0] != "/" or expanded[1] != ("number", 1.0) or expanded[2][0] != "^":
            return None
        base, period = expanded[2][1:]
        if base[0] != "+":
            return None
        if base[1] == ("number", 1.0): rate = base[2]
        elif base[2] == ("number", 1.0): rate = base[1]
        else: return None
        rate = self.alias(rate, evidence)
        period = self.alias(period, evidence)
        factor = self.number(expr, evidence)
        if not 0 < factor <= 1 or self.number(period, evidence) < 0:
            raise Unsupported("nonpositive_or_nonstandard_discount_factor")
        return {"signature": (rate, period), "rate": rate, "period": period, "factor": factor}

    def pv(self, expr: Expr, evidence: set[Node]):
        expanded = self.expanded(expr, evidence)
        if expanded[0] != "*":
            return None
        for flow, discount in ((expanded[1], expanded[2]), (expanded[2], expanded[1])):
            item = self.discount(discount, evidence)
            if item and flow[0] == "ref":
                flow = self.alias(flow, evidence)
                node = flow[1], flow[2]
                labels = self.has_label(node, _FCF)
                if labels:
                    evidence.update(labels)
                    self.value(node)
                    return {"flow": flow, "flow_node": node, "discount_expr": discount, **item}
        return None

    def sum_terms(self, expression: Expr) -> list[Node]:
        if expression[0] == "ref":
            return [(expression[1], expression[2])]
        if expression[0] == "range":
            return sorted(self.references(expression), key=lambda n: _address_parts(n[1]))
        if expression[0] in {"+", "sum"}:
            children = expression[1] if expression[0] == "sum" else expression[1:]
            result = [node for child in children for node in self.sum_terms(child)]
            if len(result) > MAX_RANGE_CELLS:
                raise Unsupported("sum_size_limit")
            return result
        raise Unsupported("unsupported_sum_shape")

    def valuation_date(self, sheet: str):
        choices = []
        for address, cell in self.wb.sheets[sheet].cells.items():
            if not isinstance(cell.value, str) or not _VALUATION_DATE.search(cell.value):
                continue
            column, row = _address_parts(address)
            for offset in range(1, 5):
                target = self.positions[sheet].get((column + offset, row))
                value = self.cell((sheet, target)).value if target else None
                if not isinstance(value, str):
                    continue
                for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"):
                    try:
                        parsed = datetime.strptime(value.strip(), fmt).date()
                    except ValueError:
                        continue
                    choices.append((parsed, {(sheet, address), (sheet, target)}))
                    break
        dates = {value for value, _ in choices}
        if len(dates) > 1:
            raise Unsupported("ambiguous_valuation_dates")
        return choices[0] if choices else None

    def period_year(self, node: Node):
        column, row = _address_parts(node[1])
        choices = []
        for header_row in range(row - 1, max(0, row - 80), -1):
            address = self.positions[node[0]].get((column, header_row))
            cell = self.cell((node[0], address)) if address else None
            value = cell.value if cell else None
            if type(value) not in (int, float) or not _finite(value) or value != int(value) or not 1900 <= value <= 2199:
                continue
            label_nodes = [(node[0], a) for c, a in self.rows[node[0], header_row]
                           if c < column and isinstance(self.cell((node[0], a)).value, str)]
            labels = [self.cell(x).value.strip() for x in label_nodes]
            if any(not (_YEAR_HEADER_LABEL.fullmatch(label) or _CALENDAR_YEAR_LABEL.fullmatch(label)) for label in labels):
                continue
            # A raw serial date or period counter is not a calendar year,
            # even if its saved integer happens to lie in the year range.
            if any(_NON_YEAR_TIME_LABEL.fullmatch(label) for label in labels):
                continue
            explicit_calendar = any(_CALENDAR_YEAR_LABEL.fullmatch(label) for label in labels)
            # Unlabelled headers still need a neighboring sequential year.
            # An explicit year label also permits skipped calendar years, but
            # never an isolated number or conflicting local year ordering.
            neighbors = []
            for delta in (-2, -1, 1, 2):
                other = self.positions[node[0]].get((column + delta, header_row))
                other_cell = self.cell((node[0], other)) if other else None
                other_value = other_cell.value if other_cell else None
                if _finite(other_value) and other_value == int(other_value) and 1900 <= other_value <= 2199:
                    neighbors.append((delta, other_value, (node[0], other)))
            sequence = sorted([(0, value, (node[0], address)), *neighbors])
            if neighbors and any(left[1] >= right[1] for left, right in zip(sequence, sequence[1:])):
                raise Unsupported("ambiguous_period_headers")
            if neighbors and (explicit_calendar or any(other_value == value + delta for delta, other_value, _ in neighbors)):
                choices.append((int(value), {(node[0], address), *(entry[2] for entry in neighbors), *label_nodes}))
        years = {year for year, _ in choices}
        if len(years) > 1:
            raise Unsupported("ambiguous_period_headers")
        return choices[0] if choices else None

    def pv_sums(self):
        found = False
        for node, cell in (( (s, a), c) for s, data in self.wb.sheets.items() for a, c in data.cells.items()):
            labels = self.has_label(node, _FCF) if cell.formula else []
            if not labels or not self.has_label(node, _PV) or not re.match(r"^=?\s*SUM\s*\(", cell.formula or "", re.I):
                continue
            found = True
            try:
                if node not in self.parsed:
                    raise Unsupported(self.parse_errors.get(node, "unsupported_sum"))
                terms = self.sum_terms(self.parsed[node])
                groups = defaultdict(list)
                pv_items = []
                for term in terms:
                    if not self.cell(term) or self.cell(term).value is None:
                        continue
                    evidence = {node, term, *labels}
                    item = self.pv(("ref", *term), evidence)
                    if item:
                        period_evidence = set(evidence)
                        signature = self.signature(item["flow"], evidence), item["signature"]
                        groups[signature].append((term, evidence))
                        pv_items.append((term, item, period_evidence))
                for members in groups.values():
                    if len(members) < 2:
                        continue
                    evidence = set().union(*(entry[1] for entry in members))
                    addresses = [entry[0] for entry in members]
                    self.finding("pv_fcff_duplicate_term", "Структурно повторённый поток в сумме PV FCFF",
                        "Несколько включённых слагаемых сводятся к одному источнику FCFF и одному коэффициенту дисконтирования через формулы/alias. Совпадение чисел не использовалось как доказательство. Назначение повторного включения требует проверки.", evidence,
                        self.calculation("Сохранённые значения повторённых слагаемых", "sum(repeated_terms)", addresses,
                                         value=sum(self.value(x) for x in addresses)))
                self.check("pv_fcff_duplicate_term", "checked", f"Проверено дисконтированных слагаемых: {len(pv_items)}.", {node, *labels})
                valuation = self.valuation_date(node[0])
                if not valuation:
                    self.check("pv_fcff_historical_period", "unsupported", "Нет однозначной текстовой даты с явной подписью даты оценки.", {node})
                    continue
                for term, item, evidence in pv_items:
                    period = self.period_year(item["flow_node"])
                    if not period:
                        self.check("pv_fcff_historical_period", "unsupported", "Не найден однозначный календарный заголовок потока.", {node, term})
                        continue
                    year, headers = period
                    if date(year, 12, 31) < valuation[0]:
                        self.finding("pv_fcff_historical_period", "Завершённый календарный период в сумме PV FCFF",
                            f"В сумму включён поток за {year}; календарный год завершён до явно указанной даты оценки {valuation[0].isoformat()}. Нужна проверка границ прогнозного ряда и назначения этой суммы; автоматическое исключение не выполнялось.",
                            evidence | headers | valuation[1])
                self.check("pv_fcff_historical_period", "checked", "Сопоставлены найденные годовые заголовки и явная дата оценки.", {node} | valuation[1])
            except Unsupported as exc:
                status = "ambiguous" if "ambiguous" in str(exc) else "unsupported"
                self.check("pv_fcff_sum", status, str(exc), {node})
        if not found:
            self.check("pv_fcff_sum", "not_applicable", "Не найден поддерживаемый SUM с явной подписью дисконтированных FCFF.")

    def irr_discounted_flows(self):
        """Inspect IRR inputs; never solve or replace an IRR output."""
        found = False
        for sheet, data in self.wb.sheets.items():
            for address, cell in data.cells.items():
                if not cell.formula or not re.match(r"^\s*=\s*IRR\s*\(", cell.formula, re.I):
                    continue
                found = True
                node, evidence = (sheet, address), {(sheet, address)}
                try:
                    if len(cell.formula) > MAX_FORMULA:
                        raise Unsupported("formula_size_limit")
                    body = re.sub(r"^\s*=\s*IRR\s*", "", cell.formula, count=1, flags=re.I)
                    parsed = _Parser("SUM" + body, sheet).parse()
                    args = parsed[1]
                    if not 1 <= len(args) <= 2 or args[0][0] != "range":
                        raise Unsupported("irr_requires_one_explicit_range")
                    if len(args) == 2 and args[1][0] != "number":
                        raise Unsupported("irr_guess_not_literal")
                    c1, r1, c2, r2 = args[0][2]
                    if c1 != c2 and r1 != r2:
                        raise Unsupported("irr_range_not_one_dimensional")
                    terms = self.sum_terms(args[0])
                    if not 2 <= len(terms) <= 48:
                        raise Unsupported("irr_range_size_limit")
                    amounts, discounted, rate_signatures = [], [], set()
                    for index, term in enumerate(terms):
                        evidence.add(term)
                        amounts.append(self.value(term))
                        try:
                            item = self.pv(("ref", *term), evidence)
                        except Unsupported:
                            if index != 0:
                                raise
                            item = None
                        if item:
                            period = self.number(item["period"], evidence)
                            if period > 0 and item["factor"] < 1:
                                discounted.append(term)
                            rate_signatures.add(repr(item["rate"]))
                            continue
                        # The initial outflow may be undiscounted, including
                        # FCFF * a literal/cached constant 1 at time zero.
                        expr = self.expanded(("ref", *term), evidence)
                        initial = False
                        if index == 0 and amounts[-1] < 0:
                            if self.has_label(term, _FCF):
                                evidence.update(self.has_label(term, _FCF)); initial = True
                            elif expr[0] == "*":
                                for flow, factor in ((expr[1], expr[2]), (expr[2], expr[1])):
                                    if flow[0] != "ref":
                                        continue
                                    flow = self.alias(flow, evidence)
                                    labels = self.has_label((flow[1], flow[2]), _FCF)
                                    constant = self.expanded(factor, evidence)
                                    if labels and constant == ("number", 1.0):
                                        evidence.update(labels); self.value((flow[1], flow[2])); initial = True
                                    elif labels and factor[0] == "ref" and not self.cell((factor[1], factor[2])).formula and self.number(factor, evidence) == 1:
                                        evidence.update(labels); self.value((flow[1], flow[2])); initial = True
                                    elif labels and constant[0] == "/" and constant[1] == ("number", 1.0) and constant[2][0] == "^" and self.number(constant[2][2], evidence) == 0 and self.number(factor, evidence) == 1:
                                        # A cached factor one at explicit t=0:
                                        # the blank initial-rate cell is not
                                        # interpreted as a forecast rate.
                                        evidence.update(labels); self.value((flow[1], flow[2])); initial = True
                        if not initial:
                            raise Unsupported("irr_input_not_supported_discounted_fcff_or_initial_outflow")
                    if len(rate_signatures) > 1:
                        raise Unsupported("ambiguous_irr_discount_rates")
                    if discounted and min(amounts) < 0 < max(amounts):
                        self.finding("irr_on_discounted_cashflows", "IRR применён к уже дисконтированным потокам",
                            "Входной ряд IRR содержит FCFF, уже умноженные на коэффициенты 1/(1+r)^t. "
                            "Это кандидат смешения IRR и предварительного дисконтирования: проверьте назначение показателя. "
                            "IRR исходного ряда не вычислен и исправленное значение не утверждается.", evidence)
                    self.check("irr_on_discounted_cashflows", "checked", "Проверен явный одномерный ряд IRR и поддержанные источники FCFF.", evidence)
                except (Unsupported, ValueError, ArithmeticError, RecursionError) as exc:
                    self.check("irr_on_discounted_cashflows", "ambiguous" if "ambiguous" in str(exc) else "unsupported", str(exc), {node})
        if not found:
            self.check("irr_on_discounted_cashflows", "not_applicable", "Явные формулы IRR не найдены.")

    def fcff_profit_overlap(self):
        patterns = (re.compile(r"^(?:net\s+profit|net\s+income|чистая\s+прибыль)$", re.I),
                    re.compile(r"^NOPAT$", re.I))
        found = False
        for node, expression in self.parsed.items():
            labels = self.has_label(node, _FCF)
            if not labels:
                continue
            try:
                terms = self.sum_terms(expression)
            except Unsupported:
                continue
            selected = [[term for term in terms if self.has_label(term, pattern)] for pattern in patterns]
            if not all(selected):
                continue
            found = True
            if any(len(items) != 1 for items in selected):
                self.check("fcff_net_profit_nopat_overlap", "ambiguous", "Несколько слагаемых с одинаковой подписью прибыли.", {node})
                continue
            profit, nopat = selected[0][0], selected[1][0]
            evidence = {node, profit, nopat, *labels, *self.labels(profit), *self.labels(nopat)}
            try:
                # Strict shared additive provenance, not matching saved values.
                left = self.sum_terms(self.parsed[profit])
                right = self.sum_terms(self.parsed[nopat])
                signatures = Counter(self.signature(("ref", *x), evidence) for x in left)
                needed = Counter(self.signature(("ref", *x), evidence) for x in right)
                common = right
                if not common or any(count > signatures[key] for key, count in needed.items()):
                    raise Unsupported("profit_nopat_shared_additive_sources_not_proven")
                for item in (profit, nopat, *common):
                    self.value(item)
                evidence.update(common)
                self.finding("fcff_net_profit_nopat_overlap", "FCFF суммирует Net Profit и NOPAT с общими источниками",
                    "В положительном составе суммы FCFF одновременно присутствуют явно подписанные Net Profit/Net Income и NOPAT. "
                    "Все аддитивные источники NOPAT также включены в Net Profit. Это кандидат повторного включения прибыли; "
                    "периметр, знаки и методология FCFF требуют проверки, исправленный поток не вычислялся.", evidence)
                self.check("fcff_net_profit_nopat_overlap", "checked", "Совпадение подтверждено аддитивными источниками, не равенством чисел.", evidence)
            except (Unsupported, KeyError, ValueError, ArithmeticError, RecursionError) as exc:
                self.check("fcff_net_profit_nopat_overlap", "unsupported", str(exc), {node, profit, nopat})
        if not found:
            self.check("fcff_net_profit_nopat_overlap", "not_applicable", "Не найдена прямая сумма FCFF с однозначными Net Profit и NOPAT.")

    def gordon(self, expr: Expr, evidence: set[Node]):
        if expr[0] != "/" or expr[2][0] != "-" or expr[1][0] != "*":
            return None
        rate, growth = (self.alias(x, evidence) for x in expr[2][1:])
        for pv_expr, growth_part in ((expr[1][1], expr[1][2]), (expr[1][2], expr[1][1])):
            if growth_part[0] != "+":
                continue
            candidate = growth_part[2] if growth_part[1] == ("number", 1.0) else growth_part[1] if growth_part[2] == ("number", 1.0) else None
            if candidate is None or self.alias(candidate, evidence) != growth:
                continue
            item = self.pv(pv_expr, evidence)
            if item and item["rate"] == rate:
                if self.number(rate, evidence) <= self.number(growth, evidence):
                    raise Unsupported("gordon_denominator_nonpositive")
                return item
        return None

    def discount_powers(self, expr: Expr, evidence: set[Node], depth=0):
        """Signed multiplicities, so DF/DF never masquerades as repetition."""
        if depth > MAX_DEPTH:
            raise Unsupported("discount_factor_depth_limit")
        direct = self.discount(expr, evidence)
        if direct:
            return [{**direct, "power": 1.0, "inputs": self.references(expr)}]
        expanded = self.expanded(expr, evidence)
        if expanded[0] in {"+", "-"}:
            # A cached scalar weight may be a complement such as 1-other_weight.
            # Its saved value still controls exposure; retain the expression's
            # direct operands and aliases without recalculating the weight or
            # recursively expanding unrelated valuation/rate dependencies.
            sources = self.references(expanded)
            if len(sources) > 16:
                raise Unsupported("weight_evidence_source_limit")
            for source in sorted(sources):
                self.alias(("ref", *source), evidence)
            return []
        if expanded[0] in {"*", "/"}:
            left = self.discount_powers(expanded[1], evidence, depth + 1)
            right = self.discount_powers(expanded[2], evidence, depth + 1)
            if expanded[0] == "/":
                right = [{**record, "power": -record["power"]} for record in right]
            return left + right
        if expanded[0] == "^":
            # A reciprocal of the normal discount denominator is DF itself.
            inverse = self.discount(("/", ("number", 1.0), expanded), evidence)
            if inverse:
                return [{**inverse, "power": -1.0, "inputs": self.references(expr)}]
            records = self.discount_powers(expanded[1], evidence, depth + 1)
            if records:
                power = self.number(expanded[2], evidence)
                if not power.is_integer() or abs(power) > 8:
                    raise Unsupported("unsupported_discount_power")
                return [{**record, "power": record["power"] * power} for record in records]
        return []

    def exposure(self, expr: Expr, target: Node, reachable: set[Node], evidence: set[Node], seen=frozenset(), depth=0):
        if depth > MAX_DEPTH:
            raise Unsupported("downstream_depth_limit")
        if expr[0] == "ref":
            node = expr[1], expr[2]
            if node == target:
                evidence.add(node)
                return 1.0, []
            if node not in reachable:
                return None
            if node in seen or node not in self.parsed:
                raise Unsupported("downstream_cycle_or_unsupported")
            evidence.add(node)
            return self.exposure(self.parsed[node], target, reachable, evidence, seen | {node}, depth + 1)
        if expr[0] not in {"+", "-", "*", "/"}:
            return None
        left, right = (self.exposure(x, target, reachable, evidence, seen, depth + 1) for x in expr[1:])
        if left and right:
            raise Unsupported("ambiguous_multiple_target_paths")
        if not left and not right:
            return None
        if expr[0] in {"+", "-"}:
            result = left or right
            return ((-result[0] if expr[0] == "-" and right else result[0]), result[1])
        if expr[0] == "/" and right:
            raise Unsupported("nonlinear_downstream_target")
        active, other = (left, expr[2]) if left else (right, expr[1])
        multiplier_expr = ("/", ("number", 1.0), other) if expr[0] == "/" else other
        multiplier = self.number(multiplier_expr, evidence)
        discounts = self.discount_powers(multiplier_expr, evidence)
        coefficient = active[0] * multiplier
        if not math.isfinite(coefficient):
            raise Unsupported("nonfinite_downstream_coefficient")
        return coefficient, active[1] + discounts

    def gordon_discounts(self):
        found = False
        for node, expr in self.parsed.items():
            evidence = {node}
            recognized = False
            try:
                item = self.gordon(expr, evidence)
                if item is None:
                    continue
                recognized = True
                found = True
                queue, reachable, ordered = deque([(node, 0)]), {node}, []
                while queue:
                    current, depth = queue.popleft()
                    if depth >= 5:
                        continue
                    for other in sorted(self.reverse.get(current, ())):
                        if other in reachable:
                            continue
                        if len(reachable) >= 150:
                            raise Unsupported("downstream_node_limit")
                        reachable.add(other)
                        ordered.append(other)
                        queue.append((other, depth + 1))
                matched = False
                for other in ordered:
                    path_evidence = set(evidence)
                    exposure = self.exposure(("ref", *other), node, reachable, path_evidence)
                    records = [x for x in exposure[1] if x["signature"] == item["signature"]] if exposure else []
                    net_power = sum(x["power"] for x in records)
                    if exposure and exposure[0] > 0 and net_power > 0:
                        if any(x["factor"] != item["factor"] for x in records):
                            raise Unsupported("inconsistent_saved_discount_factor_caches")
                        discount_inputs = self.references(item["discount_expr"]) | set().union(*(x["inputs"] for x in records))
                        total_power = 1 + net_power
                        self.finding("gordon_double_discount", "Повторный коэффициент дисконтирования в ветви Gordon",
                            "Числитель Gordon использует уже дисконтированный FCFF. В последующей линейной ветви тот же коэффициент применяется повторно; учтены alias и сохранённые веса выбора. Это структурный кандидат на двойное дисконтирование, а не установленная корректировка стоимости бизнеса.", path_evidence,
                            self.calculation("Совокупный коэффициент в ветви Gordon", f"discount_factor ** {total_power:g}", discount_inputs,
                                             value=item["factor"] ** total_power, unit="доля"))
                        matched = True
                        break
                self.check("gordon_double_discount", "checked", "Проверены доступные линейные downstream-ветви; совпадение коэффициента " + ("найдено." if matched else "не найдено."), evidence)
            except Unsupported as exc:
                # Only surface candidates already identified as financial.
                if self.has_label(node, re.compile(r"gordon|\bggm\b|гордон", re.I)) or recognized:
                    self.check("gordon_double_discount", "ambiguous" if "ambiguous" in str(exc) else "unsupported", str(exc), {node})
        if not found:
            self.check("gordon_double_discount", "not_applicable", "Не найдена поддерживаемая формула Gordon на уже дисконтированном FCFF.")

    def data_tables(self):
        found = False
        for sheet, data in self.wb.sheets.items():
            for address, cell in data.cells.items():
                if cell.formula_type != "dataTable":
                    continue
                found = True
                node = sheet, address
                try:
                    attrs = cell.formula_attributes
                    if attrs.get("dt2D") != "1" or attrs.get("dtr") != "1":
                        raise Unsupported("unsupported_datatable_orientation")
                    c1, r1, c2, r2 = parse_range(attrs.get("ref", ""))
                    if (c1, r1) != _address_parts(address):
                        raise Unsupported("datatable_anchor_range_mismatch")
                    if c1 <= 1 or r1 <= 1 or (c2 - c1 + 1) * (r2 - r1 + 1) > MAX_RANGE_CELLS:
                        raise Unsupported("datatable_bounds_limit")
                    first = _Parser(attrs.get("r1", ""), sheet).parse()
                    second = _Parser(attrs.get("r2", ""), sheet).parse()
                    if first[0] != "ref" or second[0] != "ref":
                        raise Unsupported("unsupported_datatable_input")
                    input1, input2 = (first[1], first[2]), (second[1], second[2])
                    value1, value2 = self.value(input1), self.value(input2)
                    top = [(sheet, f"{_column_letters(c)}{r1 - 1}") for c in range(c1, c2 + 1)]
                    left = [(sheet, f"{_column_letters(c1 - 1)}{r}") for r in range(r1, r2 + 1)]
                    def matches(cells, value):
                        return [x for x in cells if self.cell(x) and _finite(self.cell(x).value)
                                and math.isclose(self.value(x), value, rel_tol=1e-6, abs_tol=1e-7)]
                    across, down = matches(top, value1), matches(left, value2)
                    if len(across) != 1 or len(down) != 1:
                        raise Unsupported("ambiguous_basepoint_axes" if across and down else "no_near_basepoint_axes")
                    corner = sheet, f"{_column_letters(c1 - 1)}{r1 - 1}"
                    corner_expr = self.parsed.get(corner)
                    if not corner_expr or corner_expr[0] != "ref":
                        raise Unsupported("unsupported_datatable_corner_target")
                    evidence = {node, corner, input1, input2, across[0], down[0]}
                    target = self.alias(corner_expr, evidence)
                    target_node = target[1], target[2]
                    point = sheet, _column_letters(_address_parts(across[0][1])[0]) + str(_address_parts(down[0][1])[1])
                    evidence.add(point)
                    actual, base = self.value(point), self.value(target_node)
                    if not math.isclose(actual, base, rel_tol=1e-6, abs_tol=1e-7):
                        exact = self.value(across[0]) == value1 and self.value(down[0]) == value2
                        self.finding("datatable_basepoint_cache_mismatch", "Расхождение сохранённой точки таблицы чувствительности",
                            "Сохранённая ячейка dataTable при " + ("совпадающих" if exact else "близких, но не строго равных") + " входных осях отличается от сохранённого целевого выхода. Это наблюдение кэша; причина, актуальность пересчёта и финансовое влияние не установлены.", evidence,
                            self.calculation("Сохранённая точка минус целевой выход", "table_saved - target_saved", {point, target_node},
                                             actual=actual, expected=base, difference=actual - base), status="observation")
                    self.check("datatable_basepoint_cache_mismatch", "checked", "Сопоставлены сохранённые значения; оси: rel_tol=1e-6, abs_tol=1e-7. DataTable не пересчитывалась.", evidence)
                except (Unsupported, ValueError) as exc:
                    self.check("datatable_basepoint_cache_mismatch", "ambiguous" if "ambiguous" in str(exc) else "unsupported", str(exc), {node})
        if not found:
            self.check("datatable_basepoint_cache_mismatch", "not_applicable", "Таблицы dataTable не найдены.")


def run_financial_checks(workbook: Workbook) -> dict:
    """Return source-bound candidates without mutating or recalculating a book."""
    checks = _Checks(workbook)
    checks.pv_sums()
    checks.gordon_discounts()
    checks.data_tables()
    checks.irr_discounted_flows()
    checks.fcff_profit_overlap()
    counts = {status: sum(item["status"] == status for item in checks.checks)
              for status in ("checked", "unsupported", "ambiguous")}
    return {"version": VERSION, "status": "limited", "findings": checks.findings,
            "checks": checks.checks,
            "summary": {"candidate_count": sum(x["status"] == "candidate" for x in checks.findings),
                        "observation_count": sum(x["status"] == "observation" for x in checks.findings),
                        **{key + "_count": value for key, value in counts.items()},
                        "formula_count": checks.formulas_seen, "parsed_formula_count": len(checks.parsed)},
            "limitations": ["Проверка ограничена поддерживаемыми арифметическими/SUM/A1 паттернами и явными финансовыми подписями; отсутствие находок не подтверждает правильность модели.",
                "Внешние ссылки, динамические/структурированные ссылки, defined names, IF, массивы и произвольные функции не вычисляются.",
                "Используются сохранённые значения и веса. Excel, циклы и dataTable не пересчитывались.",
                "Проверка не определяет финансовую существенность, справедливую стоимость или утверждённый исправленный EV.",
                "Годы связываются только с поддержанными последовательными заголовками; неоднозначные даты и периоды не угадываются.",
                *sorted(checks.limits)]}
