"""Tests for invoice.schema.json — the formal Foken Invoice contract.

Checks the schema file structurally against what gate.py enforces, and runs
a minimal stdlib JSON-Schema (draft-07 subset) checker over representative
invoices. No third-party jsonschema dependency.

Subset covered: type, required, properties, items, additionalProperties,
pattern, minLength, minimum, exclusiveMinimum, enum.

Run with:  python3 -m unittest discover foken -v
Stdlib only (unittest, json, re, os).
"""

import json
import os
import re
import unittest

import gate
from test_gate import base_invoice, build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "invoice.schema.json")

with open(SCHEMA_PATH, encoding="utf-8") as fh:
    SCHEMA = json.load(fh)


# --------------------------------------------------------------------------
# Minimal JSON-Schema (draft-07 subset) validator
# --------------------------------------------------------------------------

def _matches_type(value, schema_type):
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _check(value, schema, path, errors):
    if not isinstance(schema, dict):
        return
    schema_type = schema.get("type")
    if schema_type is not None and not _matches_type(value, schema_type):
        errors.append(f"{path}: expected {schema_type}, "
                      f"got {type(value).__name__}")
    elif schema_type in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} not > "
                          f"exclusiveMinimum {schema['exclusiveMinimum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: length {len(value)} < "
                          f"minLength {schema['minLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern "
                          f"{schema['pattern']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, dict):
        for prop in schema.get("required", []):
            if prop not in value:
                errors.append(f"{path}.{prop}: missing required property")
        properties = schema.get("properties", {})
        for key, sub in value.items():
            sub_schema = properties.get(key)
            if sub_schema is not None:
                _check(sub, sub_schema, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: additional property not allowed")
    if isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            for i, item in enumerate(value):
                _check(item, items, f"{path}[{i}]", errors)


def schema_errors(instance) -> list:
    """Return the list of schema violations for `instance` (empty = valid)."""
    errors = []
    _check(instance, SCHEMA, "$", errors)
    return errors


# --------------------------------------------------------------------------
# Structural consistency with gate.py
# --------------------------------------------------------------------------

class TestSchemaStructure(unittest.TestCase):

    def test_schema_is_draft07(self):
        self.assertEqual(SCHEMA.get("$schema"),
                         "http://json-schema.org/draft-07/schema#")

    def test_top_level_required_matches_gate(self):
        # netUtility is intentionally excluded from the schema's top-level
        # required list, exactly like gate.REQUIRED_TOP: a missing netUtility
        # is a REVIEW in gate.py, never a FAIL.
        self.assertEqual(set(SCHEMA["required"]), set(gate.REQUIRED_TOP))

    def test_nested_required_covers_gate(self):
        for key, subkeys in gate.REQUIRED_NESTED.items():
            with self.subTest(key=key):
                props = SCHEMA["properties"][key]
                schema_required = set(props.get("required", []))
                self.assertTrue(set(subkeys) <= schema_required,
                                f"{key} schema required {schema_required} "
                                f"missing {set(subkeys) - schema_required}")

    def test_ip_assignment_requires_statement_and_signature(self):
        # Deliberate difference from gate.REQUIRED_NESTED, documented in the
        # schema description + README: the IP hard gate makes an invoice
        # without these un-passable, so the schema requires them.
        ip_required = set(SCHEMA["properties"]["ipAssignment"]["required"])
        self.assertIn("statement", ip_required)
        self.assertIn("signature", ip_required)

    def test_net_utility_optional_but_self_validating(self):
        nu = SCHEMA["properties"]["netUtility"]
        self.assertNotIn("netUtility", SCHEMA["required"])
        self.assertEqual(set(nu["required"]), set(gate.NET_UTILITY_FIELDS))
        for field in gate.NET_UTILITY_FIELDS:
            with self.subTest(field=field):
                spec = nu["properties"][field]
                self.assertEqual(spec["type"], "number")
                self.assertEqual(spec["minimum"], 0)

    def test_contributor_pattern(self):
        self.assertEqual(SCHEMA["properties"]["contributor"]["pattern"],
                         "^0x[0-9a-fA-F]{40}$")

    def test_provenance_hash_pattern(self):
        self.assertEqual(
            SCHEMA["properties"]["artifact"]["properties"]["provenanceHash"]
            ["pattern"], "^sha256://")

    def test_signature_pattern(self):
        self.assertEqual(
            SCHEMA["properties"]["ipAssignment"]["properties"]["signature"]
            ["pattern"], "^0x[0-9a-fA-F]+$")

    def test_fokens_positive_integer(self):
        spec = SCHEMA["properties"]["valuation"]["properties"] \
            ["selfAssessedFokens"]
        self.assertEqual(spec["type"], "integer")
        self.assertEqual(spec["exclusiveMinimum"], 0)


# --------------------------------------------------------------------------
# Behavioral validation of representative invoices
# --------------------------------------------------------------------------

class TestSchemaValidation(unittest.TestCase):

    def test_valid_invoice_has_no_errors(self):
        self.assertEqual(schema_errors(base_invoice()), [])

    def test_invoice_without_net_utility_is_valid(self):
        # gate.py: missing netUtility -> REVIEW, not FAIL; the schema agrees.
        self.assertEqual(schema_errors(build({"netUtility": None})), [])

    def test_missing_top_level_field_fails(self):
        self.assertTrue(schema_errors(build({"dedup": None})))

    def test_invalid_contributor_fails(self):
        self.assertTrue(schema_errors(build({"contributor": "0x1234"})))

    def test_bad_provenance_fails(self):
        self.assertTrue(
            schema_errors(build({"artifact.provenanceHash": "ipfs://abc"})))

    def test_bad_signature_fails(self):
        self.assertTrue(
            schema_errors(build({"ipAssignment.signature": "deadbeef"})))

    def test_statement_without_irrevocable_fails(self):
        self.assertTrue(
            schema_errors(build({"ipAssignment.statement": "IRREVOCABLE LICENSE"})))

    def test_non_positive_fokens_fail(self):
        for bad in (0, -5):
            with self.subTest(bad=bad):
                self.assertTrue(schema_errors(
                    build({"valuation.selfAssessedFokens": bad})))

    def test_boolean_fokens_fail(self):
        self.assertTrue(schema_errors(
            build({"valuation.selfAssessedFokens": True})))

    def test_float_version_fails(self):
        self.assertTrue(schema_errors(build({"version": 1.5})))

    def test_empty_invoice_id_fails(self):
        self.assertTrue(schema_errors(build({"invoiceId": ""})))

    def test_negative_net_utility_fails(self):
        self.assertTrue(schema_errors(build({"netUtility.workAddedHours": -1})))

    def test_evidence_with_non_string_item_fails(self):
        invoice = build({"valuation.evidence": ["https://ok", 42]})
        self.assertTrue(schema_errors(invoice))

    def test_missing_nested_field_fails(self):
        self.assertTrue(schema_errors(build({"work.commit": None})))


if __name__ == "__main__":
    unittest.main()
