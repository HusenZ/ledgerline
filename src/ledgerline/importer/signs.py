"""Sign-convention and row-order inference.

Internal convention: **negative = money leaving the account**.

With a running balance we can *prove* the convention: for the right hypothesis,
``balance[i] == balance[i-1] + signed_amount[i]`` holds on (almost) every row when
rows are walked in chronological order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


@dataclass
class SignGuess:
    flip: bool  # multiply raw amounts by -1
    descending: bool  # file lists newest first
    confidence: float
    method: str
    match_ratio: float = 0.0


def reconcile(amounts: Sequence[Decimal | None], balances: Sequence[Decimal | None]) -> SignGuess | None:
    """Test {ascending, descending} × {as-is, flipped} against the balance column."""
    pairs = [(a, b) for a, b in zip(amounts, balances) if a is not None and b is not None]
    if len(pairs) < 3:
        return None
    best: SignGuess | None = None
    for descending in (False, True):
        seq = list(reversed(pairs)) if descending else pairs
        for flip in (False, True):
            hits = 0
            for (_, prev_bal), (amt, bal) in zip(seq, seq[1:]):
                signed = -amt if flip else amt
                if abs(prev_bal + signed - bal) < Decimal("0.005"):
                    hits += 1
            ratio = hits / (len(seq) - 1)
            if best is None or ratio > best.match_ratio:
                best = SignGuess(flip, descending, ratio, "balance reconciliation", ratio)
    if best and best.match_ratio >= 0.6:
        return best
    return None


def heuristic(amounts: Sequence[Decimal | None], descriptions: Sequence[str]) -> SignGuess:
    """No balance column: use the shape of the data."""
    vals = [a for a in amounts if a is not None and a != 0]
    if not vals:
        return SignGuess(False, False, 0.3, "no data")
    pos_share = sum(1 for a in vals if a > 0) / len(vals)
    if pos_share > 0.65:
        # Looks like a credit-card export: purchases positive, payments negative.
        payment_like = [a for a, d in zip(amounts, descriptions) if a is not None and _is_payment(d)]
        if payment_like and sum(1 for a in payment_like if a < 0) / len(payment_like) >= 0.5:
            return SignGuess(True, False, 0.85, "credit-card style: purchases positive, payments negative")
        return SignGuess(True, False, 0.6, f"{pos_share:.0%} of amounts positive: assuming purchases are positive")
    return SignGuess(False, False, 0.75, "mixed signs: assuming outflows are negative")


_PAYMENT_WORDS = ("payment", "thank you", "autopay", "zahlung", "paiement", "pago")


def _is_payment(desc: str) -> bool:
    d = (desc or "").lower()
    return any(w in d for w in _PAYMENT_WORDS)
