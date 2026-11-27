"""Data-access layer. All SQL lives here."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from ..importer.pipeline import ImportPlan


@dataclass
class Account:
    id: int
    name: str
    kind: str
    currency: str
    opening_balance_minor: int


@dataclass
class Transaction:
    id: int
    account_id: int
    date: date
    amount_minor: int
    currency: str
    description: str
    merchant: str
    merchant_key: str
    category: str | None = None
    category_source: str | None = None
    category_confidence: float | None = None
    confirmed: bool = False
    transfer_id: int | None = None
    suspect_duplicate_of: int | None = None
    balance_minor: int | None = None
    bank_category: str | None = None
    dedupe_key: str = ""
    account_name: str = ""

    @property
    def is_outflow(self) -> bool:
        return self.amount_minor < 0

    @property
    def is_transfer(self) -> bool:
        return self.transfer_id is not None


_TX_COLS = ("t.id, t.account_id, t.date, t.amount_minor, t.currency, t.description, t.merchant, t.merchant_key, "
            "t.category, t.category_source, t.category_confidence, t.confirmed, t.transfer_id, "
            "t.suspect_duplicate_of, t.balance_minor, t.bank_category, t.dedupe_key, a.name AS account_name")


def _tx(row: sqlite3.Row) -> Transaction:
    d = dict(row)
    d["date"] = date.fromisoformat(d["date"])
    d["confirmed"] = bool(d["confirmed"])
    return Transaction(**d)


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------------------------------------------------------- accounts
    def add_account(self, name: str, kind: str = "checking", currency: str = "USD", opening_minor: int = 0) -> Account:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO accounts(name, kind, currency, opening_balance_minor) VALUES (?,?,?,?)",
                (name, kind, currency.upper(), opening_minor))
        return Account(cur.lastrowid, name, kind, currency.upper(), opening_minor)

    def account(self, name: str) -> Account | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
        return Account(**dict(row)) if row else None

    def account_by_id(self, account_id: int) -> Account:
        return Account(**dict(self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()))

    def accounts(self) -> list[Account]:
        return [Account(**dict(r)) for r in self.conn.execute("SELECT * FROM accounts ORDER BY id")]

    def balance(self, account_id: int) -> int:
        acc = self.account_by_id(account_id)
        s = self.conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE account_id=?",
                              (account_id,)).fetchone()[0]
        return acc.opening_balance_minor + s

    # ----------------------------------------------------------------- imports
    def import_exists(self, account_id: int, sha256: str) -> bool:
        return self.conn.execute("SELECT 1 FROM imports WHERE account_id=? AND sha256=?",
                                 (account_id, sha256)).fetchone() is not None

    def add_import(self, account_id: int, filename: str, sha256: str, plan: ImportPlan, total: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR REPLACE INTO imports(account_id, filename, sha256, plan_json, rows_total, rows_inserted) "
                "VALUES (?,?,?,?,?,0)", (account_id, filename, sha256, plan.to_json(), total))
        return cur.lastrowid

    def finish_import(self, import_id: int, inserted: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE imports SET rows_inserted=? WHERE id=?", (inserted, import_id))

    def imports(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT i.*, a.name AS account FROM imports i JOIN accounts a ON a.id=i.account_id ORDER BY i.id"))

    # ---------------------------------------------------------------- profiles
    def profile(self, fingerprint: str) -> ImportPlan | None:
        row = self.conn.execute("SELECT plan_json FROM profiles WHERE fingerprint=?", (fingerprint,)).fetchone()
        return ImportPlan.from_json(row[0]) if row else None

    def save_profile(self, plan: ImportPlan, name: str | None = None) -> None:
        stored = ImportPlan.from_json(plan.to_json())
        stored.warnings, stored.from_profile = [], False
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO profiles(fingerprint, name, plan_json) VALUES (?,?,?)",
                              (plan.fingerprint, name, stored.to_json()))

    # ------------------------------------------------------------ transactions
    def dedupe_counts(self, account_id: int, keys: Iterable[str]) -> Counter:
        keys = list(set(keys))
        counts: Counter = Counter()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = ",".join("?" * len(chunk))
            for k, n in self.conn.execute(
                    f"SELECT dedupe_key, COUNT(*) FROM transactions WHERE account_id=? AND dedupe_key IN ({q}) "
                    f"GROUP BY dedupe_key", (account_id, *chunk)):
                counts[k] = n
        return counts

    def insert_transactions(self, rows: Sequence[dict]) -> list[int]:
        ids = []
        with self.conn:
            for r in rows:
                cur = self.conn.execute(
                    "INSERT INTO transactions(account_id, import_id, date, amount_minor, currency, description, "
                    "merchant, merchant_key, balance_minor, bank_category, dedupe_key, suspect_duplicate_of) "
                    "VALUES (:account_id, :import_id, :date, :amount_minor, :currency, :description, :merchant, "
                    ":merchant_key, :balance_minor, :bank_category, :dedupe_key, :suspect_duplicate_of)", r)
                ids.append(cur.lastrowid)
        return ids

    def transactions(self, *, account_id: int | None = None, start: date | None = None, end: date | None = None,
                     category: str | None = None, search: str | None = None, uncategorized: bool = False,
                     include_transfers: bool = True, ids: Sequence[int] | None = None,
                     limit: int | None = None, newest_first: bool = False) -> list[Transaction]:
        where, args = [], []
        if account_id is not None:
            where.append("t.account_id = ?"); args.append(account_id)
        if start:
            where.append("t.date >= ?"); args.append(start.isoformat())
        if end:
            where.append("t.date < ?"); args.append(end.isoformat())
        if category:
            where.append("(t.category = ? OR t.category LIKE ?)"); args += [category, category + ":%"]
        if search:
            where.append("(t.description LIKE ? OR t.merchant LIKE ?)"); args += [f"%{search}%"] * 2
        if uncategorized:
            where.append("(t.category IS NULL OR t.category = 'Uncategorized')")
        if not include_transfers:
            where.append("t.transfer_id IS NULL")
        if ids is not None:
            where.append(f"t.id IN ({','.join('?' * len(ids))})" if ids else "0"); args += list(ids)
        sql = f"SELECT {_TX_COLS} FROM transactions t JOIN accounts a ON a.id = t.account_id"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.date DESC, t.id DESC" if newest_first else " ORDER BY t.date, t.id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_tx(r) for r in self.conn.execute(sql, args)]

    def get(self, tx_id: int) -> Transaction | None:
        r = self.transactions(ids=[tx_id])
        return r[0] if r else None

    def set_category(self, tx_id: int, category: str, source: str, confidence: float, confirmed: bool = False) -> None:
        self.conn.execute(
            "UPDATE transactions SET category=?, category_source=?, category_confidence=?, confirmed=? WHERE id=?",
            (category, source, confidence, int(confirmed), tx_id))

    def set_categories(self, updates: Iterable[tuple[str, str, float, int]]) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE transactions SET category=?, category_source=?, category_confidence=? "
                "WHERE id=? AND confirmed=0", list(updates))

    def link_transfer(self, a: int, b: int) -> int:
        tid = (self.conn.execute("SELECT COALESCE(MAX(transfer_id),0)+1 FROM transactions").fetchone()[0])
        with self.conn:
            self.conn.execute("UPDATE transactions SET transfer_id=?, category='Transfer', category_source='transfer',"
                              " category_confidence=1.0 WHERE id IN (?,?)", (tid, a, b))
        return tid

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------- rules
    def add_rule(self, text: str, priority: int = 100, builtin: bool = False) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO rules(priority, text, builtin) VALUES (?,?,?)",
                                    (priority, text, int(builtin)))
        return cur.lastrowid

    def rules(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM rules ORDER BY builtin, priority, id"))

    def remove_rule(self, rule_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0

    # ------------------------------------------------------------------ models
    def load_model(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT blob_json FROM models WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_model(self, name: str, blob: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO models(name, blob_json, updated_at) "
                              "VALUES (?,?,datetime('now'))", (name, json.dumps(blob)))

    # ----------------------------------------------------------------- budgets
    def set_budget(self, category: str, limit_minor: int, rollover: bool) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO budgets(category, limit_minor, rollover) VALUES (?,?,?)",
                              (category, limit_minor, int(rollover)))

    def budgets(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM budgets ORDER BY category"))

    # ----------------------------------------------------------------- aliases
    def add_alias(self, pattern: str, name: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO aliases(pattern, name) VALUES (?,?)", (pattern, name))

    def aliases(self) -> list[tuple[str, str]]:
        return [(r["pattern"], r["name"]) for r in self.conn.execute("SELECT * FROM aliases ORDER BY id")]
