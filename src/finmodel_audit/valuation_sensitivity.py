"""Bounded counterfactual arithmetic on the workbook's own valuation formula.

Unaffected source caches are frozen. This is neither a general Excel evaluator
nor a replacement valuation: duplicate flows, timing and discounting are kept.
"""
from __future__ import annotations

import math
import re

from .financial import _Checks, _finite, _ref, _UNIT_LABEL, _FCF, _PV, Unsupported
from .references import DependencyGraph
from .workbook import Workbook

VERSION = "2026-11-04.1"
MAX_ANCESTORS = 6000
MAX_ACTIVE = 160
MAX_SOURCES = 180
MAX_TARGETS = 3
_TARGET = re.compile(r"^(?:EV|enterprise\s+value|NPV|net\s+present\s+value|ЧДД|чистая\s+привед[её]нная\s+стоимость)$", re.I)
_GROWTH = re.compile(r"^(?:LTGR|terminal\s+growth(?:\s+rate)?|perpetual\s+growth(?:\s+rate)?|долгосрочн\w*\s+темп\s+роста)$", re.I)
_LIMITS = [
    "Локальный сценарий исходной формулы: сохранённые незатронутые входы фиксированы; Excel, dataTable и вся книга не пересчитываются.",
    "Состав потоков, исторические периоды, дублирование, веса и повторное дисконтирование сохранены как в исходной формуле, а не исправлены.",
    "Изменяется эффективная ставка в выбранной ячейке, а не пересчитываются её экономические компоненты или связанные управленческие реакции.",
    "Явно подписанные недисконтированные FCFF фиксируются на сохранённом уровне; их upstream-формулы не вычисляются даже при возможной зависимости бизнеса от ставки.",
    "Результат не является утверждённым EV, исправленной оценкой или установленной финансовой существенностью.",
]


def _ancestors(graph, target, stop_nodes=frozenset()):
    seen, pending = set(), [target]
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        if len(seen) > MAX_ANCESTORS:
            raise Unsupported("ancestor_limit")
        if node not in stop_nodes:
            pending.extend(graph.forward.get(node, ()))
    return seen


def _affected(graph, parameters, ancestors):
    seen, pending = set(), list(parameters)
    while pending:
        node = pending.pop()
        if node in seen or node not in ancestors:
            continue
        seen.add(node)
        if len(seen) > MAX_ACTIVE:
            raise Unsupported("affected_formula_limit")
        pending.extend(graph.reverse.get(node, ()))
    return seen


class _Evaluator:
    def __init__(self, checker, active, overrides):
        self.c, self.active, self.overrides = checker, active, overrides
        self.sources, self.formulas, self.memo = {}, {}, {}
        self.steps = 0

    def node(self, node, seen=frozenset()):
        self.steps += 1
        if self.steps > 5000 or len(seen) > 80:
            raise Unsupported("evaluation_limit")
        if node in seen:
            raise Unsupported("affected_formula_cycle")
        if node in self.memo:
            return self.memo[node]
        cell = self.c.cell(node)
        if cell is None:
            raise Unsupported("missing_arithmetic_source")
        if node in self.overrides or node not in self.active or not cell.formula:
            value = self.overrides.get(node, self.c.value(node))
            self.sources[node] = {**_ref(node), "value": self.c.value(node), "formula": cell.formula,
                                  "frozen": node not in self.overrides}
            if len(self.sources) > MAX_SOURCES:
                raise Unsupported("source_limit")
        else:
            if cell.formula_type in {"array", "dataTable"} or node not in self.c.parsed:
                raise Unsupported("unsupported_affected_formula")
            self.formulas[node] = {**_ref(node), "formula": cell.formula}
            value = self.expr(self.c.parsed[node], seen | {node})
        if not _finite(value):
            raise Unsupported("nonfinite_scenario")
        self.memo[node] = float(value)
        return float(value)

    def expr(self, expr, seen):
        kind = expr[0]
        if kind == "number":
            return expr[1]
        if kind == "ref":
            return self.node((expr[1], expr[2]), seen)
        if kind in {"sum", "range"}:
            parts = expr[1] if kind == "sum" else (("ref", *node) for node in sorted(self.c.references(expr)))
            values = []
            for part in parts:
                if part[0] == "ref":
                    cell = self.c.cell((part[1], part[2]))
                    # Excel SUM ignores blank/text cells; errors and booleans
                    # are not silently accepted as numeric frozen inputs.
                    if cell is None or cell.value is None and not cell.formula:
                        continue
                    if isinstance(cell.value, str) and not cell.formula and not cell.value.startswith("#"):
                        continue
                values.append(self.expr(part, seen))
            value = sum(values)
        elif kind in {"+", "-", "*", "/", "^"}:
            a, b = self.expr(expr[1], seen), self.expr(expr[2], seen)
            try:
                if kind == "+": value = a + b
                elif kind == "-": value = a - b
                elif kind == "*": value = a * b
                elif kind == "/": value = a / b
                else:
                    if abs(b) > 1000:
                        raise Unsupported("power_limit")
                    value = a ** b
            except (ArithmeticError, ValueError):
                raise Unsupported("invalid_scenario_arithmetic") from None
        else:
            raise Unsupported("unsupported_affected_expression")
        if not _finite(value):
            raise Unsupported("nonfinite_scenario")
        return float(value)


def _growth_nodes(checker, ancestors, rate):
    matches = set()
    for node in ancestors:
        expr = checker.parsed.get(node)
        if not expr or expr[0] != "/" or expr[2][0] != "-" or expr[1][0] != "*":
            continue
        evidence = set()
        r, g = [checker.alias(x, evidence) for x in expr[2][1:]]
        if r != ("ref", *rate):
            continue
        for part in expr[1][1:]:
            if part[0] == "+":
                other = part[2] if part[1] == ("number", 1.0) else part[1] if part[2] == ("number", 1.0) else None
                if other and checker.alias(other, evidence) == g:
                    if g[0] != "ref":
                        raise Unsupported("terminal_growth_not_explicit_cell")
                    if not checker.has_label((g[1], g[2]), _GROWTH):
                        raise Unsupported("terminal_growth_label_not_established")
                    matches.add((g[1], g[2]))
    return matches


def build_valuation_sensitivity(workbook: Workbook) -> dict:
    """Recognize at most three explicit EV/NPV targets; fail closed per target."""
    c, graph = _Checks(workbook), DependencyGraph(workbook)
    scenarios, checks = [], []
    targets = [node for node in c.parsed if c.has_label(node, _TARGET)]
    # Unsupported formulas with an explicit output label still get a refusal.
    targets += [node for node in c.parse_errors if c.has_label(node, _TARGET)]
    for target in targets[:MAX_TARGETS]:
        try:
            alias = c.alias(("ref", *target), set())
            if alias[0] == "ref" and (alias[1], alias[2]) != target and (alias[1], alias[2]) in targets[:MAX_TARGETS]:
                checks.append({"target": _ref(target), "status": "not_applicable", "reason": "alias_of_recognized_output",
                               "source_target": {"sheet": alias[1], "cell": alias[2]}})
                continue
            # The contract explicitly freezes labelled operating cashflows;
            # discover that boundary before inspecting their upstream logic.
            frozen_flows = {node for node in graph.forward if c.has_label(node, _FCF) and not c.has_label(node, _PV)}
            frozen_flows.update((sheet, address) for sheet, data in workbook.sheets.items()
                                for address, cell in data.cells.items() if _finite(cell.value)
                                and c.has_label((sheet, address), _FCF) and not c.has_label((sheet, address), _PV))
            ancestors = _ancestors(graph, target, frozen_flows)
            if any(graph.issues.get(node) for node in ancestors):
                raise Unsupported("unresolved_dependency_in_output_sources")
            rates, periods = set(), {}
            for node in sorted(ancestors):
                if node not in c.parsed:
                    continue
                evidence = set()
                try:
                    item = c.discount(("ref", *node), evidence)
                except Unsupported:
                    continue
                if item:
                    if item["rate"][0] != "ref":
                        raise Unsupported("discount_rate_not_explicit_cell")
                    rates.add((item["rate"][1], item["rate"][2]))
                    periods[node] = {"discount_cell": _ref(node), "period_expression": repr(item["period"]),
                        "value": c.number(item["period"], evidence), "evidence": c.evidence(evidence)}
            if len(rates) != 1:
                raise Unsupported("ambiguous_or_missing_discount_rate")
            rate = next(iter(rates))
            growths = _growth_nodes(c, ancestors, rate)
            if len(growths) > 1:
                raise Unsupported("ambiguous_terminal_growth")
            growth = next(iter(growths), None)
            parameters = {rate: c.value(rate)}
            if growth:
                parameters[growth] = c.value(growth)
            if parameters[rate] <= -1 or growth and parameters[rate] <= parameters[growth]:
                raise Unsupported("invalid_effective_parameters")
            active = _affected(graph, parameters, ancestors) - frozen_flows
            baseline_eval = _Evaluator(c, active, parameters)
            baseline = baseline_eval.node(target)
            saved = c.value(target)
            if not math.isclose(baseline, saved, rel_tol=1e-7, abs_tol=1e-6):
                raise Unsupported("baseline_does_not_match_saved_output")
            units = [c.cell(node).value for node in c.labels(target)
                     if _UNIT_LABEL.fullmatch(c.cell(node).value.strip())]
            if len(set(units)) != 1:
                raise Unsupported("missing_or_ambiguous_output_unit")
            plans = [("discount_rate", rate, delta) for delta in (-.01, .01)]
            if growth:
                plans += [("terminal_growth", growth, delta) for delta in (-.005, .005)]
            for kind, parameter, delta in plans:
                effective = {**parameters, parameter: parameters[parameter] + delta}
                if effective[rate] <= -1 or growth and effective[rate] <= effective[growth]:
                    checks.append({"target": _ref(target), "status": "unsupported", "reason": "scenario_rate_not_above_growth", "parameter": kind, "change": delta})
                    continue
                evaluator = _Evaluator(c, active, effective)
                value = evaluator.node(target)
                scenarios.append({"target": _ref(target), "parameter": kind,
                    "changed_parameter": {**_ref(parameter), "baseline": parameters[parameter], "change": delta, "effective_value": effective[parameter], "change_unit": "absolute_fraction"},
                    "explicit_formula": c.cell(target).formula,
                    "effective_parameters": [{**_ref(node), "value": value, "role": "discount_rate" if node == rate else "terminal_growth"} for node, value in sorted(effective.items())],
                    "formula_path": [evaluator.formulas[node] for node in sorted(evaluator.formulas)],
                    "source_inputs": [evaluator.sources[node] for node in sorted(evaluator.sources)],
                    "periods": [periods[node] for node in sorted(periods)], "raw_unit": units[0],
                    "frozen_cashflows": [_ref(node) for node in sorted(frozen_flows & ancestors)],
                    "baseline": baseline, "saved_output": saved, "baseline_cache_difference": baseline - saved,
                    "result": value, "delta": value - baseline,
                    "relative_delta": (value - baseline) / abs(baseline) if baseline else None,
                    "basis": "saved_unaffected_inputs_and_original_formula", "recalculated_workbook": False,
                    "limitations": list(_LIMITS)})
            checks.append({"target": _ref(target), "status": "checked", "growth_supported": growth is not None,
                           "reason": "original_formula_arithmetic_only" if growth else "rate_only_no_supported_terminal_growth_branch"})
        except (Unsupported, ValueError, ArithmeticError, RecursionError) as exc:
            # Atomic target output: never leave partial scenarios after failure.
            scenarios[:] = [x for x in scenarios if x["target"] != _ref(target)]
            checks.append({"target": _ref(target), "status": "unsupported", "reason": str(exc)[:160]})
    return {"version": VERSION, "status": "limited" if scenarios else "unsupported",
        "scenarios": scenarios, "checks": checks,
        "limitations": [*_LIMITS, *(["Не найдены поддержанные явно подписанные выходы EV/NPV."] if not targets else []),
                        *(["Число выходов ограничено; остальные явно пропущены."] if len(targets) > MAX_TARGETS else [])]}
