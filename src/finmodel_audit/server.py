"""Local FastAPI service; analysis runs in the separate durable worker process."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit
import uuid
from zipfile import BadZipFile, ZipFile

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import JobStore
from .storage import LocalStorage, StorageError, make_storage
from .workbook import WorkbookReadError, load_workbook


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024


class RequestTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Максимальный размер файла — 20 МиБ.")


class UploadLimitMiddleware:
    """Bound multipart bytes before Starlette spools the complete upload."""
    def __init__(self, app, maximum: int = MAX_REQUEST_BYTES):
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length:
            try:
                if int(length) > self.maximum:
                    return await JSONResponse({"detail": "Максимальный размер файла — 20 МиБ."}, 413)(scope, receive, send)
            except ValueError:
                return await JSONResponse({"detail": "Некорректный Content-Length."}, 400)(scope, receive, send)
        consumed = 0
        response_started = False

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.maximum:
                    raise RequestTooLarge()
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestTooLarge:
            if not response_started:
                await JSONResponse({"detail": "Максимальный размер файла — 20 МиБ."}, 413)(scope, receive, send)


def validate_upload(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if any("vbaproject" in name.lower() or "macrosheets/" in name.lower() for name in names):
                raise WorkbookReadError("Macro-enabled workbooks are not supported")
            # The OOXML reader enforces archive, XML and cell bounds.
        load_workbook(path)
    except (BadZipFile, WorkbookReadError, KeyError, OSError, ValueError):
        raise HTTPException(422, "Файл не является поддерживаемой книгой XLSX или содержит макросы.") from None


def _job_or_404(store: JobStore, job_id: str) -> dict[str, Any]:
    try:
        job_id = str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404, "Задача не найдена.") from None
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена.")
    return job


def create_app(*, data_dir: Path | str | None = None, database_url: str | None = None,
               store: JobStore | None = None, storage: LocalStorage | None = None,
               allowed_hosts: list[str] | None = None, frontend_dir: Path | str | None = None,
               load_environment: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if load_environment:
            from .gigachat import load_env_file
            load_env_file(os.environ.get("FINMODEL_ENV_FILE", ".env"))
        directory = Path(data_dir or os.environ.get("DATA_DIR", ".data")).resolve()
        application.state.jobs = store or JobStore(database_url, data_dir=directory)
        application.state.storage = storage or make_storage(directory)
        try:
            yield
        finally:
            if store is None:
                application.state.jobs.close()

    application = FastAPI(title="Аудит финансовых моделей", version="0.2.0", lifespan=lifespan)
    hosts = allowed_hosts or [part.strip() for part in os.environ.get(
        "ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if part.strip()]
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)
    application.add_middleware(UploadLimitMiddleware)

    @application.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    valid_origin = parsed.scheme in {"http", "https"} and parsed.netloc.lower() == request.url.netloc.lower()
                except ValueError:
                    valid_origin = False
                if not valid_origin:
                    return JSONResponse({"detail": "Запрос разрешён только из интерфейса этого сервиса."}, 403)
            elif request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Межсайтовый запрос запрещён."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError):
        return JSONResponse({"detail": "Файл недоступен или не прошёл проверку целостности."}, 503)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return JSONResponse({"detail": "Хранилище задач временно недоступно."}, 503)

    @application.get("/api/health")
    def health(request: Request):
        return {"status": "ok", "provider": {"name": "gigachat", "configured": bool(
            os.environ.get("GIGACHAT_CREDENTIALS", "").strip())},
            "storage": request.app.state.storage.backend,
            "worker": request.app.state.jobs.worker_status()}

    @application.get("/api/jobs")
    def list_jobs(request: Request, limit: int = Query(default=50, ge=1, le=100)):
        return {"jobs": request.app.state.jobs.list(limit=limit)}

    @application.post("/api/jobs", status_code=202)
    async def upload_job(request: Request, file: UploadFile = File(...), mode: str = Form(default="gigachat")):
        if mode not in {"local", "gigachat"}:
            raise HTTPException(422, "Режим должен быть gigachat или local.")
        if mode == "gigachat" and not os.environ.get("GIGACHAT_CREDENTIALS", "").strip():
            raise HTTPException(503, "GigaChat не настроен. Добавьте ключ в локальную конфигурацию сервиса.")
        filename = Path((file.filename or "").replace("\\", "/")).name
        filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)[:240]
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(415, "Поддерживаются только файлы .xlsx.")
        current_storage = request.app.state.storage
        fd, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".xlsx", dir=current_storage.root / "uploads")
        staged = Path(temp_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as destination:
                while chunk := await file.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Максимальный размер файла — 20 МиБ.")
                    digest.update(chunk)
                    destination.write(chunk)
            await run_in_threadpool(validate_upload, staged)
            identifier = str(uuid.uuid4())
            await run_in_threadpool(current_storage.save_original, identifier, staged)
            return await run_in_threadpool(request.app.state.jobs.create,
                job_id=identifier, filename=filename, size_bytes=size, sha256=digest.hexdigest(), mode=mode)
        finally:
            staged.unlink(missing_ok=True)
            await file.close()

    @application.get("/api/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        return _job_or_404(request.app.state.jobs, job_id)

    @application.post("/api/jobs/{job_id}/cancel")
    def cancel_job(request: Request, job_id: str):
        job = _job_or_404(request.app.state.jobs, job_id)
        if job["status"] not in {"queued", "running", "cancelled"}:
            raise HTTPException(409, "Завершённую задачу нельзя отменить.")
        return request.app.state.jobs.cancel(job["id"])

    @application.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(request: Request, job_id: str):
        job = _job_or_404(request.app.state.jobs, job_id)
        try:
            return request.app.state.jobs.retry(job["id"])
        except ValueError:
            raise HTTPException(409, "Повторить можно только ошибочную или отменённую задачу.") from None

    @application.get("/api/jobs/{job_id}/report")
    def get_report(request: Request, job_id: str):
        job = _job_or_404(request.app.state.jobs, job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "Отчёт будет доступен после завершения анализа.")
        path = request.app.state.storage.report_path(job["id"], "json")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise HTTPException(503, "Сохранённый отчёт недоступен.") from None
        if not isinstance(report, dict):
            raise HTTPException(503, "Сохранённый отчёт недоступен.")
        from .presentation import build_presentation
        from .decision import build_decision_assessment
        return {**report, "decision_assessment": build_decision_assessment(report), "presentation": build_presentation(report)}

    @application.get("/api/jobs/{job_id}/download")
    def download_report(request: Request, job_id: str, format: str = Query(default="html", pattern="^(json|html|pptx)$")):
        job = _job_or_404(request.app.state.jobs, job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "Отчёт будет доступен после завершения анализа.")
        if format == "pptx":
            from .export_service import cached_pptx, ExportError
            source = request.app.state.storage.report_path(job["id"], "json")
            try:
                path = cached_pptx(source, request.app.state.storage.output_dir(job["id"]))
            except ExportError as exc:
                raise HTTPException(503, str(exc)) from None
            return FileResponse(path, filename=f"analysis-{job['id']}.pptx",
                media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        if format == "html":
            # Render the current reader view from saved results; never rerun the
            # agent or overwrite either original JSON or historic HTML exports.
            path = request.app.state.storage.report_path(job["id"], "json")
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                raise HTTPException(503, "Сохранённый отчёт недоступен.") from None
            if not isinstance(report, dict):
                raise HTTPException(503, "Сохранённый отчёт недоступен.")
            from .report import render_html
            return HTMLResponse(render_html(report), headers={
                "Content-Disposition": f'attachment; filename="analysis-{job["id"]}.html"',
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"})
        path = request.app.state.storage.report_path(job["id"], "json")
        return FileResponse(path, filename=f"analysis-{job['id']}.{format}", media_type=(
            "application/json" if format == "json" else "text/html"), headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"})

    @application.get("/api/jobs/{job_id}/cells")
    def evidence_cells(request: Request, job_id: str, sheet: str = Query(min_length=1, max_length=128),
                       range: str = Query(min_length=1, max_length=32)):
        job = _job_or_404(request.app.state.jobs, job_id)
        path = request.app.state.storage.source_path(job["id"], job["sha256"])
        from .tools import WorkbookTools
        result = WorkbookTools(load_workbook(path)).call("read_cells", {"sheet": sheet, "range": range})
        if result.get("error"):
            return JSONResponse(result, status_code=422)
        return result

    # Mount built assets after API routes; unknown API routes never return the SPA.
    frontend = Path(frontend_dir or os.environ.get("FRONTEND_DIR", "frontend/dist")).resolve()
    if (frontend / "assets").is_dir():
        application.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @application.get("/{path:path}", include_in_schema=False)
    def frontend_fallback(path: str):
        if path.startswith("api/") or path == "api":
            raise HTTPException(404, "Endpoint не найден.")
        index = frontend / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "API работает. Для интерфейса соберите frontend или запустите dev-сервер."})

    return application


app = create_app()
