"""Narrow optional-auditor process. Never imports providers or reads their environment."""
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def main() -> None:
    # Limits supplement the parent's wall-time and pipe bounds. No child process is needed.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
    except (ImportError, OSError, ValueError):
        pass
    try:
        if importlib.metadata.version("spreadsheet-auditor") != "0.4.0":
            raise ValueError("version")
        from spreadsheet_auditor.audit import audit_workbook
        source, config = map(Path, sys.argv[1:3])
        args = Namespace(workbook=str(source), config=str(config), ignore=None,
                         recalc_timeout=None, annotated=None, fail_on="None", strict=False,
                         pin_suppressions=False)
        # Third-party diagnostics can contain workbook text. Do not forward them to logs.
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result, _ = audit_workbook(args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    except Exception as exc:
        print(json.dumps({"adapter_error": type(exc).__name__}))


if __name__ == "__main__":
    main()
