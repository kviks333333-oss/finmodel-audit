"""Optional static candidates from a pinned auditor; never a financial verdict."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time
from typing import Callable
from zipfile import BadZipFile, ZipFile

from .workbook import Workbook, _address_parts, _column_letters

VERSION = "supplemental-1"
TOOL_VERSION = "0.4.0"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_RAW_FINDINGS = 500
MAX_RETAINED = 20
MAX_TRAIL = 200
WALL_SECONDS = 55
RUNNER = Path(__file__).with_name("supplemental_child.py").resolve()
ALLOWED_RULES = {
    "FORMULA_DRIFT", "HARDCODE_IN_FORMULA_BLOCK", "LITERAL_CONSTANT", "IFERROR_MASK",
    "RANGE_EXCLUSION", "RANGE_INCLUDES_SUBTOTAL", "RANGE_LENGTH_MISMATCH", "BLANK_PRECEDENT",
}
DUPLICATE_RULES = {"LIVE_ERROR", "CIRCULAR_REFERENCE", "BROKEN_REFERENCE"}
TITLES = {
    "FORMULA_DRIFT": "Формула отличается от соседнего шаблона",
    "HARDCODE_IN_FORMULA_BLOCK": "Число внутри блока формул",
    "LITERAL_CONSTANT": "Числовая константа внутри формулы",
    "IFERROR_MASK": "Формула заменяет ошибку значением",
    "RANGE_EXCLUSION": "Возможный пропуск в диапазоне",
    "RANGE_INCLUDES_SUBTOTAL": "Возможное включение промежуточного итога",
    "RANGE_LENGTH_MISMATCH": "Диапазоны различаются по длине",
    "BLANK_PRECEDENT": "Формула ссылается на пустую ячейку",
}
BASE_LIMITATIONS = [
    "Это дополнительные статические кандидаты, а не подтверждённые финансовые ошибки; влияние не оценено.",
    "Пересчёт, обновление внешних данных и исполнение макросов отключены; используются формулы и сохранённые значения.",
    "Data Tables не вычисляются; их метаданные берутся из нашего OOXML reader.",
]


def _result(status: str, reason: str | None = None) -> dict:
    result = {"version": VERSION, "status": status, "findings": [], "summary": {
        "tool_version": TOOL_VERSION, "total": 0, "eligible": 0, "retained": 0,
        "suppressed": 0, "truncated_count": 0, "truncated": False,
        "upstream_complete": False, "suppression_reasons": {}, "audit_trail": [],
    }, "limitations": list(BASE_LIMITATIONS)}
    if reason:
        result["summary"]["reason"] = reason
    return result


def _failure(status: str, reason: str, note: str) -> dict:
    result = _result(status, reason)
    result["limitations"].append(note)
    return result


def _hash(path: Path) -> str:
    digest = sha256()
    count = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            count += len(block)
            if count > MAX_FILE_BYTES:
                raise ValueError("source_size_limit")
            digest.update(block)
    return digest.hexdigest()


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)


def _execute(source: Path, config: Path, cwd: Path, cancel_check: Callable[[], bool] | None) -> tuple[str, bytes]:
    # -I ignores Python environment/custom user site; no credentials, HOME override or PATH search.
    process = subprocess.Popen([sys.executable, "-I", str(RUNNER), str(source), str(config)],
        cwd=cwd, env={}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = bytearray()
    total = 0
    deadline = time.monotonic() + WALL_SECONDS
    try:
        with selectors.DefaultSelector() as ready:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                ready.register(pipe, selectors.EVENT_READ)
            while ready.get_map():
                if cancel_check and cancel_check():
                    return "cancelled", b""
                if time.monotonic() >= deadline:
                    return "timeout", b""
                for key, _ in ready.select(timeout=0.1):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        ready.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > MAX_OUTPUT_BYTES:
                        return "output_limit", b""
                    if key.fileobj is process.stdout:
                        output.extend(block)
            try:
                process.wait(timeout=max(0.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                return "timeout", b""
            return ("ok" if process.returncode == 0 else "child_failed"), bytes(output)
    finally:
        _kill(process)
        process.stdout.close()
        process.stderr.close()


def _preflight(path: Path) -> None:
    if not path.is_file() or path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("source_unavailable")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("source_size_limit")
    with ZipFile(path) as archive:
        parts = archive.infolist()
        if len(parts) > 10000 or sum(p.file_size for p in parts) > MAX_UNPACKED_BYTES:
            raise ValueError("source_expansion_limit")


def _cell_ref(wb: Workbook, sheet: str, text: str) -> tuple[str, str] | None:
    text = text.strip()
    if "!" in text:
        sheet, text = text.rsplit("!", 1)
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    text = text.replace("$", "").upper()
    if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", text):
        return None
    if sheet not in wb.sheets or text not in wb.sheets[sheet].cells:
        return None
    return sheet, text


def _locations(wb: Workbook, raw: dict) -> list[tuple[str, str]] | None:
    primary = _cell_ref(wb, raw.get("sheet", ""), str(raw.get("cell") or ""))
    if primary is None:
        return None
    members = raw.get("members") or [raw.get("location")]
    if not isinstance(members, list) or len(members) > 128:
        return None
    found = [primary]
    for member in members:
        if not isinstance(member, str) or "!" not in member or len(member) > 512:
            return None
        if ":" in member.rsplit("!", 1)[1]:
            return None  # A grouped range is not evidence for every populated cell in it.
        parsed = _cell_ref(wb, primary[0], member)
        if parsed is None:
            return None
        if parsed not in found:
            found.append(parsed)
    reported_formula = raw.get("formula")
    primary_cell = wb.sheets[primary[0]].cells[primary[1]]
    empty_xml_formula = reported_formula == "=" and primary_cell.formula_type == "normal" and primary_cell.formula is None
    if reported_formula is not None and reported_formula != primary_cell.formula and not empty_xml_formula:
        return None
    return found


def _row_text(wb: Workbook, sheet: str, address: str) -> list[tuple[str, str]]:
    column, row = _address_parts(address)
    found = []
    for col in range(max(1, column-16), column):
        cell = wb.sheets[sheet].cells.get(f"{_column_letters(col)}{row}")
        if cell and isinstance(cell.value, str) and not cell.formula_type:
            found.append((cell.address, cell.value.strip()))
    return found


def _unit(wb: Workbook, ref: tuple[str, str]) -> tuple[str, int, str] | None:
    for address, text in reversed(_row_text(wb, *ref)):
        if text.casefold() in {"#", "x", "shares", "шт", "шт."}:
            return "count", 0, address
        match = re.fullmatch(r"(RUB|RUR|USD|EUR|руб\.?)(?:\s+(M|B|K|млн|тыс|млрд))?", text, re.I)
        if match:
            dimension = match[1].upper().replace("RUR", "RUB")
            if dimension.startswith("РУБ"):
                dimension = "RUB"
            power = {None: 0, "m": 6, "b": 9, "k": 3, "млн": 6, "тыс": 3, "млрд": 9}[match[2].casefold() if match[2] else None]
            return dimension, power, address
        # A later label may start a different row block. Never jump over it to an earlier unit.
        return None
    return None


def _unit_conversion(wb: Workbook, ref: tuple[str, str]) -> list[tuple[str, str]] | None:
    cell = wb.sheets[ref[0]].cells[ref[1]]
    match = re.fullmatch(r"=(.+)([*/])10\^([36])", re.sub(r"\s+(?=[*/^])|(?<=[*/^])\s+", "", cell.formula or ""))
    target = _unit(wb, ref)
    if not match or not target or target[0] == "count":
        return None
    pieces = re.split(r"([*/])", match[1])
    if len(pieces) not in {1, 3}:
        return None
    evidence = [ref, (ref[0], target[2])]
    dimension, power = Counter(), 0
    for index in range(0, len(pieces), 2):
        source = _cell_ref(wb, ref[0], pieces[index])
        unit = _unit(wb, source) if source else None
        if not unit:
            return None
        sign = -1 if index and pieces[index-1] == "/" else 1
        if unit[0] != "count":
            dimension[unit[0]] += sign
        power += sign * unit[1]
        evidence.extend([source, (source[0], unit[2])])
    factor = int(match[3]) * (1 if match[2] == "*" else -1)
    if {k: v for k, v in dimension.items() if v} == {target[0]: 1} and power-target[1] == factor:
        return list(dict.fromkeys(evidence))
    return None


def _datatable_corner(wb: Workbook, ref: tuple[str, str]) -> list[tuple[str, str]] | None:
    cell = wb.sheets[ref[0]].cells[ref[1]]
    target = _cell_ref(wb, ref[0], (cell.formula or "")[1:])
    if not target:
        return None
    for anchor in wb.sheets[ref[0]].cells.values():
        attrs = anchor.formula_attributes
        if anchor.formula_type != "dataTable" or attrs.get("dt2D") != "1":
            continue
        start = attrs.get("ref", "").split(":")[0]
        try:
            col, row = _address_parts(start)
        except ValueError:
            continue
        if col > 1 and row > 1 and ref[1] == f"{_column_letters(col-1)}{row-1}":
            return [ref, (ref[0], anchor.address), target]
    return None


def _midyear_start(wb: Workbook, ref: tuple[str, str]) -> list[tuple[str, str]] | None:
    cell = wb.sheets[ref[0]].cells[ref[1]]
    formula = re.sub(r"\s+", "", cell.formula or "")
    match = re.fullmatch(r'=IF\((\$?[A-Z]{1,3}\$?\d+)="yes",(\$?[A-Z]{1,3}\$?\d+)/2,\2\)', formula, re.I)
    if not match or cell.value != 0.5 or isinstance(cell.value, bool):
        return None
    control, period = (_cell_ref(wb, ref[0], match[n]) for n in (1, 2))
    if not control or not period:
        return None
    if str(wb.sheets[control[0]].cells[control[1]].value).casefold() != "yes" or wb.sheets[period[0]].cells[period[1]].value != 1:
        return None
    labels = [(ref[0], addr) for addr, text in _row_text(wb, *ref) if "discount" in text.casefold() or "дисконт" in text.casefold()]
    conventions = [(ref[0], addr) for addr, text in _row_text(wb, *control) if re.search(r"mid[ -]?year|середин", text, re.I)]
    if not labels or not conventions:
        return None
    col, row = _address_parts(ref[1])
    period_col, period_row = _address_parts(period[1])
    if period_col != col or col < 2:
        return None
    previous = (ref[0], f"{_column_letters(col-1)}{period_row}")
    previous_cell = wb.sheets[ref[0]].cells.get(previous[1])
    if not previous_cell or previous_cell.value != 0:
        return None
    evidence = [ref, control, period, previous, *labels, *conventions]
    for offset in (1, 2):
        address = f"{_column_letters(col+offset)}{row}"
        neighbor = wb.sheets[ref[0]].cells.get(address)
        prior = f"{_column_letters(col+offset-1)}{row}"
        if not neighbor or re.sub(r"[\s$]", "", neighbor.formula or "").upper() != f"={prior}+1" or neighbor.value != offset+0.5:
            return None
        evidence.append((ref[0], address))
    return evidence


def _suppress(wb: Workbook, rule: str, refs: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]] | None:
    # Suppress the whole grouped candidate only if every member has the same explanation.
    checks = {"FORMULA_DRIFT": [("data_table_output_corner", _datatable_corner), ("documented_midyear_first_period", _midyear_start)],
              "LITERAL_CONSTANT": [("documented_unit_conversion", _unit_conversion)]}
    for reason, check in checks.get(rule, []):
        evidence = [check(wb, ref) for ref in refs]
        if all(evidence):
            return reason, list(dict.fromkeys(item for group in evidence for item in group))
    return None


def _normalize(wb: Workbook, payload: dict, source_hash: str) -> dict:
    if not isinstance(payload, dict) or payload.get("tool_version") != TOOL_VERSION:
        raise ValueError("invalid_tool_response")
    source = payload.get("workbook")
    coverage = payload.get("coverage")
    raw_findings = payload.get("findings")
    if not isinstance(source, dict) or source.get("sha256") != source_hash or source.get("recalc_status") != "disabled":
        raise ValueError("source_or_recalc_mismatch")
    if not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool or not isinstance(raw_findings, list) or len(raw_findings) > MAX_RAW_FINDINGS:
        raise ValueError("invalid_coverage_or_findings")
    if coverage.get("macros_executed") is not False:
        raise ValueError("invalid_macro_status")
    report = _result("complete")
    summary = report["summary"]
    counts = coverage.get("finding_counts", {})
    upstream_total = counts.get("total", len(raw_findings)) if isinstance(counts, dict) else len(raw_findings)
    if type(upstream_total) is not int or not len(raw_findings) <= upstream_total <= 1000000:
        raise ValueError("invalid_finding_count")
    upstream_omitted = upstream_total-len(raw_findings)
    summary.update(source_sha256=source_hash, total=upstream_total, received=len(raw_findings), upstream_complete=coverage["complete"])
    candidates = []
    reasons = Counter()
    invalid = 0
    for index, raw in enumerate(raw_findings):
        reason, supporting = None, []
        refs = _locations(wb, raw) if isinstance(raw, dict) else None
        rule = raw.get("rule_id", "") if isinstance(raw, dict) else ""
        if not refs:
            reason = "invalid_source_or_formula"
            invalid += 1
        elif rule in DUPLICATE_RULES:
            reason = "already_covered_by_primary_checks"
        elif rule not in ALLOWED_RULES:
            reason = "outside_formula_candidate_scope"
        elif any(wb.sheets[s].cells[c].formula_type and wb.sheets[s].cells[c].formula is None for s, c in refs):
            reason = "unsupported_formula_metadata"
        elif raw.get("suppressed"):
            reason = "upstream_suppressed"
        else:
            suppressed = _suppress(wb, rule, refs)
            if suppressed:
                reason, supporting = suppressed
        trail = {"index": index, "rule": str(rule)[:60], "disposition": "suppressed" if reason else "eligible", "reason": reason}
        if refs:
            trail["sources"] = [{"sheet": s, "cell": c} for s, c in refs[:8]]
        if supporting:
            trail["filter_evidence"] = [{"sheet": s, "cell": c} for s, c in supporting[:12]]
        if len(summary["audit_trail"]) < MAX_TRAIL:
            summary["audit_trail"].append(trail)
        if reason:
            reasons[reason] += 1
            continue
        key = sha256(json.dumps([source_hash, rule, refs], ensure_ascii=False).encode()).hexdigest()[:16]
        trail["candidate_id"] = "supplemental_"+key
        origin = {"severity": str(raw.get("severity", ""))[:30],
                  "confidence": str(raw.get("confidence", ""))[:50],
                  "messages": [str(x)[:240] for x in raw.get("evidence", [])[:3]] if isinstance(raw.get("evidence"), list) else []}
        candidates.append({"id": "supplemental_"+key, "code": "supplemental_"+rule.lower(),
            "status": "candidate", "title": TITLES[rule],
            "detail": "Дополнительная эвристика обнаружила отличие структуры. Намеренность, причина и финансовое влияние требуют отдельной проверки.",
            "evidence": [{"sheet": s, "cell": c} for s, c in refs[:32]],
            "evidence_truncated_count": max(0, len(refs)-32),
            "financial_materiality": "not_assessed", "origin_rule": rule,
            "detection_mode": str(raw.get("detection_mode", ""))[:20], "confidence": "review_required",
            "origin_metadata": origin})
    # Upstream severity ordering must not monopolize the display with one rule family.
    groups: dict[str, list[dict]] = {}
    for candidate in candidates:
        groups.setdefault(candidate["origin_rule"], []).append(candidate)
    chosen = []
    for index in range(MAX_RETAINED):
        for group in groups.values():
            if index < len(group) and len(chosen) < MAX_RETAINED:
                chosen.append(group[index])
    report["findings"] = chosen
    summary["selection"] = "round_robin_by_rule"
    returned_ids = {candidate["id"] for candidate in report["findings"]}
    for trail in summary["audit_trail"]:
        if trail["disposition"] == "eligible":
            trail["disposition"] = "retained" if trail.get("candidate_id") in returned_ids else "omitted"
            if trail["disposition"] == "omitted":
                trail["reason"] = "presentation_limit"
    summary.update(eligible=len(candidates), retained=len(report["findings"]), suppressed=sum(reasons.values()),
                   truncated_count=max(0, len(candidates)-MAX_RETAINED)+upstream_omitted, upstream_omitted=upstream_omitted, suppression_reasons=dict(reasons),
                   audit_trail_truncated_count=max(0, len(raw_findings)-MAX_TRAIL))
    upstream_truncated = coverage.get("truncated", {})
    summary["truncated"] = bool(summary["truncated_count"] or (isinstance(upstream_truncated, dict) and any(upstream_truncated.values())))
    # Source-validated notes only; never propagate arbitrary upstream instructions or paths.
    if not coverage["complete"] or summary["truncated"] or invalid or any(x["evidence_truncated_count"] for x in report["findings"]):
        report["status"] = "partial"
        report["limitations"].append("Проверка или представление ограничены: часть кандидатов/источников не показана либо не прошла привязку к книге.")
    if len(candidates) > MAX_RETAINED:
        report["limitations"].append(f"Показаны {MAX_RETAINED} из {len(candidates)} допустимых кандидатов с чередованием видов проверок; это не ранжирование финансового риска.")
    if upstream_omitted:
        report["limitations"].append("Сам дополнительный auditor ограничил число возвращённых кандидатов; непоказанные результаты не оценены.")
    summary["upstream_incomplete_reasons"] = [str(x.get("reason", "unknown"))[:50] for x in coverage.get("incomplete", [])[:10] if isinstance(x, dict)] if isinstance(coverage.get("incomplete", []), list) else ["invalid_metadata"]
    summary["upstream_truncated"] = {str(k)[:40]: bool(v) for k, v in upstream_truncated.items()} if isinstance(upstream_truncated, dict) else {}
    return report


def run_supplemental_audit(workbook: Workbook, *, cancel_check: Callable[[], bool] | None = None) -> dict:
    """Run installed 0.4.0 against a read-only copy, fail visibly without blocking the app."""
    if cancel_check and cancel_check():
        return _failure("skipped", "cancelled", "Дополнительная проверка отменена.")
    try:
        installed = importlib.metadata.version("spreadsheet-auditor")
    except importlib.metadata.PackageNotFoundError:
        return _failure("unavailable", "dependency_missing", "Не установлен необязательный spreadsheet-auditor==0.4.0; дополнительная проверка не выполнена.")
    if installed != TOOL_VERSION:
        return _failure("unavailable", "dependency_version_mismatch", "Версия дополнительного auditor отличается от проверенной 0.4.0; запуск пропущен.")
    source = Path(workbook.path)
    before = None
    try:
        _preflight(source)
        before = _hash(source)
        with tempfile.TemporaryDirectory(prefix="finmodel-supplemental-") as directory:
            temporary = Path(directory)
            copied = temporary / ("source"+source.suffix.lower())
            # Bounded chunked copy; source metadata and permissions are untouched.
            with source.open("rb") as input_file, copied.open("wb") as target:
                count = 0
                for block in iter(lambda: input_file.read(1024 * 1024), b""):
                    count += len(block)
                    if count > MAX_FILE_BYTES:
                        raise ValueError("source_size_limit")
                    target.write(block)
            copied.chmod(0o400)
            if _hash(copied) != before:
                raise ValueError("source_changed_during_copy")
            config = temporary / "config.json"
            config.write_text(json.dumps({"recalc": {"enabled": False}, "finance": {"enabled": False},
                "limits": {"max_cells": 1000000, "max_formulas": 30000, "max_reported_findings": MAX_RAW_FINDINGS, "timeout_seconds": 45}}))
            outcome, data = _execute(copied, config, temporary, cancel_check)
            if _hash(copied) != before or _hash(source) != before:
                raise ValueError("source_integrity_mismatch")
            if outcome != "ok":
                status = {"timeout": "timeout", "cancelled": "skipped"}.get(outcome, "failed")
                return _failure(status, outcome, "Дополнительная проверка прервана или превысила ограничение ресурсов; результатов нет.")
            payload = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
            return _normalize(workbook, payload, before)
    except (OSError, ValueError, TypeError, KeyError, BadZipFile, subprocess.SubprocessError):
        return _failure("failed", "invalid_source_or_result", "Дополнительная проверка не выполнена: источник, целостность или ответ процесса не прошли проверку.")
