"""Compile methodological selections from evidence actually delivered to an agent.

The caller owns the delivered registries and workbook/snapshot checks. A finding
entry is {finding, cell_evidence, limitations}; a tool entry is
{tool, arguments, result}, where result is the existing packed tool response.
Registry membership must mean sent in a request, not merely prepared by a tool.
This module performs no Excel evaluation and does not verify financial semantics.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from typing import Any


VERSION = "methodology-1"
STAGES = {"audit": "Аудит расчётов", "quality": "Оценка качества модели"}
LIMITATIONS = {
    "no_full_recalculation": "Полный пересчёт Excel не выполнен; используются сохранённые значения и явно указанные локальные расчёты.",
    "external_facts_unverified": "Внешние факты и реалистичность предпосылок независимо не проверены.",
    "financial_materiality_not_assessed": "Общая финансовая существенность не установлена. Поддержанные условные эффекты и порядок их проверки представлены отдельно, когда расчёт доступен.",
    "limited_source_scope": "Выводы ограничены перечисленными источниками; проверка всех периодов или всей модели не заявляется.",
    "incomplete_dependencies": "Полнота зависимостей не установлена; пропущенные и неподдержанные связи требуют отдельной проверки.",
    "calculation_not_performed": "Связанный с предметом гипотезы локальный расчёт не подтверждён инструментальным результатом.",
    "periods_unverified": "Календарные периоды и статус факт/прогноз этой проверки не установлены.",
}
DEFAULT_LIMITS = tuple(LIMITATIONS)[:4]
MAX_SOURCES = 1000
TOOL_NAMES = {"trace_precedents", "trace_dependents", "calculate_cells"}
MISSING_EVIDENCE = {
    "target_formula_not_delivered": "Полная формула предмета гипотезы не получена.",
    "target_precedents_not_traced": "Статические входы предмета гипотезы не прослежены.",
    "linked_calculation_not_delivered": "Связанный с предметом локальный расчёт не получен.",
    "calculation_outside_target_dependencies": "Часть расчётов использует источники вне прослеженных зависимостей предмета.",
    "trace_outside_target": "Часть трассировок относится к другому предмету.",
    "tool_result_incomplete": "Инструментальный ответ сокращён; полнота доказательств не установлена.",
    "tool_scope_incomplete": "Инструмент указал неполную область проверки.",
    "tool_failed": "Один из инструментальных ответов содержит ошибку.",
    "calculation_result_unavailable": "Числовой результат локального расчёта не получен.",
}


class MethodologyError(ValueError):
    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or [{"code": "invalid_methodology_selection", "message": message}]


def _fail(code: str, message: str, **details) -> None:
    raise MethodologyError(message, [{"code": code, "message": message, **details}])


def methodology_schema(stage_id: str) -> dict:
    if stage_id not in STAGES:
        _fail("unsupported_stage", "Methodological selection is supported only for audit and quality.")
    string = {"type": "string", "minLength": 1, "maxLength": 200}
    limits = {"type": "array", "maxItems": len(LIMITATIONS), "uniqueItems": True,
              "items": {"type": "string", "enum": list(LIMITATIONS)},
              "description": "Только коды ограничений; свободные выводы сюда не добавляй."}
    return {"name": "submit_stage_report",
            "description": "Выбери полученные finding_ids. Код сохранит исходную находку, формулы, расчёт, статус и ограничения. Собственный текст допускается только как качественная непроверенная гипотеза; не передавай summary, status или готовый вывод.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {
                "id": {"type": "string", "enum": [stage_id], "description": "Текущая стадия."},
                "finding_ids": {"type": "array", "maxItems": 12, "uniqueItems": True, "items": string,
                                "description": "Точные id доставленных находок; при квалифицированном id сохрани scope:id целиком. Не заменяй находку свободным пересказом."},
                "hypotheses": {"type": "array", "maxItems": 6, "description": "Собственные качественные гипотезы, отдельно от выбранных детерминированных находок.", "items": {"type": "object", "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Качественное название собственной гипотезы, без чисел и адресов."},
                        "detail": {"type": "string", "minLength": 1, "maxLength": 1500, "description": "Непроверенное качественное предположение, без числовых фактов; оно не будет опубликовано как доказанный вывод."},
                        "target": {"type": "object", "additionalProperties": False, "description": "Одна исходная ячейка, формула которой является предметом гипотезы.", "properties": {
                            "sheet": {**string, "description": "Точный лист предмета гипотезы; это источник, не установленная компания."},
                            "cell": {"type": "string", "pattern": "^[A-Z]{1,3}[1-9][0-9]{0,6}$", "description": "Одна уже прочитанная ячейка предмета гипотезы."}}, "required": ["sheet", "cell"]},
                        "tool_evidence_ids": {"type": "array", "maxItems": 6, "uniqueItems": True, "items": string,
                                              "description": "Доставленные evidence_id трассировок и calculate_cells, относящихся к target. Наличие инструмента не доказывает финансовый смысл."},
                        "limitations": limits,
                    }, "required": ["title", "detail", "target", "tool_evidence_ids", "limitations"]}},
                "limitations": limits,
            }, "required": ["id", "finding_ids", "hypotheses", "limitations"]}}


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
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


def _ref(value: Any) -> tuple[str, str]:
    if (not isinstance(value, dict) or not isinstance(value.get("sheet"), str)
            or not 0 < len(value["sheet"]) <= 200 or not _address(value.get("cell"))):
        _fail("invalid_source", "A source must contain an exact worksheet and one valid A1 address.")
    return value["sheet"], value["cell"]


def _refs(nodes) -> list[dict]:
    return [{"sheet": sheet, "cell": cell} for sheet, cell in sorted(set(nodes))]


def _argument_ref(value: Any) -> tuple[str, str]:
    """Match the tools' documented A1 normalization, never repair worksheet names."""
    if not isinstance(value, dict) or not isinstance(value.get("cell"), str) or len(value["cell"]) > 20:
        _fail("invalid_source", "A tool argument requires one valid A1 address.")
    return _ref({**value, "cell": value["cell"].replace("$", "").upper()})


def _argument_sources(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        _fail("invalid_source_list", "Tool input sources must be a bounded list.")
    return {_argument_ref(item) for item in value}


def _source_list(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        _fail("invalid_source_list", "Source evidence must be a bounded list.")
    return {_ref(item) for item in value}


def _ids(value: Any, maximum: int, field: str) -> list[str]:
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in value)
            or len(set(value)) != len(value)):
        _fail("invalid_selection", "Selection ids must be unique bounded strings.", field=field)
    return value


def _limits(value: Any) -> list[str]:
    selected = _ids(value, len(LIMITATIONS), "limitations")
    if any(item not in LIMITATIONS for item in selected):
        _fail("unknown_limitation", "Select only limitation codes from the schema.")
    return selected


def _json_checked(value: Any) -> None:
    try:
        valid = len(json.dumps(value, ensure_ascii=False, allow_nan=False)) <= 200_000
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        _fail("invalid_registry_entry", "Delivered evidence must be finite, bounded JSON data.")


def _cells(values: Any, retrieved: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    if not isinstance(values, list) or len(values) > MAX_SOURCES:
        _fail("invalid_source_cells", "Delivered source cells must be a bounded list.")
    cells = {}
    for item in values:
        key = _ref(item)
        if key not in retrieved:
            _fail("unread_source", "Every selected source must be in the delivered source ledger.", sheet=key[0], cell=key[1])
        if "value" not in item or "formula" not in item or type(item.get("present")) is not bool:
            _fail("incomplete_source_record", "Each source requires saved value, exact formula (or null), and presence.", sheet=key[0], cell=key[1])
        if item["formula"] is not None and not isinstance(item["formula"], str):
            _fail("invalid_formula_record", "A delivered formula must be exact text or null.")
        if item.get("value_truncated") or item.get("formula_truncated"):
            _fail("truncated_source", "A selected proof cannot use clipped source text.", sheet=key[0], cell=key[1])
        cell = deepcopy(item)
        if key in cells and cells[key] != cell:
            _fail("conflicting_source", "Selected evidence contains conflicting records for one cell.", sheet=key[0], cell=key[1])
        cells[key] = cell
    return cells


def _require_sources(sources, cells, retrieved) -> None:
    missing = set(sources) - set(cells)
    unread = set(sources) - retrieved
    if missing or unread:
        key = sorted(missing | unread)[0]
        _fail("missing_proof_source", "Every referenced source, including the target and calculation inputs, must be delivered in full.", sheet=key[0], cell=key[1])


def _merge_cells(target: dict, incoming: dict) -> None:
    for key, cell in incoming.items():
        if key in target and any(type(target[key].get(field)) is not type(cell.get(field))
                                 or target[key].get(field) != cell.get(field) for field in ("value", "formula", "present")):
            _fail("conflicting_source", "Selected proofs do not share one source snapshot.", sheet=key[0], cell=key[1])
        target[key] = cell


def _scope(cells: dict) -> dict:
    return {"kind": "listed_sources_only", "checked_cells": _refs(cells), "all_periods": False,
            "calendar_periods": "not_inferred", "financial_materiality": "not_assessed",
            "recalculated_workbook": False}


def _finding(identity: str, registry: dict, retrieved: set[tuple[str, str]]) -> tuple[dict, dict]:
    entry = registry.get(identity)
    if not isinstance(entry, dict) or not isinstance(entry.get("finding"), dict):
        _fail("unknown_finding_id", "Select an exact id from delivered validation findings.", finding_id=identity)
    _json_checked(entry)
    finding = entry["finding"]
    scope = finding.get("scope")
    if scope not in {"financial", "supplemental"} or identity not in {finding.get("id"), f"{scope}:{finding.get('id')}"}:
        _fail("invalid_finding_identity", "A registry key must match the delivered finding id and scope.", finding_id=identity)
    matches = [other["finding"] for other in registry.values() if isinstance(other, dict)
               and isinstance(other.get("finding"), dict) and other["finding"].get("id") == finding.get("id")]
    if identity == finding.get("id") and any(other != finding for other in matches):
        _fail("ambiguous_finding_id", "This finding id collides; select its delivered scope-qualified id.", finding_id=identity)
    if finding.get("status") not in {"candidate", "hypothesis", "observation", "not_verified"}:
        _fail("invalid_finding_status", "A delivered finding cannot claim financial verification.")
    for field in ("title", "detail"):
        if not isinstance(finding.get(field), str) or not finding[field].strip():
            _fail("invalid_finding_text", "A delivered finding requires its original title and detail.")
    cells = _cells(entry.get("cell_evidence"), retrieved)
    sources = _source_list(finding.get("evidence"))
    if not sources:
        _fail("missing_proof_source", "A finding needs source-bound evidence.")
    sources |= _source_list(finding.get("source_features", []))
    if finding.get("target") is not None:
        sources.add(_ref(finding["target"]))
    calculation = deepcopy(finding.get("calculation"))
    if calculation is not None:
        if not isinstance(calculation, dict) or calculation.get("basis") != "saved_values" or calculation.get("recalculated") is not False:
            _fail("invalid_calculation_proof", "A finding calculation must declare its saved-value basis and no full recalculation.")
        sources |= _source_list(calculation.get("inputs"))
    _require_sources(sources, cells, retrieved)
    if calculation is not None:
        for item in calculation["inputs"]:
            value = cells[_ref(item)]["value"]
            if not _finite(value) or ("value" in item and item["value"] != value):
                _fail("calculation_source_mismatch", "Calculation input values must match their delivered saved source cells.")
            item["value"] = value
    # Keep all supplied rows, not just a convenient coefficient or one citation.
    limits = entry.get("limitations", [])
    if not isinstance(limits, list) or any(not isinstance(item, str) for item in limits):
        _fail("invalid_registry_entry", "Delivered scope limitations must be strings.")
    status = {"candidate": "hypothesis", "hypothesis": "hypothesis", "observation": "observation", "not_verified": "not_verified"}[finding["status"]]
    evidence = _refs(set(cells) | sources)
    proof = {"version": VERSION, "origin": "deterministic_finding", "finding_id": identity,
             "original_status": finding["status"], "detector_scope": scope,
             "original_finding": deepcopy(finding), "source_cells": list(cells.values()),
             "scope": _scope(cells), "limitations": list(dict.fromkeys(limits + [LIMITATIONS[key] for key in DEFAULT_LIMITS])),
             "verification": "source_bound_not_semantically_verified"}
    if calculation is not None:
        proof["calculation"] = calculation
    return {"title": finding["title"], "detail": finding["detail"] + "\nПроисхождение: детерминированная проверка; это не самостоятельное обнаружение агента. "
            + ("Кандидат требует проверки. " if status == "hypothesis" else "")
            + "Предмет и границы сохранены в исходных доказательствах; проверка всех периодов и финансовая существенность не установлены.",
            "status": status, "impact": "not_assessed", "evidence": evidence,
            "finding_id": identity, "methodology": proof, "narrative_verification": "deterministic_finding_text_generated"}, cells


def _tool(identity: str, registry: dict, retrieved: set[tuple[str, str]]) -> tuple[dict, dict, list[str]]:
    entry = registry.get(identity)
    if not isinstance(entry, dict) or entry.get("tool") not in TOOL_NAMES:
        _fail("unknown_tool_evidence_id", "Select an exact delivered trace/calculation evidence_id.", evidence_id=identity)
    _json_checked(entry)
    packed, arguments = entry.get("result"), entry.get("arguments")
    if not isinstance(packed, dict) or not isinstance(arguments, dict) or not isinstance(packed.get("metadata"), dict):
        _fail("invalid_tool_proof", "Tool evidence requires its original arguments and packed result.")
    metadata = packed["metadata"]
    if metadata.get("evidence_id") != identity:
        _fail("tool_identity_mismatch", "Evidence id must match the id actually delivered with the tool result.")
    if packed.get("cell_columns") != ["cell", "saved_value", "formula", "kind"] or not isinstance(packed.get("cell_data"), dict):
        _fail("invalid_tool_cells", "Tool proof requires the canonical packed source-cell columns.")
    records = []
    for sheet, rows in packed["cell_data"].items():
        if not isinstance(rows, list):
            _fail("invalid_tool_cells", "Packed source rows must be lists.")
        for row in rows:
            if not isinstance(row, list) or len(row) != 4:
                _fail("invalid_tool_cells", "Each source row must preserve all canonical columns.")
            records.append({"sheet": sheet, "cell": row[0], "value": row[1], "formula": row[2], "present": row[3] != "blank"})
    cells = _cells(records, retrieved)
    reasons = []
    if packed.get("truncated") or packed.get("metadata_truncated") or packed.get("omitted_cell_count") or packed.get("cell_text_limits"):
        reasons.append("tool_result_incomplete")
    completeness = metadata.get("completeness")
    if not isinstance(completeness, dict) or completeness.get("complete") is not True:
        reasons.append("tool_scope_incomplete")
    if packed.get("error") or metadata.get("error"):
        reasons.append("tool_failed")
    sources = set(cells)
    if entry["tool"].startswith("trace_"):
        root = _ref(metadata.get("root"))
        if root != _argument_ref(arguments) or metadata.get("direction") != entry["tool"].removeprefix("trace_"):
            _fail("tool_target_mismatch", "A trace must preserve its requested root and direction.")
        sources.add(root)
        edges = metadata.get("edges")
        if not isinstance(edges, list) or len(edges) > 5000:
            _fail("invalid_tool_edges", "A trace must preserve a bounded edge list.")
        for edge in edges:
            if not isinstance(edge, dict):
                _fail("invalid_tool_edges", "A trace edge needs exact from/to sources.")
            sources.update((_ref(edge.get("from")), _ref(edge.get("to"))))
    else:
        sources |= _source_list(metadata.get("evidence")) | _argument_sources(arguments.get("inputs"))
        if (not isinstance(metadata.get("expression"), str) or metadata["expression"] != arguments.get("expression")
                or metadata.get("basis") != "saved_values" or metadata.get("recalculated") is not False):
            _fail("invalid_calculation_proof", "A calculation must preserve expression, saved-value basis and recalculation boundary.")
        if not isinstance(metadata.get("result"), dict) or not _finite(metadata["result"].get("value")):
            reasons.append("calculation_result_unavailable")
        expected = {(item.get("name"), *_argument_ref(item)) for item in arguments["inputs"]}
        actual = {(item.get("name"), *_ref(item)) for item in metadata["evidence"]}
        if expected != actual:
            _fail("calculation_source_mismatch", "Calculation variables must preserve every exact source binding.")
    _require_sources(sources, cells, retrieved)
    if entry["tool"] == "calculate_cells" and "calculation_result_unavailable" not in reasons:
        if any(not _finite(cells[_argument_ref(item)]["value"]) for item in arguments["inputs"]):
            _fail("calculation_source_mismatch", "A successful arithmetic calculation requires finite numeric source inputs.")
    return deepcopy(entry), cells, reasons


def _hypothesis(item: Any, registry: dict, retrieved: set[tuple[str, str]]) -> tuple[dict, dict]:
    fields = {"title", "detail", "target", "tool_evidence_ids", "limitations"}
    if not isinstance(item, dict) or set(item) != fields:
        _fail("invalid_hypothesis", "Use exactly the hypothesis fields from the schema.")
    for field, maximum in (("title", 200), ("detail", 1500)):
        value = item[field]
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            _fail("invalid_hypothesis_text", "Hypothesis text must be short nonempty strings.", field=field)
        # This is a conservative input grammar, not a semantic classifier.
        # Quantities belong in immutable tool results, never in the free draft.
        if any(character.isnumeric() for character in value):
            _fail("numeric_hypothesis_text", "Own hypothesis text must be qualitative; numbers and addresses belong in target and tool evidence.", field=field)
    if not isinstance(item["target"], dict) or set(item["target"]) != {"sheet", "cell"}:
        _fail("invalid_hypothesis_target", "A hypothesis needs one exact target cell.")
    target = _ref(item["target"])
    if target not in retrieved:
        _fail("unread_hypothesis_target", "The hypothesis target must already be delivered.", sheet=target[0], cell=target[1])
    identities = _ids(item["tool_evidence_ids"], 6, "tool_evidence_ids")
    chosen_limits = _limits(item["limitations"])
    cells, proofs, reasons = {}, [], []
    for identity in identities:
        proof, source_cells, missing = _tool(identity, registry, retrieved)
        _merge_cells(cells, source_cells)
        proofs.append({"evidence_id": identity, **proof})
        reasons.extend(missing)
    target_cell = cells.get(target)
    if not target_cell or not target_cell["present"] or not target_cell["formula"]:
        reasons.append("target_formula_not_delivered")
    traces = [proof for proof in proofs if proof["tool"] == "trace_precedents"
              and _ref(proof["result"]["metadata"]["root"]) == target]
    reachable = {target}
    for trace in traces:
        edges = trace["result"]["metadata"]["edges"]
        while True:
            added = {_ref(edge["to"]) for edge in edges if _ref(edge["from"]) in reachable} - reachable
            if not added:
                break
            reachable.update(added)
    if not traces:
        reasons.append("target_precedents_not_traced")
    calculations = [proof for proof in proofs if proof["tool"] == "calculate_cells"]
    linked_calculations = [proof for proof in calculations if proof["arguments"]["inputs"]
                           and _argument_sources(proof["arguments"]["inputs"]) <= reachable]
    if not linked_calculations:
        reasons.append("linked_calculation_not_delivered")
    if any(proof not in linked_calculations for proof in calculations):
        reasons.append("calculation_outside_target_dependencies")
    if any(proof["tool"].startswith("trace_") and _ref(proof["result"]["metadata"]["root"]) != target for proof in proofs):
        reasons.append("trace_outside_target")
    reasons = sorted(set(reasons))
    scope = _scope(cells)
    scope["target"] = deepcopy(item["target"])
    scope["dependency_cells"] = _refs(reachable)
    scope["checked_cells"] = _refs(set(cells) | {target})
    proof = {"version": VERSION, "origin": "agent_hypothesis", "candidate_text": {"title": item["title"], "detail": item["detail"]},
             "tool_evidence": proofs, "source_cells": list(cells.values()), "scope": scope,
             "verification": "tool_backed_hypothesis" if not reasons else "not_verified", "missing_evidence": reasons,
             "limitations": [LIMITATIONS[key] for key in dict.fromkeys(DEFAULT_LIMITS + tuple(chosen_limits))]
                + ["Инструментальные связи и локальная арифметика не подтверждают финансовый смысл гипотезы."]}
    detail = f"Непроверенное предположение агента: «{item['title']} — {item['detail']}».\n"
    detail += "Это текст гипотезы, а не установленный финансовый вывод. "
    detail += "Инструментальная база неполна. " if reasons else "Сохранены формула предмета, статическая трассировка и связанный локальный расчёт. "
    detail += "Область проверки ограничена перечисленными ячейками; календарные периоды и проверка всей модели не заявляются."
    if reasons:
        detail += "\nНедостающие доказательства: " + " ".join(MISSING_EVIDENCE[reason] for reason in reasons)
    return {"title": "Собственная гипотеза агента", "detail": detail, "status": "not_verified" if reasons else "hypothesis",
            "impact": "not_assessed", "evidence": _refs(set(cells) | {target}), "methodology": proof,
            "narrative_verification": "agent_hypothesis_unverified"}, cells


def validate_candidate_coverage(payload: Any, finding_registry: dict) -> dict:
    """Require one selection per delivered financial code theme when requested.

    This is an optional audit workflow gate, not part of report normalization.
    The caller supplies the actual-sent registry for this run, never the full
    detector output. Selecting a theme neither proves it nor changes its status.
    Supplemental signals, conflicted entries and findings without a code do not
    create obligations. Full identity/source validation remains in the builder.
    """
    if not isinstance(payload, dict) or not isinstance(finding_registry, dict):
        _fail("invalid_candidate_coverage", "Candidate coverage requires a selection and the delivered finding registry.")
    selected = set(_ids(payload.get("finding_ids"), 12, "finding_ids"))
    themes: dict[str, dict] = {}
    for identity, entry in finding_registry.items():
        if not isinstance(identity, str) or not isinstance(entry, dict) or entry.get("conflict"):
            continue
        finding = entry.get("finding")
        if not isinstance(finding, dict) or finding.get("scope") != "financial":
            continue
        code = finding.get("code")
        if not isinstance(code, str) or not code.strip():
            continue
        # A different registry key cannot grant coverage of this finding.
        if identity not in {finding.get("id"), f"financial:{finding.get('id')}"}:
            continue
        title = finding.get("title")
        theme = themes.setdefault(code, {"theme_code": code, "title": title[:300] if isinstance(title, str) else code,
                                         "candidate_ids": []})
        theme["candidate_ids"].append(identity)
    covered = {code for code, theme in themes.items() if selected.intersection(theme["candidate_ids"])}
    missing = [themes[code] for code in sorted(set(themes) - covered)]
    if missing:
        issues = [{"code": "missing_candidate_theme",
                   "message": "Select at least one delivered finding id for this financial theme; preserve its original candidate/observation status.",
                   "theme_code": theme["theme_code"], "title": theme["title"],
                   "candidate_ids": sorted(theme["candidate_ids"])[:6],
                   "additional_candidate_count": max(0, len(theme["candidate_ids"]) - 6)} for theme in missing]
        raise MethodologyError(
            "Audit selection omits delivered financial themes. Select at least one candidate_id from each missing theme; repeated findings of one theme do not cover another.",
            issues,
        )
    return {"scope": "delivered_financial_code_themes", "required_codes": sorted(themes),
            "selected_codes": sorted(covered), "required_theme_count": len(themes),
            "selected_theme_count": len(covered), "financial_verification": False}


def build_methodology_report(payload: Any, stage_id: str, finding_registry: dict,
                             retrieved: set[tuple[str, str]], *, tool_registry: dict | None = None) -> dict:
    """Normalize a strict selection; registry values are never supplied by the LLM."""
    if stage_id not in STAGES:
        _fail("unsupported_stage", "Methodological selection is supported only for audit and quality.")
    fields = {"id", "finding_ids", "hypotheses", "limitations"}
    if not isinstance(payload, dict) or set(payload) != fields or payload.get("id") != stage_id:
        _fail("invalid_methodology_selection", "Use exactly id, finding_ids, hypotheses and limitations for the current stage.")
    if not isinstance(finding_registry, dict) or (tool_registry is not None and not isinstance(tool_registry, dict)):
        _fail("invalid_registry", "Delivered registries must be mappings owned by the caller.")
    identities = _ids(payload["finding_ids"], 12, "finding_ids")
    limits = _limits(payload["limitations"])
    hypotheses = payload["hypotheses"]
    if not isinstance(hypotheses, list) or len(hypotheses) > 6:
        _fail("invalid_hypotheses", "At most six own hypotheses may be submitted.")
    observations, all_cells, selected = [], {}, set()
    for identity in identities:
        observation, cells = _finding(identity, finding_registry, retrieved)
        key = observation["methodology"]["detector_scope"], observation["methodology"]["original_finding"]["id"]
        if key in selected:
            _fail("duplicate_finding", "Do not select the same finding using both bare and qualified ids.")
        selected.add(key)
        _merge_cells(all_cells, cells)
        observations.append(observation)
    seen_hypotheses = set()
    for item in hypotheses:
        observation, cells = _hypothesis(item, tool_registry or {}, retrieved)
        identity = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if identity in seen_hypotheses:
            _fail("duplicate_hypothesis", "Do not repeat an identical own hypothesis.")
        seen_hypotheses.add(identity)
        _merge_cells(all_cells, cells)
        observations.append(observation)
    summary = (f"Сохранены исходные находки детерминированных проверок: {len(identities)}; собственные гипотезы агента: {len(hypotheses)}. "
               "Кандидаты не признаны доказанными финансовыми ошибками; ссылки и локальные расчёты не подтверждают методологию всей модели.")
    if not observations:
        summary = "Доказательства для методологического вывода не выбраны; проверка не подтверждена."
    if stage_id == "quality":
        summary = "Пригодность модели для принятия решений не установлена. " + summary
    return {"id": stage_id, "title": STAGES[stage_id],
            "status": "not_verified" if stage_id == "quality" or not observations or all(item["status"] == "not_verified" for item in observations) else "limited",
            "summary": summary, "observations": observations,
            "limitations": [LIMITATIONS[key] for key in dict.fromkeys(DEFAULT_LIMITS + tuple(limits))],
            "selection": deepcopy(payload), "methodology_version": VERSION}
