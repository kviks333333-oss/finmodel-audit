"""Separate sequential worker. Run with ``python -m finmodel_audit.worker``."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
from typing import Any, Callable

from sqlalchemy import text

from .jobs import JobStore
from .storage import LocalStorage, StorageError, atomic_write, make_storage


logger = logging.getLogger(__name__)


class WorkerAlreadyRunning(RuntimeError):
    pass


@contextmanager
def singleton_worker(data_dir: Path, store: JobStore):
    """Shared-volume flock plus a database advisory lock for PostgreSQL."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (data_dir / ".worker.lock").open("a+")
    connection = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerAlreadyRunning("Другой worker уже работает с этим хранилищем.") from None
        if store.engine.dialect.name == "postgresql":
            connection = store.engine.connect()
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(642791387241)"))
            if not acquired:
                raise WorkerAlreadyRunning("Другой worker уже работает с этой базой.")
        yield
    finally:
        if connection is not None:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(642791387241)"))
            finally:
                connection.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def safe_error(exc: Exception) -> str:
    """Never persist an exception's raw text, response body, DSN or credentials."""
    if isinstance(exc, StorageError):
        return "Ошибка доступа к файлам или проверке их целостности. Исходник не прошёл проверку либо хранилище недоступно."
    if type(exc).__name__ in {"GigaChatError", "AnalysisError"}:
        return "Анализ не завершён. Проверьте настройки провайдера, сеть и доступность модели, затем повторите задачу."
    return "Анализ не завершён из-за внутренней ошибки. Задачу можно повторить с сохранённых этапов."


class Worker:
    def __init__(self, store: JobStore, storage: LocalStorage, *,
                 pipeline: Callable[..., dict[str, Any]] | None = None,
                 poll_interval: float = 1.0) -> None:
        self.store = store
        self.storage = storage
        self.pipeline = pipeline
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.active_job_id: str | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def _heartbeat_loop(self, finished: threading.Event) -> None:
        while not finished.is_set():
            try:
                self.store.heartbeat(active_job_id=self.active_job_id,
                                     state="busy" if self.active_job_id else "idle")
            except Exception:
                # Connectivity diagnostics must not print DSNs or DB exceptions.
                logger.warning("Worker heartbeat could not be saved")
            finished.wait(5)

    def _process_next(self) -> bool:
        if self.stop_event.is_set():
            return False
        job = self.store.claim()
        if job is None:
            return False
        job_id = job["id"]
        self.active_job_id = job_id
        self.store.heartbeat(active_job_id=job_id, state="busy")

        def cancelled() -> bool:
            return self.stop_event.is_set() or self.store.cancel_requested(job_id)

        last_progress = 0
        stage_requests: dict[str, int] = {}

        def progress(event: dict[str, Any]) -> None:
            nonlocal last_progress
            stage = str(event.get("stage_id") or event.get("stage") or event.get("event") or "analysis")
            stages = ("audit", "assumptions", "quality", "business")
            event_name = event.get("event", "")
            value = event.get("progress")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                value = stages.index(stage) * 25 if stage in stages else last_progress
                if event_name in {"stage_complete", "stage_resumed"} and stage in stages:
                    value += 25
            requests = event.get("requests", 0)
            if not isinstance(requests, int) or isinstance(requests, bool):
                requests = 0
            if event_name == "stage_start":
                stage_requests[stage] = requests
            elif event_name == "tool_result" and stage in stages:
                start = stage_requests.setdefault(stage, max(0, requests - 1))
                value = max(value, stages.index(stage) * 25 + min(20, max(0, requests - start) * 2))
            last_progress = max(last_progress, min(99, int(value)))
            self.store.progress(job_id, stage=stage, progress=last_progress)

        try:
            path = self.storage.source_path(job_id, job["sha256"])
            output_dir = self.storage.output_dir(job_id)
            pipeline = self.pipeline
            if pipeline is None:
                from .pipeline import run_analysis
                pipeline = run_analysis
            report = pipeline(path, output_dir, client=None, on_progress=progress,
                              resume=True, cancel_check=cancelled, mode=job["mode"])
            # The worker independently enforces the source-preservation contract.
            self.storage.source_path(job_id, job["sha256"])
            if cancelled():
                status = "cancelled" if self.store.cancel_requested(job_id) else "queued"
                self.store.finish(job_id, status)
                return True
            from .report import render_html
            report["source"] = job["filename"]
            report["job_id"] = job_id
            report["source_sha256"] = job["sha256"]
            report["source_unchanged"] = True
            atomic_write(output_dir / "report.json", json.dumps(
                report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
            atomic_write(output_dir / "report.html", render_html(report).encode("utf-8"))
            self.storage.publish_reports(job_id)
            self.storage.source_path(job_id, job["sha256"])
            self.store.finish(job_id, "completed")
            logger.info("Job %s completed", job_id)
        except Exception as exc:
            if self.store.cancel_requested(job_id):
                self.store.finish(job_id, "cancelled")
            elif self.stop_event.is_set():
                # A graceful stop checkpoints and leaves work for the next process.
                self.store.finish(job_id, "queued")
            elif type(exc).__name__ == "AnalysisCancelled":
                self.store.finish(job_id, "cancelled")
            else:
                self.store.finish(job_id, "failed", error=safe_error(exc))
                logger.warning("Job %s failed; diagnostic stored without raw exception", job_id)
        finally:
            self.active_job_id = None
            self.store.heartbeat(state="idle")
        return True

    def run_once(self) -> bool:
        """Run one queued task, useful for a batch invocation and integration tests."""
        with singleton_worker(self.storage.root, self.store):
            self.store.recover_interrupted()
            try:
                return self._process_next()
            finally:
                self.store.heartbeat(state="stopped")

    def run_forever(self) -> None:
        with singleton_worker(self.storage.root, self.store):
            self.store.recover_interrupted()
            finished = threading.Event()
            heartbeat = threading.Thread(target=self._heartbeat_loop, args=(finished,), daemon=True)
            heartbeat.start()
            try:
                while not self.stop_event.is_set():
                    if not self._process_next():
                        self.stop_event.wait(self.poll_interval)
            finally:
                finished.set()
                heartbeat.join(timeout=6)
                self.store.heartbeat(state="stopped")


def main() -> int:
    from .gigachat import load_env_file

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        load_env_file(os.environ.get("FINMODEL_ENV_FILE", ".env"))
        data_dir = Path(os.environ.get("DATA_DIR", ".data"))
        store = JobStore(data_dir=data_dir)
        storage = make_storage(data_dir)
        worker = Worker(store, storage)
        signal.signal(signal.SIGTERM, lambda *_: worker.stop())
        signal.signal(signal.SIGINT, lambda *_: worker.stop())
        try:
            worker.run_forever()
        finally:
            store.close()
        return 0
    except WorkerAlreadyRunning as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.error("Worker could not start or continue; check configuration and storage")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
