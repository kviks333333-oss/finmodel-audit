"""Read Excel's OOXML evidence without calculating or modifying the workbook.

Only the standard library is used. Numeric dates remain Excel serial numbers;
``value`` is the last saved cache for formula cells, not a newly calculated result.
Shared A1 formulas are expanded; unsupported translations remain explicitly absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import posixpath
import re
from typing import Any
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN_NS = "http://purl.oclc.org/ooxml/spreadsheetml/main"
MAX_ZIP_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_COLUMN = 16_384
MAX_ROW = 1_048_576


class WorkbookReadError(ValueError):
    """The input cannot be read safely as a supported OOXML workbook."""


class FormulaTranslationError(ValueError):
    """A shared formula could not be expanded without guessing."""


@dataclass
class Cell:
    address: str
    value: str | float | int | bool | None
    formula: str | None = None
    formula_type: str | None = None
    data_type: str = "n"
    style_id: int | None = None
    formula_attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class Sheet:
    name: str
    state: str = "visible"
    cells: dict[str, Cell] = field(default_factory=dict)


@dataclass
class Workbook:
    path: Path
    sheets: dict[str, Sheet]
    defined_names: list[dict[str, Any]]
    external_links: list[dict[str, Any]]
    calculation: dict[str, str]
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        """Return compact, JSON-safe counts of stored evidence, not audit verdicts."""
        sheet_summaries = []
        for sheet in self.sheets.values():
            sheet_summaries.append({
                "name": sheet.name,
                "state": sheet.state,
                "formula_cells": sum(c.formula_type is not None for c in sheet.cells.values()),
                "cached_errors": sum(c.data_type == "e" for c in sheet.cells.values()),
                "actual_nonempty_cells": len(sheet.cells),
                "missing_formula_caches": sum(
                    c.formula_type is not None and c.value is None
                    for c in sheet.cells.values()
                ),
            })
        return {
            "path": str(self.path),
            "sheet_count": len(self.sheets),
            "formula_cells": sum(s["formula_cells"] for s in sheet_summaries),
            "cached_errors": sum(s["cached_errors"] for s in sheet_summaries),
            "actual_nonempty_cells": sum(s["actual_nonempty_cells"] for s in sheet_summaries),
            "missing_formula_caches": sum(s["missing_formula_caches"] for s in sheet_summaries),
            "defined_name_count": len(self.defined_names),
            "external_link_count": len(self.external_links),
            "calculation": dict(self.calculation),
            "sheets": sheet_summaries,
            "warnings": list(self.warnings),
        }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_number(letters: str) -> int:
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _column_letters(number: int) -> str:
    if not 1 <= number <= MAX_COLUMN:
        raise FormulaTranslationError("relative column moves outside Excel's grid")
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


_ADDRESS = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*)$")
_CELL_REF = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)")
_COLUMN_RANGE = re.compile(r"(\$?)([A-Za-z]{1,3}):(\$?)([A-Za-z]{1,3})")
_ROW_RANGE = re.compile(r"(\$?)([1-9][0-9]*):(\$?)([1-9][0-9]*)")
# A sheet identifier can resemble a cell (Q1!); recognize its qualifier first.
_SHEET_QUALIFIER = re.compile(r"(?:\[[^\[\]]+\])?[^\s+*/^&=<>(),;{}\"'\[\]!-]+!")


def _address_parts(address: str) -> tuple[int, int]:
    match = _ADDRESS.fullmatch(address)
    if not match:
        raise WorkbookReadError(f"Invalid cell address: {address!r}")
    column, row = _column_number(match[1]), int(match[2])
    if column > MAX_COLUMN or row > MAX_ROW:
        raise WorkbookReadError(f"Cell address outside Excel's grid: {address}")
    return column, row


def _is_name_char(char: str) -> bool:
    return char.isalnum() or char in "_.\\"


def translate_formula(formula: str, origin: str, target: str) -> str:
    """Translate A1 references in a shared formula, retaining literals and sheets.

    Supports mixed/absolute cell references, ranges, entire columns/rows, quoted
    sheet identifiers, external workbook qualifiers and 3-D sheet qualifiers.
    Structured table references require table-column context and are rejected.
    """
    origin_col, origin_row = _address_parts(origin)
    target_col, target_row = _address_parts(target)
    delta_col, delta_row = target_col - origin_col, target_row - origin_row
    if not delta_col and not delta_row:
        return formula

    def shift_col(absolute: str, letters: str) -> str:
        number = _column_number(letters)
        if number > MAX_COLUMN:
            raise FormulaTranslationError("column reference outside Excel's grid")
        return absolute + (letters if absolute else _column_letters(number + delta_col))

    def shift_row(absolute: str, digits: str) -> str:
        row = int(digits) + (0 if absolute else delta_row)
        if not 1 <= row <= MAX_ROW:
            raise FormulaTranslationError("relative row moves outside Excel's grid")
        return absolute + str(row)

    result: list[str] = []
    position = 0
    while position < len(formula):
        char = formula[position]
        if char in ('"', "'"):
            # Double quotes delimit string values; single quotes delimit sheet names.
            start = position
            position += 1
            while position < len(formula):
                if formula[position] == char:
                    if position + 1 < len(formula) and formula[position + 1] == char:
                        position += 2
                        continue
                    position += 1
                    break
                position += 1
            else:
                raise FormulaTranslationError("unterminated quoted token")
            result.append(formula[start:position])
            continue
        qualifier = _SHEET_QUALIFIER.match(formula, position)
        if qualifier:
            result.append(qualifier[0])
            position = qualifier.end()
            continue
        if char in "[]":
            raise FormulaTranslationError("structured table references require table-column context")
        if position and _is_name_char(formula[position - 1]):
            result.append(char)
            position += 1
            continue
        match = _COLUMN_RANGE.match(formula, position)
        if match and (match.end() == len(formula) or not _is_name_char(formula[match.end()])):
            result.append(shift_col(match[1], match[2]) + ":" + shift_col(match[3], match[4]))
            position = match.end()
            continue
        match = _ROW_RANGE.match(formula, position)
        if match and (match.end() == len(formula) or not _is_name_char(formula[match.end()])):
            result.append(shift_row(match[1], match[2]) + ":" + shift_row(match[3], match[4]))
            position = match.end()
            continue
        match = _CELL_REF.match(formula, position)
        if match:
            after = formula[match.end():match.end() + 1]
            # LOG10(...), qualified sheets, and names containing cell-like fragments
            # must not be interpreted as cell references.
            valid_boundary = not after or (not _is_name_char(after) and after not in "(!")
            if valid_boundary and _column_number(match[2]) <= MAX_COLUMN and int(match[4]) <= MAX_ROW:
                result.append(shift_col(match[1], match[2]) + shift_row(match[3], match[4]))
                position = match.end()
                continue
        result.append(char)
        position += 1
    return "".join(result)


def _xml(archive: ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise WorkbookReadError(f"Missing OOXML part: {name}") from exc
    if info.file_size > MAX_XML_BYTES:
        raise WorkbookReadError(f"OOXML part exceeds {MAX_XML_BYTES} bytes: {name}")
    data = archive.read(info)
    # XLSX XML has no legitimate need for DTDs or entity definitions.
    # Removing NUL bytes also detects declarations in UTF-16/UTF-32 XML.
    upper = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise WorkbookReadError(f"DTD/entity declarations are not supported: {name}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise WorkbookReadError(f"Invalid XML in {name}: {exc}") from exc


def _part_path(owner: str, target: str) -> str:
    target = unquote(target)
    if "\\" in target or ":" in target or "?" in target or "#" in target:
        raise WorkbookReadError(f"Invalid internal OOXML target: {target!r}")
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(owner), target))
    if path == ".." or path.startswith("../"):
        raise WorkbookReadError("OOXML relationship leaves the archive root")
    return path


def _relationships(archive: ZipFile, owner: str) -> dict[str, dict[str, str]]:
    path = posixpath.join(posixpath.dirname(owner), "_rels", posixpath.basename(owner) + ".rels") if owner else "_rels/.rels"
    if path not in archive.namelist():
        return {}
    return {element.attrib["Id"]: dict(element.attrib) for element in _xml(archive, path) if _local(element.tag) == "Relationship"}


def _text(element: ET.Element | None, namespace: str) -> str:
    if element is None:
        return ""
    # Ignore phonetic annotations (rPh); combine ordinary and rich-text runs.
    texts = []
    for child in element:
        if _local(child.tag) == "t":
            texts.append(child.text or "")
        elif _local(child.tag) == "r":
            texts.extend(node.text or "" for node in child.findall(f"{{{namespace}}}t"))
    return "".join(texts)


def _cell_value(element: ET.Element, namespace: str, strings: list[str]) -> str | float | int | bool | None:
    cell_type = element.get("t", "n")
    if cell_type == "inlineStr":
        return _text(element.find(f"{{{namespace}}}is"), namespace)
    value_element = element.find(f"{{{namespace}}}v")
    raw = value_element.text if value_element is not None else None
    if raw is None:
        return "" if cell_type == "str" and value_element is not None else None
    if cell_type == "s":
        try:
            index = int(raw)
            if index < 0:
                raise ValueError("negative shared-string index")
            return strings[index]
        except (ValueError, IndexError) as exc:
            raise WorkbookReadError(f"Invalid shared-string index in {element.get('r')}: {raw!r}") from exc
    if cell_type == "b":
        if raw not in ("0", "1"):
            raise WorkbookReadError(f"Invalid boolean in {element.get('r')}: {raw!r}")
        return raw == "1"
    if cell_type in ("str", "e", "d"):
        return raw
    if cell_type != "n":
        raise WorkbookReadError(f"Unsupported cell type {cell_type!r} in {element.get('r')}")
    try:
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError("non-finite numeric cell")
        return int(raw) if re.fullmatch(r"[+-]?[0-9]+", raw) else number
    except ValueError as exc:
        raise WorkbookReadError(f"Invalid number in {element.get('r')}: {raw!r}") from exc


def _inside_range(address: str, reference: str) -> bool:
    """Membership in the rectangular ref carried by an array/data-table anchor."""
    endpoints = reference.replace("$", "").split(":")
    if len(endpoints) not in (1, 2):
        return False
    try:
        start_col, start_row = _address_parts(endpoints[0])
        end_col, end_row = _address_parts(endpoints[-1])
        column, row = _address_parts(address)
    except WorkbookReadError:
        return False
    return start_col <= column <= end_col and start_row <= row <= end_row


def load_workbook(path: str | Path) -> Workbook:
    """Read worksheet evidence, names, external targets and calculation settings.

    No formula evaluation, external-link refresh, macro execution or disk writes
    take place. Formula errors are cached evidence only. The default limits are
    10,000 ZIP members, 512 MiB total expansion and 64 MiB per XML part.
    """
    source = Path(path).expanduser().resolve()
    try:
        archive = ZipFile(source)
    except (BadZipFile, OSError) as exc:
        raise WorkbookReadError(f"Cannot open OOXML workbook: {exc}") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_ENTRIES:
            raise WorkbookReadError(f"Workbook exceeds {MAX_ZIP_ENTRIES} ZIP entries")
        if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES:
            raise WorkbookReadError(f"Workbook exceeds {MAX_UNCOMPRESSED_BYTES} uncompressed bytes")
        if len({member.filename for member in members}) != len(members):
            raise WorkbookReadError("Duplicate ZIP member names are not supported")
        if any(member.flag_bits & 1 for member in members):
            raise WorkbookReadError("Encrypted ZIP entries are not supported")
        package_relationships = _relationships(archive, "")
        workbook_part = "xl/workbook.xml"
        for relation in package_relationships.values():
            if relation.get("Type", "").endswith("/officeDocument"):
                if relation.get("TargetMode") == "External":
                    raise WorkbookReadError("Workbook part cannot be external")
                workbook_part = _part_path("", relation["Target"])
                break
        root = _xml(archive, workbook_part)
        namespace = root.tag.partition("}")[0].lstrip("{")
        if namespace not in (MAIN_NS, STRICT_MAIN_NS):
            raise WorkbookReadError(f"Unsupported spreadsheet namespace: {namespace}")
        prefix = f"{{{namespace}}}"
        relationships = _relationships(archive, workbook_part)
        warnings: list[str] = []
        strings: list[str] = []
        for relation in relationships.values():
            if relation.get("Type", "").endswith("/sharedStrings"):
                if relation.get("TargetMode") == "External":
                    raise WorkbookReadError("Shared strings cannot be external")
                strings_root = _xml(archive, _part_path(workbook_part, relation["Target"]))
                strings = [_text(item, namespace) for item in strings_root.findall(prefix + "si")]
                break
        defined_names = [dict(element.attrib, value=element.text or "") for element in root.findall(f"{prefix}definedNames/{prefix}definedName")]
        calculation_node = root.find(prefix + "calcPr")
        calculation = dict(calculation_node.attrib) if calculation_node is not None else {}
        external_links: list[dict[str, Any]] = []
        for index, external_ref in enumerate(root.findall(f"{prefix}externalReferences/{prefix}externalReference"), 1):
            relation_id = next((value for key, value in external_ref.attrib.items() if _local(key) == "id"), "")
            relation = relationships.get(relation_id)
            if not relation:
                warnings.append(f"External reference {index}: missing relationship {relation_id!r}")
                continue
            link = {"index": index, "relationship_id": relation_id, "part": relation.get("Target", ""), "targets": []}
            if relation.get("TargetMode") == "External":
                link["targets"].append(relation["Target"])
            else:
                part = _part_path(workbook_part, relation["Target"])
                link["part"] = part
                for target in _relationships(archive, part).values():
                    if target.get("TargetMode") == "External":
                        link["targets"].append(target.get("Target", ""))
            external_links.append(link)
        sheets: dict[str, Sheet] = {}
        for sheet_element in root.findall(f"{prefix}sheets/{prefix}sheet"):
            name = sheet_element.get("name", "")
            if not name or name in sheets:
                raise WorkbookReadError(f"Missing or duplicate worksheet name: {name!r}")
            relation_id = next((value for key, value in sheet_element.attrib.items() if _local(key) == "id"), "")
            relation = relationships.get(relation_id)
            if not relation or relation.get("TargetMode") == "External":
                raise WorkbookReadError(f"Missing or external worksheet relationship: {name}")
            if not relation.get("Type", "").endswith("/worksheet"):
                warnings.append(f"Sheet {name!r}: non-worksheet part is not analysed")
                sheets[name] = Sheet(name, sheet_element.get("state", "visible"))
                continue
            sheet_root = _xml(archive, _part_path(workbook_part, relation["Target"]))
            sheet = Sheet(name, sheet_element.get("state", "visible"))
            shared_anchors: dict[str, tuple[str, str]] = {}
            shared_followers: list[Cell] = []
            empty_formulas: list[Cell] = []
            result_ranges: list[str] = []
            for element in sheet_root.findall(f"{prefix}sheetData/{prefix}row/{prefix}c"):
                address = element.get("r", "").upper()
                _address_parts(address)
                formula_element = element.find(prefix + "f")
                value = _cell_value(element, namespace, strings)
                if formula_element is None and (value is None or value == ""):
                    continue
                if address in sheet.cells:
                    raise WorkbookReadError(f"Duplicate cell address: {name}!{address}")
                style = element.get("s")
                try:
                    style_id = int(style) if style is not None else None
                except ValueError as exc:
                    raise WorkbookReadError(f"Invalid style id: {name}!{address}") from exc
                cell = Cell(address, value, data_type=element.get("t", "n"), style_id=style_id)
                if formula_element is not None:
                    cell.formula_attributes = dict(formula_element.attrib)
                    cell.formula_type = formula_element.get("t", "normal")
                    if formula_element.text:
                        cell.formula = "=" + formula_element.text.lstrip("=")
                    if cell.formula_type in ("array", "dataTable") and formula_element.get("ref"):
                        result_ranges.append(formula_element.attrib["ref"])
                    if cell.formula_type == "shared":
                        shared_id = formula_element.get("si")
                        if shared_id is None:
                            warnings.append(f"{name}!{address}: shared formula has no index")
                        elif cell.formula:
                            if shared_id in shared_anchors:
                                raise WorkbookReadError(f"Duplicate shared formula anchor: {name}, index {shared_id}")
                            shared_anchors[shared_id] = (address, cell.formula)
                        else:
                            shared_followers.append(cell)
                    elif cell.formula_type not in ("normal", "array", "dataTable"):
                        warnings.append(f"{name}!{address}: unsupported formula type {cell.formula_type!r}")
                    elif cell.formula_type == "normal" and not cell.formula:
                        empty_formulas.append(cell)
                sheet.cells[address] = cell
            # Excel/R7 may store empty <f/> markers for data-table or array results.
            # They inherit the anchor's operation rather than an ordinary formula.
            for cell in empty_formulas:
                if not any(_inside_range(cell.address, reference) for reference in result_ranges):
                    warnings.append(f"{name}!{cell.address}: formula text is missing")
            for cell in shared_followers:
                shared_id = cell.formula_attributes.get("si", "")
                anchor = shared_anchors.get(shared_id)
                if not anchor:
                    warnings.append(f"{name}!{cell.address}: shared formula anchor {shared_id!r} is missing")
                    continue
                try:
                    cell.formula = translate_formula(anchor[1], anchor[0], cell.address)
                except FormulaTranslationError as exc:
                    warnings.append(f"{name}!{cell.address}: shared formula expansion unavailable: {exc}")
            sheets[name] = sheet
        if any("vbaProject" in member.filename for member in members):
            warnings.append("VBA is present and was not executed or analysed")
        return Workbook(source, sheets, defined_names, external_links, calculation, warnings)
