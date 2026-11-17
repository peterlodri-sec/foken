#!/usr/bin/env python3
"""Foken — exact-rational ledger (prototype).

Half 4 of the Foken governance design: after `gate.py` has priced a claim and
`tally.py` has reached consensus, *someone has to do the arithmetic*. This
module does it with fractions only — no floats, no rounding drift, and a
result that is reproducible on any machine from the two integers that were
agreed on.

The rule for a claimed compression is deliberately conservative. Given the
fraction that *survives* compression, ``numerator/denominator``:

    saved = original - floor(original * numerator / denominator)

That is the tokens *actually* removed, floored. Flooring can only ever
understate a saving, never overstate one, which is the only direction a
collections ledger is allowed to be wrong in. `denominator <= 0` is rejected:
a zero ratio is not a 0% saving, it is a malformed claim.

Stdlib only (fractions, math). Python 3.11+.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


class LedgerError(ValueError):
    """A ledger input that cannot mean anything sensible."""


def _require_non_negative(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise LedgerError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise LedgerError(f"{name} must be non-negative, got {value}")
    return value


def compression_savings(original_tokens: int, numerator: int, denominator: int) -> int:
    """Tokens saved when ``numerator/denominator`` of `original_tokens` survives.

    ``numerator/denominator`` is the *retained* fraction, so ``3/10`` means a
    claim that 30% of the tokens are left and 70% were saved. The result is
    floored, so a ledger can never claim more than was removed.

    Raises `LedgerError` when the ratio is malformed or the saving would exceed
    the original (a ratio above 1 means the claim is nonsense, not generous).
    """
    _require_non_negative("original_tokens", original_tokens)
    _require_non_negative("numerator", numerator)
    _require_non_negative("denominator", denominator)
    if denominator == 0:
        raise LedgerError("denominator must be positive; a zero ratio is not a claim")

    ratio = Fraction(numerator, denominator)
    if ratio > 1:
        raise LedgerError(f"ratio {numerator}/{denominator} exceeds 1: cannot save more than exists")
    return original_tokens - (original_tokens * ratio).numerator // (original_tokens * ratio).denominator


def compression_ratio(original_tokens: int, compressed_tokens: int) -> Fraction:
    """The exact achieved ratio ``compressed/original`` as a `Fraction`.

    Reported as a fraction, not a percentage: ``3/10`` is the claim, ``30.0%`` is
    a rendering of it. `original_tokens == 0` is an error rather than ``0``.
    """
    _require_non_negative("original_tokens", original_tokens)
    _require_non_negative("compressed_tokens", compressed_tokens)
    if original_tokens == 0:
        raise LedgerError("original_tokens must be positive to state a ratio")
    if compressed_tokens > original_tokens:
        raise LedgerError("compressed_tokens exceeds original_tokens")
    return Fraction(compressed_tokens, original_tokens)


@dataclass(frozen=True)
class LedgerEntry:
    """One settled contribution: who, how much claimed, how much verified."""

    recipient: str
    claimed_tokens: int
    verified_tokens: int

    def __post_init__(self) -> None:
        _require_non_negative("claimed_tokens", self.claimed_tokens)
        _require_non_negative("verified_tokens", self.verified_tokens)
        if self.verified_tokens > self.claimed_tokens:
            raise LedgerError(
                f"verified {self.verified_tokens} exceeds claimed {self.claimed_tokens}"
            )
        if not self.recipient:
            raise LedgerError("recipient must be a non-empty string")

    @property
    def fokens(self) -> int:
        """Payout: the *verified* tokens, never the claimed ones."""
        return self.verified_tokens

    def as_payload(self) -> dict[str, object]:
        """Mint-ready view, integer-only so it survives JSON round-trips."""
        return {
            "recipient": self.recipient,
            "claimedTokens": self.claimed_tokens,
            "verifiedTokens": self.verified_tokens,
            "fokens": self.fokens,
        }


def settle(entries: list[LedgerEntry]) -> dict[str, object]:
    """Sum a batch of entries into one deterministic payload.

    Duplicate recipients are summed, not merged into one claim: two separate
    contributions are two separate savings. The total is recomputed from the
    entries so it can never disagree with its parts.
    """
    by_recipient: dict[str, int] = {}
    for entry in entries:
        by_recipient[entry.recipient] = by_recipient.get(entry.recipient, 0) + entry.fokens
    total = sum(by_recipient.values())
    return {
        "entries": len(entries),
        "recipients": len(by_recipient),
        "payouts": dict(sorted(by_recipient.items())),
        "totalFokens": total,
    }
