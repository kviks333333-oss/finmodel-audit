from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from finmodel_audit.__main__ import main
from finmodel_audit.workbook import Workbook


class OutputPreservationTests(unittest.TestCase):
    def test_pptx_command_uses_saved_report_and_refuses_existing_output(self):
        with TemporaryDirectory() as folder, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root=Path(folder); source=root/'report.json'; source.write_text('{"source":"model.xlsx","stages":[]}')
            target=root/'analysis.pptx'; before=source.read_bytes()
            self.assertEqual(main(['export-pptx',str(source),'--output',str(target)]),0)
            pptx=target.read_bytes();self.assertTrue(pptx.startswith(b'PK'))
            self.assertEqual(main(['export-pptx',str(source),'--output',str(target)]),2)
            self.assertEqual(target.read_bytes(),pptx);self.assertEqual(source.read_bytes(),before)

    def test_analysis_html_alias_rejected_before_provider_call(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "model.xlsx"
            source.write_bytes(b"source workbook")
            (root / "report.html").symlink_to(source)
            with patch("finmodel_audit.pipeline.run_analysis") as run, \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                result = main(["analyze", str(source), "--output-dir", str(root)])
            self.assertEqual(result, 2)
            run.assert_not_called()
            self.assertEqual(source.read_bytes(), b"source workbook")

    def test_source_alias_as_report_is_rejected_before_writing(self):
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "model.xlsx"
                original = b"original workbook bytes"
                source.write_bytes(original)
                output = root / "reports"
                output.mkdir()
                if kind == "symlink":
                    (output / "report.json").symlink_to(source)
                else:
                    os.link(source, output / "report.json")
                workbook = Workbook(source, {}, [], [], {}, [])
                with patch("finmodel_audit.__main__.load_workbook", return_value=workbook), \
                     redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    result = main(["audit", str(source), "--output-dir", str(output)])
                self.assertEqual(result, 2)
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse((output / "report.html").exists())


if __name__ == "__main__":
    unittest.main()
