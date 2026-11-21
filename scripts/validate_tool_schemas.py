"""Send only public tool descriptions to GigaChat's function validator.

Run explicitly before a release, not on every financial-model job:
PYTHONPATH=src .venv/bin/python scripts/validate_tool_schemas.py
No workbook or user report is loaded. Responses are limited to schema diagnostics.
"""

import argparse
import json
from pathlib import Path

from finmodel_audit.gigachat import GigaChatClient, GigaChatError, load_env_file
from finmodel_audit.tools import WorkbookTools
from finmodel_audit.pipeline import _report_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/tool_schema_validation.json"))
    parser.add_argument("--tools", nargs="+", help="Optional exact tool names to check, including submit_stage_report")
    parser.add_argument("--stage", choices=("audit", "assumptions", "quality", "business"), default="business",
                        help="Production submit contract to validate; sends schemas only.")
    args = parser.parse_args()
    # Business includes the optional fact-claim fields, covering the largest
    # submit contract without sending any actual workbook or stage output.
    schemas = WorkbookTools.schemas() + [_report_schema(args.stage, args.stage in {"assumptions", "business"},
                                                       args.stage in {"audit", "quality"})]
    if args.tools:
        unknown = set(args.tools) - {schema["name"] for schema in schemas}
        if unknown:
            parser.error("Unknown tool name")
        schemas = [schema for schema in schemas if schema["name"] in args.tools]
    load_env_file()
    client = GigaChatClient()
    results = []
    for schema in schemas:
        try:
            raw = client.validate_function(schema)
        except GigaChatError as error:
            # GigaChatError is sanitized by the transport; no raw HTTP body.
            results.append({"name": schema["name"], "transport_error": str(error)})
            break
        result = {"name": schema["name"]}
        for field in ("status", "message", "json_ai_rules_version", "errors", "warnings"):
            if field in raw:
                result[field] = raw[field]
        results.append(result)
        print(schema["name"] + ": " + str(result.get("message", "no validation result")), flush=True)
    passed = len(results) == len(schemas) and all(
        item.get("message") == "Function is valid" and not item.get("errors") for item in results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"valid": passed, "functions": results}, ensure_ascii=False, indent=2) + "\n")
    print("Schema format only; no financial conclusions were validated.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
