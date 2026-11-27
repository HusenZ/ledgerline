"""Import orchestration: bytes → ImportPlan → ParsedTransaction[]."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable

from .amounts import infer_decimal_separator, parse_amount
from .columns import ColumnRoles, infer_roles, normalize_header
from .dates import infer_date_format, parse_date
from .signs import SignGuess, heuristic, reconcile
from .sniff import SniffResult, sniff

_NEGATIVE_INDICATORS = {"DR", "D", "DEBIT", "S", "SOLL", "-"}


class ImportError_(Exception):
    """Raised when a file cannot be understood well enough to import."""


@dataclass
class ImportPlan:
    encoding: str
    delimiter: str
    has_header: bool
    header: list[str]
    roles: dict
    date_format: str
    decimal_sep: str
    flip: bool
    descending: bool
    fingerprint: str
    confidence: float
    sign_method: str
    from_profile: bool = False
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "ImportPlan":
        return cls(**json.loads(s))

    def explain(self) -> str:
        r = ColumnRoles.from_dict(self.roles)
        name = lambda i: f"'{self.header[i]}'" if i is not None and i < len(self.header) else "—"  # noqa: E731
        lines = [
            f"encoding      : {self.encoding}",
            f"delimiter     : {self.delimiter!r}   header: {'yes' if self.has_header else 'no (synthetic names)'}",
            f"date column   : {name(r.date)}  format {self.date_format}",
            f"description   : {name(r.description)}" + (
                f" + {', '.join(name(i) for i in r.description_extra)}" if r.description_extra else ""),
        ]
        if r.debit is not None:
            lines.append(f"amount        : debit {name(r.debit)} / credit {name(r.credit)}")
        else:
            lines.append(f"amount        : {name(r.amount)}" + (f"  (sign from {name(r.indicator)})" if r.indicator is not None else ""))
        if r.balance is not None:
            lines.append(f"balance       : {name(r.balance)}")
        if r.currency is not None:
            lines.append(f"currency      : {name(r.currency)}")
        lines += [
            f"number locale : decimal separator {self.decimal_sep!r}",
            f"sign / order  : {'flip' if self.flip else 'as-is'}, {'newest first' if self.descending else 'oldest first'} ({self.sign_method})",
            f"confidence    : {self.confidence:.0%}" + ("  [from saved bank profile]" if self.from_profile else ""),
        ]
        lines += [f"note          : {n}" for n in self.notes]
        lines += [f"WARNING       : {w}" for w in self.warnings]
        return "\n".join(lines)


@dataclass
class ParsedTransaction:
    date: date
    amount: Decimal  # negative = outflow
    description: str
    balance: Decimal | None = None
    currency: str | None = None
    bank_category: str | None = None
    row: int = 0


@dataclass
class ImportResult:
    plan: ImportPlan
    transactions: list[ParsedTransaction]
    errors: list[str]


@dataclass
class Overrides:
    date_format: str | None = None
    decimal_sep: str | None = None
    flip_sign: bool | None = None


def fingerprint(s: SniffResult) -> str:
    if s.has_header:
        basis = s.delimiter + "|" + "|".join(normalize_header(h) for h in s.header)
    else:
        basis = f"{s.delimiter}|noheader|{len(s.header)}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _raw_amount(row: list[str], r: ColumnRoles, dec: str) -> Decimal | None:
    if r.debit is not None and r.credit is not None:
        d = parse_amount(_cell(row, r.debit), dec)
        c = parse_amount(_cell(row, r.credit), dec)
        if d is None and c is None:
            return None
        return (c.copy_abs() if c else Decimal(0)) - (d.copy_abs() if d else Decimal(0))
    a = parse_amount(_cell(row, r.amount), dec)
    if a is None:
        return None
    if r.indicator is not None:
        ind = _cell(row, r.indicator).strip().upper()
        return -a.copy_abs() if ind in _NEGATIVE_INDICATORS else a.copy_abs()
    return a


def _infer_sign(rows, roles: ColumnRoles, dec: str) -> tuple[ColumnRoles, SignGuess]:
    descs = [_cell(r, roles.description) for r in rows]
    # Amount vs balance is unknown: let arithmetic decide.
    if roles.ambiguous_numeric:
        best: tuple[float, int, int, SignGuess] | None = None
        for a in roles.ambiguous_numeric:
            for b in roles.ambiguous_numeric:
                if a == b:
                    continue
                g = reconcile([parse_amount(_cell(r, a), dec) for r in rows],
                              [parse_amount(_cell(r, b), dec) for r in rows])
                if g and (best is None or g.match_ratio > best[0]):
                    best = (g.match_ratio, a, b, g)
        if best:
            roles.amount, roles.balance = best[1], best[2]
            roles.notes.append("amount and balance columns identified by reconciliation")
            roles.ambiguous_numeric = []
            return roles, best[3]
        roles.ambiguous_numeric = []

    amounts = [_raw_amount(r, roles, dec) for r in rows]
    if roles.balance is not None:
        g = reconcile(amounts, [parse_amount(_cell(r, roles.balance), dec) for r in rows])
        if g:
            if roles.debit is not None or roles.indicator is not None:
                g.flip = False  # sign is explicit in these layouts; reconciliation only fixes order
            return roles, g
        roles.notes.append(f"balance column did not reconcile; ignoring it")
        roles.balance = None
    if roles.debit is not None or roles.indicator is not None:
        return roles, SignGuess(False, False, 0.95, "explicit debit/credit")
    return roles, heuristic(amounts, descs)


def plan_import(data: bytes, overrides: Overrides | None = None,
                profile_lookup: Callable[[str], ImportPlan | None] | None = None) -> tuple[ImportPlan, SniffResult]:
    overrides = overrides or Overrides()
    s = sniff(data)
    if not s.rows:
        raise ImportError_("no tabular data found")
    fp = fingerprint(s)

    if profile_lookup:
        saved = profile_lookup(fp)
        if saved:
            saved.from_profile = True
            saved.encoding = s.encoding
            _apply_overrides(saved, overrides)
            return saved, s

    roles, _ = infer_roles(s.header, s.rows)
    warnings: list[str] = []
    if roles.date is None:
        raise ImportError_("could not find a date column")
    if roles.amount is None and roles.debit is None:
        raise ImportError_("could not find an amount column")
    if roles.description is None:
        warnings.append("no description column found")

    dg = infer_date_format([_cell(r, roles.date) for r in s.rows])
    if dg.fmt is None:
        raise ImportError_("could not determine date format")
    if dg.confidence < 0.6:
        warnings.append(f"date format is ambiguous ({dg.fmt} vs {', '.join(dg.ambiguous_with)}); "
                        f"pass --date-format to override")

    num_cols = [i for i in (roles.amount, roles.debit, roles.credit, roles.balance, *roles.ambiguous_numeric) if i is not None]
    lg = infer_decimal_separator([_cell(r, i) for r in s.rows for i in num_cols])
    dec = overrides.decimal_sep or lg.decimal_sep

    roles, sg = _infer_sign(s.rows, roles, dec)
    if sg.method != "balance reconciliation":
        ds = [d for d in (parse_date(_cell(r, roles.date), dg.fmt) for r in s.rows) if d]
        sg.descending = sum(b < a for a, b in zip(ds, ds[1:])) > sum(b > a for a, b in zip(ds, ds[1:]))
    if sg.confidence < 0.7:
        warnings.append(f"sign convention guessed ({sg.method}); pass --flip-sign if outflows look positive")

    plan = ImportPlan(
        encoding=s.encoding, delimiter=s.delimiter, has_header=s.has_header, header=list(s.header),
        roles=roles.to_dict(), date_format=dg.fmt, decimal_sep=dec, flip=sg.flip, descending=sg.descending,
        fingerprint=fp, confidence=round(min(dg.confidence, max(lg.confidence, 0.6), sg.confidence), 3),
        sign_method=sg.method, warnings=warnings, notes=roles.notes + ([f"date: {dg.reason}"] if dg.reason else []),
    )
    if s.preamble_lines:
        plan.notes.append(f"skipped {s.preamble_lines} preamble line(s)")
    if s.footer_lines:
        plan.notes.append(f"skipped {s.footer_lines} footer line(s)")
    _apply_overrides(plan, overrides)
    return plan, s


def _apply_overrides(plan: ImportPlan, o: Overrides) -> None:
    if o.date_format:
        plan.date_format = o.date_format
        plan.notes.append("date format overridden by user")
    if o.decimal_sep:
        plan.decimal_sep = o.decimal_sep
    if o.flip_sign is not None:
        plan.flip = o.flip_sign
        plan.sign_method = "user override"
        plan.confidence = max(plan.confidence, 0.95)
        plan.warnings = [w for w in plan.warnings if "sign convention" not in w]


def apply_plan(plan: ImportPlan, s: SniffResult) -> tuple[list[ParsedTransaction], list[str]]:
    r = ColumnRoles.from_dict(plan.roles)
    out: list[ParsedTransaction] = []
    errors: list[str] = []
    base = s.preamble_lines + (1 if s.has_header else 0) + 1
    for i, row in enumerate(s.rows):
        d = parse_date(_cell(row, r.date), plan.date_format)
        amt = _raw_amount(row, r, plan.decimal_sep)
        if d is None or amt is None:
            if any(row):
                errors.append(f"line {base + i}: unparseable row {row!r}")
            continue
        if plan.flip:
            amt = -amt
        desc_parts = [_cell(row, r.description)] + [_cell(row, j) for j in r.description_extra]
        desc = " ".join(p for p in desc_parts if p).strip() or "(no description)"
        bal = parse_amount(_cell(row, r.balance), plan.decimal_sep) if r.balance is not None else None
        cur = _cell(row, r.currency).upper() or None if r.currency is not None else None
        cat = _cell(row, r.bank_category) or None if r.bank_category is not None else None
        out.append(ParsedTransaction(d, amt, desc, bal, cur, cat, base + i))
    if plan.descending:
        out.reverse()
    # Stable chronological order (some exports are only roughly sorted).
    out.sort(key=lambda t: t.date)
    return out, errors


def import_bytes(data: bytes, overrides: Overrides | None = None,
                 profile_lookup: Callable[[str], ImportPlan | None] | None = None) -> ImportResult:
    plan, s = plan_import(data, overrides, profile_lookup)
    txns, errors = apply_plan(plan, s)
    if s.rows and len(errors) > 0.2 * len(s.rows):
        plan.warnings.append(f"{len(errors)} of {len(s.rows)} rows could not be parsed")
    return ImportResult(plan, txns, errors)
