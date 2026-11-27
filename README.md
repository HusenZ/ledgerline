# Ledgerline

A local-first personal expense tracker whose main feature is a **smart CSV importer**:
drop in an export from any bank and it figures out the layout by itself. No column
mapping, no cloud, **no LLMs**: every decision is a deterministic algorithm you can test.

The full plan is in [docs/DESIGN.md](docs/DESIGN.md).

## Status

| Area | State |
|---|---|
| Encoding / delimiter / preamble / footer / header detection | ✅ `importer/sniff.py` |
| Date-format inference (DD/MM vs MM/DD disambiguation) | ✅ `importer/dates.py` |
| Locale-aware amounts (`1.234,56`, `(45.00)`, `DR`/`CR`) | ✅ `importer/amounts.py` |
| Column-role inference (amount, debit/credit split, balance…) | ✅ `importer/columns.py` |
| Sign and row-order inference through balance reconciliation | ✅ `importer/signs.py` |
| Import plan + bank-layout fingerprint | ✅ `importer/pipeline.py` |
| Merchant normalization | ✅ `normalize.py` |
| SQLite schema, migrations, repository | ✅ `storage/` |
| Multiset + fuzzy dedupe | ✅ `dedupe.py` |
| Cross-account transfer matching | ✅ `transfers.py` |
| Rule DSL (tokenizer + recursive-descent parser) | ✅ `categorize/rules.py` |
| Naive Bayes + layered categorizer | ⏳ planned |
| Ingest service wiring import → dedupe → DB | ⏳ planned |
| Recurring/subscription detection, anomalies | ⏳ planned |
| Budgets, cash-flow forecast, reports | ⏳ planned |
| CLI, synthetic demo data, test suite | ⏳ planned |

## Try the importer

Python 3.11+, no runtime dependencies.

```bash
PYTHONPATH=src python3 -c "from ledgerline.importer import import_bytes; r = import_bytes(open('statement.csv','rb').read()); print(r.plan.explain())"
```

Example output for a German bank export (semicolons, comma decimals, 3 preamble
lines, a totals footer, newest-first):

```
delimiter     : ';'   header: yes
date column   : 'Buchungstag'  format %d.%m.%Y
description   : 'Verwendungszweck' + 'Buchungstext'
amount        : 'Betrag'
balance       : 'Saldo'
number locale : decimal separator ','
sign / order  : as-is, newest first (balance reconciliation)
confidence    : 100%
```
