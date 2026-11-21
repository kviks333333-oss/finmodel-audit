"""Actual-envelope registration cannot borrow unseen or clipped source rows."""
from copy import deepcopy
import unittest

from finmodel_audit.agent_evidence import register_sent_evidence
from finmodel_audit.pipeline import _data_message, _evidence_memo, _pack_tool_result


class SentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.finding = {"id": "candidate-a", "status": "candidate", "scope": "financial",
                        "title": "Test", "evidence": [{"sheet": "S", "cell": "B2"}, {"sheet": "S", "cell": "C2"}]}
        self.packed = {"metadata": {"validation_findings": [self.finding]},
                       "cell_data": {"S": [["B2", 30, "C2*2", "formula_number"], ["C2", 15, None, "number"]]}}
        self.entry = {"tool": "get_validation_findings", "arguments": {}, "result": self.packed, "automatic": True}

    def register(self, envelope, entries=None):
        registries = [{}, {}, {}]
        register_sent_evidence([{"role": "user", "content": _data_message(envelope)}],
                               entries or [self.entry], *registries)
        return registries

    def test_prepared_sources_not_seen_or_missing_target_do_not_register(self):
        self.assertEqual(self.register({})[0], {})
        clipped = deepcopy(self.packed)
        clipped["cell_data"]["S"].pop(0)
        self.assertEqual(self.register(clipped)[0], {})
        altered = deepcopy(self.packed)
        altered["cell_data"]["S"][0][2] = "C2"
        self.assertEqual(self.register(altered)[0], {})

    def test_complete_finding_keeps_all_source_formulas(self):
        registry = self.register(self.packed)[0]
        self.assertEqual(registry["candidate-a"]["cell_evidence"][0]["formula"], "C2*2")
        self.assertEqual(registry["candidate-a"]["finding"], self.finding)
        registry["candidate-a"]["finding"]["title"] = "changed"
        self.assertEqual(self.finding["title"], "Test")

    def test_forged_or_clipped_finding_is_not_registered(self):
        tampered = deepcopy(self.packed)
        tampered["metadata"]["validation_findings"][0]["status"] = "observation"
        self.assertEqual(self.register(tampered)[0], {})
        self.packed["cell_text_limits"] = [{"sheet": "S", "cell": "B2", "formula_truncated": True}]
        self.assertEqual(self.register(self.packed)[0], {})

    def test_conflicting_delivered_finding_id_cannot_reuse_old_proof(self):
        registry = self.register(self.packed)[0]
        altered = deepcopy(self.packed)
        altered["metadata"]["validation_findings"][0]["detail"] = "Different proof"
        entries = [{**self.entry, "result": altered}]
        register_sent_evidence([{"role": "user", "content": _data_message(altered)}], entries, registry, {}, {})
        self.assertEqual(registry["candidate-a"], {"conflict": True})

    def test_blank_reference_is_delivered_as_blank_not_numeric_zero(self):
        question = {"id": "q", "evidence": [{"sheet": "S", "cell": "B2"}, {"sheet": "S", "cell": "Z8"}]}
        raw = {"investigation_question": question, "cell_evidence": [
            {"sheet": "S", "cell": "B2", "value": 0, "formula": "Z8", "present": True},
            {"sheet": "S", "cell": "Z8", "value": None, "formula": None, "present": False}]}
        packed, _ = _pack_tool_result(raw, 14000)
        entry = {"tool": "inspect_question", "arguments": {"question_id": "q"}, "result": packed}
        memo = _evidence_memo([entry], 14000)
        questions = self.register(memo, [entry])[2]
        self.assertIn("q", questions)
        self.assertEqual(questions["q"]["cell_data"]["S"][1], ["Z8", None, None, "blank"])
        entry["automatic"] = True
        self.assertEqual(self.register(memo, [entry])[2], {})

    def test_question_is_omitted_atomically_when_sources_exceed_budget(self):
        raw = {"investigation_question": {"id": "q", "evidence": [{"sheet": "S", "cell": "A1"}]},
               "cell_evidence": [{"sheet": "S", "cell": "A1", "value": "x" * 2000, "present": True}]}
        packed, shown = _pack_tool_result(raw, 600)
        self.assertFalse(shown)
        self.assertNotIn("investigation_question", packed.get("metadata", {}))

    def test_bounded_trace_accepts_normalized_arguments_but_retains_scope_limit(self):
        metadata = {"evidence_id": "trace-id", "root": {"sheet": "S", "cell": "B2"},
                    "direction": "precedents", "edges": [], "completeness": {"complete": False, "reasons": ["depth_limit"]}}
        packed = {"metadata": metadata, "cell_columns": ["cell", "saved_value", "formula", "kind"],
                  "cell_data": {"S": [["B2", 30, "=C2*2", "formula_number"]]}, "truncated": True}
        entry = {"tool": "trace_precedents", "arguments": {"sheet": "S", "cell": "$b$2"}, "result": packed}
        registered = self.register(packed, [entry])[1]
        self.assertIn("trace-id", registered)
        self.assertFalse(registered["trace-id"]["result"]["metadata"]["completeness"]["complete"])
        packed["metadata_truncated"] = True
        self.assertFalse(self.register(packed, [entry])[1])


if __name__ == "__main__":
    unittest.main()
