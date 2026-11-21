"""Durable job state shared by the API and the single background worker."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
from pathlib import Path
import os
from typing import Any
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, event, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    mode: Mapped[str] = mapped_column(String(20))
    stage: Mapped[str] = mapped_column(String(80), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="idle")


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id, "filename": job.filename, "size_bytes": job.size_bytes,
        "sha256": job.sha256, "status": job.status, "mode": job.mode,
        "stage": job.stage, "progress": job.progress, "attempt": job.attempt,
        "cancel_requested": job.cancel_requested, "error": job.error,
        "created_at": iso(job.created_at), "updated_at": iso(job.updated_at),
        "started_at": iso(job.started_at), "finished_at": iso(job.finished_at),
        "report_available": job.status == "completed",
    }


class JobStore:
    def __init__(self, database_url: str | None = None, *, data_dir: Path | str = ".data") -> None:
        data_dir = Path(data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        url = database_url or os.environ.get("DATABASE_URL") or f"sqlite:///{data_dir / 'jobs.sqlite3'}"
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        options: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, record):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=30000")
        # API and worker can boot together against a fresh database.
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(642791387242)"))
                Base.metadata.create_all(connection)
        else:
            with (data_dir / ".schema.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    Base.metadata.create_all(self.engine)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create(self, *, job_id: str | None = None, filename: str, size_bytes: int,
               sha256: str, mode: str) -> dict[str, Any]:
        if mode not in {"local", "gigachat"}:
            raise ValueError("Unsupported analysis mode")
        identifier = str(uuid.UUID(job_id)) if job_id else str(uuid.uuid4())
        with self.sessions.begin() as session:
            job = Job(id=identifier, filename=filename, size_bytes=size_bytes, sha256=sha256, mode=mode)
            session.add(job)
            session.flush()
            return job_dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            job = session.get(Job, job_id)
            return job_dict(job) if job else None

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.sessions() as session:
            jobs = session.scalars(select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit))
            return [job_dict(job) for job in jobs]

    def claim(self) -> dict[str, Any] | None:
        """Conditional UPDATE prevents two workers from claiming the same task."""
        with self.sessions.begin() as session:
            statement = select(Job.id).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            job_id = session.scalar(statement)
            if not job_id:
                return None
            now = utcnow()
            result = session.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
                status="running", stage="starting", progress=0, started_at=now, updated_at=now,
                finished_at=None, error=None, attempt=Job.attempt + 1,
            ))
            if result.rowcount != 1:
                return None
            return job_dict(session.get(Job, job_id))

    def progress(self, job_id: str, *, stage: str, progress: int) -> None:
        with self.sessions.begin() as session:
            session.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(
                stage=stage[:80], progress=max(0, min(99, progress)), updated_at=utcnow()))

    def cancel_requested(self, job_id: str) -> bool:
        with self.sessions() as session:
            job = session.get(Job, job_id)
            return job is None or job.cancel_requested or job.status == "cancelled"

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self.sessions.begin() as session:
            now = utcnow()
            session.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
                status="cancelled", stage="cancelled", cancel_requested=True, updated_at=now, finished_at=now))
            session.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(
                cancel_requested=True, updated_at=now))
            job = session.get(Job, job_id)
            return job_dict(job) if job else None

    def retry(self, job_id: str) -> dict[str, Any] | None:
        with self.sessions.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                return None
            if job.status not in {"failed", "cancelled"}:
                raise ValueError("Only failed or cancelled jobs can be retried")
            result = session.execute(update(Job).where(Job.id == job_id, Job.status.in_(["failed", "cancelled"])).values(
                status="queued", stage="queued", progress=0, cancel_requested=False,
                error=None, started_at=None, finished_at=None, updated_at=utcnow()))
            if result.rowcount != 1:
                raise ValueError("Job status changed")
            session.expire_all()
            return job_dict(session.get(Job, job_id))

    def finish(self, job_id: str, status: str, *, error: str | None = None) -> None:
        if status not in {"completed", "failed", "cancelled", "queued"}:
            raise ValueError("Invalid final job status")
        with self.sessions.begin() as session:
            # Each transition tests the cancellation bit in the same SQL UPDATE.
            # A concurrent accepted cancellation cannot be overwritten by stale ORM state.
            values = {"status": status, "stage": status, "error": error, "updated_at": utcnow(),
                      "finished_at": None if status == "queued" else utcnow()}
            if status == "completed":
                values["progress"] = 100
            session.execute(update(Job).where(
                Job.id == job_id, Job.status == "running", Job.cancel_requested.is_(False)).values(**values))
            session.execute(update(Job).where(
                Job.id == job_id, Job.status == "running", Job.cancel_requested.is_(True)).values(
                    status="cancelled", stage="cancelled", error=None, updated_at=utcnow(), finished_at=utcnow()))

    def recover_interrupted(self) -> int:
        """Only the owner of the singleton worker lock may call this method."""
        with self.sessions.begin() as session:
            now = utcnow()
            cancelled = session.execute(update(Job).where(Job.status == "running", Job.cancel_requested.is_(True)).values(
                status="cancelled", stage="cancelled", finished_at=now, updated_at=now))
            resumed = session.execute(update(Job).where(Job.status == "running", Job.cancel_requested.is_(False)).values(
                status="queued", stage="queued", started_at=None, finished_at=None, updated_at=now,
                error=None))
            return cancelled.rowcount + resumed.rowcount

    def heartbeat(self, *, active_job_id: str | None = None, state: str = "idle") -> None:
        with self.sessions.begin() as session:
            row = session.get(WorkerHeartbeat, 1)
            if row is None:
                row = WorkerHeartbeat(id=1)
                session.add(row)
            row.updated_at = utcnow()
            row.active_job_id = active_job_id
            row.state = state

    def worker_status(self) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(WorkerHeartbeat, 1)
            if row is None:
                return {"available": False, "state": "offline", "last_seen_at": None, "active_job_id": None}
            timestamp = row.updated_at.replace(tzinfo=row.updated_at.tzinfo or timezone.utc)
            available = (utcnow() - timestamp).total_seconds() < 30 and row.state != "stopped"
            return {"available": available, "state": row.state if available else "offline",
                    "last_seen_at": iso(timestamp), "active_job_id": row.active_job_id if available else None}

    def close(self) -> None:
        self.engine.dispose()
