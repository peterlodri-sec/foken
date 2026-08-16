"""Tests for tally.py — the off-chain consensus service.

Run with:  python3 -m unittest discover foken -v
Stdlib only (unittest, json, subprocess, tempfile).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import gate
import tally
from test_gate import ZERO_ADDR, base_invoice, build  # reuse the builders

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TALLY_PY = os.path.join(BASE_DIR, "tally.py")


def reviewer_set(n=5):
    return ["0x" + str(i) * 40 for i in range(1, n + 1)]


def vote(addr, invoice=None, signature="0xabc"):
    return {"invoice": base_invoice() if invoice is None else invoice,
            "reviewerAddress": addr, "signature": signature}


def votes_for(addrs):
    return [vote(a) for a in addrs]


class TestRequiredApprovals(unittest.TestCase):

    def test_k_formula(self):
        for n, k in ((5, 3), (6, 4), (7, 4), (9, 5), (1, 1)):
            with self.subTest(n=n):
                self.assertEqual(tally.required_approvals(n), k)

    def test_k_matches_quorum_threshold(self):
        # k = floor(n/2)+1 is exactly the smallest count satisfying the
        # >50% rule: k-1 fails quorum, k reaches it.
        for n in (5, 6, 7, 9, 10):
            k = tally.required_approvals(n)
            self.assertFalse(gate.quorum_reached(k - 1, n))
            self.assertTrue(gate.quorum_reached(k, n))


PAYLOAD_KEYS = {"invoiceId", "recipient", "amount", "reviewRound",
                "approvalCount", "reviewerSetSize"}


class TestConsensus(unittest.TestCase):

    def test_consensus_payload_shape_and_values(self):
        reviewers = reviewer_set(5)
        payload = tally.consensus(votes_for(reviewers[:3]), reviewers)
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["invoiceId"], base_invoice()["invoiceId"])
        self.assertEqual(payload["recipient"], ZERO_ADDR)
        self.assertEqual(payload["amount"], 5)
        self.assertEqual(payload["reviewRound"], 1)
        self.assertEqual(payload["approvalCount"], 3)
        self.assertEqual(payload["reviewerSetSize"], 5)

    def test_review_round_passthrough(self):
        reviewers = reviewer_set(7)
        payload = tally.consensus(votes_for(reviewers[:4]), reviewers,
                                  review_round=2)
        self.assertEqual(payload["reviewRound"], 2)

    def test_insufficient_approvals_raises(self):
        reviewers = reviewer_set(5)
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes_for(reviewers[:2]), reviewers)
        self.assertIn("consensus not reached", str(ctx.exception))
        self.assertIn("2/3", str(ctx.exception))

    def test_more_than_quorum_counts_all(self):
        reviewers = reviewer_set(5)
        payload = tally.consensus(votes_for(reviewers), reviewers)
        self.assertEqual(payload["approvalCount"], 5)

    def test_empty_votes_raises(self):
        with self.assertRaises(ValueError) as ctx:
            tally.consensus([], reviewer_set(5))
        self.assertIn("no valid approvals", str(ctx.exception))

    def test_votes_not_a_list_raises(self):
        with self.assertRaises(ValueError):
            tally.consensus({"vote": 1}, reviewer_set(5))

    def test_malformed_signature_raises(self):
        reviewers = reviewer_set(5)
        votes = votes_for(reviewers[:3])
        votes[1]["signature"] = "not-hex"
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("not 0x-prefixed hex", str(ctx.exception))

    def test_non_member_reviewer_raises(self):
        reviewers = reviewer_set(5)
        outsider = "0x" + "f" * 40
        votes = votes_for(reviewers[:2]) + [vote(outsider)]
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("not a member", str(ctx.exception))

    def test_malformed_reviewer_address_in_vote_raises(self):
        reviewers = reviewer_set(5)
        votes = votes_for(reviewers[:2]) + [vote("0x1234")]
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("reviewerAddress", str(ctx.exception))

    def test_duplicate_reviewer_counts_once(self):
        reviewers = reviewer_set(5)
        # reviewers[0] votes twice; distinct approving reviewers = 3 -> quorum.
        votes = votes_for(reviewers[:3]) + [vote(reviewers[0])]
        payload = tally.consensus(votes, reviewers)
        self.assertEqual(payload["approvalCount"], 3)

    def test_duplicate_reviewer_on_different_invoices_raises(self):
        reviewers = reviewer_set(5)
        other = build({"invoiceId": "other-invoice"})
        votes = [vote(reviewers[0]), vote(reviewers[0], invoice=other),
                 vote(reviewers[1])]
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("more than one invoice", str(ctx.exception))

    def test_votes_for_different_invoices_raise(self):
        reviewers = reviewer_set(5)
        other = build({"invoiceId": "other-invoice"})
        votes = [vote(reviewers[0]), vote(reviewers[1], invoice=other)]
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("different invoices", str(ctx.exception))

    def test_invalid_contributor_in_invoice_raises(self):
        reviewers = reviewer_set(5)
        votes = votes_for(reviewers[:3])
        votes[0]["invoice"] = build({"contributor": "0x1234"})
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("contributor", str(ctx.exception))

    def test_non_positive_fokens_in_invoice_raises(self):
        reviewers = reviewer_set(5)
        for bad in (0, -5, True, 5.0):
            with self.subTest(bad=bad):
                votes = votes_for(reviewers[:3])
                votes[0]["invoice"] = build(
                    {"valuation.selfAssessedFokens": bad})
                with self.assertRaises(ValueError) as ctx:
                    tally.consensus(votes, reviewers)
                self.assertIn("selfAssessedFokens", str(ctx.exception))

    def test_missing_invoice_raises(self):
        reviewers = reviewer_set(5)
        votes = votes_for(reviewers[:2]) + [
            {"reviewerAddress": reviewers[2], "signature": "0xabc"}]
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes, reviewers)
        self.assertIn("vote invoice", str(ctx.exception))

    def test_invalid_reviewer_set_raises(self):
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes_for(reviewer_set(3)), reviewer_set(3))
        self.assertIn("invalid reviewer set", str(ctx.exception))

    def test_min_approvals_override(self):
        reviewers = reviewer_set(5)
        votes = votes_for(reviewers[:2])
        payload = tally.consensus(votes, reviewers, min_approvals=2)
        self.assertEqual(payload["approvalCount"], 2)

    def test_min_approvals_exceeding_set_size_raises(self):
        with self.assertRaises(ValueError) as ctx:
            tally.consensus(votes_for(reviewer_set(5)), reviewer_set(5),
                            min_approvals=6)
        self.assertIn("exceeds reviewer set size", str(ctx.exception))

    def test_min_approvals_invalid_raises(self):
        reviewers = reviewer_set(5)
        bad_values = [0, -1, 1.5, "3"]  # type: list
        for bad in bad_values:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    tally.consensus(votes_for(reviewers), reviewers,
                                    min_approvals=bad)

    def test_review_round_below_one_raises(self):
        with self.assertRaises(ValueError):
            tally.consensus(votes_for(reviewer_set(5)), reviewer_set(5),
                            review_round=0)


def run_tally(reviewers, votes, *extra_args):
    """Run tally.py as a subprocess; returns the CompletedProcess."""
    cmd = [sys.executable, TALLY_PY]
    with tempfile.TemporaryDirectory() as tmp:
        reviewers_path = os.path.join(tmp, "reviewers.json")
        with open(reviewers_path, "w", encoding="utf-8") as fh:
            json.dump(reviewers, fh)
        votes_path = os.path.join(tmp, "votes.json")
        with open(votes_path, "w", encoding="utf-8") as fh:
            json.dump(votes, fh)
        cmd += ["--reviewers", reviewers_path, "--votes", votes_path]
        cmd += list(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=BASE_DIR)


class TestTallyCLI(unittest.TestCase):

    def test_cli_emits_mint_payload_exit_0(self):
        proc = run_tally(reviewer_set(5), votes_for(reviewer_set(5)[:3]))
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["approvalCount"], 3)
        self.assertEqual(payload["reviewerSetSize"], 5)

    def test_cli_no_consensus_exit_1(self):
        proc = run_tally(reviewer_set(5), votes_for(reviewer_set(5)[:2]))
        self.assertEqual(proc.returncode, 1)
        result = json.loads(proc.stdout)
        self.assertFalse(result["consensus"])
        self.assertIn("consensus not reached", result["reason"])

    def test_cli_review_round_flag(self):
        proc = run_tally(reviewer_set(5), votes_for(reviewer_set(5)[:3]),
                         "--review-round", "2")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reviewRound"], 2)

    def test_cli_min_approvals_override(self):
        proc = run_tally(reviewer_set(5), votes_for(reviewer_set(5)[:2]),
                         "--min-approvals", "2")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["approvalCount"], 2)

    def test_cli_reviewers_object_form(self):
        reviewers = {"reviewers": reviewer_set(7), "minReviewers": 5}
        proc = run_tally(reviewers, votes_for(reviewer_set(7)[:4]))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reviewerSetSize"], 7)

    def test_cli_votes_from_stdin(self):
        cmd = [sys.executable, TALLY_PY]
        with tempfile.TemporaryDirectory() as tmp:
            reviewers_path = os.path.join(tmp, "reviewers.json")
            with open(reviewers_path, "w", encoding="utf-8") as fh:
                json.dump(reviewer_set(5), fh)
            cmd += ["--reviewers", reviewers_path, "--votes", "-"]
            proc = subprocess.run(
                cmd, input=json.dumps(votes_for(reviewer_set(5)[:3])),
                capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["approvalCount"], 3)

    def test_cli_missing_args_exits_2(self):
        proc = subprocess.run([sys.executable, TALLY_PY],
                              capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 2)

    def test_cli_unreadable_votes_exits_1(self):
        proc = subprocess.run(
            [sys.executable, TALLY_PY, "--reviewers", "/nope.json",
             "--votes", "/nope.json"],
            capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout)["consensus"])


if __name__ == "__main__":
    unittest.main()
