"""Registries of complete tool evidence actually delivered to the model.

Prepared tool results are not automatically model-seen. These registries accept
only immutable records found in the serialized request and in trusted tool
history, together with their exact source rows. They do not validate semantics.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from .references import normalize_cell


def evidence_id(tool: str, arguments: dict) -> str:
    encoded = json.dumps([tool, arguments], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "tool_" + hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _refs(value):
    """Collect explicit sheet/cell pairs, including nested calculation inputs."""
    result = set()
    if isinstance(value, dict):
        if isinstance(value.get("sheet"), str) and isinstance(value.get("cell"), str):
            result.add((value["sheet"], value["cell"]))
        for child in value.values():
            result.update(_refs(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_refs(child))
    return result


def _rows(packed):
    return {(sheet, row[0]): row for sheet, rows in packed.get("cell_data", {}).items()
            for row in rows if isinstance(row, list) and len(row) == 4 and isinstance(row[0], str)}


def register_sent_evidence(messages, tool_results, finding_registry, tool_registry, inspected_registry, impact_registry=None):
    """Mutate registries only when full trusted evidence survives the envelope.

    `inspected_registry` stores question payloads separately from findings. An
    inspection means sources were examined by a tool, not financially verified.
    """
    visible, findings, outcomes, inspections = {}, {}, {}, {}
    finding_conflicts = set()
    accepted_findings = set()
    impacts, accepted_impacts = {}, set()

    def walk(value):
        if isinstance(value, dict):
            visible.update(_rows(value))
            for finding in value.get("validation_findings", []):
                if isinstance(finding, dict) and isinstance(finding.get("id"), str):
                    if finding["id"] in findings and findings[finding["id"]] != finding:
                        finding_conflicts.add(finding["id"])
                    findings[finding["id"]] = finding
            for scenario in value.get("impact_scenarios", []):
                if isinstance(scenario, dict) and isinstance(scenario.get("id"), str):
                    impacts[scenario["id"]] = scenario
            identity = value.get("evidence_id")
            if isinstance(identity, str):
                outcomes[identity] = value
            question = value.get("investigation_question")
            if isinstance(question, dict) and isinstance(question.get("id"), str):
                inspections[question["id"]] = question
            for question in value.get("investigation_questions", []):
                if isinstance(question, dict) and isinstance(question.get("id"), str):
                    inspections[question["id"]] = question
            for child in value.values():
                if isinstance(child, (list, dict)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for message in messages:
        if message.get("role") not in {"user", "function"}:
            continue
        try:
            envelope = json.loads(message.get("content", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(envelope, dict) and "untrusted_workbook_data" in envelope:
            walk(envelope["untrusted_workbook_data"])

    for entry in tool_results:
        packed = entry.get("result", {})
        metadata = packed.get("metadata", {})
        rows = _rows(packed)
        clipped = {(flag["sheet"], flag["cell"]) for flag in packed.get("cell_text_limits", [])
                   if flag.get("value_truncated") or flag.get("formula_truncated")}

        def delivered(sources):
            return bool(sources) and not (sources & clipped) and all(
                node in rows and visible.get(node) == rows[node] for node in sources)

        for finding in metadata.get("validation_findings", []):
            identity = finding.get("id")
            sources = _refs(finding)
            if identity and findings.get(identity) == finding and delivered(sources):
                candidate = {"finding": deepcopy(finding), "cell_evidence": [
                    {"sheet": sheet, "cell": cell, "value": rows[sheet, cell][1],
                     "formula": rows[sheet, cell][2], "present": rows[sheet, cell][3] != "blank"}
                    for sheet, cell in sorted(sources)], "limitations": deepcopy(metadata.get("limitations", []))}
                if identity in finding_conflicts or (identity in finding_registry and finding_registry[identity] != candidate):
                    finding_registry[identity] = {"conflict": True}
                else:
                    finding_registry[identity] = candidate
                    accepted_findings.add(identity)
        identity = metadata.get("evidence_id")
        # Only model-requested investigations/trace/calculation count here.
        if entry.get("automatic") or entry.get("replayed"):
            continue
        if entry.get("tool") == "calculate_impact" and impact_registry is not None:
            for scenario in metadata.get("impact_scenarios", []):
                identity = scenario.get("id")
                sources = _refs(scenario)
                original = next((item for item in entry.get("impact_scenarios_raw", []) if item.get("id") == identity), None)
                if original is not None and impacts.get(identity) == scenario and delivered(sources):
                    impact_registry[identity] = {"scenario": deepcopy(original), "cell_evidence": [
                        {"sheet": sheet, "cell": cell, "value": rows[sheet, cell][1],
                         "formula": rows[sheet, cell][2], "present": rows[sheet, cell][3] != "blank"}
                        for sheet, cell in sorted(sources)]}
                    accepted_impacts.add(identity)
        sent_outcome = outcomes.get(identity)
        exact_or_compacted = isinstance(sent_outcome, dict) and all(
            key in metadata and metadata[key] == value for key, value in sent_outcome.items())
        if entry.get("tool") in {"calculate_cells", "sensitivity_cells"}:
            exact_or_compacted = exact_or_compacted and any(key in sent_outcome for key in ("result", "baseline"))
        else:
            exact_or_compacted = sent_outcome == metadata
        if identity and exact_or_compacted and delivered(set(rows)):
            # A deliberately bounded graph may be incomplete yet fully
            # delivered. Preserve that scope limitation for hypothesis checks;
            # transport clipping is different and cannot authorize a proof.
            try:
                argument_sources = {(sheet, normalize_cell(cell)) for sheet, cell in _refs(entry.get("arguments", {}))}
            except ValueError:
                argument_sources = {(None, None)}
            required = _refs(metadata) | argument_sources
            if (not packed.get("metadata_truncated") and not packed.get("omitted_cell_count")
                    and not metadata.get("error") and required <= set(rows)):
                tool_registry[identity] = deepcopy({"tool": entry["tool"], "arguments": entry["arguments"], "result": packed})
        question = metadata.get("investigation_question")
        if isinstance(question, dict) and inspections.get(question.get("id")) == question:
            sources = _refs(question)
            if delivered(sources) or (question.get("status") == "unsupported" and question.get("evidence") == []):
                inspected_registry[question["id"]] = deepcopy(packed)
    return {"finding_ids": sorted(accepted_findings), "impact_ids": sorted(accepted_impacts)}
