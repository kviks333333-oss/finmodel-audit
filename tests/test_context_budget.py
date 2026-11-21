"""Tool descriptions must leave room for the current complete source proof."""
import unittest

from finmodel_audit.pipeline import AnalysisConfig, _agent_messages, _collection_schemas, _data_message, _json, _report_schema
from finmodel_audit.tools import WorkbookTools
from finmodel_audit.agent_evidence import register_sent_evidence


class CollectionContextTests(unittest.TestCase):
    def test_full_impact_tail_survives_stage_menu_when_all_tools_would_overflow(self):
        scenario = {"id": "impact-one", "target": {"sheet": "S", "cell": "C1"},
                    "evidence": [{"sheet": "S", "cell": "C1"}], "assumption": "Длинное условие " * 450}
        packed = {"metadata": {"impact_scenarios": [scenario]},
                  "cell_data": {"S": [["C1", 25, "=A1+B1", "formula_number"]]}}
        entry = {"tool": "calculate_impact", "arguments": {"scenario_id": "impact-one"}, "result": packed,
                 "impact_scenarios_raw": [scenario]}
        pair = [{"role": "assistant", "function_call": {"name": "calculate_impact", "arguments": entry["arguments"]}},
                {"role": "function", "name": "calculate_impact", "content": _data_message(packed)}]
        active = {"id": "audit", "phase": "collection", "base_messages": [{"role": "system", "content": "Evidence contract. " * 450}],
                  "tool_results": [entry], "latest_pair": pair, "latest_pair_has_data": True}
        config = AnalysisConfig()
        schema = _report_schema("audit", False, True)
        full = WorkbookTools.schemas() + [schema]
        oversized = _agent_messages(active, config, full, {})
        self.assertGreater(len(_json({"messages": oversized, "functions": full})), config.max_context_chars)
        limited = _collection_schemas(active, WorkbookTools.schemas(), schema)
        messages = _agent_messages(active, config, limited, {})
        self.assertLessEqual(len(_json({"messages": messages, "functions": limited})), config.max_context_chars)
        self.assertEqual(messages[-2:], pair)
        registry = {}
        register_sent_evidence(messages, [entry], {}, {}, {}, registry)
        self.assertEqual(registry["impact-one"]["scenario"], scenario)
        self.assertIn("trace_precedents", {item["name"] for item in limited})
        self.assertNotIn("read_fact_cards", {item["name"] for item in limited})

    def test_stage_choices_preserve_latest_function_definition(self):
        schemas = WorkbookTools.schemas()
        active = {"id": "assumptions", "latest_pair": [{"function_call": {"name": "sensitivity_cells"}}]}
        names = {item["name"] for item in _collection_schemas(active, schemas, _report_schema("assumptions", True))}
        self.assertIn("inspect_question", names)
        self.assertIn("sensitivity_cells", names)
        self.assertIn("submit_stage_report", names)
        self.assertNotIn("calculate_impact", names)


if __name__ == "__main__":
    unittest.main()
