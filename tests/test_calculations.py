import unittest
from finmodel_audit.calculations import calculate_expression


class CalculationTests(unittest.TestCase):
    def test_ratio_uses_supplied_values(self):
        result = calculate_expression("(revenue-cost)/revenue", {"revenue": 120, "cost": 90})
        self.assertEqual(result["value"], .25)
        self.assertFalse(result["recalculated_workbook"])

    def test_supported_functions(self):
        self.assertEqual(calculate_expression("round(mean(a,b),2)", {"a": 1, "b": 2})["value"], 1.5)

    def test_missing_and_division_by_zero_are_errors(self):
        self.assertIn("error", calculate_expression("a/b", {"a": 1, "b": 0}))
        self.assertIn("error", calculate_expression("unknown + 1", {}))
        self.assertIn("error", calculate_expression("a", {"a": True}))

    def test_files_processes_imports_and_large_powers_rejected(self):
        for expression in ["__import__('os').environ", "open('/etc/passwd').read()",
                           "(1).__class__", "[x for x in range(100)]", "2**100000000"]:
            with self.subTest(expression=expression):
                self.assertIn("error", calculate_expression(expression, {}))


if __name__ == "__main__":
    unittest.main()
