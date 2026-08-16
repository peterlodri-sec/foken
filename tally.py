#!/usr/bin/env python3
"""Foken — off-chain tally / consensus service (prototype).

Half 3 of the Foken governance design. After gate.py pre-filters an invoice
and the human >50% vote collects reviewer approvals, this service counts the
distinct approving reviewers and — when quorum is met — emits the mint-ready
payload that the on-chain `AttestationVerifier.mintFromConsensus` input
expects:

    { invoiceId, recipient, amount, reviewRound, approvalCount,
      reviewerSetSize }

Pure engineering: no blockchain, no tokens, no secrets. Signatures are only
format-checked (0x + hex); real Ed25519/secp256k1 verification is out of
scope for this prototype.

Quorum: k = floor(n/2) + 1 distinct approvals from a reviewer set of size n
(the strict >50% rule; identical to gate.quorum_reached's threshold). The
reviewer count is an owner parameter — derived from the set size by default,
overridable with --min-approvals, never hardcoded.

Stdlib only (argparse, json, sys). Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

import gate


def required_approvals(set_size: int) -> int:
    """k = floor(n/2) + 1 — the strict-majority quorum from the spec.

    For odd n this is (n+1)/2; for even n it is the smallest count that also
    satisfies gate.quorum_reached (approvals * 2 > n).
    """
    return set_size // 2 + 1


def _read_json(path: str) -> Any:
    """Read a JSON file; '-' reads from stdin."""
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_invoice_fields(invoice: Any) -> tuple:
    """Pull the fields the mint payload needs from one invoice, validating
    them with the same rules gate.py applies. Raises ValueError otherwise."""
    if not isinstance(invoice, dict):
        raise ValueError("vote invoice must be a JSON object")
    invoice_id = invoice.get("invoiceId")
    if not (isinstance(invoice_id, str) and invoice_id):
        raise ValueError("invoice.invoiceId must be a non-empty string")
    recipient = invoice.get("contributor")
    if not (isinstance(recipient, str) and gate.HEX_ADDRESS.match(recipient)):
        raise ValueError("invoice.contributor must be a 0x + 40-hex address")
    valuation = invoice.get("valuation")
    amount = (valuation or {}).get("selfAssessedFokens")
    if not (isinstance(amount, int) and not isinstance(amount, bool)
            and amount > 0):
        raise ValueError("invoice.valuation.selfAssessedFokens must be a "
                         "positive integer")
    return invoice_id, recipient, amount


def consensus(votes: Any, reviewers: Any, review_round: int = 1,
              min_approvals: Optional[int] = None) -> dict:
    """Count distinct reviewer approvals; return the mint-ready payload.

    votes:      list of {"invoice": {...}, "reviewerAddress": "0x...",
                "signature": "0x..."} objects.
    reviewers:  reviewer set — a list of >= 5 distinct 0x addresses (the
                unwrapped form; gate.load_reviewers produces it from a file).
    review_round: 1 for a normal vote, 2 when the gate verdict was REVIEW
                (extra review round). Passed through to the payload.
    min_approvals: required distinct approvals; default k = floor(n/2) + 1
                where n = reviewer set size (owner param, not hardcoded).

    Strict by design: any malformed vote (bad address, non-member reviewer,
    bad signature, malformed invoice) raises ValueError — the tally never
    silently drops a vote. Duplicate approvals from the same reviewer count
    once. Raises ValueError(reason) when consensus cannot be reached;
    returns the payload dict otherwise.
    """
    if review_round < 1:
        raise ValueError("review_round must be >= 1")
    problem = gate.validate_reviewer_set(reviewers)
    if problem:
        raise ValueError("invalid reviewer set: " + problem)
    set_size = len(reviewers)
    if not isinstance(votes, list):
        raise ValueError("votes must be a JSON array of vote objects")

    reviewers_set = set(reviewers)
    approvals: dict = {}  # reviewer address -> invoice dict (dedupes per reviewer)
    for i, vote in enumerate(votes):
        if not isinstance(vote, dict):
            raise ValueError(f"vote #{i}: expected an object with "
                             "reviewerAddress/signature/invoice")
        addr = vote.get("reviewerAddress")
        if not (isinstance(addr, str) and gate.HEX_ADDRESS.match(addr)):
            raise ValueError(f"vote #{i}: reviewerAddress must be a 0x + "
                             "40-hex address")
        if addr not in reviewers_set:
            raise ValueError(f"vote #{i}: reviewer {addr} is not a member "
                             "of the reviewer set")
        sig = vote.get("signature")
        if not (isinstance(sig, str) and gate.HEX_SIGNATURE.match(sig)):
            raise ValueError(f"vote #{i}: signature from {addr} is not "
                             "0x-prefixed hex")
        invoice: Any = vote.get("invoice")
        _extract_invoice_fields(invoice)  # raises on a malformed invoice
        if addr in approvals:
            if approvals[addr].get("invoiceId") != invoice.get("invoiceId"):
                raise ValueError(f"vote #{i}: reviewer {addr} voted on more "
                                 "than one invoice")
            continue  # duplicate approval from the same reviewer: count once
        approvals[addr] = invoice

    if not approvals:
        raise ValueError("no valid approvals collected")

    invoice_ids = {inv.get("invoiceId") for inv in approvals.values()}
    if len(invoice_ids) > 1:
        raise ValueError("votes reference different invoices: "
                         + ", ".join(sorted(invoice_ids)))

    k = required_approvals(set_size) if min_approvals is None else min_approvals
    if not (isinstance(k, int) and not isinstance(k, bool) and k >= 1):
        raise ValueError("min_approvals must be a positive integer")
    if k > set_size:
        raise ValueError(f"min_approvals {k} exceeds reviewer set size "
                         f"{set_size}")

    approval_count = len(approvals)
    if approval_count < k:
        raise ValueError(
            f"consensus not reached: {approval_count}/{k} approvals needed "
            f"(reviewer set size {set_size})")

    invoice_id, recipient, amount = _extract_invoice_fields(
        next(iter(approvals.values())))
    return {
        "invoiceId": invoice_id,
        "recipient": recipient,
        "amount": amount,
        "reviewRound": review_round,
        "approvalCount": approval_count,
        "reviewerSetSize": set_size,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tally.py",
        description="Foken off-chain tally: count distinct reviewer "
                    "approvals and emit a mint-ready consensus payload "
                    "when quorum is met.")
    parser.add_argument(
        "--reviewers", metavar="PATH", required=True,
        help="reviewer set file: JSON array of 0x addresses (or an object "
             "with a 'reviewers' array); '-' reads stdin")
    parser.add_argument(
        "--votes", metavar="PATH", required=True,
        help="votes file: JSON array of {invoice, reviewerAddress, "
             "signature}; '-' reads stdin")
    parser.add_argument(
        "--review-round", type=int, default=1, dest="review_round",
        help="consensus round: 1 normal vote, 2 after an extra review round "
             "(pass 2 when the gate verdict was REVIEW); default 1")
    parser.add_argument(
        "--min-approvals", type=int, default=None, dest="min_approvals",
        help="required distinct approvals; default k = floor(n/2) + 1 of the "
             "reviewer set size (owner param, not hardcoded)")
    args = parser.parse_args(argv)

    try:
        reviewers = gate.load_reviewers(args.reviewers)
        votes = _read_json(args.votes)
        payload = consensus(votes, reviewers,
                            review_round=args.review_round,
                            min_approvals=args.min_approvals)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"consensus": False, "reason": str(exc)},
                         ensure_ascii=False))
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
