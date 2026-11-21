"""Bounded source-linked questions, not a financial verdict or Excel evaluator.

The plan reveals only anchors. A deliberate inspect call returns the selected
question's sources, including absent explicit references. Coverage measures
delivery of those sources, not agent understanding or economic validation.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import math
import re

from .facts import _is_unit
from .references import DependencyGraph, parse_range
from .workbook import Workbook, _address_parts

VERSION = "2026-11-04.1"
MAX_SOURCE_CELLS = 120
MAX_CELL_TEXT = 4000
_OUTPUT = re.compile(r"^(?:EV|enterprise value|NPV|net present value|ЧДД|чистая привед[её]нная стоимость)$", re.I)
_RATE = re.compile(r"^(?:WACC|weighted average cost of capital|discount rate|ставка дисконтирования|средневзвешенная стоимость капитала)$", re.I)
_FLOW = re.compile(r"^(?:FCFF|FCF|free cash flow(?: to (?:the )?firm)?|свободный денежный поток)$", re.I)
_REVENUE = re.compile(r"^(?:revenue(?:[, ]+total)?|total revenue|net revenue|sales|выручка)$", re.I)
_GROWTH = re.compile(r"^(?:LTGR|terminal growth(?: rate)?|long term growth(?: rate)?|темп терминального роста)$", re.I)
_CALENDAR = re.compile(r"(?:valuation date|project start|start date|end date|period start|period end|period length|length of period|forecast step|time step|discount period|discounting period|period number|mid.year|дата начала|дата оценки|начало периода|конец периода|номер периода|длительность периода|продолжительность периода|шаг (?:прогнозирования|расч[её]та))", re.I)
_TIME_UNITS = {"month", "months", "mo", "mo.", "mos", "мес", "мес.", "месяц", "месяца", "месяцев",
               "day", "days", "день", "дня", "дней", "дн", "дн.", "year", "years", "yr", "yrs", "год", "года", "лет",
               "quarter", "quarters", "кв", "кв.", "квартал", "квартала", "кварталов"}
_PERIOD_START = re.compile(r"^(?:начало периода|period start|start of period)$", re.I)
_PERIOD_END = re.compile(r"^(?:конец периода|period end|end of period)$", re.I)
_ROLES = [
    ("risk_free", r"risk.?free|безрисков"), ("specific_risk", r"specific risk|специфическ"),
    ("equity_risk_premium", r"equity risk premium|премия.*акци"),
    ("risk_premium", r"premium|risk|преми|риск"), ("beta", r"beta|бета"),
    ("cost_of_equity", r"cost of equity|стоимость собственного"),
    ("cost_of_debt", r"cost of debt|стоимость долга|ставка.*кредит"),
    ("tax", r"tax|налог"), ("capital_weight", r"share|weight|debt\s*/\s*equity|(?:d|e)\s*/\s*\(\s*d\s*\+\s*e\s*\)|доля|вес"),
    ("discount_rate", _RATE.pattern), ("terminal_growth", _GROWTH.pattern),
    ("cashflow", _FLOW.pattern), ("operating_driver", r"revenue|sales|ebit|profit|nopat|capex|nwc|d&a|выруч|прибыл|затрат|амортиз|капитал"),
]
_LIMITATIONS = [
    "План и структурные вопросы предложены кодом; получение источников агентом не является самостоятельным обнаружением или финансовым подтверждением.",
    "Исследуются ограниченные статические зависимости выбранных подписанных выходов. Не определяются активные ветви IF, бизнес-периметр, рыночная реалистичность и финансовая существенность.",
    "Значения сохранены в файле. Даты Excel не преобразуются, номера периодов не объявляются календарными годами; книга не пересчитывается.",
]


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _ref(node):
    return {"sheet": node[0], "cell": node[1]}


def _node(ref):
    if isinstance(ref, dict) and isinstance(ref.get("sheet"), str) and isinstance(ref.get("cell"), str):
        return ref["sheet"], ref["cell"]
    return None


def _normal(value):
    return re.sub(r"\s+", " ", str(value).strip())


def _context_unit(value):
    """Raw time-unit labels preserve a local label block; no date conversion."""
    return _is_unit(value) or isinstance(value, str) and value.strip().casefold() in _TIME_UNITS


class _Builder:
    def __init__(self, workbook, graph, limits):
        self.wb, self.graph, self.limits = workbook, graph or DependencyGraph(workbook), limits
        self.rows = defaultdict(list)
        self.labels, self.contexts = {}, {}
        self.tables = defaultdict(list)
        for sheet, data in workbook.sheets.items():
            for address, cell in data.cells.items():
                column, row = _address_parts(address)
                self.rows[sheet, row].append((column, address))
                if cell.formula_type == "dataTable" and cell.formula_attributes.get("ref"):
                    try:
                        self.tables[sheet].append(parse_range(cell.formula_attributes["ref"]))
                    except ValueError:
                        pass
        for (sheet, _), row in self.rows.items():
            label, context = None, []
            for _, address in sorted(row):
                cell = self.cell((sheet, address))
                calendar_literal = isinstance(cell.value, str) and (re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", cell.value.strip()) or
                    label and _CALENDAR.search(str(self.cell(label).value)) and cell.value.strip().lower() in {"yes", "no", "да", "нет"})
                if isinstance(cell.value, str) and cell.value.strip() and not cell.formula and cell.data_type != "e" and not calendar_literal:
                    if not _context_unit(cell.value):
                        label, context = (sheet, address), []
                    else:
                        context.append((sheet, address))
                else:
                    self.labels[sheet, address] = label
                    self.contexts[sheet, address] = ([label] if label else []) + context

    def cell(self, node):
        sheet = self.wb.sheets.get(node[0])
        return sheet.cells.get(node[1]) if sheet else None

    def label(self, node):
        ref = self.labels.get(node)
        return _normal(self.cell(ref).value) if ref else ""

    def table(self, node):
        col, row = _address_parts(node[1])
        return any(c1 - 1 <= col <= c2 and r1 - 1 <= row <= r2 for c1, r1, c2, r2 in self.tables[node[0]])

    def walk(self, roots, *, stop=None, count=None, depth=None):
        count, depth = count or self.limits["max_nodes"], depth or self.limits["max_depth"]
        parents, edges, reasons = {}, set(), set()
        queue = deque()
        for root in roots:
            if root not in parents and len(parents) < count:
                parents[root] = None
                queue.append((root, 0))
        while queue:
            node, level = queue.popleft()
            if stop and stop(node):
                continue
            children = self.graph.forward.get(node, ())
            if level >= depth:
                if set(children) - parents.keys(): reasons.add("depth_limit")
                continue
            for other in sorted(children):
                if other not in parents:
                    if len(parents) >= count:
                        reasons.add("node_limit")
                        continue
                    parents[other] = node
                    queue.append((other, level + 1))
                edges.add((node, other))
        if any(self.graph.issues.get(node) for node in parents): reasons.add("unresolved_reference_semantics")
        if any(node in self.graph._cycle_index for node in parents): reasons.add("cycle")
        return parents, edges, reasons

    @staticmethod
    def path(parents, node):
        path = []
        while node is not None and node not in path:
            path.append(node)
            node = parents.get(node)
        return list(reversed(path))

    def sources(self, nodes):
        result = set(nodes)
        for node in nodes:
            result.update(self.contexts.get(node, ()))
        return result

    def issue(self, code, detail, nodes):
        return {"code": code, "detail": detail, "evidence": [_ref(n) for n in sorted(set(nodes))]}

    def question(self, category, output, subject, nodes, edges, reasons, paths, extra_issues=()):
        nodes = self.sources(nodes | {output, subject})
        if len(nodes) > MAX_SOURCE_CELLS:
            # Keep paths and their anchors first; never silently call a subset complete.
            priority = list(dict.fromkeys([output, subject] + [n for p in paths for n in p]))
            nodes = set((priority + sorted(nodes - set(priority)))[:MAX_SOURCE_CELLS])
            reasons.add("source_limit")
        issues = list(extra_issues)
        for node in sorted(nodes):
            cell = self.cell(node)
            if cell is None or cell.value is None and not cell.formula:
                if any(b == node for _, b in edges):
                    issues.append(self.issue("blank_dependency", "Явная формула ссылается на отсутствующий или пустой источник; экономическое назначение нуля не установлено.", [node] + [a for a, b in edges if b == node]))
            elif cell.formula and cell.value is None:
                issues.append(self.issue("missing_saved_value", "У формульной ячейки отсутствует сохранённое значение.", [node]))
            elif cell.data_type == "e" or type(cell.value) in (int, float) and not _finite(cell.value):
                issues.append(self.issue("unsupported_saved_value", "Сохранённое значение ошибочно или не является конечным числом.", [node]))
            for item in self.graph.issues.get(node, ()):
                issues.append(self.issue(item["code"], item["detail"], [node]))
        complete_issues = [i for i in issues if all(_node(r) in nodes for r in i["evidence"])]
        if len(complete_issues) != len(issues): reasons.add("issue_source_limit")
        issues = complete_issues
        titles = {"discount_rate_components": "Источники ставки и её компонентов", "cashflow_drivers": "Состав денежного потока и ближайшие финансовые источники",
                  "operating_assumptions": "Предпосылки выбранного ряда выручки", "calendar": "Календарь, номера периодов и границы прогноза"}
        identifier = hashlib.sha256(json.dumps([category, output, subject], ensure_ascii=False).encode()).hexdigest()[:20]
        components = []
        for node in sorted(nodes):
            cell, label = self.cell(node), self.label(node)
            if cell is None or not label or not (_finite(cell.value) or cell.formula): continue
            role = next((name for name, pattern in _ROLES if re.search(pattern, label, re.I)), None)
            if role is None: continue
            context = self.contexts.get(node, ())
            unit = next((n for n in context if _context_unit(self.cell(n).value)), None)
            label_node = self.labels.get(node)
            if label_node not in nodes: continue
            components.append({"role": role, "label": self.cell(label_node).value, "source": _ref(node), "label_source": _ref(label_node),
                "value": cell.value if _finite(cell.value) else None,
                "unit": self.cell(unit).value if unit in nodes else None, "unit_source": _ref(unit) if unit in nodes else None})
        paths = [[_ref(n) for n in path] for path in paths if all(n in nodes for n in path)]
        ordered = sorted(nodes)
        indexes = {node: i for i, node in enumerate(ordered)}
        return {"id": "investigation_" + identifier, "category": category, "title": titles[category], "output": _ref(output), "subject": _ref(subject),
            "origin": "deterministic_investigation", "status": "unresolved" if reasons or issues else "candidate",
            "question": {"discount_rate_components": "Исследуй формулу ставки, состав премий, веса, налог и первичные источники. Арифметическая согласованность не доказывает рыночную обоснованность.",
                "cashflow_drivers": "Проследи ближайший состав выбранных FCFF. Этот ограниченный frontier не покрывает коммерческие предпосылки цены, объёма или конверсии и не доказывает все периоды.",
                "operating_assumptions": "Исследуй ближайшие upstream-источники выбранных значений выручки. Установи, есть ли цена/объём/темп роста, либо явно отметь предел; наличие пути не доказывает реалистичность или охват всех компаний и периодов.",
                "calendar": "Проверь источники начала/конца периода и шага, повторы и пустые зависимости. Отличай календарные даты от индексов дисконтирования; не выдумывай год."}[category],
            "evidence": [_ref(n) for n in ordered], "components": components,
            "dependency_paths": paths, "dependency_links": [[indexes[a], indexes[b]] for a, b in sorted(edges) if a in nodes and b in nodes],
            "dependency_link_encoding": "zero_based_indexes_into_evidence; direction=formula_to_source",
            "issues": issues[:30], "completeness": {"complete": not reasons and len(issues) <= 30, "reasons": sorted(reasons | ({"issue_limit"} if len(issues) > 30 else set()))},
            "financial_materiality": "not_assessed", "recalculated": False}

    def build(self):
        outputs = []
        for node, label_node in sorted(self.labels.items()):
            cell = self.cell(node)
            if (self.wb.sheets[node[0]].state == "visible" and label_node and _OUTPUT.fullmatch(self.label(node))
                    and cell.formula and _finite(cell.value) and not self.table(node)):
                outputs.append(node)
        # Scalar EV/NPV rows are anchors; aliases to an already chosen output are not separate models.
        chosen = []
        for node in outputs:
            ancestors, _, _ = self.walk([node], count=100, depth=6)
            if not any(other != node and other in ancestors for other in outputs): chosen.append(node)
        chosen = chosen[:2]
        questions = []
        for output in chosen:
            stop = lambda n: n != output and bool(_RATE.fullmatch(self.label(n)) or _FLOW.fullmatch(self.label(n)))
            parents, outer_edges, outer_reasons = self.walk([output], stop=stop)
            rate_roots = sorted(n for n in parents if _RATE.fullmatch(self.label(n)))
            flow_roots = sorted(n for n in parents if _FLOW.fullmatch(self.label(n)))
            if rate_roots:
                # Repeated period aliases use the same rate; a representative
                # root suffices for composition, not proof of all period rates.
                capital_base = lambda n: n not in rate_roots and bool(re.fullmatch(r"(?:total debt|debt|equity|cash|долг|собственный капитал)", self.label(n), re.I))
                rate_nodes, edges, reasons = self.walk(rate_roots[:1], stop=capital_base, count=min(60, self.limits["max_nodes"]))
                if any(capital_base(n) and self.graph.forward.get(n) for n in rate_nodes): reasons.add("capital_balance_sources_not_expanded")
                paths = [self.path(parents, n) for n in rate_roots[:1]]
                nodes = set(rate_nodes) | {n for p in paths for n in p}
                questions.append(self.question("discount_rate_components", output, rate_roots[0], nodes, edges | {(a, b) for p in paths for a, b in zip(p, p[1:])}, reasons, paths))
            else:
                questions.append(self.question("discount_rate_components", output, output, {output}, set(), {"discount_rate_not_recognized"} | outer_reasons, [[output]]))
            if flow_roots:
                # Inspect both selected endpoints per source row, not every repeated period.
                rows = defaultdict(list)
                for node in flow_roots: rows[node[0], _address_parts(node[1])[1]].append(node)
                endpoints = list(dict.fromkeys(n for row in rows.values() for n in (min(row, key=lambda x: _address_parts(x[1])[0]), max(row, key=lambda x: _address_parts(x[1])[0]))))[:4]
                flow_nodes, edges, reasons = self.walk(endpoints, count=min(24, self.limits["max_nodes"]), depth=2)
                paths = [self.path(parents, n) for n in endpoints]
                nodes = set(flow_nodes) | {n for p in paths for n in p}
                questions.append(self.question("cashflow_drivers", output, endpoints[0], nodes, edges | {(a, b) for p in paths for a, b in zip(p, p[1:])}, reasons, paths))
            else:
                questions.append(self.question("cashflow_drivers", output, output, {output}, set(), {"cashflow_boundary_not_recognized"} | outer_reasons, [[output]]))
            if flow_roots:
                discovery, _, discovery_reasons = self.walk(flow_roots, count=self.limits["max_nodes"],
                    stop=lambda n: bool(_REVENUE.fullmatch(self.label(n)) or _RATE.fullmatch(self.label(n))))
                revenue = [n for n in discovery if _REVENUE.fullmatch(self.label(n))]
                revenue.sort(key=lambda n: (not bool(re.search(r"\btotal\b", self.label(n), re.I)), len(self.path(discovery, n)), n))
                if revenue:
                    first = revenue[0]
                    row = sorted([n for n in revenue if n[0] == first[0] and _address_parts(n[1])[1] == _address_parts(first[1])[1]], key=lambda n: _address_parts(n[1])[0])
                    # One source frontier fits the atomic tool envelope. Column
                    # order is a selection policy, not a claim about chronology.
                    selected = [row[-1]]
                    op_nodes, edges, reasons = self.walk(selected, count=min(24, self.limits["max_nodes"]), depth=2)
                    paths = []
                    for node in selected:
                        branch = self.path(discovery, node)
                        paths.append(self.path(parents, branch[0]) + branch[1:])
                    nodes = set(op_nodes) | {n for path in paths for n in path}
                    reasons.add("selected_revenue_frontier_only")
                    reasons.update("discovery_" + reason for reason in discovery_reasons)
                    if len(revenue) > len(selected): reasons.add("other_revenue_sources_not_inspected")
                    questions.append(self.question("operating_assumptions", output, selected[0], nodes,
                        edges | {(a, b) for path in paths for a, b in zip(path, path[1:])}, reasons, paths))
                else:
                    questions.append(self.question("operating_assumptions", output, output, {output}, set(),
                        {"revenue_dependency_not_recognized"} | discovery_reasons, [[output]]))
            related_sheets = {output[0], *(n[0] for n in flow_roots)}
            calendar_rows = defaultdict(list)
            for node in self.labels:
                if node[0] in related_sheets and _CALENDAR.search(self.label(node)) and not self.table(node):
                    calendar_rows[node[0], _address_parts(node[1])[1]].append(node)
            endpoints = []
            for row in calendar_rows.values():
                row.sort(key=lambda x: _address_parts(x[1])[0])
                endpoints += list(dict.fromkeys(row[:2] + row[-1:]))
            endpoints = endpoints[:24]
            if endpoints:
                cal_nodes, edges, reasons = self.walk(endpoints, count=min(65, self.limits["max_nodes"]), depth=4)
                issues = []
                for row in calendar_rows.values():
                    present = [n for n in row if n in cal_nodes and _finite(self.cell(n).value)]
                    if present and (_PERIOD_START.fullmatch(self.label(present[0])) or _PERIOD_END.fullmatch(self.label(present[0]))):
                        if len(present) > 1 and len({self.cell(n).value for n in present}) == 1:
                            issues.append(self.issue("repeated_calendar_values", "Выбранные одноимённые календарные заголовки разных колонок содержат одинаковые сохранённые значения; границы периодов требуют проверки.", present))
                starts = [n for n in cal_nodes if _PERIOD_START.fullmatch(self.label(n)) and _finite(self.cell(n).value)]
                ends = [n for n in cal_nodes if _PERIOD_END.fullmatch(self.label(n)) and _finite(self.cell(n).value)]
                for end in ends:
                    same_column = [n for n in starts if n[0] == end[0] and _address_parts(n[1])[0] == _address_parts(end[1])[0]]
                    if len(same_column) == 1 and self.cell(end).value < self.cell(same_column[0]).value:
                        issues.append(self.issue("calendar_end_before_start", "Подписанный конец периода меньше начала в той же колонке по сохранённым значениям; календарная семантика и причины не пересчитаны.", [same_column[0], end]))
                questions.append(self.question("calendar", output, endpoints[0], set(cal_nodes), edges, reasons, [[output]], issues))
            else:
                questions.append(self.question("calendar", output, output, {output}, set(), {"calendar_context_not_recognized"}, [[output]]))
        omitted = max(0, len(outputs) - len(chosen))
        if len(questions) > self.limits["max_questions"]: omitted += len(questions) - self.limits["max_questions"]
        return chosen, questions[:self.limits["max_questions"]], omitted


def _limits(max_questions, max_nodes, max_depth):
    values = {"max_questions": (max_questions, 8), "max_nodes": (max_nodes, 600), "max_depth": (max_depth, 20)}
    if any(type(value) is not int or not 1 <= value <= limit for value, limit in values.values()):
        raise ValueError("Invalid investigation limits")
    return {key: value for key, (value, _) in values.items()}


def build_investigation(workbook: Workbook, *, graph=None, max_questions=8, max_nodes=400, max_depth=16) -> dict:
    """Return compact questions and anchors; no complete evidence is delivered."""
    limits = _limits(max_questions, max_nodes, max_depth)
    builder = _Builder(workbook, graph, limits)
    outputs, details, omitted = builder.build()
    questions = []
    for detail in details:
        item = {key: deepcopy(detail[key]) for key in ("id", "category", "title", "output", "subject", "origin", "status", "question", "completeness")}
        item["evidence"] = list({(r["sheet"], r["cell"]): r for r in (item["output"], item["subject"])}.values())
        item["required_source_count"] = len(detail["evidence"])
        questions.append(item)
    return {"version": VERSION, "status": "limited" if questions else "unsupported", "origin": "deterministic_investigation_plan",
            "outputs": [_ref(n) for n in outputs], "questions": questions, "limits": limits,
            "truncated": bool(omitted) or any(not q["completeness"]["complete"] for q in questions), "omitted_candidates": omitted,
            "limitations": _LIMITATIONS + ([] if outputs else ["Подписанный формульный выход EV/NPV не найден; назначение модели не угадывается."])}


def inspect_question(workbook: Workbook, question_id: str, *, plan=None, graph=None) -> dict:
    """Read only a planned question. Caller-supplied arbitrary sources are ignored."""
    plan = plan if plan is not None else build_investigation(workbook, graph=graph)
    if plan.get("version") != VERSION or not any(q.get("id") == question_id for q in plan.get("questions", [])):
        raise ValueError("Unknown investigation question")
    limits = _limits(**plan["limits"])
    builder = _Builder(workbook, graph, limits)
    _, questions, _ = builder.build()
    question = next((q for q in questions if q["id"] == question_id), None)
    if question is None: raise ValueError("Investigation plan no longer matches workbook")
    evidence = []
    for ref in question["evidence"]:
        cell = builder.cell(_node(ref))
        value, formula = (cell.value, cell.formula) if cell else (None, None)
        if (isinstance(value, str) and len(value) > MAX_CELL_TEXT) or (formula and len(formula) > MAX_CELL_TEXT):
            short = {key: question[key] for key in ("id", "category", "title", "output", "subject", "origin")}
            short.update(status="unsupported", evidence=[], issues=[{"code": "cell_text_limit", "detail": "Полный атомарный ответ превышает лимит текста; источник не обрезается.", "evidence": []}], completeness={"complete": False, "reasons": ["cell_text_limit"]})
            return {"version": VERSION, "origin": "deterministic_investigation", "investigation_question": short, "cell_evidence": [], "truncated": True}
        evidence.append({**ref, "present": cell is not None, "value": value if not type(value) in (int, float) or _finite(value) else None,
                         "formula": formula, "formula_type": cell.formula_type if cell else None, "data_type": cell.data_type if cell else None,
                         "nonfinite_value": type(value) in (int, float) and not _finite(value), "value_truncated": False, "formula_truncated": False})
    return {"version": VERSION, "origin": "deterministic_investigation", "investigation_question": question,
            "cell_evidence": evidence, "truncated": not question["completeness"]["complete"], "basis": "saved_values", "recalculated": False}


def _inspected(results):
    values = list(results.values()) if isinstance(results, dict) else results if isinstance(results, list) else []
    for result in values:
        if not isinstance(result, dict): continue
        data = result.get("metadata") if isinstance(result.get("metadata"), dict) else result
        question = data.get("investigation_question")
        if isinstance(question, dict): yield result, question


def _delivered_nodes(result):
    actual = {_node(r) for r in result.get("cell_evidence", [])} - {None}
    for sheet, rows in result.get("cell_data", {}).items():
        actual.update((sheet, row[0]) for row in rows if isinstance(row, list) and row and isinstance(row[0], str))
    return actual


def investigation_coverage(plan, inspected_results, *, delivered_cells=None):
    """Only deliberate, fully delivered inspection results count as examined."""
    inspected = {q.get("id"): (result, q) for result, q in _inspected(inspected_results)}
    statuses = []
    for expected in plan.get("questions", []):
        result, question = inspected.get(expected["id"], ({}, {}))
        required = {_node(r) for r in question.get("evidence", [])} - {None}
        actual = _delivered_nodes(result)
        if delivered_cells is not None: actual &= set(delivered_cells)
        matching = question.get("output") == expected.get("output") and question.get("subject") == expected.get("subject")
        if matching and question.get("status") == "unsupported": status = "unsupported"
        elif matching and required and required <= actual and len(required) == expected.get("required_source_count"):
            status = "sources_examined"
        else: status = "not_examined"
        statuses.append({"id": expected["id"], "category": expected["category"], "status": status,
                         "required_sources": expected.get("required_source_count", 0), "delivered_sources": len(required & actual),
                         "limitations": question.get("completeness", {}).get("reasons", []) if status != "not_examined" else []})
    examined = sum(s["status"] == "sources_examined" for s in statuses)
    unsupported = sum(s["status"] == "unsupported" for s in statuses)
    return {"version": VERSION, "questions": statuses, "sources_examined": examined, "unsupported": unsupported,
            "not_examined": len(statuses) - examined - unsupported,
            "ready_for_synthesis": bool(statuses) and examined + unsupported == len(statuses),
            "financial_verification": "not_assessed", "scope_truncated": bool(plan.get("truncated"))}


def investigation_observations(inspected_results):
    """Source-generated investigation notes; never a financial verification."""
    observations = []
    for result, question in _inspected(inspected_results):
        required = {_node(r) for r in question.get("evidence", [])} - {None}
        if question.get("status") != "unsupported" and (not required or not required <= _delivered_nodes(result)):
            continue
        issues = question.get("issues", [])
        detail = "Получены источники выбранного вопроса. " if question.get("status") != "unsupported" else "Источники вопроса не получены полностью. "
        if question.get("category") == "discount_rate_components":
            order = {name: i for i, name in enumerate(("discount_rate", "specific_risk", "risk_free", "cost_of_equity", "cost_of_debt", "capital_weight", "tax", "equity_risk_premium", "beta", "risk_premium"))}
            components = sorted(question.get("components", []), key=lambda c: order.get(c.get("role"), 20))
            seen, selected = set(), []
            for c in components:
                key = c.get("role"), "" if c.get("role") == "discount_rate" else _normal(c.get("label", "")).lower()
                if key in seen: continue
                seen.add(key); selected.append(c)
                if len(selected) == 8: break
            detail += " ".join(f"{c['label']}: сохранённое значение {c['value']}; подпись единицы: {c.get('unit') or 'не установлена'}; источник: {c['source']['sheet']}!{c['source']['cell']}." for c in selected)
        detail += " " + " ".join(item["detail"] + " Источники: " + ", ".join(f"{r['sheet']}!{r['cell']}" for r in item.get("evidence", [])) + "." for item in issues if isinstance(item, dict) and isinstance(item.get("detail"), str))
        if not issues:
            detail += ("В прочитанном ограниченном наборе явных структурных препятствий не отмечено. " if question.get("completeness", {}).get("complete")
                       else "Оставшиеся зависимости и пределы исследования указаны в границах. ")
        if question.get("completeness", {}).get("reasons"):
            detail += " Границы: " + ", ".join(question["completeness"]["reasons"]) + "."
        detail += " Рыночная обоснованность, полный охват и финансовая существенность не установлены."
        observations.append({"title": question["title"], "detail": detail, "status": "not_verified", "impact": "not_assessed",
                             "evidence": deepcopy(question.get("evidence", [])), "origin": "deterministic_investigation",
                             "investigation_question_id": question["id"]})
    return observations
