from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from finmodel_audit import pptx_backend


class PowerPointBackendTests(unittest.TestCase):
    def test_process_receives_only_slide_plan_and_whitelisted_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('render.cjs', 'package-lock.json', 'package.json', 'node'):
                (root / name).write_text('{}')
            environment = {'FINMODEL_NODE_PATH': str(root / 'node'), 'FINMODEL_PPTX_RUNTIME': str(root),
                           'GIGACHAT_CREDENTIALS': 'synthetic-private', 'NODE_OPTIONS': '--require=untrusted.js'}
            with patch.dict('os.environ', environment), patch.object(pptx_backend.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0, stdout=b'PK\x03\x04payload', stderr=b'')) as run:
                pptx_backend.render_plan({'slides': [], 'label': 'Сохранённый отчёт'})
            command = run.call_args.args[0]
            options = run.call_args.kwargs
            self.assertEqual(command[-1], str((root / 'render.cjs').resolve()))
            self.assertIn('--max-old-space-size=192', command)
            self.assertNotIn('GIGACHAT_CREDENTIALS', options['env'])
            self.assertNotIn('NODE_OPTIONS', options['env'])
            self.assertNotIn(b'synthetic-private', options['input'])
            self.assertEqual(options['timeout'], 20)

    def test_renderer_failures_are_bounded_and_never_publish_stderr(self):
        environment = {'FINMODEL_NODE_PATH': '/test/node', 'FINMODEL_PPTX_RUNTIME': '/test'}
        faults = [OSError('private source text'), subprocess.TimeoutExpired('private source text', 20),
                  SimpleNamespace(returncode=1, stdout=b'', stderr=b'private source text'),
                  SimpleNamespace(returncode=0, stdout=b'<html>failure</html>', stderr=b'')]
        for fault in faults:
            with self.subTest(fault=type(fault).__name__), patch.object(pptx_backend, 'isolated_environment', return_value=environment):
                setting = {'side_effect': fault} if isinstance(fault, Exception) else {'return_value': fault}
                with patch.object(pptx_backend.subprocess, 'run', **setting), self.assertRaises(ValueError) as caught:
                    pptx_backend.render_plan({'slides': []})
                self.assertNotIn('private source text', str(caught.exception))

    def test_runtime_assets_change_cache_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('render.cjs', 'package-lock.json', 'package.json'):
                (root / name).write_text('{}')
            with patch.dict('os.environ', {'FINMODEL_PPTX_RUNTIME': str(root)}):
                first = pptx_backend.runtime_identity()
                (root / 'render.cjs').write_text('changed adapter')
                second = pptx_backend.runtime_identity()
                self.assertEqual(first['backend'], second['backend'])
                self.assertNotEqual(first['assets_sha256'], second['assets_sha256'])


if __name__ == '__main__':
    unittest.main()
