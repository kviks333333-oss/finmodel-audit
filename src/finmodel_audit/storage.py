"""Private original files and report artifacts, locally or in S3-compatible storage."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid


class StorageError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class LocalStorage:
    backend = "local"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "uploads").mkdir(exist_ok=True, mode=0o700)

    def job_dir(self, job_id: str) -> Path:
        identifier = str(uuid.UUID(job_id))
        return self.root / "jobs" / identifier

    def output_dir(self, job_id: str) -> Path:
        path = self.job_dir(job_id) / "output"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def save_original(self, job_id: str, staged: Path) -> Path:
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        destination = directory / "original.xlsx"
        # Use an exclusive destination and never use an untrusted original name.
        with staged.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        destination.chmod(0o400)
        return destination

    def source_path(self, job_id: str, expected_sha256: str) -> Path:
        path = self.job_dir(job_id) / "original.xlsx"
        if not path.is_file() or path.is_symlink() or file_sha256(path) != expected_sha256:
            raise StorageError("Исходный файл отсутствует или не прошёл проверку целостности.")
        return path

    def publish_reports(self, job_id: str) -> None:
        pass

    def report_path(self, job_id: str, format: str) -> Path:
        if format not in {"json", "html"}:
            raise ValueError("Unsupported report format")
        path = self.output_dir(job_id) / f"report.{format}"
        if not path.is_file() or path.is_symlink():
            raise StorageError("Отчёт пока недоступен.")
        return path


class S3Storage(LocalStorage):
    backend = "s3"

    def __init__(self, data_dir: Path | str) -> None:
        super().__init__(data_dir)
        import boto3

        self.bucket = os.environ.get("S3_BUCKET", "finmodel")
        self.client = boto3.client(
            "s3", endpoint_url=os.environ.get("S3_ENDPOINT") or os.environ.get("S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY") or None,
            region_name=os.environ.get("S3_REGION", "us-east-1"),
        )

    def _key(self, job_id: str, filename: str) -> str:
        return f"jobs/{uuid.UUID(job_id)}/{filename}"

    def save_original(self, job_id: str, staged: Path) -> Path:
        path = super().save_original(job_id, staged)
        try:
            # Bucket creation belongs to deployment initialization, not user uploads.
            self.client.upload_file(str(path), self.bucket, self._key(job_id, "original.xlsx"))
        except Exception:
            raise StorageError("Не удалось сохранить исходный файл в объектном хранилище.") from None
        return path

    def _download(self, job_id: str, filename: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".download-", dir=path.parent)
        os.close(fd)
        staged = Path(name)
        try:
            self.client.download_file(self.bucket, self._key(job_id, filename), str(staged))
            os.replace(staged, path)
        except Exception:
            raise StorageError("Файл недоступен в объектном хранилище.") from None
        finally:
            staged.unlink(missing_ok=True)

    def source_path(self, job_id: str, expected_sha256: str) -> Path:
        path = self.job_dir(job_id) / "original.xlsx"
        if not path.exists():
            self._download(job_id, "original.xlsx", path)
            path.chmod(0o400)
        return super().source_path(job_id, expected_sha256)

    def publish_reports(self, job_id: str) -> None:
        try:
            for format in ("json", "html"):
                path = super().report_path(job_id, format)
                self.client.upload_file(str(path), self.bucket, self._key(job_id, f"report.{format}"))
        except Exception:
            raise StorageError("Не удалось сохранить отчёты в объектном хранилище.") from None

    def report_path(self, job_id: str, format: str) -> Path:
        if format not in {"json", "html"}:
            raise ValueError("Unsupported report format")
        path = self.output_dir(job_id) / f"report.{format}"
        if not path.exists():
            self._download(job_id, f"report.{format}", path)
        return super().report_path(job_id, format)


def make_storage(data_dir: Path | str, backend: str | None = None) -> LocalStorage:
    backend = backend or os.environ.get("STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage(data_dir)
    if backend == "s3":
        return S3Storage(data_dir)
    raise StorageError("STORAGE_BACKEND должен быть local или s3.")
