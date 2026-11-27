"""Money helpers. Amounts are stored as integer minor units, never floats."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ISO 4217 exponents that differ from the default of 2.
_EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "BHD": 3, "KWD": 3, "OMR": 3, "TND": 3}

SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₣": "CHF"}


def exponent(currency: str) -> int:
    return _EXPONENTS.get((currency or "").upper(), 2)


def to_minor(amount: Decimal, currency: str = "USD") -> int:
    exp = exponent(currency)
    q = Decimal(1).scaleb(-exp)
    return int((amount.quantize(q, rounding=ROUND_HALF_UP)).scaleb(exp))


def from_minor(minor: int, currency: str = "USD") -> Decimal:
    return Decimal(minor).scaleb(-exponent(currency))


def fmt(minor: int, currency: str = "USD", signed: bool = True) -> str:
    value = from_minor(minor, currency)
    exp = exponent(currency)
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    body = f"{abs(value):,.{exp}f}"
    return f"{sign}{body} {currency}" if currency else f"{sign}{body}"
