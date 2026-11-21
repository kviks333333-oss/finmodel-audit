#!/usr/bin/env python3
"""Reproduce the reviewed example's saved-value checks; no API or Excel writes.

This is a dataset-specific evaluation fixture, intentionally outside the generic
application runtime. A different source hash is rejected before workbook parsing.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finmodel_audit.tools import WorkbookTools
from finmodel_audit.workbook import load_workbook


EXPECTED_SHA256 = "ea7392b4323844ebe8baa430e0dc219468ae671b59accf88c682a862e55f4df1"
DEFAULT_SOURCE = ROOT / "examples" / "Fin_Model_for_test.xlsx"
REL_TOL = 1e-10
ABS_TOL = 1e-6  # Monetary values are in RUB millions unless the check says otherwise.


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_fixture(source: str | Path = DEFAULT_SOURCE) -> dict[str, Any]:
    """Return deterministic assertions and exact tool evidence for this one book."""
    source = Path(source)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "source": {"name": source.name, "expected_sha256": EXPECTED_SHA256},
        "scope": "reviewed_example_saved_values_and_local_expressions",
        "local_expression_only": True,
        "recalculated_workbook": False,
        "api_requests": 0,
        "checks": [],
        "calculations": {},
        "sensitivity": {},
        "limitations": [
            "Checks are specific to the source hash and do not audit arbitrary workbooks.",
            "Saved values are evidence of file state, not proof of fresh Excel calculation.",
            "Conditional DCF scenarios assume forecast periods I:N and one terminal discount; they are not an approved corrected valuation.",
            "No full workbook recalculation, dataTable execution, iteration convergence or external refresh was performed.",
            "Market assumptions and tax law were not independently verified.",
        ],
    }
    if not source.is_file():
        report["status"] = "fixture_missing"
        return report
    before = sha256(source)
    report["source"]["sha256_before"] = before
    if before != EXPECTED_SHA256:
        report["status"] = "fixture_mismatch"
        return report

    def check(name: str, actual: Any, expected: Any, *, numeric: bool = False,
              abs_tol: float = ABS_TOL, evidence: list[dict[str, Any]] | None = None) -> None:
        passed = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                  and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=abs_tol)) if numeric else actual == expected
        item = {"name": name, "passed": passed, "actual": actual, "expected": expected}
        if numeric:
            item["tolerance"] = {"absolute": abs_tol, "relative": REL_TOL}
        if evidence is not None:
            item["evidence"] = evidence
        report["checks"].append(item)

    try:
        workbook = load_workbook(source)
        tools = WorkbookTools(workbook)

        def evidence(sheet: str, *addresses: str) -> list[dict[str, Any]]:
            result = []
            for address in addresses:
                cell = workbook.sheets[sheet].cells.get(address)
                result.append({"sheet": sheet, "cell": address, "present": cell is not None,
                               "value": cell.value if cell else None,
                               "formula": cell.formula if cell else None,
                               "formula_type": cell.formula_type if cell else None})
            return result

        def value(sheet: str, address: str) -> Any:
            return workbook.sheets[sheet].cells[address].value

        def calculate(name: str, expression: str, bindings: dict[str, tuple[str, str]], expected: float,
                      abs_tol: float = ABS_TOL) -> float | None:
            result = tools.call("calculate_cells", {
                "expression": expression,
                "inputs": [{"name": var, "sheet": sheet, "cell": cell} for var, (sheet, cell) in bindings.items()],
            })
            report["calculations"][name] = result
            actual = result.get("result", {}).get("value")
            check(name, actual, expected, numeric=True, abs_tol=abs_tol, evidence=result.get("evidence", []))
            check(name + "_complete", result.get("completeness", {}).get("complete"), True)
            return actual

        summary = workbook.summary()
        summary.pop("path", None)
        report["workbook_summary"] = summary
        for field, expected in {"sheet_count": 27, "formula_cells": 12710, "cached_errors": 34,
                                "actual_nonempty_cells": 19156, "missing_formula_caches": 0,
                                "external_link_count": 2, "defined_name_count": 9}.items():
            check(field, summary[field], expected)
        check("reader_warnings", workbook.warnings, [])
        shared = [cell for sheet in workbook.sheets.values() for cell in sheet.cells.values() if cell.formula_type == "shared"]
        check("expanded_shared_formula_cells", sum(cell.formula is not None for cell in shared), 7017)
        # Frozen expected expansions are independent of translate_formula's result.
        for sheet, address, expected in [
            ("Merger", "N5", "=EDATE(M5,12)"),
            ("Multiples", "T13", '=IFERROR(Q13/N13, "NA")'),
            ("DCF", "N37", "=1/(1+$E$13)^N36"),
            ("DCF", "N39", "=N27*N37"),
        ]:
            check("shared_expansion_" + sheet + "_" + address, workbook.sheets[sheet].cells[address].formula,
                  expected, evidence=evidence(sheet, address))

        errors = sorted((sheet.name, cell.address, cell.value) for sheet in workbook.sheets.values()
                        for cell in sheet.cells.values() if cell.data_type == "e")
        expected_errors = sorted([("Synergies & Debt", col + "434", "#DIV/0!") for col in "NOP"]
                                 + [("Other - POSI", "W146", "#DIV/0!")]
                                 + [("DCF", col + str(row), "#NUM!") for row in range(82, 88) for col in "JKLMN"])
        check("exact_saved_error_locations", errors, expected_errors)
        report["saved_error_counts"] = dict(Counter(error[2] for error in errors))
        check("data_table_anchors", [(cell.address, cell.formula_attributes.get("ref"))
                                    for cell in workbook.sheets["DCF"].cells.values() if cell.formula_type == "dataTable"],
              [("J71", "J71:N76"), ("J82", "J82:N87")])
        for sheet, address in [("Synergies & Debt", col + str(row)) for col in "NOP" for row in (432, 433)] + [("Other - POSI", "W145"), ("Other - POSI", "W172")]:
            check("zero_division_input_" + sheet + "_" + address, value(sheet, address), 0, evidence=evidence(sheet, address))

        for address, expected in {"E39": "=SUM(H39:R39)", "E45": "=P39*(1+$E$14)/($E$13-$E$14)",
                                  "E44": "=E45*F45+E46*F46", "E41": "=$E$44*$N$37", "E55": "=E39+E41",
                                  "E60": "=E55-E56+E57", "E64": "=E60/E62*10^6"}.items():
            check("dcf_formula_" + address, workbook.sheets["DCF"].cells[address].formula, expected, evidence=evidence("DCF", address))
        for address, expected in {"E33": "30/6/2026", "H9": 2025, "I9": 2026, "N9": 2031,
                                  "P9": "TV", "H36": 0, "H37": 1, "F45": 1, "F46": 0}.items():
            check("dcf_context_" + address, value("DCF", address), expected, evidence=evidence("DCF", address))
        for row in (27, 36, 37, 39):
            check("terminal_column_matches_last_forecast_" + str(row), value("DCF", "P" + str(row)),
                  value("DCF", "N" + str(row)), numeric=True, evidence=evidence("DCF", "N" + str(row), "P" + str(row)))

        bind = lambda **items: {name: ("DCF", address) for name, address in items.items()}
        calculate("saved_ev_bridge", "pv+terminal", bind(pv="E39", terminal="E41"), 100579.37480758553)
        calculate("saved_equity_bridge", "ev-debt+cash", bind(ev="E55", debt="E56", cash="E57"), 81729.83837263408)
        calculate("saved_share_price", "equity/shares*1000000", bind(equity="E60", shares="E62"), 1147.6653238497217)
        calculate("forecast_only_saved_pv", "pv-last-historic", bind(pv="E39", last="N39", historic="H39"), 63232.652623586844)
        calculate("single_discount_terminal_pv", "last*(1+g)/(w-g)", bind(last="N39", w="E13", g="E14"), 81516.3339797421)
        calculate("existing_second_terminal_discount", "terminal*discount", bind(terminal="E45", discount="N37"), 27791.881167953183)
        calculate("only_remove_duplicate_column", "ev-duplicate", bind(ev="E55", duplicate="P39"), 90306.97589153993)
        conditional = "pv-last-historic+last*(1+g)/(w-g)"
        base = bind(pv="E39", last="N39", historic="H39", w="E13", g="E14")
        calculate("conditional_ev", conditional, base, 144748.98660332896)
        calculate("conditional_equity", conditional + "-debt+cash", {**base, **bind(debt="E56", cash="E57")}, 125899.45016837752)
        calculate("conditional_share_price", "(" + conditional + "-debt+cash)/shares*1000000",
                  {**base, **bind(debt="E56", cash="E57", shares="E62")}, 1767.903083219276)
        calculate("saved_grid_ev_difference", "grid-base", bind(grid="N73", base="E55"), -46813.13339298556)
        calculate("saved_grid_wacc_difference", "grid-base", bind(grid="I73", base="E13"), 3.544539803335134e-8, abs_tol=1e-12)
        calculate("minimum_grid_wacc_minus_max_growth", "low_w-high_g", bind(low_w="I87", high_g="N81"), 0.0760978, abs_tol=1e-12)
        calculate("fixed_ebit_tax_step", "ebit*(new-old)", bind(ebit="N19", new="N20", old="M20"), 9609.074457865901)
        calculate("revenue_2026_2031_cagr", "(last/first)**(1/5)-1",
                  {"first": ("FSs - POSI", "R18"), "last": ("FSs - POSI", "W18")}, 0.2234695519057055, abs_tol=1e-12)
        calculate("ebitda_margin_change_pp", "(last-first)*100",
                  {"first": ("FSs - POSI", "R40"), "last": ("FSs - POSI", "W40")}, 7.624157047405306)

        expression = "+".join(f"f{i}/(1+w)**t{i}" for i in range(1, 7)) + "+f6*(1+g)/(w-g)/(1+w)**t6"
        inputs = [{"name": f"{prefix}{i}", "sheet": "DCF", "cell": col + str(row)}
                  for prefix, row in [("f", 27), ("t", 36)] for i, col in enumerate("IJKLMN", 1)]
        inputs += [{"name": "w", "sheet": "DCF", "cell": "E13"}, {"name": "g", "sheet": "DCF", "cell": "E14"}]
        for variable, expected in {"w": [173925.39711201642, 144748.98660332896, 123657.40301423639],
                                   "g": [139653.07227435923, 144748.98660332896, 150481.40407149872]}.items():
            result = tools.call("sensitivity_cells", {"expression": expression, "inputs": inputs,
                                                      "vary_name": variable, "changes": [-0.1, 0, 0.1]})
            report["sensitivity"][variable] = result
            check("sensitivity_" + variable + "_complete", result.get("completeness", {}).get("complete"), True)
            check("sensitivity_" + variable + "_local_scope", result.get("local_expression_only"), True)
            check("sensitivity_" + variable + "_no_workbook_recalc", result.get("recalculated_workbook"), False)
            scenarios = result.get("scenarios", [])
            check("sensitivity_" + variable + "_scenario_count", len(scenarios), 3)
            for index, expected_value in enumerate(expected):
                actual = scenarios[index].get("result", {}).get("value") if index < len(scenarios) else None
                check("sensitivity_" + variable + "_" + str(index), actual, expected_value, numeric=True)
    finally:
        after = sha256(source)
        report["source"]["sha256_after"] = after
        report["source"]["unchanged"] = before == after
        check("source_unchanged", after, before)
    report["passed_checks"] = sum(check["passed"] for check in report["checks"])
    report["total_checks"] = len(report["checks"])
    report["status"] = "passed" if report["passed_checks"] == report["total_checks"] else "failed"
    return report


def write_report(output: Path, report: dict[str, Any], source: Path) -> None:
    """Write JSON atomically, rejecting originals, symlinks and non-JSON targets."""
    if output.suffix.lower() != ".json" or output.is_symlink():
        raise ValueError("Output must be a regular .json target")
    if output.resolve() == source.resolve() or (output.exists() and source.exists() and output.samefile(source)):
        raise ValueError("Output must not be the source workbook")
    if source.parent.resolve() == output.parent.resolve():
        raise ValueError("Write verification artifacts outside the original attachments directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, help="Write JSON outside the original attachments folder; default: stdout")
    args = parser.parse_args(argv)
    report = validate_fixture(args.source)
    if args.output:
        write_report(args.output, report, args.source)
        print(json.dumps({"status": report["status"], "passed_checks": report.get("passed_checks", 0),
                          "total_checks": report.get("total_checks", 0)}, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
