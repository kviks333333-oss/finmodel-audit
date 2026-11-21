from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from finmodel_audit.export_service import cached_pptx, ExportError


class ExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.report=self.root/'report.json'
        self.report.write_text(json.dumps({'source':'test.xlsx','stages':[],'usage':{'requests':7}}))

    def test_real_subprocess_cached_without_touching_report_or_provider(self):
        original=self.report.read_bytes()
        with patch.dict('os.environ',{'GIGACHAT_CREDENTIALS':'synthetic-provider-secret'}):
            first=cached_pptx(self.report,self.root/'output')
        with patch('finmodel_audit.export_service.subprocess.run') as run:
            second=cached_pptx(self.report,self.root/'output');run.assert_not_called()
        self.assertEqual(first,second);self.assertEqual(self.report.read_bytes(),original)
        with ZipFile(first) as archive:
            self.assertIn('ppt/presentation.xml',archive.namelist())
            self.assertNotIn('synthetic-provider-secret', '\n'.join(archive.read(n).decode(errors='ignore') for n in archive.namelist()))

    def test_same_key_concurrency_generates_once_and_changed_content_new_key(self):
        actual=subprocess.run
        with patch('finmodel_audit.export_service.subprocess.run',wraps=actual) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:cached_pptx(self.report,self.root/'output'),range(2)))
            self.assertEqual(run.call_count,1)
        self.assertEqual(results[0],results[1])
        self.report.write_text(json.dumps({'source':'changed.xlsx','stages':[]}))
        changed=cached_pptx(self.report,self.root/'output')
        self.assertNotEqual(changed,results[0]);self.assertTrue(results[0].is_file())

    def test_corrupt_cache_rebuild_and_symlink_not_followed(self):
        output=self.root/'output';first=cached_pptx(self.report,output)
        first.write_bytes(b'corrupt')
        cached_pptx(self.report,output)
        self.assertTrue(first.read_bytes().startswith(b'PK'))
        first.unlink();first.symlink_to(self.report)
        before=self.report.read_bytes()
        with self.assertRaises(ExportError):cached_pptx(self.report,output)
        self.assertEqual(self.report.read_bytes(),before)

    def test_timeout_and_process_error_are_bounded_and_sanitized(self):
        before=self.report.read_bytes()
        for fault in (subprocess.TimeoutExpired('private-secret',25),OSError('private-secret')):
            with self.subTest(fault=type(fault).__name__),patch('finmodel_audit.export_service.subprocess.run',side_effect=fault):
                with self.assertRaises(ExportError) as caught:cached_pptx(self.report,self.root/'output')
                self.assertNotIn('private-secret',str(caught.exception))
        self.assertEqual(self.report.read_bytes(),before)
        self.assertFalse(list((self.root/'output').rglob('*.pptx')))

    def test_invalid_json_fails_without_publishing_and_source_alias_rejected(self):
        self.report.write_text('invalid')
        with self.assertRaises(ExportError):cached_pptx(self.report,self.root/'output')
        link=self.root/'linked.json';link.symlink_to(self.report)
        with self.assertRaises(ExportError):cached_pptx(link,self.root/'output')


if __name__=='__main__':unittest.main()
