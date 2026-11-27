"""Column-role inference.

Each column gets a feature vector computed from its *values* (date/number ratios,
emptiness, sign mix, text shape) plus multilingual header keyword hits. Roles are
then assigned greedily under structural constraints. Debit/credit splits are found by
*complementary emptiness*; amount-vs-balance ambiguity is left to the sign module,
which can verify it arithmetically.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Sequence

from .amounts import looks_numeric
from .dates import infer_date_format

SAMPLE = 300

KEYWORDS: dict[str, list[str]] = {
    "date": ["date", "datum", "fecha", "data", "buchungstag", "booking date", "trans date", "transaction date",
             "posting date", "posted", "buchung", "dato", "день"],
    "secondary_date": ["value date", "valuta", "wertstellung", "post date", "posting date", "posted date",
                       "settlement", "date valeur"],
    "description": ["description", "desc", "memo", "payee", "narrative", "details", "merchant", "name",
                    "beschreibung", "verwendungszweck", "buchungstext", "empfanger", "auftraggeber", "libelle",
                    "concepto", "descripcion", "omschrijving", "descrizione", "particulars", "transaction details",
                    "counterparty", "beneficiary", "text"],
    "amount": ["amount", "betrag", "importe", "montant", "bedrag", "importo", "sum", "umsatz", "valor",
               "transaction amount", "amt"],
    "debit": ["debit", "debits", "withdrawal", "withdrawals", "paid out", "money out", "soll", "cargo",
              "ausgang", "charge", "charges", "spent", "out amount", "debit amount"],
    "credit": ["credit", "credits", "deposit", "deposits", "paid in", "money in", "haben", "abono",
               "eingang", "received", "in amount", "credit amount", "payment received"],
    "balance": ["balance", "saldo", "solde", "kontostand", "running balance", "available balance", "bal"],
    "currency": ["currency", "wahrung", "devise", "moneda", "ccy", "cur", "waehrung"],
    "indicator": ["dr cr", "cr dr", "debit credit", "s h", "indicator", "sign", "d c", "credit debit"],
    "bank_category": ["category", "kategorie", "categorie", "categoria"],
}

_INDICATOR_VALUES = {"DR", "CR", "D", "C", "DEBIT", "CREDIT", "S", "H", "SOLL", "HABEN", "-", "+"}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$|^[$€£¥₹]$")


def normalize_header(h: str) -> str:
    h = unicodedata.normalize("NFKD", h or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", h).strip()


def header_hit(header: str, role: str) -> float:
    h = normalize_header(header)
    if not h:
        return 0.0
    best = 0.0
    for kw in KEYWORDS[role]:
        if h == kw:
            return 1.0
        if re.search(rf"\b{re.escape(kw)}\b", h):
            best = max(best, 0.7)
    return best


@dataclass
class ColumnFeatures:
    index: int
    header: str
    fill: float
    date_ratio: float
    numeric_ratio: float
    negative_ratio: float
    alpha_ratio: float
    avg_len: float
    distinct_ratio: float
    is_indicator: bool
    is_currency: bool

    def describe(self) -> str:
        return (f"fill={self.fill:.2f} date={self.date_ratio:.2f} num={self.numeric_ratio:.2f} "
                f"neg={self.negative_ratio:.2f} alpha={self.alpha_ratio:.2f} len={self.avg_len:.1f}")


def column_features(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[ColumnFeatures]:
    sample = rows[:SAMPLE]
    feats = []
    for j, name in enumerate(header):
        col = [r[j] if j < len(r) else "" for r in sample]
        filled = [c for c in col if c]
        n = len(filled)
        if n == 0:
            feats.append(ColumnFeatures(j, name, 0, 0, 0, 0, 0, 0, 0, False, False))
            continue
        numeric = [c for c in filled if looks_numeric(c)]
        neg = sum(1 for c in numeric if c.lstrip().startswith(("-", "(")) or c.rstrip().endswith("-"))
        alpha = sum(1 for c in filled if any(ch.isalpha() for ch in c))
        num_ratio = len(numeric) / n
        dr = infer_date_format(filled, need=0.0).success_ratio if num_ratio < 1.0 or any(
            ch in filled[0] for ch in "/-.") else 0.0
        upper = [c.upper() for c in filled]
        feats.append(ColumnFeatures(
            index=j,
            header=name,
            fill=n / len(col),
            date_ratio=dr,
            numeric_ratio=num_ratio,
            negative_ratio=neg / len(numeric) if numeric else 0.0,
            alpha_ratio=alpha / n,
            avg_len=sum(len(c) for c in filled) / n,
            distinct_ratio=len(set(filled)) / n,
            is_indicator=sum(u in _INDICATOR_VALUES for u in upper) / n >= 0.95 and len(set(upper)) <= 4,
            is_currency=sum(bool(_CURRENCY_RE.match(u)) for u in upper) / n >= 0.95,
        ))
    return feats


@dataclass
class ColumnRoles:
    date: int | None = None
    description: int | None = None
    description_extra: list[int] = field(default_factory=list)
    amount: int | None = None
    debit: int | None = None
    credit: int | None = None
    balance: int | None = None
    currency: int | None = None
    indicator: int | None = None
    bank_category: int | None = None
    # Numeric columns that could be amount or balance when headers don't tell us.
    ambiguous_numeric: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("notes", "ambiguous_numeric")}

    @classmethod
    def from_dict(cls, d: dict) -> "ColumnRoles":
        allowed = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


def _complementary(a: int, b: int, rows: Sequence[Sequence[str]]) -> float:
    """Fraction of rows where exactly one of the two columns is filled."""
    sample = rows[:SAMPLE]
    if not sample:
        return 0.0
    xor = sum(1 for r in sample if bool(r[a].strip()) != bool(r[b].strip()))
    return xor / len(sample)


def infer_roles(header: Sequence[str], rows: Sequence[Sequence[str]]) -> tuple[ColumnRoles, list[ColumnFeatures]]:
    feats = column_features(header, rows)
    roles = ColumnRoles()
    used: set[int] = set()

    # 1. Date: strong value signal; penalise "value date"/"posting date" style secondaries.
    date_cands = [f for f in feats if f.date_ratio >= 0.9]
    if date_cands:
        def date_score(f: ColumnFeatures) -> float:
            return f.date_ratio * 3 + header_hit(f.header, "date") * 2 - header_hit(f.header, "secondary_date") * 1.5 - f.index * 0.01
        best = max(date_cands, key=date_score)
        roles.date = best.index
        used.update(f.index for f in date_cands)  # secondary date columns are never amounts/descriptions
        if len(date_cands) > 1:
            roles.notes.append(f"multiple date columns; using '{best.header}'")

    # 2. Small categorical columns.
    for f in feats:
        if f.index in used:
            continue
        if f.is_currency and roles.currency is None:
            roles.currency = f.index
            used.add(f.index)
        elif f.is_indicator and roles.indicator is None and f.numeric_ratio < 0.5:
            roles.indicator = f.index
            used.add(f.index)

    # 3. Numeric columns.
    numeric = [f for f in feats if f.index not in used and f.numeric_ratio >= 0.9 and f.fill > 0]
    pair = None
    best_pair_score = 0.0
    for i, a in enumerate(numeric):
        for b in numeric[i + 1:]:
            comp = _complementary(a.index, b.index, rows)
            if comp >= 0.9 and 0.02 < a.fill < 0.98 and 0.02 < b.fill < 0.98:
                score = comp + header_hit(a.header, "debit") + header_hit(b.header, "credit") \
                    + header_hit(b.header, "debit") + header_hit(a.header, "credit")
                if score > best_pair_score:
                    pair, best_pair_score = (a, b), score
    if pair:
        a, b = pair
        a_debit = header_hit(a.header, "debit") + header_hit(b.header, "credit")
        b_debit = header_hit(b.header, "debit") + header_hit(a.header, "credit")
        if a_debit == b_debit:
            # No header evidence: expenses are usually the more frequent kind.
            a_debit, b_debit = a.fill, b.fill
            roles.notes.append("debit/credit split found by complementary emptiness (headers unclear)")
        roles.debit, roles.credit = (a.index, b.index) if a_debit >= b_debit else (b.index, a.index)
        used.update((a.index, b.index))

    full_numeric = [f for f in numeric if f.index not in used and f.fill >= 0.9]
    bal = [f for f in full_numeric if header_hit(f.header, "balance") > 0]
    if bal:
        roles.balance = max(bal, key=lambda f: header_hit(f.header, "balance")).index
        used.add(roles.balance)
        full_numeric = [f for f in full_numeric if f.index != roles.balance]

    if roles.debit is None:
        amt = [f for f in full_numeric if header_hit(f.header, "amount") > 0]
        if amt:
            roles.amount = max(amt, key=lambda f: header_hit(f.header, "amount")).index
        elif full_numeric:
            # Prefer columns with mixed signs (balances are rarely negative), then leftmost.
            ranked = sorted(full_numeric, key=lambda f: (-(0 < f.negative_ratio < 1), f.index))
            roles.amount = ranked[0].index
            if roles.balance is None and len(full_numeric) >= 2:
                roles.ambiguous_numeric = [f.index for f in full_numeric]
                roles.notes.append("amount/balance not identifiable from headers; will verify arithmetically")
        if roles.amount is not None:
            used.add(roles.amount)
    if roles.balance is None and not roles.ambiguous_numeric:
        rest = [f for f in full_numeric if f.index not in used]
        if len(rest) == 1 and (roles.amount is not None or roles.debit is not None):
            roles.balance = rest[0].index
            used.add(roles.balance)
            roles.notes.append(f"assuming '{rest[0].header}' is a running balance (will verify)")

    # 4. Description and category (text columns).
    text = [f for f in feats if f.index not in used and f.alpha_ratio >= 0.5 and f.date_ratio < 0.5]
    cat = [f for f in text if header_hit(f.header, "bank_category") > 0]
    if cat:
        roles.bank_category = cat[0].index
        text = [f for f in text if f.index != roles.bank_category]

    def desc_score(f: ColumnFeatures) -> float:
        return (f.alpha_ratio * 1.5 + min(f.avg_len / 20, 1.5) + f.distinct_ratio
                + header_hit(f.header, "description") * 2 + f.fill)

    if text:
        ranked = sorted(text, key=desc_score, reverse=True)
        roles.description = ranked[0].index
        for f in ranked[1:]:
            if header_hit(f.header, "description") > 0 and f.distinct_ratio > 0.2:
                roles.description_extra.append(f.index)
    return roles, feats
