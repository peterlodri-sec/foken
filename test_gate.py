"""Tests for gate.py — the Foken proof-of-contribution valuation gate.

Run with:  python3 -m unittest test_gate -v   (from the foken/ directory)
Stdlib only (unittest, json, subprocess, tempfile).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import gate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GATE_PY = os.path.join(BASE_DIR, "gate.py")
ZERO_ADDR = "0x" + "0" * 40


def base_invoice():
    """A structurally complete, gating-to-PASS invoice."""
    return {
        "invoiceId": "0190f4c0-2f2a-7000-8000-000000000000",
        "version": 1,
        "contributor": ZERO_ADDR,
        "work": {"title": "docs", "description": "manual", "repo": "repo",
                 "commit": "abc123"},
        "artifact": {"provenanceHash": "sha256://abc123",
                     "artifactUri": "ipfs://QmX"},
        "valuation": {"selfAssessedFokens": 5, "basis": "USD",
                      "rationale": "r", "evidence": ["https://example.org"]},
        "ipAssignment": {"statement": "IRREVOCABLE ASSIGNMENT TO UNIVERSAL TREASURY",
                         "license": "MIT", "signedAt": "2026-08-15",
                         "signature": "0xabcd"},
        "netUtility": {"futureWorkAvoidedHours": 100, "workAddedHours": 10,
                       "maintenanceDebtHours": 5},
        "dedup": {"claimedNovelty": "", "relatedInvoiceIds": []},
    }


def build(overrides):
    """Deep-override helper. Keys are dotted paths ('ipAssignment.signature');
    a None value deletes the key."""
    invoice = base_invoice()
    for path, value in overrides.items():
        parts = path.split(".")
        node = invoice
        for part in parts[:-1]:
            node = node[part]
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    return invoice


def history_with_fokens(fokens):
    """A history list of minimal invoices carrying the given selfAssessedFokens."""
    return [{"valuation": {"selfAssessedFokens": fk}} for fk in fokens]


# --------------------------------------------------------------------------
# Unit tests on gate.evaluate()
# --------------------------------------------------------------------------

class TestEvaluate(unittest.TestCase):

    def test_pass_positive_net_utility(self):
        result = gate.evaluate(base_invoice())
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["invoiceId"], base_invoice()["invoiceId"])
        self.assertIn("net utility positive", result["reason"])
        # 100 - (10 + 1.5 * 5) = 82.5
        self.assertEqual(result["netUtility"], 82.5)

    def test_result_dict_shape(self):
        result = gate.evaluate(base_invoice())
        self.assertEqual(set(result), {"invoiceId", "verdict", "reason",
                                       "netUtility"})

    def test_fail_net_negative_moving_rocks(self):
        invoice = build({"netUtility": {"futureWorkAvoidedHours": 0,
                                        "workAddedHours": 40,
                                        "maintenanceDebtHours": 60}})
        result = gate.evaluate(invoice)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("net-negative utility", result["reason"])
        # net = 0 - (40 + 1.5*60) = -130
        self.assertEqual(result["netUtility"], -130.0)

    def test_fail_zero_net_is_not_pass(self):
        # 10 - (4 + 1.5*4) = 0 -> FAIL (net <= 0)
        invoice = build({"netUtility": {"futureWorkAvoidedHours": 10,
                                        "workAddedHours": 4,
                                        "maintenanceDebtHours": 4}})
        self.assertEqual(gate.evaluate(invoice)["verdict"], "FAIL")

    def test_fail_missing_ip_signature(self):
        result = gate.evaluate(build({"ipAssignment.signature": None}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["reason"],
                         "missing/unsigned IP assignment — no IP, no mint")

    def test_fail_non_hex_signature(self):
        result = gate.evaluate(build({"ipAssignment.signature": "deadbeef"}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("IP assignment", result["reason"])

    def test_fail_statement_without_irrevocable_phrase(self):
        result = gate.evaluate(build({"ipAssignment.statement": "IRREVOCABLE LICENSE"}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("IP assignment", result["reason"])

    def test_fail_invalid_contributor_address(self):
        result = gate.evaluate(build({"contributor": "0x1234"}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("contributor", result["reason"])

    def test_pass_lowercase_hex_contributor(self):
        addr = "0x" + "a" * 40
        self.assertEqual(gate.evaluate(build({"contributor": addr}))["verdict"],
                         "PASS")

    def test_fail_provenance_not_sha256(self):
        result = gate.evaluate(build({"artifact.provenanceHash": "ipfs://abc"}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("provenance", result["reason"])

    def test_fail_missing_provenance(self):
        result = gate.evaluate(build({"artifact.provenanceHash": None}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("provenance", result["reason"])

    def test_fail_self_assessed_fokens_not_positive_integer(self):
        for bad in (0, -5, "5", True):
            with self.subTest(bad=bad):
                result = gate.evaluate(
                    build({"valuation.selfAssessedFokens": bad}))
                self.assertEqual(result["verdict"], "FAIL")
                self.assertIn("selfAssessedFokens", result["reason"])

    def test_review_missing_net_utility(self):
        result = gate.evaluate(build({"netUtility": None}))
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("netUtility", result["reason"])
        self.assertIsNone(result["netUtility"])

    def test_review_negative_net_utility_field(self):
        result = gate.evaluate(build({"netUtility.workAddedHours": -1}))
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("netUtility.workAddedHours", result["reason"])

    def test_review_missing_net_utility_field(self):
        result = gate.evaluate(
            build({"netUtility.maintenanceDebtHours": None}))
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("netUtility.maintenanceDebtHours", result["reason"])

    def test_review_valuation_deviates_over_2x_median(self):
        # median of [1]*10 is 1; 5 > 2*1 -> REVIEW
        result = gate.evaluate(base_invoice(), history=history_with_fokens([1] * 10))
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("deviates >2x median", result["reason"])
        self.assertEqual(result["netUtility"], 82.5)

    def test_pass_valuation_within_2x_median(self):
        # median 2 -> threshold 4; selfAssessedFokens 3 -> PASS
        invoice = build({"valuation.selfAssessedFokens": 3})
        result = gate.evaluate(invoice, history=history_with_fokens([2] * 10))
        self.assertEqual(result["verdict"], "PASS")

    def test_review_uses_only_last_10_history_invoices(self):
        # Last 10 have median 1 (5 > 2 -> REVIEW); the first 2 outliers would
        # pull the all-12 median up to 50.5 (5 not > 101 -> PASS).
        history = history_with_fokens([100, 100] + [1] * 9 + [100])
        result = gate.evaluate(base_invoice(), history=history)
        self.assertEqual(result["verdict"], "REVIEW")

    def test_no_history_skips_deviation_check(self):
        self.assertEqual(gate.evaluate(base_invoice())["verdict"], "PASS")

    def test_net_negative_beats_valuation_review(self):
        # Documented precedence: a rejected (net <= 0) invoice does not go to
        # an extra review round, even if the valuation deviates.
        invoice = build({"valuation": {"selfAssessedFokens": 500, "basis": "USD",
                                       "rationale": "r", "evidence": []},
                         "netUtility": {"futureWorkAvoidedHours": 0,
                                        "workAddedHours": 40,
                                        "maintenanceDebtHours": 60}})
        result = gate.evaluate(invoice, history=history_with_fokens([1] * 10))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("net-negative utility", result["reason"])

    def test_malformed_invoice_fails(self):
        result = gate.evaluate(build({"work": None}))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("malformed invoice", result["reason"])

    def test_malformed_non_object_fails(self):
        result = gate.evaluate(["not", "an", "invoice"])
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("malformed invoice", result["reason"])


# --------------------------------------------------------------------------
# CLI tests: exit codes 0/1/2 and input/output plumbing
# --------------------------------------------------------------------------

def run_cli(invoice, history=None, human=False):
    """Run gate.py as a subprocess; returns the CompletedProcess."""
    cmd = [sys.executable, GATE_PY]
    with tempfile.TemporaryDirectory() as tmp:
        invoice_path = os.path.join(tmp, "invoice.json")
        with open(invoice_path, "w", encoding="utf-8") as fh:
            json.dump(invoice, fh)
        cmd.append(invoice_path)
        if history is not None:
            history_path = os.path.join(tmp, "history.json")
            with open(history_path, "w", encoding="utf-8") as fh:
                json.dump(history, fh)
            cmd += ["--history", history_path]
        if human:
            cmd.append("--human")
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=BASE_DIR)


class TestCLI(unittest.TestCase):

    def test_exit_0_pass(self):
        proc = run_cli(base_invoice())
        self.assertEqual(proc.returncode, 0)
        result = json.loads(proc.stdout)
        self.assertEqual(result["verdict"], "PASS")

    def test_exit_1_net_negative_fail(self):
        invoice = build({"netUtility": {"futureWorkAvoidedHours": 0,
                                        "workAddedHours": 40,
                                        "maintenanceDebtHours": 60}})
        proc = run_cli(invoice)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "FAIL")

    def test_exit_1_missing_ip_assignment(self):
        proc = run_cli(build({"ipAssignment.signature": None}))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "FAIL")

    def test_exit_1_invalid_contributor(self):
        proc = run_cli(build({"contributor": "0x1234"}))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "FAIL")

    def test_exit_2_review_missing_net_utility(self):
        proc = run_cli(build({"netUtility": None}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "REVIEW")

    def test_exit_2_review_valuation_deviation(self):
        proc = run_cli(base_invoice(), history=history_with_fokens([1] * 10))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "REVIEW")

    def test_stdin_dash(self):
        proc = subprocess.run([sys.executable, GATE_PY, "-"],
                              input=json.dumps(base_invoice()), text=True,
                              capture_output=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "PASS")

    def test_json_flag_with_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inv.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(base_invoice(), fh)
            proc = subprocess.run([sys.executable, GATE_PY, "--json", path],
                                  capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "PASS")

    def test_json_flag_with_stdin(self):
        proc = subprocess.run([sys.executable, GATE_PY, "--json", "-"],
                              input=json.dumps(base_invoice()), text=True,
                              capture_output=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "PASS")

    def test_human_flag(self):
        proc = run_cli(base_invoice(), human=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("verdict", proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_missing_invoice_argument_exits_2(self):
        proc = subprocess.run([sys.executable, GATE_PY],
                              capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 2)

    def test_unreadable_file_exits_1(self):
        proc = subprocess.run([sys.executable, GATE_PY, "/nonexistent/x.json"],
                              capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
