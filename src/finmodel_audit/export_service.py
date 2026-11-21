"""Versioned derived PPTX cache; the saved analysis and usage stay immutable."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zipfile import BadZipFile, ZipFile

from .storage import atomic_write

MAX_REPORT_BYTES = 32 * 1024 * 1024
EXPORT_TIMEOUT_SECONDS = 25


class ExportError(RuntimeError):
    pass


@contextmanager
def _lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS + 2
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ExportError("Презентация ещё формируется. Повторите скачивание немного позже.") from None
                time.sleep(.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _valid_package(path):
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_REPORT_BYTES:
        return False
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            return (len(entries) <= 5000 and sum(item.file_size for item in entries) <= 64 * 1024 * 1024
                    and {"[Content_Types].xml", "ppt/presentation.xml", "ppt/slides/slide1.xml"} <= set(archive.namelist())
                    and archive.testzip() is None)
    except (BadZipFile, OSError, ValueError):
        return False


def cached_pptx(report_path: Path, output_dir: Path) -> Path:
    """A sync API route runs this outside the event loop; rendering is isolated."""
    from .decision import VERSION as decision_version
    from .pptx_export import PPTX_VERSION as pptx_version, STYLE
    from .presentation import VERSION as presentation_version
    from .pptx_backend import runtime_identity, isolated_environment
    report_path, output_dir = Path(report_path), Path(output_dir)
    if report_path.is_symlink() or not report_path.is_file() or report_path.stat().st_size > MAX_REPORT_BYTES:
        raise ExportError("Исходный отчёт недоступен или превышает предел экспорта.")
    source = report_path.read_bytes()
    if len(source) > MAX_REPORT_BYTES:
        raise ExportError("Исходный отчёт превышает предел экспорта.")
    source_hash = hashlib.sha256(source).hexdigest()
    try:
        renderer_identity = runtime_identity()
        environment = isolated_environment()
    except (ValueError, OSError):
        raise ExportError("Модуль PowerPoint не установлен. Требуется Node.js 24 и зависимости pptx_runtime.") from None
    identity = {"source_sha256": source_hash, "decision_version": decision_version, "pptx_version": pptx_version,
                "presentation_version": presentation_version,
                "renderer": renderer_identity,
                "style_sha256": hashlib.sha256(json.dumps(STYLE, sort_keys=True).encode()).hexdigest()}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = output_dir / "presentation_exports"
    if output_dir.is_symlink() or cache.is_symlink():
        raise ExportError("Каталог презентаций недоступен.")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination, manifest = cache / f"{key}.pptx", cache / f"{key}.json"
    try:
        with _lock(cache / ".export.lock"):
            if destination.is_symlink() or manifest.is_symlink():
                raise ExportError("Файл презентации не прошёл проверку целостности.")
            if _valid_package(destination) and manifest.is_file():
                try:
                    metadata = json.loads(manifest.read_text())
                    if metadata.get("identity") == identity and metadata.get("sha256") == hashlib.sha256(destination.read_bytes()).hexdigest():
                        return destination
                except (OSError, ValueError, AttributeError):
                    pass
            with tempfile.TemporaryDirectory(prefix=".build-", dir=cache) as temporary:
                build = Path(temporary)
                snapshot, candidate = build / "report.json", build / "presentation.pptx"
                snapshot.write_bytes(source)
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
                try:
                    result = subprocess.run([sys.executable, "-m", "finmodel_audit.pptx_runner", str(snapshot), str(candidate)],
                        cwd=build, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=EXPORT_TIMEOUT_SECONDS, check=False)
                except subprocess.TimeoutExpired:
                    raise ExportError("Презентация не сформирована за отведённое время. Сам анализ сохранён.") from None
                if result.returncode != 0 or not _valid_package(candidate):
                    raise ExportError("Не удалось сформировать презентацию. Сам анализ и HTML-отчёт сохранены.")
                data = candidate.read_bytes()
                atomic_write(destination, data)
                atomic_write(manifest, json.dumps({"identity": identity, "sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True).encode())
                return destination
    except ExportError:
        raise
    except OSError:
        raise ExportError("Не удалось сохранить презентацию. Сам анализ сохранён.") from None
