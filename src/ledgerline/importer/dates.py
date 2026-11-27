"""Date-format inference with DMY/MDY disambiguation.

Instead of guessing per value, we evaluate every candidate format against the whole
column and pick the one that (1) parses nearly everything, (2) produces a consistently
ordered sequence, and (3) produces the tightest plausible date range.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

CANDIDATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d/%m/%y",
    "%m/%d/%y",
    "%d.%m.%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d-%b-%y",
    "%b %d, %Y",
)

_TIME_SUFFIX = re.compile(r"[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*(AM|PM|Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE)
_MIN_YEAR, _MAX_YEAR = 1970, 2100


def clean_date_text(text: str) -> str:
    s = (text or "").strip().strip('"')
    s = _TIME_SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s)


def parse_date(text: str, fmt: str) -> date | None:
    s = clean_date_text(text)
    if not s:
        return None
    try:
        d = datetime.strptime(s, fmt).date()
    except ValueError:
        return None
    if not (_MIN_YEAR <= d.year <= _MAX_YEAR):
        return None
    return d


@dataclass
class DateGuess:
    fmt: str | None
    success_ratio: float
    confidence: float
    ambiguous_with: list[str] = field(default_factory=list)
    reason: str = ""


def _inversions(dates: Sequence[date]) -> int:
    """Count adjacent order breaks relative to the dominant direction."""
    up = sum(1 for a, b in zip(dates, dates[1:]) if b < a)
    down = sum(1 for a, b in zip(dates, dates[1:]) if b > a)
    return min(up, down)


def date_ratio(values: Sequence[str]) -> float:
    """Fraction of non-empty values parseable by the best single format (fast path)."""
    guess = infer_date_format(values, need=0.0)
    return guess.success_ratio


def infer_date_format(values: Sequence[str], need: float = 0.98) -> DateGuess:
    sample = [v for v in values if v and v.strip()]
    if not sample:
        return DateGuess(None, 0.0, 0.0, reason="no values")

    scored: list[tuple[str, float, list[date]]] = []
    for fmt in CANDIDATE_FORMATS:
        parsed: list[date] = []
        ok = 0
        for v in sample:
            d = parse_date(v, fmt)
            if d is not None:
                ok += 1
                parsed.append(d)
        ratio = ok / len(sample)
        if ratio > 0:
            scored.append((fmt, ratio, parsed))
    if not scored:
        return DateGuess(None, 0.0, 0.0, reason="no format matched")

    best_ratio = max(r for _, r, _ in scored)
    survivors = [s for s in scored if s[1] >= max(need, best_ratio - 1e-9)]
    if not survivors:
        fmt, ratio, _ = max(scored, key=lambda s: s[1])
        return DateGuess(fmt, ratio, ratio * 0.5, reason="below success threshold")
    if len(survivors) == 1:
        fmt, ratio, _ = survivors[0]
        return DateGuess(fmt, ratio, ratio, reason="unique format")

    # Tie-break 1: order consistency. Tie-break 2: tightest date span.
    def key(s: tuple[str, float, list[date]]):
        _, _, parsed = s
        span = (max(parsed) - min(parsed)).days if parsed else 10**9
        return (_inversions(parsed), span)

    ranked = sorted(survivors, key=key)
    best, runner = ranked[0], ranked[1]
    k_best, k_runner = key(best), key(runner)
    others = [s[0] for s in ranked[1:]]
    if k_best == k_runner:
        return DateGuess(best[0], best[1], 0.5, others, reason="ambiguous: identical order and span")
    reason = "fewer order inversions" if k_best[0] != k_runner[0] else "tighter date span"
    return DateGuess(best[0], best[1], 0.85 if k_best[0] != k_runner[0] else 0.7, others, reason=reason)
