from pathlib import Path
import tempfile
import unittest

from scripts.validate_example import DEFAULT_SOURCE, EXPECTED_SHA256, validate_fixture, write_report


class ExampleValidationTests(unittest.TestCase):
    @unittest.skipUnless(DEFAULT_SOURCE.is_file(), "Private example workbook is not bundled with the repository")
    def test_reviewed_financial_fixture(self):
        result = validate_fixture()
        failures = [check for check in result["checks"] if not check["passed"]]
        self.assertEqual(result["status"], "passed", failures or result)
        self.assertEqual(result["source"]["sha256_after"], EXPECTED_SHA256)
        self.assertGreaterEqual(result["total_checks"], 70)
        self.assertEqual(result["saved_error_counts"], {"#NUM!": 30, "#DIV/0!": 4})
        self.assertFalse(result["recalculated_workbook"])
        self.assertEqual(result["api_requests"], 0)
        self.assertAlmostEqual(result["calculations"]["conditional_ev"]["result"]["value"], 144748.98660332896, places=6)
        self.assertAlmostEqual(result["sensitivity"]["w"]["scenarios"][0]["result"]["value"], 173925.39711201642, places=6)

    def test_different_fixture_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not_the_reviewed_book.xlsx"
            path.write_bytes(b"not even OOXML")
            result = validate_fixture(path)
            self.assertEqual(result["status"], "fixture_mismatch")
            self.assertEqual(result["checks"], [])
            self.assertEqual(result["calculations"], {})
            self.assertEqual(path.read_bytes(), b"not even OOXML")

    def test_verification_output_cannot_overwrite_original_or_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.xlsx"
            source.write_bytes(b"original")
            alias = root / "alias.json"
            alias.hardlink_to(source)
            link = root / "link.json"
            link.symlink_to(source)
            for output in [source, alias, link, root / "report.json"]:
                with self.subTest(output=output.name), self.assertRaises(ValueError):
                    write_report(output, {}, source)
            self.assertEqual(source.read_bytes(), b"original")
            target = root / "outputs" / "checks.json"
            write_report(target, {"passed": True}, source)
            self.assertEqual(target.read_text(), '{\n  "passed": true\n}\n')


if __name__ == "__main__":
    unittest.main()
