"""Duplicate handling.

1. **Multiset dedupe** — every row gets a content key. For each key we insert only
   ``count_in_file − count_in_db`` rows. Overlapping statements become idempotent while
   two genuine identical coffees on the same day are both kept.
2. **Fuzzy dedupe** — a pending charge and its posted version often differ by a few days
   and a slightly different description. Those are flagged, not silently dropped.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Iterable, Sequence

FUZZY_DAYS = 3
FUZZY_SIMILARITY = 0.85


def _norm_desc(desc: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", desc.lower())


def dedupe_key(account_id: int, d: date, amount_minor: int, description: str) -> str:
    basis = f"{account_id}|{d.isoformat()}|{amount_minor}|{_norm_desc(description)}"
    return hashlib.sha1(basis.encode()).hexdigest()


@dataclass
class Candidate:
    date: date
    amount_minor: int
    merchant_key: str
    key: str
    payload: dict


def select_new(candidates: Sequence[Candidate], existing_counts: Counter) -> tuple[list[Candidate], int]:
    """Return (rows to insert, number skipped as exact duplicates)."""
    seen: Counter = Counter()
    keep, skipped = [], 0
    for c in candidates:
        seen[c.key] += 1
        if seen[c.key] > existing_counts.get(c.key, 0):
            keep.append(c)
        else:
            skipped += 1
    return keep, skipped


def similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def find_fuzzy(new: Iterable[Candidate], existing: Iterable[tuple[int, date, int, str]]) -> int:
    """Flag candidates that probably duplicate an existing transaction; returns how many.

    ``existing`` rows are ``(id, date, amount_minor, merchant_key)`` from *other imports*.
    Each existing row can absorb at most one candidate.
    """
    by_amount: dict[int, list[tuple[int, date, str]]] = defaultdict(list)
    for tid, d, amt, mk in existing:
        by_amount[amt].append((tid, d, mk))
    used: set[int] = set()
    flagged = 0
    for c in new:
        best = None
        for tid, d, mk in by_amount.get(c.amount_minor, []):
            if tid in used:
                continue
            gap = abs((d - c.date).days)
            if 0 < gap <= FUZZY_DAYS:
                sim = similarity(mk, c.merchant_key)
                if sim >= FUZZY_SIMILARITY and (best is None or (gap, -sim) < best[0]):
                    best = ((gap, -sim), tid)
        if best:
            used.add(best[1])
            flagged += 1
            c.payload["suspect_duplicate_of"] = best[1]
    return flagged
