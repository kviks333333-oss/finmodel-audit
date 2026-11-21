"""Actual bounded loop behavior and source-specific feedback; no model API."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from finmodel_audit.pipeline import (run_analysis, _validation_error_identity, _stage_navigation,
                                    _pack_tool_result, _business_row_candidates, _agent_messages, AnalysisConfig)
from finmodel_audit.tools import WorkbookTools
from finmodel_audit.workbook import Workbook, Sheet, Cell
from test_pipeline import FakeTools, FakeClient, function_response, make_workbook, stage_report


class WorkflowControlTests(unittest.TestCase):
    def business_fixture(self, sheet="Source O'Brien", shift=0):
        def address(col, row):
            return f"{col}{row + shift}"
        cells = {}
        for col, row, value in [("D", 2, 2020), ("F", 2, 2025),
                                ("B", 3, "Revenue"), ("C", 3, "RUB M"), ("D", 3, 100), ("F", 3, 200),
                                ("B", 4, "Operating Profit"), ("C", 4, "RUB M"), ("D", 4, 20), ("F", 4, 40),
                                ("B", 5, "Debt"), ("C", 5, "RUB M"), ("D", 5, 50), ("F", 5, 60)]:
            ref = address(col, row)
            cells[ref] = Cell(ref, value)
        tools = WorkbookTools(Workbook(Path("unused.xlsx"), {sheet: Sheet(sheet, cells=cells)}, [], [], {}, []))
        def entry(name, arguments):
            packed, _ = _pack_tool_result(tools.call(name, arguments), 14_000)
            return {"tool": name, "arguments": arguments, "result": packed}
        rows = entry("read_rows", {"sheet": sheet, "rows": [row + shift for row in (3, 4, 5)]})
        revenue = entry("read_fact_cards", {"sheet": sheet, "cells": [address("D", 3), address("F", 3)]})
        return sheet, address, entry, [rows, revenue]

    def test_business_requires_cards_for_read_numeric_families_before_submission(self):
        available = {"read_rows", "read_fact_cards", "search_cells"}
        for sheet, shift in (("Source O'Brien", 0), ("Другой лист", 47)):
            sheet, address, entry, entries = self.business_fixture(sheet, shift)
            active = {"id": "business", "fact_selection": True, "tool_results": entries}
            name, missing, hint = _stage_navigation(active, available)
            self.assertEqual(name, "read_fact_cards")
            self.assertTrue(missing)
            self.assertIn(sheet, hint)
            for row in (4, 5):
                for col in ("D", "F"):
                    self.assertIn(address(col, row), hint)
            # Covering read rows is insufficient until cards for their sources
            # exist. No cell or year is borrowed from a neighboring header.
            entries.append(entry("read_fact_cards", {"sheet": sheet, "cells": [address(col, row) for row in (4, 5) for col in ("D", "F")]}))
            name, missing, hint = _stage_navigation(active, available)
            self.assertEqual((name, missing), ("submit_stage_report", []))
            self.assertIn("ОБА endpoints", hint)
            for card in entries[1]["result"]["metadata"]["facts"]:
                self.assertIn(card["id"], hint)

    def test_rejected_or_unsupported_cards_do_not_trigger_infinite_reads(self):
        for mode in ("unsupported", "error"):
            sheet, address, entry, entries = self.business_fixture()
            attempt = entry("read_fact_cards", {"sheet": sheet, "cells": [address(col, row) for row in (4, 5) for col in ("D", "F")]})
            if mode == "unsupported":
                for card in attempt["result"]["metadata"]["facts"]:
                    card["binding_status"] = "unsupported"
            else:
                attempt["result"] = {"metadata": {"error": "failed_read"}, "cell_data": {}}
            entries.append(attempt)
            active = {"id": "business", "fact_selection": True, "tool_results": entries}
            for _ in range(3):
                name, missing, hint = _stage_navigation(active, {"read_rows", "read_fact_cards", "search_cells"})
                self.assertEqual((name, missing), ("submit_stage_report", []), mode)
                self.assertIn("пригодные карточки не получены", hint)

    def test_business_candidates_respect_row_and_unknown_block_boundaries(self):
        entries = [{"tool": "read_cells", "arguments": {}, "result": {"cell_data": {"S": [
            ["A2", "Revenue", None, "text"], ["B2", "тыс. руб.", None, "text"], ["C2", 100, None, "number"],
            ["D2", "Custom expense", None, "text"], ["E2", 80, None, "number"],
            ["A3", "Profit", None, "text"], ["A4", "Debt", None, "text"],
            ["A5", "Period", None, "text"], ["B5", 2025, None, "number"],
        ]}}}]
        self.assertEqual(_business_row_candidates(entries), {"revenue": [("S", "C2")]})

    def test_synthesis_suggests_existing_endpoint_ids_without_expanding_selection(self):
        sheet, address, entry, entries = self.business_fixture()
        active = {"id": "business", "phase": "synthesis", "fact_selection": True,
                  "base_messages": [], "tool_results": entries}
        messages = _agent_messages(active, AnalysisConfig(), [], {})
        hint = messages[-1]["content"]
        self.assertIn("выбор не расширяется автоматически", hint)
        for card in entries[1]["result"]["metadata"]["facts"]:
            self.assertIn(card["id"], hint)
        self.assertNotIn("selection", active)
        for card in entries[1]["result"]["metadata"]["facts"]:
            card["period"] = card["period_cell"] = None
            card["binding_status"] = "partial"
        hint = _agent_messages(active, AnalysisConfig(), [], {})[-1]["content"]
        self.assertIn("Допустимых пар", hint)
        self.assertNotIn('"start_fact_id"', hint)

    def test_repeated_successful_calls_are_replayed_then_synthesis_is_forced(self):
        executed = []
        class CountingTools(FakeTools):
            def call(self, name, arguments):
                executed.append((name, arguments))
                return super().call(name, arguments)
        class RepeatingClient(FakeClient):
            def chat(self, messages, functions=None, function_call=None, **kwargs):
                self.calls.append({"function_call": function_call, "messages": messages})
                stage = functions[-1]["parameters"]["properties"]["id"]["enum"][0]
                if function_call == {"name": "submit_stage_report"}:
                    return function_response("submit_stage_report", stage_report(stage))
                return function_response("read_cells", {"sheet": "Model", "cell": "A1"})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.xlsx"
            destination = Path(directory) / "result"
            make_workbook(source)
            module = types.ModuleType("finmodel_audit.tools")
            module.WorkbookTools = CountingTools
            with patch.dict(sys.modules, {"finmodel_audit.tools": module}):
                client = RepeatingClient()
                result = run_analysis(source, destination, client=client)
            state = json.loads(next((destination / "checkpoints").glob("*/state.json")).read_text())
            self.assertEqual(len(executed), 4)  # One real read per stage.
            self.assertEqual(sum(bool(entry.get("replayed")) for entry in state["tool_log"]), 8)
            self.assertEqual(len(client.calls), 16)  # Includes attempts; no hidden free retries.
            self.assertTrue(all(stage["completion"] == "submitted" for stage in result["stages"]))
            self.assertEqual(result["calculations"], [])

    def test_generic_validation_error_count_tracks_the_actual_sources(self):
        issue = {"code": "fact_claim_required", "observation": 1, "message": "same generic message"}
        def payload(cell, title="first"):
            return {"observations": [{"title": title, "evidence": [{"sheet": "M", "cell": cell}]}]}
        self.assertNotEqual(_validation_error_identity(issue, payload("A1")), _validation_error_identity(issue, payload("B1")))
        self.assertEqual(_validation_error_identity(issue, payload("A1")), _validation_error_identity(issue, payload("A1", "rewritten")))

    def test_hidden_business_context_is_explicit_opt_in_in_starting_map(self):
        cells = {cell.address: cell for cell in [Cell("A1", "Revenue"), Cell("B1", "RUB M"), Cell("C1", 100)]}
        workbook = Workbook(Path("unused.xlsx"), {
            "Main": Sheet("Main", cells=cells),
            "Unrelated": Sheet("Unrelated", state="hidden", cells=cells),
        }, [], [], {}, [])
        tools = WorkbookTools(workbook)
        default = tools.call("financial_map", {"stage": "business"})
        self.assertEqual(default["excluded_hidden_sheets"], ["Unrelated"])
        self.assertEqual({item["sheet"] for item in default["candidates"]}, {"Main"})
        explicit = tools.call("financial_map", {"stage": "business", "include_hidden": True})
        self.assertIn("Unrelated", {item["sheet"] for item in explicit["candidates"]})


if __name__ == "__main__":
    unittest.main()
