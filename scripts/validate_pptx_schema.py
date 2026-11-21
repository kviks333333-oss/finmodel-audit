#!/usr/bin/env python3
"""Validate PPTX/embedded XLSX XML against externally supplied ECMA-376 XSDs.

This independent QA command does not import the renderer, modify the package,
download schemas, or contact an API. It prefers lxml; xmllint is a fallback.
Supply the unmodified Transitional XSD directory from ECMA-376 Part 4. The
schemas are not vendored. OPC/core-properties parts are recorded separately:
their schemas are in Part 2, so Part 4 validation cannot certify those parts.
This command validates raw XML and does not implement Part 3 markup-compatibility
preprocessing; diagnostics for MC/extension attributes need separate review.

Example:
  python scripts/validate_pptx_schema.py deck.pptx --schemas /tmp/ecma/xsd \\
      --output outputs/schema-result.json
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

SCHEMAS = {
    "http://schemas.openxmlformats.org/presentationml/2006/main": "pml.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/main": "dml-main.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/chart": "dml-chart.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing": "dml-chartDrawing.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing": "dml-spreadsheetDrawing.xsd",
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main": "sml.xsd",
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties": "shared-documentPropertiesExtended.xsd",
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties": "shared-documentPropertiesCustom.xsd",
}
OPC_NAMESPACES = {
    "http://schemas.openxmlformats.org/package/2006/content-types",
    "http://schemas.openxmlformats.org/package/2006/relationships",
    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
}
MAX_ENTRIES = 10000
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SchemaValidator:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        self.compiled = {}
        try:
            from lxml import etree
        except ImportError:
            etree = None
        self.etree = etree
        self.xmllint = shutil.which("xmllint")
        if etree is None and not self.xmllint:
            raise RuntimeError("Install lxml in the QA environment or provide xmllint on PATH")
        self.backend = "lxml" if etree is not None else "xmllint"

    def validate(self, payload, schema_name):
        schema_path = self.directory / schema_name
        if not schema_path.is_file():
            raise FileNotFoundError(f"Required external schema is missing: {schema_path}")
        if self.etree is not None:
            etree = self.etree
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            if schema_name not in self.compiled:
                self.compiled[schema_name] = etree.XMLSchema(etree.parse(str(schema_path), parser))
            validator = self.compiled[schema_name]
            tree = etree.fromstring(payload, parser)
            valid = validator.validate(tree)
            return valid, [entry.message for entry in validator.error_log]
        result = subprocess.run(
            [self.xmllint, "--nonet", "--noout", "--schema", str(schema_path), "-"],
            input=payload, capture_output=True, timeout=30, check=False,
        )
        errors = [line.split("validity error : ", 1)[-1]
                  for line in result.stderr.decode("utf-8", "replace").splitlines()
                  if "validity error" in line or "Schemas parser error" in line or "parser error" in line]
        if result.returncode and not errors:
            errors = [result.stderr.decode("utf-8", "replace").strip() or f"xmllint exit {result.returncode}"]
        return result.returncode == 0, errors


def validate_package(source: Path, schema_directory: Path):
    source = source.resolve(strict=True)
    before = source.read_bytes()
    validator = SchemaValidator(schema_directory)
    records, omitted, packages = [], [], []
    total_bytes = 0
    total_entries = 0

    def scan(data, owner, depth=0):
        nonlocal total_bytes, total_entries
        if depth > 2:
            raise ValueError("Embedded package depth exceeds QA bound")
        packages.append({"path": owner, "sha256": sha(data)})
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate ZIP members in {owner}")
            for member in archive.infolist():
                if member.is_dir():
                    continue
                total_entries += 1
                total_bytes += member.file_size
                if (total_entries > MAX_ENTRIES or member.file_size > MAX_PART_BYTES
                        or total_bytes > MAX_UNCOMPRESSED_BYTES):
                    raise ValueError("Package exceeds bounded independent QA size")
                payload = archive.read(member)
                path = owner + "::" + member.filename
                if member.filename.endswith((".xlsx", ".pptx")):
                    scan(payload, path, depth + 1)
                    continue
                if not member.filename.endswith((".xml", ".rels")):
                    continue
                if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
                    raise ValueError(f"DTD/entity declarations are not allowed: {path}")
                element = ET.fromstring(payload)
                namespace = element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""
                if namespace in OPC_NAMESPACES:
                    omitted.append({"part": path, "reason": "OPC/core-properties need ECMA-376 Part 2 schemas"})
                    continue
                schema = SCHEMAS.get(namespace)
                if schema:
                    valid, errors = validator.validate(payload, schema)
                else:
                    valid, errors = False, [f"No declared validation schema for root {element.tag}"]
                records.append({"part": path, "schema": schema, "passed": valid, "errors": errors})

    scan(before, source.name)
    counts = Counter(error for part in records for error in part["errors"])
    unchanged = sha(source.read_bytes()) == sha(before)
    return {
        "source": str(source), "source_sha256": sha(before), "source_unchanged": unchanged,
        "schema_directory": str(validator.directory), "backend": validator.backend,
        "schema_sha256": {p.name: sha(p.read_bytes()) for p in sorted(validator.directory.glob("*.xsd"))},
        "parts": records, "excluded_parts": omitted, "packages": packages,
        "summary": {"validated_parts": len(records), "invalid_parts": sum(not p["passed"] for p in records),
                    "validation_errors": sum(counts.values()), "excluded_opc_parts": len(omitted)},
        "distinct_errors": [{"message": message, "count": count} for message, count in counts.items()],
        "limitations": ["ECMA-376 Part 4 XML-schema validation, not a native PowerPoint opening test.",
                        "OPC/core-properties, package relationships and application semantics need separate checks.",
                        "Raw XML is checked without Part 3 markup-compatibility preprocessing; MC/extension diagnostics require separate interpretation.",
                        "No LLM, financial-model recalculation or source write is performed."],
        "passed": bool(records) and unchanged and all(p["passed"] for p in records),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--schemas", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        parser.error("--output must be a new path; prior QA records are immutable")
    try:
        result = validate_package(args.source, args.schemas)
    except (ValueError, OSError, RuntimeError, BadZipFile, ET.ParseError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"Schema validation could not run: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": result["passed"], **result["summary"]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
