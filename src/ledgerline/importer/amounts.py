"""Locale-aware amount parsing and decimal-separator inference.

Handles: ``1,234.56`` ``1.234,56`` ``1'234.56`` ``1 234,56`` ``(45.00)`` ``45.00-``
``-€12`` ``$ 1,200`` ``45.00 DR`` ``45.00 CR`` ``USD 12.50``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

_SPACES = re.compile(r"[\s   ']")
_CRDR = re.compile(r"\s*(?<![A-Za-z])(CR|DR|CRED|DEB)\.?\s*$", re.IGNORECASE)
_CURRENCY_CODE = re.compile(r"\b[A-Z]{3}\b")
_NON_NUMERIC = re.compile(r"[^\d.,+\-()]")
_FINAL = re.compile(r"^\d+(\.\d+)?$|^\.\d+$")
_NUMERIC_SHAPE = re.compile(r"^[+\-]?\(?[+\-]?\d[\d.,]*\)?-?$|^[+\-]?\(?[.,]\d+\)?-?$")


def _strip_decorations(text: str) -> tuple[str, int]:
    """Return (core, sign_hint) where sign_hint is -1 for DR, +1 for CR, 0 for none."""
    s = text.strip()
    hint = 0
    m = _CRDR.search(s)
    if m:
        hint = -1 if m.group(1).upper().startswith("D") else 1
        s = s[: m.start()]
    s = _CURRENCY_CODE.sub("", s.upper())
    s = _SPACES.sub("", s)
    s = _NON_NUMERIC.sub("", s)
    return s, hint


def looks_numeric(text: str) -> bool:
    if text is None:
        return False
    core, _ = _strip_decorations(text)
    if not core or not any(ch.isdigit() for ch in core):
        return False
    # Reject strings where letters made up most of the content (e.g. "Store 12").
    letters = sum(ch.isalpha() for ch in text if ch.upper() not in "CRDB")
    if letters > 3:
        return False
    return bool(_NUMERIC_SHAPE.match(core))


def parse_amount(text: str | None, decimal_sep: str = ".") -> Decimal | None:
    if text is None:
        return None
    raw = text.strip()
    if not raw or raw in {"-", "--", "—"}:
        return None
    core, hint = _strip_decorations(raw)
    if not core or not any(ch.isdigit() for ch in core):
        return None

    negative = False
    if core.startswith("(") and core.endswith(")"):
        negative, core = True, core[1:-1]
    core = core.strip("()")
    if core.endswith("-"):
        negative, core = True, core[:-1]
    if core.startswith("-"):
        negative, core = True, core.lstrip("-")
    core = core.lstrip("+")

    thousands = "," if decimal_sep == "." else "."
    core = core.replace(thousands, "")
    if decimal_sep == ",":
        core = core.replace(",", ".")
    if core.count(".") > 1 or not _FINAL.match(core):
        return None
    try:
        value = Decimal(core)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    if hint == -1:
        value = -abs(value)
    elif hint == 1:
        value = abs(value)
    return value


@dataclass
class LocaleGuess:
    decimal_sep: str
    confidence: float
    votes: dict[str, float]


def infer_decimal_separator(values: Iterable[str]) -> LocaleGuess:
    votes = {".": 0.0, ",": 0.0}
    for v in values:
        if not v or not looks_numeric(v):
            continue
        core, _ = _strip_decorations(v)
        core = core.strip("()+-")
        has_dot, has_comma = "." in core, "," in core
        if has_dot and has_comma:
            votes["." if core.rfind(".") > core.rfind(",") else ","] += 3
            continue
        if not (has_dot or has_comma):
            continue
        sep = "." if has_dot else ","
        other = "," if sep == "." else "."
        tail = re.search(r"[.,](\d+)$", core)
        if not tail:
            continue
        n = len(tail.group(1))
        if n in (1, 2):
            votes[sep] += 2
        elif n == 3:
            if core.count(sep) > 1:
                votes[other] += 2  # 1.234.567 → sep is a thousands separator
            else:
                votes[other] += 0.5  # ambiguous "1,234": weak thousands vote
        else:
            votes[sep] += 1
    total = votes["."] + votes[","]
    if total == 0:
        return LocaleGuess(".", 0.5, votes)
    winner = "." if votes["."] >= votes[","] else ","
    return LocaleGuess(winner, votes[winner] / total, votes)
