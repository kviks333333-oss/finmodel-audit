"""Bounded JSON-to-PPTX adapter for the pinned public PptxGenJS runtime."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

BACKEND = "pptxgenjs-4.0.1"
MAX_BYTES = 32 * 1024 * 1024


def runtime_directory():
    configured = os.environ.get("FINMODEL_PPTX_RUNTIME")
    directory = Path(configured) if configured else Path(__file__).resolve().parents[2] / "pptx_runtime"
    if not directory.is_dir() or not all((directory / n).is_file() for n in ("render.cjs", "package-lock.json")):
        raise ValueError("PowerPoint runtime is not installed")
    return directory.resolve()


def runtime_identity():
    directory = runtime_directory()
    digest = hashlib.sha256()
    for name in ("render.cjs", "package.json", "package-lock.json"):
        digest.update(name.encode() + b"\0" + (directory / name).read_bytes() + b"\0")
    return {"backend": BACKEND, "assets_sha256": digest.hexdigest()}


def isolated_environment():
    node = os.environ.get("FINMODEL_NODE_PATH") or shutil.which("node")
    if not node or not Path(node).is_file():
        raise ValueError("Node.js 24 is required for PowerPoint export")
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "FINMODEL_NODE_PATH": str(Path(node).resolve()),
            "FINMODEL_PPTX_RUNTIME": str(runtime_directory())}


def render_plan(plan):
    payload = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ValueError("PowerPoint plan is too large")
    environment = isolated_environment()
    directory = Path(environment["FINMODEL_PPTX_RUNTIME"])
    try:
        result = subprocess.run([
            environment["FINMODEL_NODE_PATH"], "--max-old-space-size=192", "--max-semi-space-size=8",
            str(directory / "render.cjs")], input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=directory, env=environment, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("PowerPoint renderer did not complete") from None
    if result.returncode or not 0 < len(result.stdout) <= MAX_BYTES or not result.stdout.startswith(b"PK\x03\x04"):
        # Library errors can contain report text: never surface stderr to the API.
        raise ValueError("PowerPoint renderer rejected the slide plan")
    return result.stdout
