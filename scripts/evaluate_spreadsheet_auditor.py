#!/usr/bin/env python3
"""Offline evaluation of an independently installed Spreadsheet Auditor CLI.

Never installs dependencies, reads credentials, recalculates, or edits the source.
Run with a dedicated --auditor-python; results are separate from app reports.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--auditor-python", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    python = args.auditor_python.absolute()  # Do not resolve a venv's interpreter symlink.
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    copy = out / "input-copy.xlsx"
    if source == copy:
        parser.error("source must not be the evaluation copy")
    before = digest(source)
    if copy.exists() and digest(copy) != before:
        parser.error("existing evaluation copy differs; choose a new output directory")
    if not copy.exists():
        shutil.copyfile(source, copy)
        copy.chmod(0o400)
    env = {"PATH": f"{python.parent}:/usr/bin:/bin", "LANG": "en_US.UTF-8",
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    version = subprocess.run([str(python), "-m", "spreadsheet_auditor", "--version"],
                             env=env, cwd=out, capture_output=True, text=True, check=True).stdout.strip()
    summary = {"source_sha256": before, "auditor_version": version, "runs": []}
    for name, finance in [("baseline", False), ("finance", True)]:
        config = {"recalc": {"enabled": False}, "finance": {"enabled": finance},
                  "limits": {"max_cells": 1000000, "max_formulas": 50000,
                             "max_reported_findings": 10000, "timeout_seconds": 180}}
        config_path = out / f"{name}-config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        cmd = [str(python), "-m", "spreadsheet_auditor", str(copy),
               "--config", str(config_path), "--json", str(out / f"{name}.json"),
               "--out", str(out / f"{name}.md"), "--summary"]
        started = time.monotonic()
        run = subprocess.run(cmd, cwd=out, env=env, capture_output=True, text=True, timeout=210)
        (out / f"{name}.stdout.txt").write_text(run.stdout)
        (out / f"{name}.stderr.txt").write_text(run.stderr)
        result = {"name": name, "command": cmd, "elapsed_seconds": round(time.monotonic()-started, 3),
                  "exit_code": run.returncode}
        report_path = out / f"{name}.json"
        if report_path.exists():
            report = json.loads(report_path.read_text())
            result.update({"findings": len(report["findings"]), "coverage": report["coverage"],
                           "by_rule": dict(Counter(f["rule_id"] for f in report["findings"])),
                           "by_severity": dict(Counter(f["severity"] for f in report["findings"]))})
        summary["runs"].append(result)
    summary.update({"source_unchanged": digest(source) == before,
                    "copy_unchanged": digest(copy) == before})
    (out / "run-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    assert summary["source_unchanged"] and summary["copy_unchanged"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
