"""Source-bound priorities and a conditional model-use conclusion.

The input is a saved report. The module performs no I/O and changes neither
that report nor a workbook. An ordinal review priority is not an accounting
materiality threshold, a financial approval, or a claim of complete detection.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re

from .agent_evidence import _refs
from .financial import _Checks, _Parser, _ref, _UNIT_LABEL, Unsupported
from .impact import _PatchedEvaluator, _without, _NP, _NOPAT, MAX_PROOF_CELLS
from .impact_report import validated_impact
from .valuation_sensitivity import _TARGET
from .workbook import Cell, Sheet, Workbook

VERSION = "2026-11-04.2"
_KINDS = {
    "pv_fcff_duplicate_term": {"remove_sum_terms"},
    "pv_fcff_historical_period": {"remove_historical_sum_term"},
    "gordon_double_discount": {"replace_gordon_numerator"},
    "fcff_net_profit_nopat_overlap": {"remove_net_profit", "remove_nopat"},
}
_ACTION = {
    "gordon_double_discount": "Проверить числитель Gordon и последующее дисконтирование, подтвердить выбранную формулу терминальной стоимости.",
    "pv_fcff_duplicate_term": "Проверить назначение повторного слагаемого и согласовать состав суммы PV FCFF.",
    "pv_fcff_historical_period": "Сопоставить дату оценки и границы потоков, подтвердить допустимость включения завершённого периода.",
    "fcff_net_profit_nopat_overlap": "Согласовать состав FCFF и роль Net Profit/NOPAT во всём указанном ряду, затем пересчитать выбранный вариант.",
    "irr_on_discounted_cashflows": "Проверить назначение IRR и его входной ряд; сопоставить с недисконтированными денежными потоками.",
    "datatable_basepoint_cache_mismatch": "Проверить входы и сохранённую базовую точку Data Table, затем обновить таблицу в совместимом Excel-движке на копии.",
}
_LIMITATIONS = [
    "Порядок относится к проверке найденных кандидатов по измеренному условному эффекту. Порог финансовой существенности не задавался.",
    "Альтернативы одной темы не складываются. Выходы разных листов, ячеек, единиц или исходных баз не сравниваются в общем рейтинге.",
    "Число технических замечаний не используется для ухудшения заключения. Неизмеренное влияние не считается нулевым или малым.",
    "Проверяется арифметика сохранённого доказательства; достоверность исходных кэшей, экономическая правильность коррекции и полнота аудита не подтверждены.",
]


def _objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _ref_ok(value):
    return (isinstance(value, dict) and isinstance(value.get("sheet"), str) and bool(value["sheet"])
            and isinstance(value.get("cell"), str) and re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", value["cell"]) is not None)


def _node(value):
    if not _ref_ok(value):
        raise ValueError("invalid_source_reference")
    return value["sheet"], value["cell"]


def _id(prefix, value):
    return prefix + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]


def _same(a, b):
    return _finite(a) and _finite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _number(value):
    return format(value, ".8g").replace(".", ",")


def _relative_description(relative):
    if relative is None:
        return ". Относительное изменение не определено при нулевой базе."
    percent = relative * 100
    if _finite(percent):
        return " (" + _number(percent) + "% от модуля исходного выхода)."
    return ". Относительное изменение в долях модуля исходного выхода: " + _number(relative) + "."


def _snapshot(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PROOF_CELLS:
        raise ValueError("missing_or_excessive_source_proof")
    records, sheets = {}, {}
    for row in rows:
        node = _node(row)
        if node in records and records[node] != row:
            raise ValueError("conflicting_source_records")
        if "value" not in row or row.get("formula") is not None and not isinstance(row["formula"], str):
            raise ValueError("invalid_source_record")
        records[node] = row
        if row.get("present") is False:
            if row["value"] is not None or row.get("formula"):
                raise ValueError("absent_source_has_content")
            continue
        if isinstance(row["value"], (int, float)) and not isinstance(row["value"], bool) and not _finite(row["value"]):
            raise ValueError("nonfinite_source")
        if len(str(row["value"])) > 2500 or len(row.get("formula") or "") > 4096:
            raise ValueError("source_text_limit")
        sheets.setdefault(node[0], Sheet(node[0])).cells[node[1]] = Cell(
            node[1], row["value"], row.get("formula"), "normal" if row.get("formula") else None)
    return records, Workbook(Path("saved-report-evidence.xlsx"), sheets, [], [], {}, [])


def _patch_expression(checker, patch, code):
    node, kind = _node(patch), patch.get("kind")
    if kind not in _KINDS.get(code, set()) or node not in checker.parsed:
        raise ValueError("unsupported_conditional_patch")
    original = checker.parsed[node]
    changed = _Parser(patch["changed_formula"], node[0]).parse()
    if kind != "replace_gordon_numerator":
        excluded = [_node(item) for item in patch.get("excluded", [])]
        if not excluded:
            raise ValueError("missing_excluded_terms")
        terms = checker.sum_terms(original)
        if changed != _without(terms, excluded):
            raise ValueError("changed_formula_is_not_declared_exclusion")
        if kind in {"remove_net_profit", "remove_nopat"}:
            pattern = _NP if kind == "remove_net_profit" else _NOPAT
            if len(excluded) != 1 or not checker.has_label(excluded[0], pattern):
                raise ValueError("excluded_profit_source_not_labelled")
        elif kind == "remove_historical_sum_term":
            valuation = checker.valuation_date(node[0])
            for term in excluded:
                item = checker.pv(("ref", *term), set())
                period = checker.period_year(item["flow_node"]) if item else None
                if not valuation or not period or date(period[0], 12, 31) >= valuation[0]:
                    raise ValueError("historical_period_not_proven")
        else:
            remaining = Counter(terms)
            remaining.subtract(excluded)
            for term in excluded:
                proof = set()
                item = checker.pv(("ref", *term), proof)
                if not item:
                    raise ValueError("excluded_duplicate_has_no_pv_proof")
                signature = checker.signature(item["flow"], proof), item["signature"]
                matched = False
                for other, count in remaining.items():
                    if count <= 0:
                        continue
                    try:
                        other_item = checker.pv(("ref", *other), set())
                        matched = bool(other_item and (checker.signature(other_item["flow"], set()), other_item["signature"]) == signature)
                    except (Unsupported, KeyError, ValueError):
                        continue
                    if matched:
                        break
                if not matched:
                    raise ValueError("retained_duplicate_not_proven")
    else:
        item = checker.gordon(original, set())
        if not item:
            raise ValueError("gordon_source_not_proven")
        candidates = []
        for index in (1, 2):
            try:
                pv = checker.pv(original[1][index], set())
            except (Unsupported, ValueError):
                pv = None
            if pv and pv["flow"] == item["flow"] and pv["signature"] == item["signature"]:
                parts = list(original[1]); parts[index] = item["flow"]
                candidates.append(("/", tuple(parts), original[2]))
        if len(candidates) != 1 or changed != candidates[0]:
            raise ValueError("gordon_patch_not_exact_numerator_replacement")
    return changed


def _validate_scenario(scenario, findings):
    """Re-evaluate the declared local path, not the original Excel workbook."""
    public = deepcopy(scenario)
    rows = public.pop("cell_evidence", None)
    records, workbook = _snapshot(rows)
    if public.get("status") != "calculated" or public.get("code") not in _KINDS:
        raise ValueError("unsupported_impact_status_or_theme")
    finding_ids = public.get("finding_ids")
    if (not isinstance(finding_ids, list) or not finding_ids or any(not isinstance(i, str) for i in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)
            or any(i not in findings or findings[i]["code"] != public["code"] for i in finding_ids)):
        raise ValueError("missing_or_conflicting_finding_link")
    if any(not _refs(findings[i].get("evidence")) <= set(records) for i in finding_ids):
        raise ValueError("finding_proof_not_in_scenario")
    validated_impact({"scenario": public, "cell_evidence": rows}, set(records))
    if _refs(public.get("evidence")) != set(records) or not _refs(public) <= set(records):
        raise ValueError("incomplete_scenario_evidence")
    checker, target = _Checks(workbook), _node(public["target"])
    labels = checker.labels(target)
    units = {checker.cell(n).value for n in labels if _UNIT_LABEL.fullmatch(checker.cell(n).value.strip())}
    metrics = [(n, checker.cell(n).value) for n in labels if _TARGET.fullmatch(checker.cell(n).value.strip())]
    if units != {public["raw_unit"]} or len(metrics) != 1:
        raise ValueError("output_label_or_unit_not_proven")
    active = {_node(item) for item in public["formula_path"]}
    if target not in active:
        raise ValueError("target_outside_declared_path")
    patches = {}
    for patch in public["patches"]:
        node = _node(patch)
        if node in patches or node not in active:
            raise ValueError("duplicate_or_outside_path_patch")
        patches[node] = _patch_expression(checker, patch, public["code"])
    if {patch.get("kind") for patch in public["patches"]} != {public.get("kind")}:
        raise ValueError("patch_kind_mismatch")
    original = _PatchedEvaluator(checker, active, {})
    changed = _PatchedEvaluator(checker, active, patches)
    before, after = original.node(target), changed.node(target)
    if not _same(before, public["baseline"]) or not _same(after, public["result"]):
        raise ValueError("scenario_replay_mismatch")
    if any(not _same(value, checker.value(node)) for node, value in original.memo.items() if node in active):
        raise ValueError("intermediate_cache_mismatch")
    if set(original.formulas) != active or not set(changed.formulas) <= active:
        raise ValueError("declared_formula_path_mismatch")
    actual_inputs = original.sources | changed.sources
    declared_inputs = {_node(item): item["value"] for item in public["source_inputs"]}
    if set(actual_inputs) != set(declared_inputs) or any(not _same(value["value"], declared_inputs[node]) for node, value in actual_inputs.items()):
        raise ValueError("frozen_boundary_mismatch")
    if original.ignored_sum_inputs | changed.ignored_sum_inputs != {_node(x) for x in public.get("ignored_sum_inputs", [])}:
        raise ValueError("ignored_sum_boundary_mismatch")
    return public, metrics[0][1], _ref(metrics[0][0])


def _finding_registry(report, issues):
    findings, conflicts = {}, set()
    financial = report.get("financial_checks") if isinstance(report.get("financial_checks"), dict) else {}
    rows = _objects(financial.get("findings"))
    if len(rows) > 100:
        issues.append({"code": "finding_limit", "detail": "Рассмотрены первые 100 финансовых кандидатов."})
    for row in rows[:100]:
        identity, code = row.get("id"), row.get("code")
        if not isinstance(identity, str) or not isinstance(code, str) or row.get("status") not in {"candidate", "observation", "hypothesis"}:
            continue
        if not isinstance(row.get("title"), str) or not row["title"] or not _refs(row.get("evidence")):
            continue
        if identity in findings and findings[identity] != row:
            conflicts.add(identity)
        findings[identity] = row
    for identity in conflicts:
        findings.pop(identity, None)
        issues.append({"code": "conflicting_finding_id", "id": identity, "detail": "Конфликтующее доказательство исключено из приоритета."})
    return findings


def _unknown(groups, covered, checks):
    result = []
    for code, rows in sorted(groups.items()):
        missing = [row for row in rows if row["id"] not in covered]
        if not missing:
            continue
        ids = sorted(row["id"] for row in missing)
        reasons = sorted({check.get("reason") for check in checks if isinstance(check.get("reason"), str)
                          and check.get("status") in {"unsupported", "ambiguous"}
                          and (check.get("code") == code or set(_strings(check.get("finding_ids"))) & set(ids))})
        result.append({"id": _id("unknown_", [code, ids]), "code": code, "title": missing[0]["title"],
                       "finding_ids": ids, "rank": None, "status": "impact_unknown", "delta": None,
                       "reason": "Влияние на конечный показатель не измерено. Место среди численно ранжированных проблем не назначается.",
                       "calculation_limits": reasons,
                       "action": _ACTION.get(code, "Проверить финансовую логику и установить связь замечания с целевым показателем."),
                       "evidence": [_ref(n) for n in sorted(set().union(*(_refs(row["evidence"]) for row in missing)))]})
    return result


def build_decision_assessment(report):
    """Return a JSON-safe, deterministic assessment of the supplied evidence."""
    if not isinstance(report, dict):
        report = {}
    issues, buckets = [], defaultdict(list)
    findings = _finding_registry(report, issues)
    by_code = defaultdict(list)
    for finding in findings.values():
        by_code[finding["code"]].append(finding)
    raw_impact = report.get("impact_analysis") if isinstance(report.get("impact_analysis"), dict) else {}
    scenarios = _objects(raw_impact.get("scenarios"))
    duplicates, seen, source_records, source_conflicts = set(), {}, {}, set()
    for scenario in scenarios[:12]:
        identity = scenario.get("id")
        if isinstance(identity, str):
            if identity in seen and seen[identity] != scenario:
                duplicates.add(identity)
            seen[identity] = scenario
        for row in _objects(scenario.get("cell_evidence")):
            if not _ref_ok(row):
                continue
            node = _node(row)
            value = (row.get("value"), row.get("formula"), row.get("present", True))
            if node in source_records and source_records[node] != value:
                source_conflicts.add(node)
            source_records[node] = value
    if len(scenarios) > 12:
        issues.append({"code": "scenario_limit", "detail": "Рассмотрены первые 12 условных сценариев."})
    accepted = set()
    for scenario in scenarios[:12]:
        identity = scenario.get("id")
        if isinstance(identity, str) and identity in accepted:
            continue
        try:
            if not isinstance(identity, str) or not identity:
                raise ValueError("missing_scenario_id")
            if identity in duplicates:
                raise ValueError("conflicting_scenario_id")
            if _refs(scenario.get("cell_evidence")) & source_conflicts:
                raise ValueError("conflicting_report_source_values")
            public, label, label_source = _validate_scenario(scenario, findings)
            target = _node(public["target"])
            key = target, public["raw_unit"], public["baseline"]
            buckets[key].append((public, label, label_source))
            accepted.add(identity)
        except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError, AttributeError) as error:
            issues.append({"code": "scenario_not_ranked", "id": identity if isinstance(identity, str) else None,
                           "reason": str(error)[:160], "detail": "Сценарий не прошёл проверку полного доказательства и повторной локальной арифметики."})
    outputs, covered = [], set()
    for (target, unit, baseline), entries in sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        themes = defaultdict(list)
        for public, label, label_source in entries:
            themes[public["code"]].append(public)
        ranked = []
        for code, values in themes.items():
            ids = sorted(set().union(*(set(value["finding_ids"]) for value in values)))
            covered.update(ids)
            magnitude = max(abs(value["delta"]) for value in values)
            relative = magnitude / abs(baseline) if baseline else None
            alternatives = [{key: value.get(key) for key in ("id", "title", "assumption", "result", "delta", "relative_delta")}
                            for value in sorted(values, key=lambda value: (-abs(value["delta"]), value["id"]))]
            ranked.append({"code": code, "title": findings[ids[0]]["title"], "finding_ids": ids,
                           "scenario_ids": [value["id"] for value in alternatives], "max_absolute_delta": magnitude,
                           "max_absolute_relative_delta": relative, "alternatives": alternatives,
                           "evidence": [_ref(n) for n in sorted(set().union(*(_refs(value["evidence"]) for value in values)))],
                           "reason": ("Наибольший по модулю эффект отдельной альтернативы: " + _number(magnitude) + " " + unit
                                      + _relative_description(relative)
                                      + " Альтернативы не суммируются."),
                           "action": _ACTION.get(code, "Проверить указанное изменение формулы и его экономическое назначение."),
                           "impact_status": "conditional_measured", "financial_error_confirmed": False})
        ranked.sort(key=lambda row: (-row["max_absolute_delta"], row["code"]))
        previous, rank = None, 0
        for index, row in enumerate(ranked, 1):
            if previous is None or row["max_absolute_delta"] != previous:
                rank = index
            row["rank"] = rank
            previous = row["max_absolute_delta"]
        label = entries[0][1]
        location = target[0] + "!" + target[1]
        leader = ranked[0]
        conclusion = (f"До использования {label} ({location}) в решении требуется проверить: {leader['title']}. "
                      + leader["reason"] + " Порядок остальных измеренных тем показан для этого же выхода; неизвестное влияние рассматривается отдельно.")
        outputs.append({"id": _id("output_", [target, unit, baseline]), "target": _ref(target), "label": label,
                        "label_source": entries[0][2], "raw_unit": unit, "baseline": baseline,
                        "ranking_basis": "absolute_relative_change_within_same_output" if baseline else "absolute_change_within_zero_baseline_output",
                        "ranked_findings": ranked, "conclusion": conclusion})
    unknown = _unknown(by_code, covered, _objects(raw_impact.get("checks")))
    stages = {stage["id"]: stage for stage in _objects(report.get("stages")) if isinstance(stage.get("id"), str)}
    incomplete = [stage for stage in ("audit", "assumptions", "quality", "business")
                  if stages.get(stage, {}).get("completion") != "submitted"]
    required = list(dict.fromkeys(row["action"] for output in outputs for row in output["ranked_findings"]))
    required.extend(row["action"] for row in unknown if row["action"] not in required)
    if incomplete:
        required.append("Завершить или отдельно проверить блоки: " + ", ".join(incomplete) + ".")
    required.extend(["Подтвердить экономическое назначение выбранных формул и ключевые предпосылки по актуальным источникам.",
                     "После согласования изменений пересчитать копию книги в совместимом Excel-движке и повторить проверки."])
    supported = ["Изучение структуры модели, сохранённых формул и перечисленных исходных значений."]
    if outputs:
        supported.append("Сопоставление перечисленных условных вариантов и выбор порядка их проверки внутри каждого целевого выхода.")
    if stages.get("business", {}).get("completion") == "submitted":
        supported.append("Описательный обзор выбранных финансовых показателей с указанными источниками и ограничениями периодов.")
    output_names = ", ".join(dict.fromkeys(output["label"] + " " + output["target"]["sheet"] + "!" + output["target"]["cell"] for output in outputs))
    uses = (["Использование " + output_names + " как обоснованного результата для оценки или инвестиционного решения до проверки перечисленных вопросов."] if outputs else
            ["Использование итоговых финансовых показателей для решения без установленной связи замечаний с результатом."])
    if "irr_on_discounted_cashflows" in by_code:
        uses.append("Вывод о доходности или выборе проекта по текущему IRR до проверки его входных денежных потоков.")
    if "datatable_basepoint_cache_mismatch" in by_code:
        uses.append("Выбор сценария по сохранённой Data Table до проверки её базовой точки и обновления кэшей.")
    uses.append("Подтверждение рыночной реалистичности предпосылок и полной корректности всей книги.")
    status = "review_required" if findings else "insufficient_evidence"
    summary = ("Модель позволяет изучить сохранённые расчёты и сравнить измеренные условные варианты. "
               "Для использования " + output_names + " в решениях сначала нужны адресные проверки с наибольшим измеренным влиянием."
               if outputs else "Доступные материалы позволяют изучить сохранённую структуру. Измеренного влияния на конечные показатели недостаточно для содержательного подтверждения их применения в решениях.")
    if unknown:
        summary += " Для части финансовых тем влияние остаётся неизвестным; они не считаются малозначимыми."
    if incomplete:
        summary += " Анализ отдельных блоков отсутствует или не завершён."
    return {"version": VERSION, "status": status, "summary": summary,
            "policy": {"name": "ordinal_conditional_effect_review", "scope": "within_same_output_unit_baseline",
                       "grouping": "financial_theme_and_output", "alternative_aggregation": "max_absolute_effect_never_sum",
                       "relative_denominator": "abs(baseline)", "zero_baseline_relative": None, "thresholds": None,
                       "unknown_effect": "separate_unranked", "technical_counts_used": False,
                       "meaning": "Расчётный приоритет проверки по имеющимся сценариям; нормативный порог существенности и одобрение модели не устанавливаются."},
            "outputs": outputs, "unquantified_findings": unknown,
            "suitability": {"status": status, "summary": summary, "supported_uses": supported,
                            "unsubstantiated_uses": uses, "required_checks": required,
                            "incomplete_stages": incomplete, "financial_approval": False},
            "issues": issues, "limitations": list(_LIMITATIONS)}
