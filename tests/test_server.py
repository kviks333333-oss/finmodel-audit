"""API integration with a private temp database and no external requests."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from finmodel_audit.audit import audit_workbook
from finmodel_audit.jobs import JobStore
from finmodel_audit.server import create_app
from finmodel_audit.storage import LocalStorage
from finmodel_audit.workbook import load_workbook
from finmodel_audit.worker import Worker
from test_workbook import write_fixture


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(data_dir=self.root)
        self.addCleanup(self.store.close)
        self.storage = LocalStorage(self.root)
        app = create_app(store=self.store, storage=self.storage, data_dir=self.root,
                         allowed_hosts=["testserver"], load_environment=False)
        self.client = self.enterContext(TestClient(app))
        source = self.root / "fixture.xlsx"
        write_fixture(source, '<row r="1"><c r="A1"><v>42</v></c><c r="B1"><f>A1*2</f><v>84</v></c></row>')
        self.workbook = source.read_bytes()

    def upload(self, **kwargs):
        return self.client.post("/api/jobs", files={"file": ("../../model.xlsx", self.workbook)},
                                data={"mode": "local"}, **kwargs)

    def test_upload_progress_report_and_download(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        self.assertEqual(job["filename"], "model.xlsx")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"][0]["id"], job["id"])
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/report").status_code, 409)
        Worker(self.store, self.storage, pipeline=lambda path, *_args, **_kwargs: audit_workbook(load_workbook(path))).run_once()
        finished = self.client.get(f"/api/jobs/{job['id']}").json()
        self.assertTrue(finished["report_available"])
        report = self.client.get(f"/api/jobs/{job['id']}/report")
        self.assertEqual(report.status_code, 200)
        self.assertFalse(report.json()["recalculated"])
        download = self.client.get(f"/api/jobs/{job['id']}/download?format=html")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertIn("sandbox", download.headers["content-security-policy"])
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download?format=xml").status_code, 422)

    def test_invalid_uploads_never_queue_and_macro_rejected(self):
        self.assertEqual(self.client.post("/api/jobs", files={"file": ("file.xlsm", self.workbook)}, data={"mode": "local"}).status_code, 415)
        self.assertEqual(self.client.post("/api/jobs", files={"file": ("file.xlsx", b"not zip")}, data={"mode": "local"}).status_code, 422)
        path = self.root / "macro.xlsx"
        path.write_bytes(self.workbook)
        with ZipFile(path, "a") as archive:
            archive.writestr("xl/vbaProject.bin", b"not executed")
        self.assertEqual(self.client.post("/api/jobs", files={"file": ("macro.xlsx", path.read_bytes())}, data={"mode": "local"}).status_code, 422)
        self.assertEqual(self.store.list(), [])

    def test_pptx_download_preserves_analysis_and_export_failure_is_independent(self):
        from io import BytesIO
        from finmodel_audit.export_service import ExportError
        job = self.upload().json()
        url = f"/api/jobs/{job['id']}/download?format=pptx"
        self.assertEqual(self.client.get(url).status_code, 409)
        Worker(self.store, self.storage, pipeline=lambda path, *_a, **_k: audit_workbook(load_workbook(path))).run_once()
        json_path = self.storage.report_path(job['id'], 'json')
        original = json_path.read_bytes()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
        self.assertIn('presentationml.presentation', response.headers['content-type'])
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertIn('ppt/presentation.xml', archive.namelist())
        self.assertEqual(self.client.get(url).content, response.content)
        self.assertEqual(json_path.read_bytes(), original)
        with patch('finmodel_audit.export_service.cached_pptx', side_effect=ExportError('Экспорт недоступен. Анализ сохранён.')):
            self.assertEqual(self.client.get(url).status_code, 503)
        self.assertEqual(self.store.get(job['id'])['status'], 'completed')
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download?format=json").content, original)

    def test_stored_decision_approval_is_never_trusted_as_current_assessment(self):
        job = self.upload().json()
        Worker(self.store, self.storage, pipeline=lambda path, *_a, **_k: audit_workbook(load_workbook(path))).run_once()
        path = self.storage.report_path(job['id'], 'json')
        payload = json.loads(path.read_text()); payload['decision_assessment'] = {'suitability': {'financial_approval': True}}
        path.write_text(json.dumps(payload)); original = path.read_bytes()
        viewed = self.client.get(f"/api/jobs/{job['id']}/report").json()
        self.assertIs(viewed['decision_assessment']['suitability']['financial_approval'], False)
        self.assertEqual(path.read_bytes(), original)

    def test_reader_view_and_html_refresh_do_not_rewrite_saved_reports_or_rerun_agent(self):
        from finmodel_audit.presentation import VERSION
        job = self.upload().json()
        Worker(self.store, self.storage, pipeline=lambda path, *_args, **_kwargs: audit_workbook(load_workbook(path))).run_once()
        original_json = self.storage.report_path(job['id'], 'json')
        original_html = self.storage.report_path(job['id'], 'html')
        payload = json.loads(original_json.read_text())
        payload['presentation'] = {'overview': {'title': 'Untrusted stored presentation'}}
        original_json.write_text(json.dumps(payload))
        original_html.write_text('Historical HTML must remain unchanged')
        json_bytes, html_bytes = original_json.read_bytes(), original_html.read_bytes()
        viewed = self.client.get(f"/api/jobs/{job['id']}/report")
        self.assertEqual(viewed.status_code, 200)
        self.assertEqual(viewed.json()['presentation']['version'], VERSION)
        self.assertNotEqual(viewed.json()['presentation'], payload['presentation'])
        for key, value in payload.items():
            if key != 'presentation':
                self.assertEqual(viewed.json()[key], value)
        export = self.client.get(f"/api/jobs/{job['id']}/download?format=html")
        self.assertEqual(export.status_code, 200)
        self.assertIn('text/html', export.headers['content-type'])
        self.assertNotIn('Historical HTML must remain unchanged', export.text)
        self.assertEqual(original_html.read_bytes(), html_bytes)
        self.assertEqual(original_json.read_bytes(), json_bytes)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download?format=json").content, json_bytes)
        self.assertEqual(self.store.list()[0]['status'], 'completed')

    def test_stream_size_and_csrf_and_host_protection(self):
        with patch("finmodel_audit.server.MAX_UPLOAD_BYTES", 50):
            self.assertEqual(self.upload().status_code, 413)
        self.assertEqual(self.upload(headers={"Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.upload(headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(self.upload(headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "attacker.example"}).status_code, 400)
        self.assertEqual(self.upload(headers={"Origin": "http://testserver"}).status_code, 202)

    def test_retry_cancel_and_missing_jobs(self):
        job = self.upload().json()
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/retry").status_code, 409)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/cancel").json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/retry").json()["status"], "queued")
        self.assertEqual(self.client.get("/api/jobs/not-a-uuid").status_code, 404)
        self.assertEqual(self.client.get("/api/no-such-endpoint").status_code, 404)

    def test_health_contains_no_credentials_and_default_is_real_ai(self):
        with patch.dict("os.environ", {"GIGACHAT_CREDENTIALS": "synthetic-secret"}):
            health = self.client.get("/api/health")
            self.assertTrue(health.json()["provider"]["configured"])
            self.assertNotIn("synthetic-secret", health.text)
            job = self.client.post("/api/jobs", files={"file": ("model.xlsx", self.workbook)}).json()
            self.assertEqual(job["mode"], "gigachat")
        with patch.dict("os.environ", {"GIGACHAT_CREDENTIALS": ""}):
            self.assertEqual(self.client.post("/api/jobs", files={"file": ("model.xlsx", self.workbook)}).status_code, 503)

    def test_evidence_returns_real_values_formulas_and_missing_cells(self):
        job = self.upload().json()
        response = self.client.get(f"/api/jobs/{job['id']}/cells", params={"sheet": "Model", "range": "A1:C1"})
        self.assertEqual(response.status_code, 200, response.text)
        cells = response.json()["cells"]
        self.assertEqual(cells[0]["value"], 42)
        self.assertEqual(cells[1]["formula"], "=A1*2")
        self.assertFalse(cells[2]["present"])
        bounded = self.client.get(f"/api/jobs/{job['id']}/cells", params={"sheet": "Model", "range": "A1:XFD1048576"}).json()
        self.assertLessEqual(len(bounded["cells"]), 200)
        self.assertTrue(bounded["truncated"])

    def test_chunked_multipart_limit_applies_without_content_length(self):
        # Directly lower the ASGI middleware bound, including multipart envelope.
        from finmodel_audit.server import UploadLimitMiddleware
        app = create_app(store=self.store, storage=self.storage, allowed_hosts=["testserver"], load_environment=False)
        for middleware in app.user_middleware:
            if middleware.cls is UploadLimitMiddleware:
                middleware.kwargs["maximum"] = 100
        boundary = b"test-boundary"
        body = (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="model.xlsx"\r\n'
                b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n" + self.workbook +
                b"\r\n--" + boundary + b'\r\nContent-Disposition: form-data; name="mode"\r\n\r\nlocal\r\n--' + boundary + b"--\r\n")
        with TestClient(app) as client:
            response = client.post("/api/jobs", content=(body[i:i+50] for i in range(0, len(body), 50)),
                                   headers={"Content-Type": "multipart/form-data; boundary=test-boundary"})
            self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.store.list(), [])


if __name__ == "__main__":
    unittest.main()
