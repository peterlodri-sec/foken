"""Tests for ledger.py — the exact-rational ledger.

Run with:  python3 -m unittest discover foken -v
Stdlib only (unittest, fractions).
"""

import unittest
from fractions import Fraction

import ledger


class TestCompressionSavings(unittest.TestCase):
    def test_savings_are_exact_and_floored(self):
        self.assertEqual(ledger.compression_savings(1000, 3, 10), 700)
        self.assertEqual(ledger.compression_savings(10, 1, 3), 7)  # floor, never 6 or 8
        self.assertEqual(ledger.compression_savings(0, 3, 10), 0)
        self.assertEqual(ledger.compression_savings(999, 0, 1), 999)  # nothing kept, all saved
        self.assertEqual(ledger.compression_savings(999, 1, 1), 0)

    def test_flooring_can_never_overstate(self):
        # `numerator/denominator` is the *retained* fraction, so savings are
        # `original - floor(original * ratio)`: exact by construction, floored
        # so a ledger can never claim a token it cannot point at.
        for original in (0, 1, 7, 13, 999, 10**15):
            for numerator, denominator in ((0, 1), (1, 3), (2, 7), (11, 13), (999, 1000), (1, 1)):
                saved = ledger.compression_savings(original, numerator, denominator)
                kept = (Fraction(original) * Fraction(numerator, denominator))
                self.assertEqual(saved + kept.numerator // kept.denominator, original)
                self.assertGreaterEqual(saved, 0)
                self.assertLessEqual(saved, original)

    def test_a_zero_denominator_is_rejected(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.compression_savings(100, 1, 0)
        with self.assertRaises(ledger.LedgerError):
            ledger.compression_savings(100, 1, -5)

    def test_a_ratio_above_one_is_rejected(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.compression_savings(100, 11, 10)

    def test_floats_never_enter(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.compression_savings(100, 0.5, 1)
        with self.assertRaises(ledger.LedgerError):
            ledger.compression_savings(100.0, 1, 2)
        with self.assertRaises(ledger.LedgerError):
            ledger.compression_savings(True, 1, 2)


class TestCompressionRatio(unittest.TestCase):
    def test_ratio_is_a_fraction_not_a_percentage(self):
        self.assertEqual(ledger.compression_ratio(1000, 300), Fraction(3, 10))
        self.assertEqual(ledger.compression_ratio(7, 3), Fraction(3, 7))
        self.assertEqual(ledger.compression_ratio(5, 5), Fraction(1, 1))

    def test_malformed_ratios_are_rejected(self):
        for original, compressed in ((0, 0), (3, 4)):
            with self.assertRaises(ledger.LedgerError):
                ledger.compression_ratio(original, compressed)


class TestLedgerEntry(unittest.TestCase):
    def test_payout_is_the_verified_amount(self):
        entry = ledger.LedgerEntry("alice", claimed_tokens=1000, verified_tokens=700)
        self.assertEqual(entry.fokens, 700)
        self.assertEqual(
            entry.as_payload(),
            {
                "recipient": "alice",
                "claimedTokens": 1000,
                "verifiedTokens": 700,
                "fokens": 700,
            },
        )

    def test_verifying_more_than_claimed_is_rejected(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.LedgerEntry("alice", claimed_tokens=10, verified_tokens=11)
        with self.assertRaises(ledger.LedgerError):
            ledger.LedgerEntry("", claimed_tokens=1, verified_tokens=1)
        with self.assertRaises(ledger.LedgerError):
            ledger.LedgerEntry("alice", claimed_tokens=-1, verified_tokens=0)


class TestSettle(unittest.TestCase):
    def test_settlement_sums_duplicates_and_sorts(self):
        entries = [
            ledger.LedgerEntry("bob", 500, 400),
            ledger.LedgerEntry("alice", 1000, 700),
            ledger.LedgerEntry("bob", 100, 100),
        ]
        report = ledger.settle(entries)
        self.assertEqual(report["entries"], 3)
        self.assertEqual(report["recipients"], 2)
        self.assertEqual(report["payouts"], {"alice": 700, "bob": 500})
        self.assertEqual(report["totalFokens"], 1200)

    def test_empty_settlement_is_zero(self):
        self.assertEqual(
            ledger.settle([]),
            {"entries": 0, "recipients": 0, "payouts": {}, "totalFokens": 0},
        )


if __name__ == "__main__":
    unittest.main()
