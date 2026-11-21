"""Conditional, source-bound formula patches; never an approved correction.

Only the path from an explicitly supported patch to a labelled EV/NPV is
evaluated. Other saved inputs are frozen, and the original path must reproduce
its cached output. No workbook object, formula or file is modified.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import math
import re

from .financial import _Checks, _FCF, _PV, _UNIT_LABEL, _finite, _ref, Unsupported, run_financial_checks
from .references import DependencyGraph
from .valuation_sensitivity import _Evaluator, _TARGET, _ancestors, _affected
from .workbook import Workbook, _address_parts

VERSION = "2026-11-04.1"
MAX_SCENARIOS = 12
MAX_PROOF_CELLS = 140
MAX_TOOL_CHARS = 14_000
_NP = re.compile(r"^(?:net\s+profit|net\s+income|чистая\s+прибыль)$", re.I)
_NOPAT = re.compile(r"^NOPAT$", re.I)
_SUPPORTED = {"pv_fcff_duplicate_term", "pv_fcff_historical_period", "gordon_double_discount", "fcff_net_profit_nopat_overlap"}
_LIMITATIONS = [
    "Условные альтернативы состава исходной формулы, не утверждённые исправления и не справедливая стоимость.",
    "Незатронутые сохранённые входы фиксированы; Excel, вся книга и dataTable не пересчитывались.",
    "Каждый сценарий независим: их изменения нельзя складывать. Комбинированные сценарии не рассчитывались.",
    "Достоверность кэшей, экономическая реалистичность и финансовая существенность не установлены.",
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _nodes(refs):
    return {(x["sheet"], x["cell"]) for x in refs}


def _transport_size(scenario):
    """Conservative compact-evidence envelope, with wrapper headroom.

    Saved JSON keeps verbose source rows; tool transport groups them by sheet.
    The pipeline additionally enforces its actual serialized 14k limit.
    """
    data = defaultdict(list)
    for row in scenario["cell_evidence"]:
        kind = "blank" if row.get("present") is False else "number" if _finite(row["value"]) else "text_label"
        data[row["sheet"]].append([row["cell"], row["value"], row.get("formula"), kind])
    public = {k: v for k, v in scenario.items() if k != "cell_evidence"}
    return len(_json({"metadata": {"impact_scenarios": [public]}, "cell_data": data})) + 512


def _formula(expr):
    kind = expr[0]
    if kind == "number":
        return format(expr[1], ".17g")
    if kind == "ref":
        return "'" + expr[1].replace("'", "''") + "'!" + expr[2]
    if kind == "sum":
        return "SUM(" + ",".join(_formula(x) for x in expr[1]) + ")"
    if kind in {"+", "-", "*", "/", "^"}:
        return "(" + _formula(expr[1]) + kind + _formula(expr[2]) + ")"
    raise Unsupported("unsupported_patch_expression")


def _without(terms, removed):
    """Remove exact occurrences from a positive additive formula, not cells."""
    pending = Counter(removed)
    kept = []
    for node in terms:
        if pending[node]:
            pending[node] -= 1
        else:
            kept.append(("ref", *node))
    if any(pending.values()):
        raise Unsupported("patch_occurrence_not_in_sum")
    return ("sum", tuple(kept)) if kept else ("number", 0.0)


class _PatchedEvaluator(_Evaluator):
    def __init__(self, checker, active, patches):
        super().__init__(checker, active, {})
        self.patches = patches
        self.ignored_sum_inputs = set()

    def expr(self, expr, seen):
        if expr[0] in {"sum", "range"}:
            parts = expr[1] if expr[0] == "sum" else (("ref", *x) for x in self.c.references(expr))
            for part in parts:
                if part[0] == "ref":
                    node = part[1], part[2]
                    cell = self.c.cell(node)
                    if cell is None or not cell.formula and (cell.value is None or isinstance(cell.value, str) and not cell.value.startswith("#")):
                        self.ignored_sum_inputs.add(node)
        return super().expr(expr, seen)

    def node(self, node, seen=frozenset()):
        if node not in self.patches:
            return super().node(node, seen)
        self.steps += 1
        if self.steps > 5000 or len(seen) > 80 or node in seen:
            raise Unsupported("patch_evaluation_limit_or_cycle")
        if node in self.memo:
            return self.memo[node]
        self.formulas[node] = {**_ref(node), "formula": self.c.cell(node).formula,
                               "changed_formula": "=" + _formula(self.patches[node])}
        value = self.expr(self.patches[node], seen | {node})
        self.memo[node] = value
        return value


def _matching(findings, code, required):
    return [f for f in findings if f.get("code") == code and required <= _nodes(f.get("evidence", []))]


def _plan(code, title, assumption, patches, evidence, findings):
    return {"code": code, "title": title, "assumption": assumption, "patches": patches,
            "evidence": set(evidence), "findings": findings}


def _sum_plans(c, findings, checks):
    plans = []
    for node, expression in c.parsed.items():
        if not c.has_label(node, _FCF) or not c.has_label(node, _PV) or not re.match(r"^=?\s*SUM\s*\(", c.cell(node).formula, re.I):
            continue
        try:
            terms = c.sum_terms(expression)
            groups, periods = defaultdict(list), []
            for term in terms:
                if not c.cell(term) or c.cell(term).value is None:
                    continue
                proof = {node, term, *c.labels(node)}
                item = c.pv(("ref", *term), proof)
                if item:
                    signature = c.signature(item["flow"], proof), item["signature"]
                    groups[signature].append((term, proof))
                    periods.append((term, item, proof))
            for members in groups.values():
                if len(members) < 2:
                    continue
                proof = set().union(*(x[1] for x in members))
                linked = _matching(findings, "pv_fcff_duplicate_term", {node, *[x[0] for x in members]})
                if not linked:
                    continue
                values = [c.value(x[0]) for x in members]
                if any(not math.isclose(values[0], x, rel_tol=1e-7, abs_tol=1e-6) for x in values):
                    raise Unsupported("duplicate_sources_have_inconsistent_caches")
                removed = [x[0] for x in members[1:]]
                plans.append(_plan("pv_fcff_duplicate_term", "Одно включение структурно повторённого PV FCFF",
                    "В данной сумме оставлено первое включение общего потока; дополнительные включения исключены только из суммы. Ячейки потоков и их прочие потребители сохранены.",
                    {node: {"kind": "remove_sum_terms", "expr": _without(terms, removed), "excluded": removed}}, proof, linked))
            valuation = c.valuation_date(node[0])
            if valuation:
                for term, item, proof in periods:
                    period = c.period_year(item["flow_node"])
                    if not period or date(period[0], 12, 31) >= valuation[0]:
                        continue
                    linked = _matching(findings, "pv_fcff_historical_period", {node, term})
                    if linked:
                        plans.append(_plan("pv_fcff_historical_period", "Исключение завершённого календарного периода из PV FCFF",
                            "Из данной суммы исключён один поток календарного года, завершившегося до явно указанной даты оценки. Назначение этой суммы и экономическая допустимость исключения требуют решения пользователя.",
                            {node: {"kind": "remove_historical_sum_term", "expr": _without(terms, [term]), "excluded": [term]}},
                            proof | period[1] | valuation[1], linked))
        except (Unsupported, ValueError, ArithmeticError, RecursionError) as exc:
            checks.append({"code": "pv_fcff_sum", "status": "unsupported", "reason": str(exc), "source": _ref(node)})
    return plans


def _overlap_plans(c, graph, target, ancestors, findings, checks):
    candidates = []
    try:
        # Require positive explicit discount intervals on every changed flow;
        # these are relative intervals, not inferred calendar/forecast years.
        pv_by_flow = defaultdict(list)
        for node in ancestors:
            if node not in c.parsed:
                continue
            proof = {node}
            try:
                item = c.pv(("ref", *node), proof)
            except Unsupported:
                continue
            if item:
                pv_by_flow[item["flow_node"]].append((item, proof))
        records = []
        for node in sorted(ancestors):
            if not c.has_label(node, _FCF) or c.has_label(node, _PV) or node not in c.parsed:
                continue
            # Initial cash investments outside the recognised discounted
            # positive-period series remain frozen, even with a SUM formula.
            if node not in pv_by_flow:
                continue
            try:
                terms = c.sum_terms(c.parsed[node])
            except Unsupported:
                continue
            np = [x for x in terms if c.has_label(x, _NP)]
            nopat = [x for x in terms if c.has_label(x, _NOPAT)]
            if not np or not nopat:
                continue
            if len(np) != 1 or len(nopat) != 1:
                raise Unsupported("ambiguous_profit_terms")
            linked = _matching(findings, "fcff_net_profit_nopat_overlap", {node, np[0], nopat[0]})
            if not linked:
                raise Unsupported("overlap_not_established_for_all_selected_flows")
            proof = {node, np[0], nopat[0], *c.labels(node), *c.labels(np[0]), *c.labels(nopat[0])}
            left, right = c.sum_terms(c.parsed[np[0]]), c.sum_terms(c.parsed[nopat[0]])
            available = Counter(c.signature(("ref", *x), proof) for x in left)
            required = Counter(c.signature(("ref", *x), proof) for x in right)
            if not right or any(n > available[k] for k, n in required.items()):
                raise Unsupported("overlap_additive_provenance_not_established")
            proof.update(left + right)
            items = pv_by_flow.get(node, [])
            if not items:
                raise Unsupported("overlap_flow_has_no_supported_discount_period")
            periods = []
            for item, extra in items:
                proof.update(extra)
                period = c.number(item["period"], proof)
                if period <= 0:
                    raise Unsupported("overlap_period_not_positive")
                periods.append(period)
            if len(set(periods)) != 1:
                raise Unsupported("ambiguous_overlap_discount_period")
            records.append((node, terms, np[0], nopat[0], periods[0], proof, linked))
        if not records:
            return []
        if len({(n[0][0], _address_parts(n[0][1])[1]) for n in records}) != 1:
            raise Unsupported("overlap_rows_not_one_consistent_series")
        ordered = sorted(records, key=lambda x: _address_parts(x[0][1])[0])
        if any(b[4] <= a[4] for a, b in zip(ordered, ordered[1:])):
            raise Unsupported("overlap_intervals_not_strictly_increasing")
        # All same-column sources must have the same additive row pattern.
        shape = lambda r: tuple((_address_parts(x[1])[0] - _address_parts(r[0][1])[0], _address_parts(x[1])[1] - _address_parts(r[0][1])[1], x[0] == r[0][0]) for x in r[1])
        if len({shape(r) for r in records}) != 1:
            raise Unsupported("overlap_formula_series_not_consistent")
        for index, kind, label in ((2, "remove_net_profit", "Net Profit / Net Income"), (3, "remove_nopat", "NOPAT")):
            patches = {r[0]: {"kind": kind, "expr": _without(r[1], [r[index]]), "excluded": [r[index]], "period": r[4]} for r in records}
            proof = set().union(*(r[5] for r in records))
            linked = list({f["id"]: f for r in records for f in r[6]}.values())
            candidates.append(_plan("fcff_net_profit_nopat_overlap", "FCFF без слагаемого " + label,
                "В согласованном ряду FCFF с положительными исходными интервалами исключено слагаемое " + label + ". Все остальные слагаемые, ставки и интервалы сохранены. Другой вариант исключения считается отдельно; правильность метода FCFF и календарные годы не утверждаются.", patches, proof, linked))
    except (Unsupported, KeyError, ValueError, ArithmeticError, RecursionError) as exc:
        checks.append({"code": "fcff_net_profit_nopat_overlap", "target": _ref(target), "status": "unsupported", "reason": str(exc)})
    return candidates


def _gordon_plans(c, graph, target, ancestors, findings, checks):
    plans = []
    for node in sorted(ancestors):
        if node not in c.parsed:
            continue
        proof = {node}
        try:
            item = c.gordon(c.parsed[node], proof)
            if not item:
                continue
            linked = _matching(findings, "gordon_double_discount", {node})
            if not linked:
                continue
            reachable = _affected(graph, {node}, ancestors)
            exposure = c.exposure(("ref", *target), node, reachable, proof)
            records = [x for x in exposure[1] if x["signature"] == item["signature"]] if exposure else []
            if not exposure or exposure[0] <= 0 or sum(x["power"] for x in records) != 1:
                raise Unsupported("gordon_output_not_unique_single_extra_discount")
            if any(x["factor"] != item["factor"] for x in records):
                raise Unsupported("inconsistent_saved_discount_factor_caches")
            expr = c.parsed[node]
            matches = []
            for i in (1, 2):
                local = set()
                pv = c.pv(expr[1][i], local)
                if pv and pv["flow"] == item["flow"] and pv["signature"] == item["signature"]:
                    expected = c.value(item["flow_node"]) * item["factor"]
                    if not math.isclose(c.number(expr[1][i], local), expected, rel_tol=1e-7, abs_tol=1e-6):
                        raise Unsupported("gordon_saved_pv_does_not_match_flow_factor")
                    proof.update(local)
                    matches.append(i)
            if len(matches) != 1:
                raise Unsupported("ambiguous_gordon_numerator_patch")
            index = matches[0]
            children = list(expr[1]); children[index] = item["flow"]
            changed = ("/", tuple(children), expr[2])
            proof.update(c.references(expr[1][index]) | {item["flow_node"]})
            plans.append(_plan("gordon_double_discount", "Недисконтированный FCFF только в числителе Gordon",
                "В числителе Gordon сохранённый PV FCFF заменён его доказанным исходным FCFF. Последующий коэффициент дисконтирования, веса выбора методов и остальные ветви сохранены; это один условный способ снять повторный дисконт.",
                {node: {"kind": "replace_gordon_numerator", "expr": changed, "replaced_sources": sorted(c.references(expr[1][index])), "replacement_sources": [item["flow_node"]]}}, proof, linked))
        except (Unsupported, ValueError, ArithmeticError, RecursionError) as exc:
            checks.append({"code": "gordon_double_discount", "target": _ref(target), "status": "unsupported", "reason": str(exc), "source": _ref(node)})
    return plans


def _build_scenario(c, graph, target, ancestors, plan):
    patches = plan["patches"]
    if not set(patches) <= ancestors:
        raise Unsupported("patch_not_in_target_dependencies")
    active = _affected(graph, set(patches), ancestors)
    if target not in active:
        raise Unsupported("patch_does_not_reach_output")
    if any(graph.issues.get(node) for node in active):
        raise Unsupported("unresolved_affected_dependency")
    original = _PatchedEvaluator(c, active, {})
    baseline = original.node(target)
    saved = c.value(target)
    if not math.isclose(baseline, saved, rel_tol=1e-7, abs_tol=1e-6):
        raise Unsupported("baseline_does_not_match_saved_output")
    # Intermediate caches on the expanded path must agree too: compensating
    # stale caches must not pass solely because the final total happens to fit.
    if any(not math.isclose(value, c.value(node), rel_tol=1e-7, abs_tol=1e-6) for node, value in original.memo.items() if node in active):
        raise Unsupported("baseline_intermediate_cache_mismatch")
    changed = _PatchedEvaluator(c, active, {node: p["expr"] for node, p in patches.items()})
    result = changed.node(target)
    units = {c.cell(x).value for x in c.labels(target) if _UNIT_LABEL.fullmatch(c.cell(x).value.strip())}
    if len(units) != 1:
        raise Unsupported("missing_or_ambiguous_output_unit")
    all_sources = original.sources | changed.sources
    ignored = original.ignored_sum_inputs | changed.ignored_sum_inputs
    proof = plan["evidence"] | set(original.formulas) | set(changed.formulas) | set(all_sources) | ignored | set(c.labels(target)) | {target}
    for f in plan["findings"]:
        proof.update(_nodes(f["evidence"]))
    if len(proof) > MAX_PROOF_CELLS:
        raise Unsupported("impact_proof_cell_limit")
    cells = []
    for node in sorted(proof):
        cell = c.cell(node)
        if cell is None and node in ignored:
            cells.append({**_ref(node), "value": None, "present": False})
            continue
        if cell is None or isinstance(cell.value, float) and not math.isfinite(cell.value):
            raise Unsupported("missing_or_nonfinite_proof_source")
        if len(str(cell.value)) > 2000 or cell.formula and len(cell.formula) > 2000:
            raise Unsupported("impact_source_text_limit")
        # Every row here is present (checked above). Omit redundant true/null
        # fields so the complete proof fits an atomic tool response.
        cells.append({**_ref(node), "value": cell.value, **({"formula": cell.formula} if cell.formula else {})})
    patch_output = []
    for node, p in sorted(patches.items()):
        entry = {**_ref(node), "kind": p["kind"], "original_formula": c.cell(node).formula, "changed_formula": "=" + _formula(p["expr"])}
        for key in ("excluded", "replaced_sources", "replacement_sources"):
            if key in p:
                entry[key] = [_ref(x) for x in p[key]]
        if "period" in p:
            entry["source_discount_interval"] = p["period"]
        patch_output.append(entry)
    identity = _json([VERSION, plan["code"], _ref(target), patch_output])
    delta = result - baseline
    scenario = {
        "id": "impact_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
        "kind": next(iter(patches.values()))["kind"],
        "code": plan["code"], "finding_ids": sorted({f["id"] for f in plan["findings"]}),
        "title": plan["title"], "assumption": plan["assumption"], "status": "calculated",
        "target": _ref(target), "raw_unit": next(iter(units)),
        "baseline": baseline, "saved_output": saved, "baseline_cache_difference": baseline - saved,
        "result": result, "delta": delta, "relative_delta": delta / abs(baseline) if baseline else None,
        "patches": patch_output,
        "formula_path": [{**value} for _, value in sorted(original.formulas.items())],
        "source_inputs": [{**_ref(node), "value": value["value"], "frozen": True} for node, value in sorted(all_sources.items())],
        "ignored_sum_inputs": [_ref(node) for node in sorted(ignored)],
        "frozen_boundaries": "Все числовые source_inputs остаются на сохранённом уровне; пересчитаны только formula_path с указанными patches.",
        "evidence": [_ref(x) for x in sorted(proof)], "cell_evidence": cells,
        "basis": "saved_unaffected_inputs_and_explicit_formula_patch", "conditional": True,
        "recalculated_workbook": False, "financial_materiality": "not_assessed",
        "limitations": list(_LIMITATIONS),
    }
    if not all(_finite(scenario[key]) for key in ("baseline", "result", "delta")) or scenario["relative_delta"] is not None and not _finite(scenario["relative_delta"]):
        raise Unsupported("nonfinite_impact")
    if _transport_size(scenario) > MAX_TOOL_CHARS:
        raise Unsupported("impact_atomic_payload_limit")
    return scenario


def build_impact_analysis(workbook: Workbook, financial_report=None) -> dict:
    """Build bounded canonical scenarios. IDs never authorize arbitrary patches.

    A supplied report only narrows/link IDs from freshly reproduced findings;
    it cannot inject candidate proofs or formulas. Full evidence is atomic.
    """
    c, graph = _Checks(workbook), DependencyGraph(workbook)
    canonical = run_financial_checks(workbook)
    findings = canonical["findings"]
    if financial_report is not None:
        supplied = {f.get("id") for f in financial_report.get("findings", []) if isinstance(f, dict)}
        findings = [f for f in findings if f["id"] in supplied]
    checks = [{"code": f["code"], "finding_ids": [f["id"]], "status": "unsupported", "reason": "no_unique_supported_conditional_output_patch"}
              for f in findings if f["code"] not in _SUPPORTED]
    checks.extend({"code": entry["code"], "status": "unsupported", "reason": "calendar_not_established_for_historical_omission", "detail": entry["detail"], "evidence": entry.get("evidence", [])}
                  for entry in canonical["checks"] if entry["code"] == "pv_fcff_historical_period" and entry["status"] in {"unsupported", "ambiguous"})
    scenarios = []
    common = _sum_plans(c, findings, checks)
    targets = sorted({node for node in c.parsed if c.has_label(node, _TARGET)} | {node for node in c.parse_errors if c.has_label(node, _TARGET)})
    primary_targets = []
    for node in targets:
        try:
            alias = c.alias(("ref", *node), set())
            if alias[0] == "ref" and (alias[1], alias[2]) != node and (alias[1], alias[2]) in targets:
                checks.append({"target": _ref(node), "status": "not_applicable", "reason": "alias_of_recognized_output", "source_target": _ref((alias[1], alias[2]))})
                continue
        except Unsupported:
            pass
        primary_targets.append(node)
    targets = primary_targets
    if len(targets) > 3:
        checks.append({"status": "unsupported", "reason": "target_limit", "omitted": len(targets) - 3})
    for target in targets[:3]:
        try:
            ancestors = _ancestors(graph, target)
            plans = [p for p in common if set(p["patches"]) <= ancestors]
            plans += _overlap_plans(c, graph, target, ancestors, findings, checks)
            plans += _gordon_plans(c, graph, target, ancestors, findings, checks)
            for plan in plans:
                if len(scenarios) >= MAX_SCENARIOS:
                    raise Unsupported("scenario_limit")
                try:
                    scenario = _build_scenario(c, graph, target, ancestors, plan)
                    if not any(x["id"] == scenario["id"] for x in scenarios):
                        scenarios.append(scenario)
                    checks.append({"code": plan["code"], "target": _ref(target), "status": "calculated", "scenario_id": scenario["id"], "finding_ids": scenario["finding_ids"]})
                except (Unsupported, ValueError, ArithmeticError, RecursionError) as exc:
                    checks.append({"code": plan["code"], "target": _ref(target), "status": "unsupported", "reason": str(exc), "finding_ids": sorted({f["id"] for f in plan["findings"]})})
        except (Unsupported, ValueError, ArithmeticError, RecursionError) as exc:
            checks.append({"target": _ref(target), "status": "unsupported", "reason": str(exc)})
    accounted = {fid for x in checks for fid in x.get("finding_ids", [])}
    checks.extend({"code": f["code"], "finding_ids": [f["id"]], "status": "unsupported", "reason": "no_supported_path_to_labelled_ev_npv"} for f in findings if f["id"] not in accounted)
    if not findings:
        checks.append({"status": "not_applicable", "reason": "no_supported_financial_candidates"})
    return {"version": VERSION, "status": "limited" if scenarios else "unsupported" if findings else "not_applicable",
            "scenarios": scenarios, "checks": checks, "limitations": list(_LIMITATIONS)}
