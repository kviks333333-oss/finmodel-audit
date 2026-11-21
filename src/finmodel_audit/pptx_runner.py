"""Bounded process for an on-demand presentation, with no provider credentials."""
from pathlib import Path
import json
import resource
import sys


def main():
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        # V8 reserves a large virtual address range. Limit its heap in the
        # backend; the Docker service also caps actual memory at 512 MiB.
        # RLIMIT_AS would prevent Node from starting even for a tiny deck.
        source, destination = map(Path, sys.argv[1:])
        if source.stat().st_size > 32 * 1024 * 1024:
            return 2
        report = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            return 2
        from .pptx_export import render_pptx
        content = render_pptx(report)
        if not isinstance(content, bytes) or len(content) > 32 * 1024 * 1024:
            return 2
        with destination.open("xb") as handle:
            handle.write(content)
        return 0
    except Exception:
        # No raw workbook text, environment or traceback in public diagnostics.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
