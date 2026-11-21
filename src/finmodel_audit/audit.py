"""Evidence from file structure and saved values, without recalculation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
import re
from typing import Any

from .workbook import Workbook


@dataclass
class Finding:
    code: str
    level: str
    title: str
    sheet: str | None = None
    cells: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    verification: str = "file_structure"
    financial_materiality: str = "not_assessed"
    impact: str = "Влияние на итоговые показатели ещё не проверено."


def _without_string_literals(formula: str) -> str:
    return re.sub(r'"(?:[^\"]|\"\")*"', '""', formula)


def _reference_features(formula: str) -> tuple[bool, bool]:
    """Recognize external qualifiers and error tokens outside text/column names."""
    formula = _without_string_literals(formula)
    quoted = re.findall(r"'(?:[^']|'')*'!", formula)
    external = any(re.search(r"\[\d+\]", qualifier) for qualifier in quoted)
    unquoted = re.sub(r"'(?:[^']|'')*'!", "", formula)
    # A workbook index precedes a sheet/name. Table1[2024] is a table column.
    external = external or bool(re.search(r"(?<![\w.\]\\])\[\d+\](?=[\w\\])", unquoted))
    outside_columns = re.sub(r"\[[^\]]*\]", "", unquoted)
    return external, "#REF!" in outside_columns


def audit_workbook(workbook: Workbook) -> dict[str, Any]:
    findings: list[Finding] = []
    for sheet in workbook.sheets.values():
        errors: dict[str, list] = defaultdict(list)
        missing_caches = []
        broken_references = []
        for cell in sheet.cells.values():
            if cell.data_type == "e":
                errors[str(cell.value)].append(cell)
            if cell.formula_type and cell.value is None:
                missing_caches.append(cell.address)
            external, broken = _reference_features(cell.formula or "")
            if broken:
                broken_references.append(cell.address)
            if external:
                findings.append(Finding(
                    code="external_formula", level="warning",
                    title="Формула ссылается на внешнюю книгу",
                    sheet=sheet.name, cells=[cell.address],
                    evidence={"formula": cell.formula, "cached_value": cell.value},
                    impact="Актуальность внешних данных не проверена. Внешняя книга не открывалась.",
                ))
            if cell.formula_type == "dataTable":
                findings.append(Finding(
                    code="what_if_table", level="info",
                    title="Таблица чувствительности требует совместимого движка пересчёта",
                    sheet=sheet.name, cells=[cell.address],
                    evidence={"attributes": cell.formula_attributes},
                    impact="Сохранённые результаты доступны для чтения; свежий пересчёт не выполнен.",
                ))
        for error, cells in errors.items():
            findings.append(Finding(
                code="cached_excel_error", level="error",
                title=f"Сохранённая ошибка Excel {error}",
                sheet=sheet.name, cells=[c.address for c in cells],
                evidence={"error": error, "count": len(cells),
                          "samples": [{"cell": c.address, "formula": c.formula,
                                       "formula_type": c.formula_type}
                                      for c in cells[:5]]},
                verification="saved_cache",
                impact="Ошибка присутствует в сохранённом файле. Причина и влияние требуют проверки зависимостей и пересчёта.",
            ))
        if broken_references:
            findings.append(Finding(
                code="broken_reference", level="error", title="В формуле есть #REF!",
                sheet=sheet.name, cells=broken_references,
            ))
        if missing_caches:
            findings.append(Finding(
                code="missing_formula_cache", level="warning",
                title="У формул нет сохранённого значения",
                sheet=sheet.name, cells=missing_caches,
                impact="Значения нельзя восстановить одним чтением файла; нужен пересчёт.",
            ))
    if workbook.calculation.get("iterate") in {"1", "true"}:
        findings.append(Finding(
            code="iterative_calculation", level="info",
            title="В книге включены итерационные вычисления",
            evidence={"calculation": workbook.calculation},
            impact="Наличие циклов и сходимость не проверены. Настройка сама по себе не считается ошибкой.",
        ))
    if workbook.external_links:
        findings.append(Finding(
            code="external_links", level="info",
            title="В пакете Excel сохранены связи с внешними книгами",
            evidence={"count": len(workbook.external_links)},
            impact="Связи не обновлялись. Наличие записи не доказывает, что она влияет на расчёты.",
        ))
    for warning in workbook.warnings:
        findings.append(Finding(code="reader_limitation", level="warning",
                                title="Ограничение чтения книги", evidence={"detail": warning}))
    return {
        "schema_version": 1,
        "stage": "structural_audit",
        "source": workbook.path.name,
        "summary": workbook.summary(),
        "recalculated": False,
        "financial_materiality_assessed": False,
        "limitations": [
            "Проверены структура, формулы и сохранённые значения. Пересчёт Excel не выполнялся.",
            "Граф зависимостей, финансовая методология и реалистичность предпосылок пока не проверены.",
            "Количество технических находок не является оценкой качества финансовой модели.",
        ],
        "findings": [asdict(f) for f in findings],
    }
