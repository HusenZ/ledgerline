"""Merchant-name normalization.

``SQ *BLUE BOTTLE COF #1234 SAN FRANCISCO CA`` → ``Blue Bottle Cof``
``AMZN Mktp US*2K4L19XY2``                    → ``Amazon``
``POS PURCHASE 03/14 STARBUCKS STORE 08812``  → ``Starbucks``
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PREFIXES = [
    r"PURCHASE AUTHORI[SZ]ED ON \d{1,2}/\d{1,2}",
    r"(?:POS|DEBIT|CREDIT|VISA|MC|CARD|CHECKCARD|CHECK CARD)(?: (?:PURCHASE|TRANSACTION|DEBIT|PAYMENT))*",
    r"RECURRING (?:PAYMENT|PURCHASE)",
    r"(?:ACH|SEPA|DIRECT) (?:DEBIT|CREDIT|LASTSCHRIFT|UEBERWEISUNG)",
    r"LASTSCHRIFT", r"KARTENZAHLUNG", r"GUTSCHRIFT", r"UEBERWEISUNG", r"ÜBERWEISUNG",
    r"SQ ?\*", r"TST ?\*", r"SP ?\*", r"PAYPAL ?\*", r"PP ?\*", r"GOOGLE ?\*", r"APL\*", r"IN ?\*",
]
_PREFIX_RE = re.compile(r"^(?:(?:" + "|".join(_PREFIXES) + r")\s*[-:]?\s*)+")

_US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
              "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()

_NOISE = [
    re.compile(r"\b(?:X{2,}|\*{2,})\d{2,4}\b"),  # masked card numbers
    re.compile(r"\bCARD ?\d{4}\b"),
    re.compile(r"#\s*\d+"),  # store numbers
    re.compile(r"\b(?:STORE|STR|NO|NR)\.? ?\d+\b"),
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\b"),  # dates
    re.compile(r"\b\d{3}[-. ]?\d{3}[-. ]?\d{4}\b"),  # phone numbers
    re.compile(r"\bREF(?:ERENCE)?[: ]?\S+"),
    re.compile(r"\*[A-Z0-9]{6,}\b"),  # AMZN Mktp US*2K4L19XY2
    re.compile(r"\b[A-Z0-9]*\d[A-Z0-9]*\d[A-Z0-9]*\b"),  # tokens with 2+ digits (ids)
    re.compile(r"\bHTTPS?://\S+|\bWWW\.\S+"),
]
_STATE_TAIL = re.compile(r"\s+(?:" + "|".join(_US_STATES) + r")$")
_COUNTRY_TAIL = re.compile(r"\s+(?:US|USA|GB|UK|DEU|FRA|NLD|IRL)$")
_STOPWORDS = {"LASTSCHRIFT", "GUTSCHRIFT", "KARTENZAHLUNG", "PENDING", "PURCHASE", "POS", "SEPA", "ONLINE",
              "DEBIT", "CREDIT", "TRANSACTION", "DIR", "DEP", "THANK", "YOU"}
_TLD = re.compile(r"\.(?:COM|NET|ORG|CO\.UK|DE|IO)\b")

# Canonical aliases: first matching pattern wins. Users can add more in the DB.
ALIASES: list[tuple[str, str]] = [
    (r"^AMZN|^AMAZON|AMZN MKTP|AMAZON\.COM", "Amazon"),
    (r"^UBER\s*\*?\s*EATS", "Uber Eats"),
    (r"^UBER", "Uber"),
    (r"^LYFT", "Lyft"),
    (r"^NETFLIX", "Netflix"),
    (r"^SPOTIFY", "Spotify"),
    (r"^APPLE\.COM|^APPLE COM BILL|^ITUNES", "Apple"),
    (r"^STARBUCKS|^SBUX", "Starbucks"),
    (r"^WAL-?MART|^WM SUPERCENTER", "Walmart"),
    (r"^TARGET", "Target"),
    (r"^COSTCO", "Costco"),
    (r"^WHOLE ?FOODS|^WFM", "Whole Foods"),
    (r"^TRADER JOE", "Trader Joe's"),
    (r"^SHELL\b", "Shell"),
    (r"^CHEVRON", "Chevron"),
    (r"^REWE", "REWE"),
    (r"^LIDL", "Lidl"),
    (r"^ALDI", "Aldi"),
    (r"^DOORDASH|^DD \*?DOORDASH", "DoorDash"),
    (r"^GOOGLE ?\*?\s*(?:YOUTUBE|YT)", "YouTube"),
    (r"^MSFT|^MICROSOFT", "Microsoft"),
    (r"^ADOBE", "Adobe"),
]
_ALIAS_RE = [(re.compile(p), name) for p, name in ALIASES]


@dataclass(frozen=True)
class Merchant:
    name: str
    key: str


def clean(description: str) -> str:
    s = (description or "").upper().strip()
    s = re.sub(r"\s+", " ", s)
    s = _PREFIX_RE.sub("", s)
    for rx in _NOISE:
        s = rx.sub(" ", s)
    s = _TLD.sub(" ", s)
    s = re.sub(r"[^\w&' -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    s = _COUNTRY_TAIL.sub("", s)
    s = " ".join(w for w in s.split() if w not in _STOPWORDS)
    stripped = _STATE_TAIL.sub("", s)
    if stripped != s and len(stripped) >= 3:
        # A US state code ends the string: the 1–2 words before it are the city.
        words = stripped.split()
        drop = 1 if len(words) == 2 else min(2, max(0, len(words) - 2))
        s = " ".join(words[: len(words) - drop])
    return s.strip(" -")


_KEEP_UPPER = {"ATM", "ACH", "BP", "CVS", "IKEA", "H&M", "REWE", "DM", "KFC", "AT&T", "USPS", "UPS"}


def _title(s: str) -> str:
    words = []
    for w in s.split():
        words.append(w if w in _KEEP_UPPER else w.capitalize())
    return " ".join(words)


def merchant(description: str, extra_aliases: list[tuple[str, str]] | None = None) -> Merchant:
    raw = (description or "").upper()
    cleaned = clean(description)
    for patterns in (extra_aliases or []):
        pat, name = patterns
        if re.search(pat, raw, re.IGNORECASE) or re.search(pat, cleaned, re.IGNORECASE):
            return Merchant(name, _key(name))
    for rx, name in _ALIAS_RE:
        if rx.search(cleaned) or rx.search(_PREFIX_RE.sub("", raw)):
            return Merchant(name, _key(name))
    if not cleaned:
        cleaned = re.sub(r"\s+", " ", raw).strip() or "UNKNOWN"
    # Keep at most the first four words: trailing words are usually locations/ids.
    name = _title(" ".join(cleaned.split()[:4]))
    return Merchant(name, _key(name))


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower()) or "unknown"
