"""Tests for settle.py — gate → tally → ledger, end to end.

Run with:  python3 -m unittest discover foken -v
Stdlib only (unittest, json, os, subprocess, sys, tempfile).

The recorded live pair is the point of this suite: `live_omni_invoice.json` and
`live_votes.json` are real data from the g8t capsule submission, not a fixture
invented to make the numbers look good.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import gate
import ledger
import settle

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_INVOICE = os.path.join(HERE, "live_omni_invoice.json")
LIVE_VOTES = os.path.join(HERE, "live_votes.json")
REVIEWERS = os.path.join(HERE, "reviewers.json")


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def run_cli(*args):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "settle.py"), *args],
        capture_output=True, text=True, timeout=60,
    )


class TestRatioParsing(unittest.TestCase):
    def test_a_plain_ratio_parses(self):
        self.assertEqual(settle.parse_ratio("3/10"), (3, 10))
        self.assertEqual(settle.parse_ratio("0/1"), (0, 1))
        self.assertEqual(settle.parse_ratio("7/7"), (7, 7))

    def test_malformed_ratios_are_bad_input_not_refusals(self):
        for text in ("3", "3/", "/4", "three/ten", "3/0", "3/-1", "4/3"):
            with self.assertRaises(ValueError, msg=text):
                settle.parse_ratio(text)


class TestLiveSettlement(unittest.TestCase):
    """The recorded invoice/votes pair actually settles — and to 400, not a guess."""

    @classmethod
    def setUpClass(cls):
        cls.invoice = load(LIVE_INVOICE)
        cls.votes = load(LIVE_VOTES)
        cls.reviewers = gate.load_reviewers(REVIEWERS)

    def test_the_recorded_pair_settles(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["reviewRound"], 1)
        self.assertEqual(report["reviewerSetSize"], 7)
        self.assertEqual(report["approvalCount"], 5)
        self.assertEqual(report["claimed"], 400)
        self.assertEqual(report["verified"], 400)
        self.assertIsNone(report["kept"])

    def test_the_total_is_the_sum_of_its_entries(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers)
        recomputed = sum(entry["fokens"] for entry in report["entries"])
        self.assertEqual(report["totalFokens"], recomputed)
        self.assertEqual(sum(report["payouts"].values()), recomputed)

    def test_the_recipient_is_the_invoice_contributor(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers)
        self.assertEqual(list(report["payouts"]), [self.invoice["contributor"]])

    def test_net_utility_is_rendered_as_integer_thousandths(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers)
        self.assertIsInstance(report["netUtilityMillis"], int)
        # 120 avoided - (10 added + 1.5 x 2 debt) = 107 hours: gate.py weights
        # maintenance debt at 1.5x, so 107_000 is the honest rendering.
        self.assertEqual(report["netUtilityMillis"], 107_000)

    def test_a_compression_claim_pays_only_what_was_removed(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers, kept=(3, 10))
        self.assertEqual(report["kept"], "3/10")
        self.assertEqual(report["claimed"], 400)
        self.assertEqual(report["verified"], 280)  # 400 - floor(400 * 3/10)
        self.assertEqual(report["verified"], ledger.compression_savings(400, 3, 10))
        self.assertEqual(report["totalFokens"], 280)

    def test_the_payload_survives_a_json_round_trip_unchanged(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers, kept=(3, 10))
        again = json.loads(json.dumps(report))
        self.assertEqual(again, report)
        self.assertEqual(again["verified"], 280)
        self.assertIsInstance(again["totalFokens"], int)


class TestReviewRound(unittest.TestCase):
    """A REVIEW verdict is the claim that should need the wider mandate."""

    @staticmethod
    def history_at(median_fokens):
        """Ten chronological invoices whose median valuation is the argument."""
        return [{"valuation": {"selfAssessedFokens": median_fokens}} for _ in range(10)]

    def setUp(self):
        self.invoice = load(LIVE_INVOICE)          # selfAssessedFokens 400
        self.votes = load(LIVE_VOTES)              # 5 distinct approvals
        self.reviewers = gate.load_reviewers(REVIEWERS)   # 7 reviewers

    def test_a_deviating_valuation_lands_in_review(self):
        result = gate.evaluate(self.invoice, self.history_at(100))
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("deviates >2x median", result["reason"])

    def test_review_round_needs_five_of_seven_and_the_live_pair_has_five(self):
        report = settle.settle(self.invoice, self.votes, self.reviewers,
                               history=self.history_at(100))
        self.assertEqual(report["verdict"], "REVIEW")
        self.assertEqual(report["reviewRound"], 2)
        self.assertEqual(report["approvalCount"], 5)
        self.assertEqual(report["totalFokens"], 400)

    def test_review_round_refuses_at_four_of_seven(self):
        with self.assertRaises(settle.Refused) as ctx:
            settle.settle(self.invoice, self.votes[:4], self.reviewers,
                          history=self.history_at(100))
        self.assertIn("tally", str(ctx.exception))

    def test_the_same_four_votes_settle_in_round_one(self):
        # Control: identical votes, but a non-deviating history.
        report = settle.settle(self.invoice, self.votes[:4], self.reviewers,
                               history=self.history_at(400))
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["reviewRound"], 1)
        self.assertEqual(report["approvalCount"], 4)


class TestRefusals(unittest.TestCase):
    def setUp(self):
        self.invoice = load(LIVE_INVOICE)
        self.votes = load(LIVE_VOTES)
        self.reviewers = gate.load_reviewers(REVIEWERS)

    def test_a_net_negative_invoice_never_reaches_the_ledger(self):
        bad = json.loads(json.dumps(self.invoice))
        bad["netUtility"] = {
            "futureWorkAvoidedHours": 0,
            "workAddedHours": 100,
            "maintenanceDebtHours": 30,
        }
        with self.assertRaises(settle.Refused):
            settle.settle(bad, self.votes, self.reviewers)

    def test_missing_quorum_refuses_instead_of_paying_zero(self):
        with self.assertRaises(settle.Refused):
            settle.settle(self.invoice, self.votes[:1], self.reviewers)
        # And the threshold is the documented floor(n/2)+1 = 4, not 1.
        self.assertEqual(settle.tally.required_approvals(7), 4)

    def test_four_approvals_are_enough_and_three_are_not(self):
        settle.settle(self.invoice, self.votes[:4], self.reviewers)
        with self.assertRaises(settle.Refused):
            settle.settle(self.invoice, self.votes[:3], self.reviewers)

    def test_a_vote_from_outside_the_set_refuses(self):
        outsider = json.loads(json.dumps(self.votes[:1]))
        outsider[0]["reviewerAddress"] = "0x" + "9" * 40
        with self.assertRaises(settle.Refused):
            settle.settle(self.invoice, outsider, self.reviewers)


class TestCliExitCodes(unittest.TestCase):
    def test_settled_is_zero(self):
        done = run_cli(LIVE_INVOICE, "--votes", LIVE_VOTES, "--reviewers", REVIEWERS)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["totalFokens"], 400)

    def test_refused_is_one(self):
        done = run_cli(LIVE_INVOICE, "--votes", LIVE_VOTES,
                       "--reviewers", REVIEWERS, "--min-approvals", "9")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertFalse(json.loads(done.stdout)["settled"])

    def test_bad_input_is_two(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
            path = handle.name
        try:
            done = run_cli(path, "--votes", LIVE_VOTES, "--reviewers", REVIEWERS)
            self.assertEqual(done.returncode, 2, done.stdout)
            self.assertIn("error", json.loads(done.stdout))
            done = run_cli(LIVE_INVOICE, "--votes", LIVE_VOTES,
                           "--reviewers", REVIEWERS, "--kept", "4/3")
            self.assertEqual(done.returncode, 2, done.stdout)
        finally:
            os.unlink(path)

    def test_human_output_names_the_anchors_of_the_decision(self):
        done = run_cli(LIVE_INVOICE, "--votes", LIVE_VOTES,
                       "--reviewers", REVIEWERS, "--human")
        self.assertEqual(done.returncode, 0)
        for needle in ("verdict", "PASS", "5/7", "claimed  400", "total    400"):
            self.assertIn(needle, done.stdout)


if __name__ == "__main__":
    unittest.main()
