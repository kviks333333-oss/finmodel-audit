"""Durability, cancellation and sequential processing without provider requests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finmodel_audit.audit import audit_workbook
from finmodel_audit.jobs import JobStore
from finmodel_audit.storage import LocalStorage, S3Storage, StorageError, file_sha256
from finmodel_audit.workbook import load_workbook
from finmodel_audit.worker import Worker, WorkerAlreadyRunning, singleton_worker
from test_workbook import write_fixture


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(data_dir=self.root)
        self.addCleanup(self.store.close)
        self.storage = LocalStorage(self.root)

    def create_job(self):
        source = self.root / "test.xlsx"
        write_fixture(source, '<row r="1"><c r="A1"><v>42</v></c></row>')
        job = self.store.create(filename="test.xlsx", size_bytes=source.stat().st_size,
                                sha256=file_sha256(source), mode="local")
        self.storage.save_original(job["id"], source)
        return job

    def test_atomic_claim_and_state_survives_new_connection(self):
        job = self.create_job()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        second = JobStore(data_dir=self.root)
        self.addCleanup(second.close)
        self.assertEqual(second.get(job["id"])["status"], "running")
        self.assertEqual(second.recover_interrupted(), 1)
        resumed = second.claim()
        self.assertEqual(resumed["id"], job["id"])
        self.assertEqual(resumed["attempt"], 2)

    def test_cancel_running_and_retry_keep_same_source(self):
        job = self.create_job()
        self.store.claim()
        cancelled = self.store.cancel(job["id"])
        self.assertEqual(cancelled["status"], "running")
        self.assertTrue(cancelled["cancel_requested"])
        self.store.finish(job["id"], "completed")
        self.assertEqual(self.store.get(job["id"])["status"], "cancelled")
        self.store.retry(job["id"])
        self.assertEqual(self.store.claim()["attempt"], 2)
        self.assertEqual(file_sha256(self.storage.source_path(job["id"], job["sha256"])), job["sha256"])

    def test_queued_cancel_and_interrupted_cancel_are_terminal(self):
        job = self.create_job()
        self.store.cancel(job["id"])
        self.assertIsNone(self.store.claim())
        self.store.retry(job["id"])
        self.store.claim()
        self.store.cancel(job["id"])
        self.store.recover_interrupted()
        self.assertEqual(self.store.get(job["id"])["status"], "cancelled")

    def test_worker_stub_produces_reports_and_preserves_original(self):
        job = self.create_job()
        calls = []

        def pipeline(path, output_dir, **kwargs):
            calls.append(kwargs["mode"])
            self.assertTrue(kwargs["resume"])
            self.assertFalse(kwargs["cancel_check"]())
            kwargs["on_progress"]({"stage_id": "audit", "progress": 25})
            return audit_workbook(load_workbook(path))

        worker = Worker(self.store, self.storage, pipeline=pipeline)
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        self.assertEqual(calls, ["local"])
        completed = self.store.get(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertTrue(self.storage.report_path(job["id"], "html").is_file())
        self.storage.source_path(job["id"], job["sha256"])

    def test_worker_error_never_persists_credentials_and_retry_succeeds(self):
        job = self.create_job()

        def broken(*args, **kwargs):
            raise RuntimeError("secret-password oauth-token=do-not-persist")

        Worker(self.store, self.storage, pipeline=broken).run_once()
        failed = self.store.get(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("secret-password", failed["error"])
        self.assertNotIn("oauth", failed["error"])
        self.store.retry(job["id"])
        Worker(self.store, self.storage, pipeline=lambda path, *_args, **_kwargs: audit_workbook(load_workbook(path))).run_once()
        self.assertEqual(self.store.get(job["id"])["status"], "completed")
        self.assertEqual(self.store.get(job["id"])["attempt"], 2)

    def test_singleton_refuses_second_worker_and_shutdown_requeues(self):
        job = self.create_job()
        with singleton_worker(self.root, self.store):
            with self.assertRaises(WorkerAlreadyRunning):
                with singleton_worker(self.root, self.store):
                    self.fail("Second lock owner")
        worker = Worker(self.store, self.storage)

        def stop_pipeline(path, *_args, **kwargs):
            worker.stop()
            self.assertTrue(kwargs["cancel_check"]())
            return audit_workbook(load_workbook(path))

        worker.pipeline = stop_pipeline
        worker.run_once()
        self.assertEqual(self.store.get(job["id"])["status"], "queued")

    def test_storage_rejects_tampering_and_non_uuid_paths(self):
        job = self.create_job()
        source = self.storage.source_path(job["id"], job["sha256"])
        source.chmod(0o600)
        source.write_bytes(b"tampered")
        with self.assertRaises(StorageError):
            self.storage.source_path(job["id"], job["sha256"])
        with self.assertRaises(ValueError):
            self.storage.job_dir("../../outside")

    def test_s3_round_trip_uses_private_uuid_keys_and_recovers_local_cache(self):
        objects = {}

        class FakeS3:
            def upload_file(self, filename, bucket, key):
                objects[(bucket, key)] = Path(filename).read_bytes()

            def download_file(self, bucket, key, filename):
                Path(filename).write_bytes(objects[(bucket, key)])

        with patch("boto3.client", return_value=FakeS3()), patch.dict("os.environ", {"S3_BUCKET": "test-bucket"}):
            storage = S3Storage(self.root / "s3-cache")
        job = self.create_job()
        source = self.storage.source_path(job["id"], job["sha256"])
        uploaded = storage.save_original(job["id"], source)
        self.assertIn(("test-bucket", f"jobs/{job['id']}/original.xlsx"), objects)
        uploaded.unlink()
        restored = storage.source_path(job["id"], job["sha256"])
        self.assertEqual(file_sha256(restored), job["sha256"])
        for format in ("json", "html"):
            (storage.output_dir(job["id"]) / f"report.{format}").write_text("{}", encoding="utf-8")
        storage.publish_reports(job["id"])
        (storage.output_dir(job["id"]) / "report.json").unlink()
        self.assertEqual(storage.report_path(job["id"], "json").read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
