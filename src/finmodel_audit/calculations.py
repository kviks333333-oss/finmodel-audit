"""Run bounded calculations outside the process that holds API credentials."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def calculate_expression(expression: str, variables: dict) -> dict:
    runner = Path(__file__).with_name("calc_runner.py").resolve()
    try:
        payload = json.dumps({"expression": expression, "variables": variables}, allow_nan=False)
        if len(payload) > 32768:
            return {"error": "Calculation input exceeds the size limit."}
        with tempfile.TemporaryDirectory(prefix="finmodel-calc-") as cwd:
            process = subprocess.run(
                [sys.executable, "-I", str(runner)], input=payload, text=True,
                capture_output=True, cwd=cwd, env={}, timeout=5, check=False,
            )
        result = json.loads(process.stdout)
        if not isinstance(result, dict):
            raise ValueError("Invalid calculation result")
        if "value" in result:
            return {"value": result["value"], "engine": "isolated_python_expression",
                    "expression": expression, "recalculated_workbook": False}
        return {"error": result.get("error", "Calculation failed.")}
    except subprocess.TimeoutExpired:
        return {"error": "Calculation exceeded the time limit."}
    except (ValueError, OSError, TypeError):
        return {"error": "Calculation failed or input is invalid."}
