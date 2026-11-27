"""SQLite connection + forward-only migrations tracked in PRAGMA user_version."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # v1 — core schema
    """
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind TEXT NOT NULL DEFAULT 'checking',
        currency TEXT NOT NULL DEFAULT 'USD',
        opening_balance_minor INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE imports (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        filename TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        rows_total INTEGER NOT NULL,
        rows_inserted INTEGER NOT NULL,
        UNIQUE (account_id, sha256)
    );
    CREATE TABLE transactions (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        import_id INTEGER REFERENCES imports(id),
        date TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        description TEXT NOT NULL,
        merchant TEXT NOT NULL,
        merchant_key TEXT NOT NULL,
        balance_minor INTEGER,
        bank_category TEXT,
        dedupe_key TEXT NOT NULL,
        category TEXT,
        category_source TEXT,
        category_confidence REAL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        transfer_id INTEGER,
        suspect_duplicate_of INTEGER REFERENCES transactions(id),
        notes TEXT
    );
    CREATE INDEX ix_tx_account_date ON transactions(account_id, date);
    CREATE INDEX ix_tx_dedupe ON transactions(dedupe_key);
    CREATE INDEX ix_tx_merchant ON transactions(merchant_key);
    CREATE INDEX ix_tx_category ON transactions(category);
    CREATE TABLE profiles (
        fingerprint TEXT PRIMARY KEY,
        name TEXT,
        plan_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE rules (
        id INTEGER PRIMARY KEY,
        priority INTEGER NOT NULL DEFAULT 100,
        text TEXT NOT NULL,
        builtin INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE models (
        name TEXT PRIMARY KEY,
        blob_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE budgets (
        category TEXT PRIMARY KEY,
        limit_minor INTEGER NOT NULL,
        rollover INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE aliases (
        id INTEGER PRIMARY KEY,
        pattern TEXT NOT NULL,
        name TEXT NOT NULL
    );
    """,
]


def default_path() -> Path:
    env = os.environ.get("LEDGERLINE_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ledgerline" / "ledger.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(p) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
    return len(MIGRATIONS)
