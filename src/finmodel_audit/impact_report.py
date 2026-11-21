"""Publish only delivered conditional calculations, never a repaired valuation."""
from copy import deepcopy
import math

from .agent_evidence import _refs


class ImpactReportError(ValueError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.issues = [{"code": "impact_evidence_invalid", "message": message, **details}]


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _close(a, b):
    return _finite(a) and _finite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def validated_impact(entry, retrieved):
    if not isinstance(entry, dict) or not isinstance(entry.get("scenario"), dict):
        raise ImpactReportError("Missing delivered conditional scenario")
    scenario = entry["scenario"]
    if (scenario.get("conditional") is not True or scenario.get("recalculated_workbook") is not False
            or not isinstance(scenario.get("id"), str) or not isinstance(scenario.get("raw_unit"), str)
            or not scenario["raw_unit"].strip()):
        raise ImpactReportError("Scenario must declare its conditional scope and source unit")
    if not all(_finite(scenario.get(key)) for key in ("baseline", "saved_output", "result", "delta")):
        raise ImpactReportError("Scenario values must be finite")
    baseline, delta = scenario["baseline"], scenario["delta"]
    if not _close(scenario["result"] - baseline, delta):
        raise ImpactReportError("Conditional delta does not match scenario minus baseline")
    if not math.isclose(baseline, scenario["saved_output"], rel_tol=1e-7, abs_tol=1e-6):
        raise ImpactReportError("Baseline does not reproduce the saved target")
    relative = scenario.get("relative_delta")
    if (baseline == 0 and relative is not None) or (baseline != 0 and not _close(relative, delta / abs(baseline))):
        raise ImpactReportError("Relative change must use the absolute baseline, or null for zero")
    required = _refs(scenario)
    records = {(row["sheet"], row["cell"]): row for row in entry.get("cell_evidence", [])
               if isinstance(row, dict) and isinstance(row.get("sheet"), str) and isinstance(row.get("cell"), str)}
    if not required or not required <= set(records) or not required <= retrieved:
        raise ImpactReportError("Every conditional calculation source must actually be delivered")
    target = scenario.get("target", {})
    source = records.get((target.get("sheet"), target.get("cell")), {})
    if source.get("value") != scenario["saved_output"]:
        raise ImpactReportError("Saved target differs from its delivered source")
    for field in ("patches", "formula_path", "source_inputs"):
        if not isinstance(scenario.get(field), list) or not scenario[field]:
            raise ImpactReportError("The conditional path, patches and frozen inputs must be explicit")
        for item in scenario[field]:
            source = records.get((item.get("sheet"), item.get("cell")), {})
            if field == "source_inputs":
                if item.get("frozen") is not True or not _finite(item.get("value")) or item["value"] != source.get("value"):
                    raise ImpactReportError("Frozen input differs from its delivered source")
            else:
                formula = item.get("original_formula" if field == "patches" else "formula")
                if not isinstance(formula, str) or formula != source.get("formula"):
                    raise ImpactReportError("Original formula differs from its delivered source")
                if field == "patches" and not isinstance(item.get("changed_formula"), str):
                    raise ImpactReportError("Conditional changed formula is missing")
    return deepcopy(scenario)


def attach_impact_reports(report, registry, retrieved):
    """The tool call is the selection. No second LLM rewrite can lose the proof."""
    if "impact_calculations" in report:
        return report
    scenarios = [validated_impact(registry[identity], retrieved) for identity in sorted(registry)]
    report["impact_calculations"] = scenarios
    for scenario in scenarios:
        target = scenario["target"]
        number = lambda value: format(value, ".12g")
        detail = (f"Условие: {scenario['assumption']}\n"
                  f"Выход: {target['sheet']}!{target['cell']}. Подпись единицы: {scenario['raw_unit']}. "
                  f"База: {number(scenario['baseline'])}; сценарий: {number(scenario['result'])}; "
                  f"изменение к базе: {number(scenario['delta'])}. "
                  "Остальные перечисленные входы зафиксированы. Это отдельный условный расчёт, не исправленная стоимость и не полный пересчёт Excel. "
                  "Эффекты разных сценариев не складываются.")
        report["observations"].append({"title": "Условный сценарий: " + scenario["title"],
            "detail": detail, "evidence": deepcopy(scenario["evidence"]), "status": "not_verified",
            "impact": "Условное изменение выбранного выхода; финансовая существенность не установлена.",
            "origin": "agent_requested_deterministic_scenario", "conditional_impact": scenario,
            "narrative_verification": "conditional_calculation_text_generated"})
    if scenarios:
        report["summary"] += " Приложены условные расчёты изменения выбранных выходов по сценариям, запрошенным агентом; это не утверждённые исправления модели."
        report["limitations"].append("Условные эффекты относятся к указанным изменениям формул при фиксированных остальных входах. Они не суммируются автоматически и не устанавливают общую финансовую существенность.")
    return report
