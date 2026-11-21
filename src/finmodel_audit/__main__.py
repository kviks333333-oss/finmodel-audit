"""CLI for local inspection and a synthetic GigaChat tool-call probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zipfile import BadZipFile

from .audit import audit_workbook
from .report import render_html
from .workbook import load_workbook


def _atomic_write(path: Path, text: str) -> None:
    """Replace the directory entry instead of writing through file aliases."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".report-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Учебный аудит финансовой модели")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Состав Excel без пересчёта и обращения к LLM")
    inspect.add_argument("workbook", type=Path)
    audit = sub.add_parser("audit", help="Локальная структурная проверка и JSON/HTML отчёт")
    audit.add_argument("workbook", type=Path)
    audit.add_argument("--output-dir", type=Path, default=Path("outputs/local_review"))
    analyze = sub.add_parser("analyze", help="Четыре стадии анализа с инструментами и checkpoint")
    analyze.add_argument("workbook", type=Path)
    analyze.add_argument("--output-dir", type=Path, default=Path("outputs/agent_review"))
    analyze.add_argument("--mode", choices=["gigachat", "local"], default="gigachat")
    analyze.add_argument("--no-resume", action="store_true")
    presentation = sub.add_parser("export-pptx", help="PowerPoint из сохранённого JSON без обращения к LLM")
    presentation.add_argument("report", type=Path)
    presentation.add_argument("--output", type=Path, required=True)
    for name, help_text in [
        ("models", "Проверить авторизацию и получить доступные модели GigaChat"),
        ("probe-gigachat", "Проверить цикл инструментов на искусственной ячейке Demo!B2 = 42"),
    ]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        if args.command == "export-pptx":
            from .export_service import cached_pptx, ExportError
            if args.output.suffix.lower() != ".pptx" or args.output.exists() or args.output.is_symlink():
                raise ValueError("Укажите новый выходной файл с расширением .pptx; существующие файлы не заменяются.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            try:
                source = cached_pptx(args.report, args.output.parent / ".pptx-cache")
            except ExportError as exc:
                raise ValueError(str(exc)) from None
            with args.output.open("xb") as target:
                target.write(source.read_bytes())
            print(json.dumps({"pptx": str(args.output), "llm_requests": 0}, ensure_ascii=False))
            return 0
        if args.command == "analyze":
            from .pipeline import AnalysisError, run_analysis
            for destination in (args.output_dir / "report.json", args.output_dir / "report.html"):
                if destination.resolve() == args.workbook.resolve() or (
                    destination.exists() and destination.samefile(args.workbook)
                ):
                    raise ValueError("Выходной файл ссылается на исходную книгу; запись отменена.")
            def progress(event):
                print(json.dumps({key: event[key] for key in ("event", "stage", "status", "progress", "tool")
                                  if key in event}, ensure_ascii=False), file=sys.stderr, flush=True)
            try:
                report = run_analysis(args.workbook, args.output_dir, mode=args.mode,
                                      resume=not args.no_resume, on_progress=progress)
                _atomic_write(args.output_dir / "report.html", render_html(report))
                if hashlib.sha256(args.workbook.read_bytes()).hexdigest() != report["source_sha256"]:
                    raise ValueError("Исходный файл изменился во время записи отчёта.")
            except AnalysisError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps({"source_unchanged": report["source_unchanged"],
                              "stages": [{"id": s["id"], "status": s["status"]} for s in report["stages"]],
                              "usage": report["usage"], "output_dir": str(args.output_dir)}, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"models", "probe-gigachat"}:
            from .gigachat import GigaChatClient, GigaChatError, load_env_file

            try:
                load_env_file(args.env_file)
                client = GigaChatClient()
                result = client.list_models() if args.command == "models" else client.probe_tool_call()
            except GigaChatError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        before = hashlib.sha256(args.workbook.read_bytes()).hexdigest()
        workbook = load_workbook(args.workbook)
        if args.command == "inspect":
            print(json.dumps(workbook.summary(), ensure_ascii=False, indent=2))
            return 0
        report = audit_workbook(workbook)
        after = hashlib.sha256(args.workbook.read_bytes()).hexdigest()
        if before != after:
            raise ValueError("Исходный файл изменился во время проверки; отчёт не сохранён.")
        report["source_sha256"] = before
        report["source_unchanged"] = True
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = args.output_dir / "report.json"
        html_path = args.output_dir / "report.html"
        for destination in (json_path, html_path):
            if destination.resolve() == args.workbook.resolve() or (
                destination.exists() and destination.samefile(args.workbook)
            ):
                raise ValueError("Выходной файл ссылается на исходную книгу; запись отменена.")
        _atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2))
        _atomic_write(html_path, render_html(report))
        if hashlib.sha256(args.workbook.read_bytes()).hexdigest() != before:
            raise ValueError("Исходный файл изменился во время записи; отчёт требует повторной проверки.")
        print(json.dumps({"findings": len(report["findings"]), "source_unchanged": True,
                          "json": str(json_path), "html": str(html_path)}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, BadZipFile) as exc:
        print(f"Проверка не выполнена: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
