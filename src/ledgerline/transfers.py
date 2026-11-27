"""Cross-account transfer detection.

Paying a credit card from checking produces −x in one account and +x in another a
few days apart. Those must not count as spending *or* income. We solve a greedy
bipartite matching: candidate pairs are scored by date gap and transfer-ish wording,
then accepted best-first so each transaction joins at most one pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .storage.repo import Transaction

MAX_GAP_DAYS = 4
_HINT = re.compile(r"transfer|xfer|payment|thank you|autopay|umbuchung|uebertrag|übertrag|virement|"
                   r"credit card|card payment|to savings|from checking|from savings|to checking|zelle to self",
                   re.IGNORECASE)


@dataclass
class TransferPair:
    outflow: Transaction
    inflow: Transaction
    score: float


def find_transfers(txns: Sequence[Transaction]) -> list[TransferPair]:
    pool = [t for t in txns if t.transfer_id is None and t.amount_minor != 0]
    inflows: dict[int, list[Transaction]] = {}
    for t in pool:
        if t.amount_minor > 0:
            inflows.setdefault(t.amount_minor, []).append(t)

    candidates: list[TransferPair] = []
    for out in pool:
        if out.amount_minor >= 0:
            continue
        for inc in inflows.get(-out.amount_minor, []):
            if inc.account_id == out.account_id:
                continue
            gap = abs((inc.date - out.date).days)
            if gap > MAX_GAP_DAYS:
                continue
            hints = bool(_HINT.search(out.description)) + bool(_HINT.search(inc.description))
            if hints == 0 and abs(out.amount_minor) < 1000:
                continue  # small, unworded coincidences are not transfers
            score = hints * 2 - gap * 0.5 + (1 if gap <= 1 else 0)
            if hints or gap <= 2:
                candidates.append(TransferPair(out, inc, score))

    candidates.sort(key=lambda p: p.score, reverse=True)
    used: set[int] = set()
    result = []
    for p in candidates:
        if p.outflow.id in used or p.inflow.id in used:
            continue
        used.update((p.outflow.id, p.inflow.id))
        result.append(p)
    return result
