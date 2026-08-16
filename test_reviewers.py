"""Tests for gate.py's reviewer-set model: validate_reviewer_set,
quorum_reached / quorumReached, and load_reviewers.

Run with:  python3 -m unittest discover foken -v   (or: python3 -m unittest test_reviewers -v)
Stdlib only (unittest, json, io, tempfile).
"""

import io
import json
import os
import tempfile
import unittest

import gate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def make_addrs(count):
    """`count` distinct fake addresses: 0x1111..., 0x2222..., ..."""
    return ["0x" + str(i) * 40 for i in range(1, count + 1)]


class TestValidateReviewerSet(unittest.TestCase):

    def test_valid_set_of_5(self):
        self.assertIsNone(gate.validate_reviewer_set(make_addrs(5)))

    def test_valid_set_larger_than_minimum(self):
        self.assertIsNone(gate.validate_reviewer_set(make_addrs(7)))

    def test_too_small(self):
        reason = gate.validate_reviewer_set(make_addrs(4))
        self.assertIsNotNone(reason)
        assert reason is not None  # narrow for the type checker
        self.assertIn("too small", reason)
        self.assertIn("4", reason)

    def test_empty_list(self):
        self.assertIsNotNone(gate.validate_reviewer_set([]))

    def test_non_list_inputs(self):
        for bad in ("0x" + "1" * 40, {"reviewers": []}, 5, None):
            with self.subTest(bad=bad):
                reason = gate.validate_reviewer_set(bad)
                self.assertIsNotNone(reason)
                assert reason is not None  # narrow for the type checker
                self.assertIn("must be a list", reason)

    def test_invalid_addresses(self):
        for bad in ("0x1234", "not-an-address", "0x" + "g" * 40, 123, None):
            with self.subTest(bad=bad):
                reviewers = make_addrs(4) + [bad]
                reason = gate.validate_reviewer_set(reviewers)
                self.assertIsNotNone(reason)
                assert reason is not None  # narrow for the type checker
                self.assertIn("invalid reviewer address", reason)

    def test_duplicates_rejected(self):
        reviewers = make_addrs(4) + [make_addrs(4)[0]]
        reason = gate.validate_reviewer_set(reviewers)
        self.assertIsNotNone(reason)
        assert reason is not None  # narrow for the type checker
        self.assertIn("duplicate", reason)

    def test_min_reviewers_override(self):
        # 3 reviewers are fine when the caller lowers the minimum.
        self.assertIsNone(gate.validate_reviewer_set(make_addrs(3),
                                                     min_reviewers=3))
        self.assertIsNotNone(gate.validate_reviewer_set(make_addrs(3),
                                                        min_reviewers=5))


class TestQuorumReached(unittest.TestCase):

    def test_strict_majority_odd_set(self):
        self.assertTrue(gate.quorum_reached(3, 5))
        self.assertFalse(gate.quorum_reached(2, 5))

    def test_strict_majority_even_set(self):
        # 3 of 6 is exactly 50% -> NOT quorum (approvals * 2 > set_size).
        self.assertTrue(gate.quorum_reached(4, 6))
        self.assertFalse(gate.quorum_reached(3, 6))

    def test_larger_set(self):
        self.assertTrue(gate.quorum_reached(4, 7))
        self.assertFalse(gate.quorum_reached(3, 7))

    def test_boundary_exactly_half_is_not_quorum(self):
        self.assertFalse(gate.quorum_reached(3, 6))
        self.assertFalse(gate.quorum_reached(5, 10))

    def test_degnerate_inputs_return_false(self):
        self.assertFalse(gate.quorum_reached(0, 5))
        self.assertFalse(gate.quorum_reached(5, 0))
        self.assertFalse(gate.quorum_reached(-1, 5))

    def test_non_int_raises_type_error(self):
        # Deliberately-wrong inputs: the container is untyped so the checker
        # does not flag the (intended) TypeError path.
        bad_pairs = [(3.0, 5), (3, 5.0), (True, 5), (3, "5")]  # type: list
        for bad_approvals, bad_size in bad_pairs:
            with self.subTest(approvals=bad_approvals, set_size=bad_size):
                with self.assertRaises(TypeError):
                    gate.quorum_reached(bad_approvals, bad_size)

    def test_camelcase_alias_matches_snake_case(self):
        self.assertIs(gate.quorumReached, gate.quorum_reached)
        self.assertTrue(gate.quorumReached(3, 5))
        self.assertFalse(gate.quorumReached(2, 5))


class TestLoadReviewers(unittest.TestCase):

    def test_bare_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reviewers.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(make_addrs(5), fh)
            self.assertEqual(gate.load_reviewers(path), make_addrs(5))

    def test_object_with_reviewers_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reviewers.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"reviewers": make_addrs(7), "minReviewers": 5}, fh)
            self.assertEqual(gate.load_reviewers(path), make_addrs(7))

    def test_wrong_shape_raises(self):
        for payload in ({"invoices": []}, "0xabc", 42):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(payload=payload):
                path = os.path.join(tmp, "reviewers.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                with self.assertRaises(ValueError):
                    gate.load_reviewers(path)

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            gate.load_reviewers(os.path.join(BASE_DIR, "no-such-file.json"))

    def test_stdin_dash(self):
        old_stdin = gate.sys.stdin
        try:
            gate.sys.stdin = io.StringIO(json.dumps(make_addrs(5)))
            self.assertEqual(gate.load_reviewers("-"), make_addrs(5))
        finally:
            gate.sys.stdin = old_stdin


if __name__ == "__main__":
    unittest.main()
