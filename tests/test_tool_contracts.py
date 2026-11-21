import json
import unittest

from finmodel_audit.pipeline import _data_message, _pack_tool_result
from finmodel_audit.tool_contracts import packed_return_parameters


def check_shape(value, schema):
    """Check the schema subset used by this application, without an API call."""
    expected = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if expected in kinds:
        assert isinstance(value, kinds[expected]), (expected, type(value).__name__)
        if expected == "integer":
            assert not isinstance(value, bool)
    if isinstance(value, dict):
        assert set(schema.get("required", [])) <= set(value)
        props = schema.get("properties", {})
        for key, child in value.items():
            child_schema = props.get(key, schema.get("additionalProperties", {}))
            if isinstance(child_schema, dict):
                check_shape(child, child_schema)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for item in value:
            check_shape(item, schema["items"])


class PackedContractTests(unittest.TestCase):
    def test_schema_matches_actual_wire_envelopes_for_data_errors_and_limits(self):
        examples = [
            {"cells": [{"sheet": "Demo", "cell": "A1", "value": 42, "formula": None},
                       {"sheet": "Demo", "cell": "B1", "value": "Revenue"}], "truncated": False},
            {"error": {"code": "invalid_tool_request", "message": "Unknown worksheet"}},
            {"cells": [{"sheet": "Demo", "cell": "A1", "value": "x" * 2000}], "truncated": True},
        ]
        schema = packed_return_parameters()
        for raw in examples:
            packed, _ = _pack_tool_result(raw, 1200)
            envelope = json.loads(_data_message(packed))
            check_shape(envelope, schema)
        # Mutating one tool's schema must not affect another tool.
        schema["required"].append("bad")
        self.assertNotIn("bad", packed_return_parameters()["required"])
