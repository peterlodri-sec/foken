#!/usr/bin/env python3
"""Foken — proof-of-contribution valuation gate (off-chain, prototype).

Half 2 of the Foken governance design. A coarse, deterministic pre-filter
that runs *before* the binding human >50% vote. It consumes a Foken
Invoice (JSON), validates it against hard IP/provenance gates, applies the
net-utility formula, and emits a verdict: PASS | FAIL | REVIEW.

This is pure engineering: no blockchain, no tokens, no secrets, no
minting, pricing, or vesting. Framing: a **verifiable contribution
pre-filter**, not equity minting. The verdict is advisory — the binding
decision is always the human vote. See README.md.

This module also ships the **reviewer-set model** used by the off-chain
tally (tally.py): validate_reviewer_set() and quorum_reached() implement
the spec's >50% rule over a reviewer set of >= 5 distinct 0x addresses.

Stdlib only (argparse, json, re, sys). Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Optional

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_REVIEW = 2

HISTORY_N = 10  # number of history invoices used for the median deviation check
MIN_REVIEWERS = 5  # minimum reviewer-set size (spec-mandated; overridable per call)
SHA256_PREFIX = "sha256://"
IP_ASSIGNMENT_PHRASE = "IRREVOCABLE ASSIGNMENT"
MAINTENANCE_DEBT_WEIGHT = 1.5

HEX_SIGNATURE = re.compile(r"^0x[0-9a-fA-F]+$")
HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Top-level keys required on every invoice. netUtility is intentionally
# excluded here: a missing/malformed netUtility is a REVIEW (a human can fix
# it), never a hard FAIL.
REQUIRED_TOP = (
    "invoiceId", "version", "contributor", "work", "artifact",
    "valuation", "ipAssignment", "dedup",
)

# ipAssignment.statement and ipAssignment.signature are intentionally NOT in
# the nested-required list: the IP hard gate in hard_fail_reason() owns their
# presence + content, so a missing/unsigned IP assignment always fails with
# the canonical "no IP, no mint" reason rather than a generic shape error.
REQUIRED_NESTED = {
    "work": ("title", "description", "repo", "commit"),
    "artifact": ("provenanceHash", "artifactUri"),
    "valuation": ("selfAssessedFokens", "basis", "rationale", "evidence"),
    "ipAssignment": ("license", "signedAt"),
    "dedup": ("claimedNovelty", "relatedInvoiceIds"),
}

NET_UTILITY_FIELDS = ("futureWorkAvoidedHours", "workAddedHours",
                      "maintenanceDebtHours")

EXIT_BY_VERDICT = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "REVIEW": EXIT_REVIEW}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_structure(invoice: Any) -> Optional[str]:
    """Return a FAIL reason if the invoice is structurally malformed.

    Presence + rough shape only; the message-specific hard gates live in
    hard_fail_reason().
    """
    if not isinstance(invoice, dict):
        return "malformed invoice: expected a JSON object"
    missing = [k for k in REQUIRED_TOP if k not in invoice]
    if missing:
        return "malformed invoice: missing field(s): " + ", ".join(missing)
    if not isinstance(invoice["invoiceId"], str) or not invoice["invoiceId"]:
        return "malformed invoice: invoiceId must be a non-empty string"
    if isinstance(invoice["version"], bool) or not isinstance(invoice["version"], int):
        return "malformed invoice: version must be an integer"
    for key, subkeys in REQUIRED_NESTED.items():
        node = invoice[key]
        if not isinstance(node, dict):
            return f"malformed invoice: {key} must be an object"
        missing_inner = [s for s in subkeys if s not in node]
        if missing_inner:
            return (f"malformed invoice: {key} missing field(s): "
                    + ", ".join(missing_inner))
    work = invoice["work"]
    for field in ("title", "description", "repo", "commit"):
        if not isinstance(work[field], str):
            return f"malformed invoice: work.{field} must be a string"
    if not isinstance(invoice["artifact"]["artifactUri"], str):
        return "malformed invoice: artifact.artifactUri must be a string"
    valuation = invoice["valuation"]
    if (not isinstance(valuation["basis"], str)
            or not isinstance(valuation["rationale"], str)
            or not isinstance(valuation["evidence"], list)):
        return ("malformed invoice: valuation.basis/rationale must be strings "
                "and valuation.evidence a list")
    ip = invoice["ipAssignment"]
    for field in ("statement", "license", "signedAt"):
        if not isinstance(ip[field], str):
            return f"malformed invoice: ipAssignment.{field} must be a string"
    dedup = invoice["dedup"]
    if (not isinstance(dedup["claimedNovelty"], str)
            or not isinstance(dedup["relatedInvoiceIds"], list)):
        return ("malformed invoice: dedup.claimedNovelty must be a string and "
                "dedup.relatedInvoiceIds a list")
    return None


def hard_fail_reason(invoice: dict) -> Optional[str]:
    """Hard gates — anything here kills the invoice with a FAIL reason.

    These are the "no IP, no mint" checks: IP assignment, provenance,
    contributor identity, and a sane self-assessment.
    """
    ip = invoice["ipAssignment"]
    signature = ip.get("signature")
    statement = ip.get("statement", "")
    if not (isinstance(signature, str) and HEX_SIGNATURE.match(signature)
            and isinstance(statement, str) and IP_ASSIGNMENT_PHRASE in statement):
        return "missing/unsigned IP assignment — no IP, no mint"

    provenance = invoice["artifact"].get("provenanceHash")
    if not (isinstance(provenance, str) and provenance.startswith(SHA256_PREFIX)):
        return "invalid artifact provenance: provenanceHash must be sha256://-prefixed"

    contributor = invoice.get("contributor")
    if not (isinstance(contributor, str) and HEX_ADDRESS.match(contributor)):
        return "invalid contributor address: expected 0x followed by 40 hex chars"

    fokens = invoice["valuation"].get("selfAssessedFokens")
    if not (isinstance(fokens, int) and not isinstance(fokens, bool) and fokens > 0):
        return "selfAssessedFokens must be a positive integer"

    return None


def net_utility_review_reason(invoice: dict) -> Optional[str]:
    """netUtility carries tool-computed inputs. If it is missing or malformed
    the gate cannot run automatically — REVIEW so a human can fix it."""
    nu = invoice.get("netUtility")
    if not isinstance(nu, dict):
        return "netUtility missing or not an object — human review required"
    for field in NET_UTILITY_FIELDS:
        value = nu.get(field)
        if not (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value >= 0):
            return (f"netUtility.{field} missing, non-numeric, or negative — "
                    "human review required")
    return None


# --------------------------------------------------------------------------
# Net-utility gate
# --------------------------------------------------------------------------

def compute_net(net_utility: dict) -> float:
    """net = futureWorkAvoidedHours - (workAddedHours + 1.5 * maintenanceDebtHours)"""
    fw = net_utility["futureWorkAvoidedHours"]
    wa = net_utility["workAddedHours"]
    md = net_utility["maintenanceDebtHours"]
    return fw - (wa + MAINTENANCE_DEBT_WEIGHT * md)


# --------------------------------------------------------------------------
# Valuation-deviation check (advisory, against --history)
# --------------------------------------------------------------------------

def median(values) -> Optional[float]:
    """Median of a list of numbers; None when empty."""
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


def load_history(path: str) -> list:
    """Read the history file: a JSON array of invoices, or an object with an
    'invoices' array. Raise ValueError on a wrong shape."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("invoices"), list):
        data = data["invoices"]
    if not isinstance(data, list):
        raise ValueError(
            "history must be a JSON array of invoices "
            "(or an object with an 'invoices' array)")
    return data


def load_reviewers(path: str) -> list:
    """Read a reviewer set file: a JSON array of 0x addresses, or an object
    with a 'reviewers' array. '-' reads from stdin. Raise ValueError on a
    wrong shape."""
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("reviewers"), list):
        data = data["reviewers"]
    if not isinstance(data, list):
        raise ValueError(
            "reviewer set must be a JSON array of 0x addresses "
            "(or an object with a 'reviewers' array)")
    return data


def validate_reviewer_set(reviewers: Any,
                          min_reviewers: int = MIN_REVIEWERS) -> Optional[str]:
    """Return an error string if `reviewers` is not a valid reviewer set: a
    list of >= min_reviewers distinct, well-formed 0x addresses."""
    if not isinstance(reviewers, list):
        return "reviewer set must be a list of 0x addresses"
    if len(reviewers) < min_reviewers:
        return (f"reviewer set too small: {len(reviewers)} < minimum "
                f"{min_reviewers}")
    for addr in reviewers:
        if not (isinstance(addr, str) and HEX_ADDRESS.match(addr)):
            return (f"invalid reviewer address: {addr!r} "
                    "(expected 0x followed by 40 hex chars)")
    seen = set()
    dupes = []
    for addr in reviewers:
        if addr in seen:
            dupes.append(addr)
        seen.add(addr)
    if dupes:
        return ("reviewer set contains duplicate address(es): "
                + ", ".join(sorted(set(dupes))[:3]))
    return None


def quorum_reached(approvals: int, set_size: int) -> bool:
    """The >50% rule from the spec: consensus holds when strictly more than
    half of the reviewer set approves, i.e. approvals * 2 > set_size.

    Degenerate inputs (negative approvals, empty set) return False; non-int
    inputs raise TypeError.
    """
    if not (isinstance(approvals, int) and not isinstance(approvals, bool)):
        raise TypeError("approvals must be an int")
    if not (isinstance(set_size, int) and not isinstance(set_size, bool)):
        raise TypeError("set_size must be an int")
    if approvals < 0 or set_size <= 0:
        return False
    return approvals * 2 > set_size


quorumReached = quorum_reached  # spec-fidelity alias (README documents both)


def valuation_deviation_reason(invoice: dict, history) -> Optional[str]:
    """If selfAssessedFokens > 2x the median of the last HISTORY_N invoices in
    `history`, flag REVIEW ("extra review round"). No history -> no check.
    File order is chronological: the newest invoices are at the end."""
    if history is None:
        return None
    fokens = []
    for inv in history[-HISTORY_N:]:
        if not isinstance(inv, dict):
            continue
        fk = (inv.get("valuation") or {}).get("selfAssessedFokens")
        if isinstance(fk, int) and not isinstance(fk, bool) and fk >= 0:
            fokens.append(fk)
    med = median(fokens)
    if med is None:
        return None
    fk = invoice["valuation"]["selfAssessedFokens"]
    if fk > 2 * med:
        return (f"valuation deviates >2x median (selfAssessedFokens {fk} vs "
                f"median {med}); extra review round")
    return None


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def evaluate(invoice: Any, history: Optional[list] = None) -> dict:
    """Run the full gate on one invoice.

    Returns {"invoiceId", "verdict", "reason", "netUtility"}.

    Precedence (documented in README):
      1. malformed structure / hard gates            -> FAIL
      2. missing/malformed netUtility                -> REVIEW
      3. net <= 0                                     -> FAIL
      4. valuation deviates >2x median                -> REVIEW
      5. net > 0                                      -> PASS

    A net-negative invoice FAILs even when the valuation also deviates:
    a rejected invoice does not go to an extra review round.
    """
    if not isinstance(invoice, dict):
        invoice = {}
    invoice_id = invoice.get("invoiceId", "?")

    problem = validate_structure(invoice)
    if problem:
        return {"invoiceId": invoice_id, "verdict": "FAIL",
                "reason": problem, "netUtility": None}

    reason = hard_fail_reason(invoice)
    if reason:
        return {"invoiceId": invoice_id, "verdict": "FAIL",
                "reason": reason, "netUtility": None}

    reason = net_utility_review_reason(invoice)
    if reason:
        return {"invoiceId": invoice_id, "verdict": "REVIEW",
                "reason": reason, "netUtility": None}

    net = compute_net(invoice["netUtility"])
    if net <= 0:
        return {"invoiceId": invoice_id, "verdict": "FAIL",
                "reason": ("net-negative utility: future work avoided is "
                           "eclipsed by work added + maintenance debt"),
                "netUtility": net}

    reason = valuation_deviation_reason(invoice, history)
    if reason:
        return {"invoiceId": invoice_id, "verdict": "REVIEW",
                "reason": reason, "netUtility": net}

    return {"invoiceId": invoice_id, "verdict": "PASS",
            "reason": "net utility positive; eligible for human vote",
            "netUtility": net}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _fmt_number(value: float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def print_human(result: dict) -> None:
    """Pretty-printed table for --human."""
    print("Foken — proof-of-contribution valuation gate (coarse pre-filter)")
    print("=" * 66)
    print(f"invoiceId  : {result['invoiceId']}")
    print(f"verdict    : {result['verdict']}")
    print(f"reason     : {result['reason']}")
    net = result["netUtility"]
    if net is not None:
        print(f"net utility: {_fmt_number(net)} h  "
              "(futureWorkAvoided - (workAdded + 1.5 x maintenanceDebt))")
    else:
        print("net utility: not computable")
    print("=" * 66)
    print("Coarse pre-filter only — the binding decision is the human >50% vote.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gate.py",
        description="Foken proof-of-contribution valuation gate "
                    "(off-chain pre-filter).")
    parser.add_argument(
        "invoice", nargs="?", default=None,
        help="path to the Foken Invoice JSON; '-' reads from stdin")
    parser.add_argument(
        "--json", metavar="PATH", dest="json_path", default=None,
        help="alternative way to pass the invoice path (e.g. --json /dev/stdin)")
    parser.add_argument(
        "--history", metavar="PATH", default=None,
        help="history file: JSON array of invoices (or {\"invoices\": [...]}); "
             "the last %d set the median for the valuation-deviation check"
             % HISTORY_N)
    parser.add_argument(
        "--human", action="store_true",
        help="print a human-readable table instead of a JSON line")
    args = parser.parse_args(argv)

    if (args.invoice is None) == (args.json_path is None):
        parser.error("provide the invoice exactly once: positional PATH (or '-') "
                     "or --json PATH")
    path = args.json_path if args.json_path is not None else args.invoice

    try:
        if path == "-":
            invoice = json.load(sys.stdin)
        else:
            with open(path, "r", encoding="utf-8") as fh:
                invoice = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        message = f"could not read invoice from {path!r}: {exc}"
        if args.human:
            print(f"error: {message}", file=sys.stderr)
        else:
            print(json.dumps({"invoiceId": "?", "verdict": "FAIL",
                              "reason": message, "netUtility": None}))
        return EXIT_FAIL

    history = None
    if args.history:
        try:
            history = load_history(args.history)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: ignoring --history ({exc})", file=sys.stderr)

    result = evaluate(invoice, history)
    if args.human:
        print_human(result)
    else:
        print(json.dumps(result, ensure_ascii=False))
    return EXIT_BY_VERDICT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
