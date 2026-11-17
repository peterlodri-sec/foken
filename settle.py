#!/usr/bin/env python3
"""Foken — end-to-end settlement: gate → tally → ledger, one path.

The three halves answered three questions separately:

    gate.py    may this claim be paid at all?
    tally.py   did the humans actually approve it?
    ledger.py  how much, exactly?

`settle.py` is the only place that asks all three in order, and it refuses to
skip a step. Nothing here is new arithmetic: the verdict comes from `gate`, the
approval count from `tally`, the money from `ledger`. This module's whole job is
the refusal — a settlement that cannot be authorised must not be able to
produce a number.

Ordering is not cosmetic:

1. **Gate first.** A net-negative claim FAILs before anyone counts votes, so a
   popular bad invoice cannot buy consensus with a crowd.
2. **Tally second.** No quorum, no settlement — `tally.consensus` raises and
   this module turns that into a refusal, not a zero.
3. **Ledger last.** Only an authorised claim reaches the integers.

The payable amount is the *verified* amount:

- a flat assessment (`--kept` omitted) pays the invoice's own valuation, because
  the gate and the reviewers have already authorised it as stated;
- a compression claim (`--kept N/D`, the fraction of the artifact that survives)
  pays `ledger.compression_savings` — the tokens actually removed, floored. The
  ledger can only ever understate that number.

`netUtility` is the one value `gate` computes with a float. It is converted to
integer thousandths (`netUtilityMillis`) exactly once, at the boundary, and
never used in arithmetic afterwards.

Stdlib only (argparse, json, sys). Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

import gate
import ledger
import tally

#: Settled: gate authorised, quorum reached, ledger wrote the integers.
EXIT_SETTLED = 0
#: Refused: the gate said no, or the reviewer set did not reach quorum.
EXIT_REFUSED = 1
#: Bad input: unreadable JSON, malformed invoice, unusable ratio.
EXIT_BAD_INPUT = 2

#: Verdicts that may reach the ledger, and the round each one settles in.
SETTLE_ROUND_BY_VERDICT = {"PASS": 1, "REVIEW": 2}


class Refused(Exception):
    """A settlement that the gate or the tally will not authorise."""


def parse_ratio(text: str) -> tuple[int, int]:
    """Parse a ``NUMERATOR/DENOMINATOR`` retained fraction.

    Rejects anything `ledger` would reject, here, so a malformed ratio is a
    bad input (exit 2) rather than a refusal (exit 1) — the difference tells the
    caller whether to fix the claim or argue with the reviewers.
    """
    if "/" not in text:
        raise ValueError(f"ratio must be NUMERATOR/DENOMINATOR, got {text!r}")
    head, _, tail = text.partition("/")
    try:
        numerator, denominator = int(head), int(tail)
    except ValueError:
        raise ValueError(f"ratio parts must be integers, got {text!r}") from None
    if denominator <= 0:
        raise ValueError(f"denominator must be positive, got {denominator}")
    if numerator < 0 or numerator > denominator:
        raise ValueError(f"numerator must be within 0..{denominator}, got {numerator}")
    return numerator, denominator


def _net_utility_millis(gate_result: dict) -> Optional[int]:
    """The gate's integer thousandths, taken as-is.

    `gate` already computes this in integers (`compute_net_millis`); re-deriving
    it from the float hours would put rounding back into a path that does not
    need it. The float fallback only exists for a gate result from an older
    caller that predates the field.
    """
    millis = gate_result.get("netUtilityMillis")
    if isinstance(millis, int) and not isinstance(millis, bool):
        return millis
    net = gate_result.get("netUtility")
    return None if net is None else round(net * 1000)


def settle(
    invoice: Any,
    votes: Any,
    reviewers: Any,
    *,
    history: Optional[list] = None,
    kept: Optional[tuple[int, int]] = None,
    min_approvals: Optional[int] = None,
) -> dict:
    """Settle one invoice; return the mint-ready report.

    Raises `Refused` when the gate or the tally will not authorise the claim,
    and `ValueError`/`ledger.LedgerError` for malformed input. Never returns a
    number it could not authorise.
    """
    gate_result = gate.evaluate(invoice, history)
    verdict = gate_result.get("verdict")
    review_round = SETTLE_ROUND_BY_VERDICT.get(str(verdict))
    if review_round is None:
        raise Refused(f"gate verdict {verdict}: {gate_result.get('reason')}")

    try:
        payload = tally.consensus(votes, reviewers, review_round=review_round,
                                  min_approvals=min_approvals)
    except ValueError as exc:  # no quorum, or a vote that is not well-formed
        raise Refused(f"tally: {exc}") from None

    claimed = payload.get("amount")
    if not isinstance(claimed, int) or isinstance(claimed, bool):
        raise ValueError(f"tally amount must be an int, got {claimed!r}")
    if claimed <= 0:
        raise Refused(f"tally amount must be positive, got {claimed}")

    if kept is None:
        verified = claimed
    else:
        verified = ledger.compression_savings(claimed, kept[0], kept[1])

    entry = ledger.LedgerEntry(payload["recipient"], claimed, verified)
    book = ledger.settle([entry])

    return {
        "invoiceId": payload.get("invoiceId"),
        "verdict": verdict,
        "reviewRound": payload.get("reviewRound", review_round),
        "approvalCount": payload.get("approvalCount"),
        "reviewerSetSize": payload.get("reviewerSetSize"),
        "netUtilityMillis": _net_utility_millis(gate_result),
        "kept": None if kept is None else f"{kept[0]}/{kept[1]}",
        "claimed": claimed,
        "verified": verified,
        "entries": [entry.as_payload()],
        "payouts": book["payouts"],
        "totalFokens": book["totalFokens"],
    }


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="settle.py",
        description="Foken end-to-end settlement: gate, tally, ledger — in that "
                    "order, refusing anything the first two do not authorise.")
    parser.add_argument("invoice", nargs="?", default=None,
                        help="path to the Foken Invoice JSON; '-' reads stdin")
    parser.add_argument("--votes", metavar="PATH", required=False,
                        help="votes file (tally.tally shape); '-' reads stdin")
    parser.add_argument("--reviewers", metavar="PATH", required=True,
                        help="reviewer set file; '-' reads stdin")
    parser.add_argument("--history", metavar="PATH", default=None,
                        help="history file for the gate's deviation check")
    parser.add_argument("--kept", metavar="N/D", default=None,
                        help="retained fraction of a compression claim, e.g. 3/10; "
                             "omitted means a flat assessment")
    parser.add_argument("--min-approvals", type=int, default=None,
                        dest="min_approvals",
                        help="override quorum (owner param; default floor(n/2)+1)")
    parser.add_argument("--human", action="store_true",
                        help="print a readable summary instead of one JSON line")
    args = parser.parse_args(argv)

    try:
        kept = None if args.kept is None else parse_ratio(args.kept)
        invoice = _read_json(args.invoice) if args.invoice else None
        if invoice is None:
            parser.error("an invoice path (or '-') is required")
        votes = _read_json(args.votes) if args.votes else None
        if votes is None:
            parser.error("--votes is required to settle")
        reviewers = gate.load_reviewers(args.reviewers)
        history = gate.load_history(args.history) if args.history else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"settled": False, "error": str(exc)}, ensure_ascii=False))
        return EXIT_BAD_INPUT

    try:
        report = settle(invoice, votes, reviewers,
                        history=history, kept=kept,
                        min_approvals=args.min_approvals)
    except Refused as exc:
        print(json.dumps({"settled": False, "refused": str(exc)},
                         ensure_ascii=False))
        return EXIT_REFUSED
    except (ValueError, ledger.LedgerError) as exc:
        print(json.dumps({"settled": False, "error": str(exc)}, ensure_ascii=False))
        return EXIT_BAD_INPUT

    if args.human:
        print(f"invoice  {report['invoiceId']}")
        print(f"verdict  {report['verdict']} (round {report['reviewRound']}, "
              f"{report['approvalCount']}/{report['reviewerSetSize']} approved)")
        print(f"claimed  {report['claimed']} FKN")
        print(f"kept     {report['kept'] or 'flat assessment'}")
        print(f"verified {report['verified']} FKN")
        for who, amount in report["payouts"].items():
            print(f"  pay    {who}  {amount}")
        print(f"total    {report['totalFokens']} FKN")
    else:
        print(json.dumps(report, ensure_ascii=False))
    return EXIT_SETTLED


if __name__ == "__main__":
    sys.exit(main())
