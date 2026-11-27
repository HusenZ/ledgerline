# Ledgerline — Design Plan

> A local-first personal finance engine that can swallow **any** bank's CSV export
> without a hand-written mapping, then turns the raw rows into a clean, deduplicated,
> categorized ledger with subscription detection, anomaly alerts, budgets and a
> cash-flow forecast.
>
> **No LLMs, no cloud APIs.** Every "smart" behaviour is a deterministic algorithm or a
> classical statistical model that you can read, test and explain.

---

## 1. Why this is hard (and why it is fun)

Every bank exports CSV differently. A real-world importer has to survive:

| Problem | Example |
|---|---|
| Unknown encoding | UTF-8 with BOM, Windows-1252 umlauts, UTF-16 from Excel |
| Unknown delimiter | `,` `;` `\t` `|` |
| Preamble / footer junk | 6 lines of "Account: ****1234 / Period: …" before the header, "Total" row at the end |
| Unknown columns | `Buchungstag`, `Posting Date`, `Fecha`, or no header at all |
| Ambiguous dates | `03/04/2026` — March 4th or April 3rd? |
| Locale-specific numbers | `1.234,56` vs `1,234.56` vs `1'234.56`, `(45.00)`, `45.00 DR`, `-€12` |
| Sign conventions | Credit cards report purchases as **positive**; checking accounts as negative; some banks split into Debit/Credit columns; some use a `DR`/`CR` indicator column |
| Row order | Newest-first vs oldest-first |
| Overlapping statements | Importing Jan–Mar and then Mar–May must not double-count March |
| Legit duplicates | Two identical $4.50 coffees on the same day are **not** duplicates |
| Transfers | Paying the credit card from checking is not "spending" |
| Messy merchants | `SQ *BLUE BOTTLE COF #1234 SAN FRANCISCO CA` → `Blue Bottle Coffee` |

Ledgerline solves each with an explicit, testable algorithm.

---

## 2. Architecture

```
            ┌──────────────────────────── Smart CSV Import ─────────────────────────────┐
 raw bytes ─▶ sniff.encoding ─▶ sniff.dialect ─▶ sniff.table_region ─▶ columns.infer_roles
            │                                                         │
            │                    dates.infer_format ◀─────────────────┤
            │                    amounts.infer_locale ◀───────────────┤
            │                    amounts.infer_sign (balance reconciliation) ◀─┘
            │                    profiles.fingerprint  (remember this bank's layout)
            └──────────────────────────────▶ ImportPlan (+ confidence + warnings)
                                                │
                                                ▼
                        normalize.merchant  ─▶  dedupe (multiset + fuzzy)  ─▶  SQLite ledger
                                                                                 │
          ┌────────────────────────────┬────────────────────┬────────────────────┤
          ▼                            ▼                    ▼                    ▼
  categorize.engine              transfers.match     analysis.recurring    analysis.anomalies
  (rules DSL → merchant memory   (cross-account      (period detection,    (robust z-score,
   → Naive Bayes, w/ confidence)  pairing)            price-hike alerts)    dup charges, spikes)
          │                                                 │
          ▼                                                 ▼
   active learning loop                            analysis.forecast (daily cash-flow
   (user corrections retrain)                      simulation) + analysis.budgets
                                                            │
                                                            ▼
                                             CLI · text reports · HTML report (inline SVG)
```

### Package layout

```
src/ledgerline/
  money.py                 Decimal ↔ minor units, currency exponents
  importer/
    sniff.py               encoding, delimiter, table region, header detection
    dates.py               date-format inference & DMY/MDY disambiguation
    amounts.py             locale-aware amount parsing, decimal-separator inference
    columns.py             column-role inference (scoring + constraint assignment)
    signs.py               sign-convention & row-order inference via balance reconciliation
                           (bank-layout fingerprinting lives in pipeline.py)
    pipeline.py            orchestration → ImportPlan → ParsedTransaction[]
  normalize.py             merchant cleaning + alias canonicalization
  dedupe.py                multiset dedupe keys + fuzzy pending/posted matching
  transfers.py             cross-account transfer pairing
  categorize/
    rules.py               tokenizer + recursive-descent parser for the rule DSL
    bayes.py               multinomial Naive Bayes (from scratch, incremental)
    engine.py              layered classifier with confidence + provenance
  analysis/
    recurring.py           subscription / bill detection, next-date prediction
    anomalies.py           MAD-based outliers, duplicate charges, category spikes
    budgets.py             envelope budgets with rollover and pace projection
    forecast.py            day-by-day cash-flow simulation
  storage/
    db.py                  SQLite schema + migrations (PRAGMA user_version)
    repo.py                data access layer
  reports/
    text.py                terminal tables & bar charts (no deps)
    html.py                self-contained HTML report with inline SVG charts
  synth.py                 realistic synthetic data in 3 different bank formats
  cli.py                   argparse CLI
```

Zero runtime dependencies — standard library only. `pytest` for tests.

---

## 3. Key algorithms

### 3.1 Encoding detection
1. BOM check (UTF-8-sig, UTF-16 LE/BE).
2. Strict UTF-8 decode.
3. Fallback to CP1252 (covers most Western bank exports), finally Latin-1 (never fails).

### 3.2 Dialect + table region
For each candidate delimiter, parse every line and compute the **modal field count**
and the **longest contiguous run** of rows with that count. The delimiter whose longest
run is largest (ties → more fields) wins. That run *is* the table: preamble and footer
lines fall outside it automatically.

### 3.3 Header detection
The first row of the region is a header if its cells are "type-different" from the
body: e.g. a header cell is non-numeric/non-date while ≥80 % of the body cells in that
column parse as numbers/dates. Otherwise synthetic names `col_0…col_n` are used.

### 3.4 Date format inference
Try ~20 candidate formats over **all** values in a column. Keep formats with ≥ 98 %
success. If several survive (`%d/%m/%Y` vs `%m/%d/%Y`), break the tie by:
1. **Order consistency** – a bank export is sorted; the right format yields the fewest
   inversions in the date sequence.
2. **Span plausibility** – the right format yields a tighter date range.
If still tied, pick the default and emit a *low-confidence warning* (overridable with
`--date-format`).

### 3.5 Amount locale inference
Vote across all values: a separator followed by exactly 1–2 trailing digits is a
decimal vote; a separator followed by groups of exactly 3 digits is a thousands vote;
when both `.` and `,` appear, the right-most one is decimal. Parser handles currency
symbols/codes, NBSP, apostrophe grouping, parentheses negatives, trailing minus,
`CR`/`DR` suffixes.

### 3.6 Column-role inference
Each column gets a feature vector (date ratio, numeric ratio, empty ratio, negative
ratio, mean text length, distinct ratio, header keyword hits in EN/DE/FR/ES/NL/IT).
Each (column, role) pair is scored, then roles are assigned greedily by score under
constraints:

* exactly one `date`, one `description`
* **either** `amount` **or** (`debit` + `credit`)
* optional `balance`, `currency`, `indicator` (DR/CR), `bank_category`

Debit/credit split is detected by **complementary emptiness**: two numeric columns
where, on ≥ 90 % of rows, exactly one is filled.

### 3.7 Sign & order inference by balance reconciliation
If a running-balance column exists, test the four hypotheses
`{ascending, descending} × {amount, −amount}` and count rows where
`balance[i] = balance[i−1] + signed_amount[i]` (to the cent). The winning hypothesis
gives both the row order and the sign convention with near-certainty.

Without a balance: if > 65 % of amounts are positive and there is no debit/credit
split, the file is assumed to be a credit-card style export (purchases positive) and
the sign is flipped. Confidence is reported as *medium*; `--flip-sign` overrides and the
choice is saved in the bank profile.

### 3.8 Bank profiles
`fingerprint = sha1(delimiter | normalized header names)`. A confirmed plan is stored;
the next file with the same fingerprint skips inference (but is still validated).

### 3.9 Merchant normalization
Pipeline of regex passes: strip processor prefixes (`SQ *`, `TST*`, `PAYPAL *`,
`POS`, `CARD PURCHASE`, `PURCHASE AUTHORIZED ON 03/14`), masked card numbers, store
numbers, trailing city/state, phone numbers, long digit runs, dates; collapse spaces;
then apply an alias table (`AMZN MKTP` → `Amazon`). Produces a display name and a
stable `merchant_key`.

### 3.10 Deduplication — multiset semantics
`key = sha1(account | date | amount | normalized description)`.
For each key, `to_insert = count_in_file − count_already_in_db`. This keeps legit
same-day duplicates and makes overlapping statement imports idempotent.
A second **fuzzy** pass flags pending→posted pairs: same account and amount, dates within
±3 days, merchant similarity ≥ 0.85.

### 3.11 Transfer detection
Pair outflow `−x` in account A with inflow `+x` in account B within ±4 days,
preferring smallest date gap and transfer-ish keywords. Matched pairs get a shared
`transfer_id` and are excluded from income/expense reports.

### 3.12 Categorization — layered, explainable
1. **User rules** (DSL, priority-ordered) — confidence 1.0
2. **Merchant memory** — majority label of user-confirmed transactions for the same
   merchant (≥ 2 confirmations, ≥ 80 % agreement)
3. **Multinomial Naive Bayes** — features: merchant word tokens, character 3-grams,
   log-amount bucket, inflow/outflow flag; Laplace smoothing; log-sum-exp
   probabilities; incremental (counts only), serialized as JSON in SQLite.
4. **Starter rules** — low-priority seed rules for cold start.

Every transaction stores `category_source` and `category_confidence`.
`categorize review` surfaces the least confident ones first (active learning);
each correction updates merchant memory and the model immediately.

Rule DSL example:
```
merchant ~ "starbucks|blue bottle"                 -> Food:Coffee
description contains "NETFLIX" or merchant = "Spotify" -> Entertainment:Streaming
amount < -1000 and merchant ~ "property|rent"       -> Housing:Rent
not (amount < 0) and description ~ "payroll|salary" -> Income:Salary
```
Grammar (recursive descent):
```
rule    := expr "->" CATEGORY
expr    := term ("or" term)*
term    := factor ("and" factor)*
factor  := "not" factor | "(" expr ")" | compare
compare := FIELD OP VALUE
FIELD   := merchant | description | amount | account | weekday | day
OP      := ~ | contains | = | != | > | < | >= | <=
```

### 3.13 Recurring detection
Group by `merchant_key` (+ sign). For ≥ 3 occurrences compute day intervals; for each
candidate period (weekly 7±1, biweekly 14±2, monthly 30.4±4, quarterly 91±7, yearly
365±10) score the fraction of intervals that are ≈ k·period (k = 1 or 2, missed
occurrences penalised). Amount stability via robust CV (MAD/median) classifies
*subscription* (fixed) vs *bill* (variable). Outputs: cadence, typical amount, next
expected date (month-aware), annualised cost, **price-change alerts**, and **lapsed**
status when an expected charge is overdue.

### 3.14 Anomaly detection
* Robust z-score per category: `|x − median| / (1.4826·MAD) > 3.5` (≥ 6 samples)
* First-time merchant with amount above the 95th percentile of all spending
* Possible duplicate charge: same merchant + amount within 48 h
* Category spike: month total > 1.5× trailing 6-month median and > threshold

### 3.15 Budgets
Hierarchical (`Food` covers `Food:Coffee`), monthly envelopes, optional rollover of
unspent amount, **pace projection** (`spent / elapsed_fraction`) → *on track / at risk /
over*.

### 3.16 Cash-flow forecast
Day-by-day simulation over N days: start from current account balances, add each
recurring item on its predicted dates, add the median daily discretionary spend
(non-recurring outflows, trailing 90 days). Reports the projected low point and the
first date the balance would cross a threshold.

---

## 4. Data model (SQLite)

```
accounts(id, name UNIQUE, kind, currency, opening_balance_minor)
imports(id, account_id, filename, sha256 UNIQUE, plan_json, imported_at, rows_total, rows_inserted)
transactions(id, account_id, import_id, date, amount_minor, currency, description,
             merchant, merchant_key, balance_minor, dedupe_key,
             category, category_source, category_confidence, confirmed,
             transfer_id, suspect_duplicate_of, notes)
profiles(fingerprint PK, name, plan_json, created_at)
rules(id, priority, text, category, builtin)
models(name PK, blob_json, updated_at)
budgets(category PK, limit_minor, rollover)
```
Money is always stored as integer minor units — never floats.

---

## 5. CLI

```
ledgerline init
ledgerline account add Checking --kind checking --currency USD --opening-balance 2500
ledgerline import statement.csv --account Checking [--dry-run] [--flip-sign] [--date-format %d/%m/%Y]
ledgerline tx list --month 2026-10 [--category Food] [--search uber] [--uncategorized]
ledgerline categorize review | set <id> <category> | rerun
ledgerline rules add 'merchant ~ "uber" -> Transport:Rideshare' | list | test '<rule>' | remove <id>
ledgerline recurring
ledgerline anomalies
ledgerline budget set Food 600 [--rollover] | status [--month 2026-10]
ledgerline forecast --days 60
ledgerline summary --month 2026-10
ledgerline report --out report.html
ledgerline demo           # generate 12 months of data in 3 bank formats and import it
```

---

## 6. Milestones

1. **Import engine** — sniffing, dates, amounts, roles, signs, profiles (+ heavy tests)
2. **Ledger** — schema, multiset dedupe, fuzzy dupes, transfers
3. **Categorization** — DSL parser, Naive Bayes, engine, review loop
4. **Analytics** — recurring, anomalies, budgets, forecast
5. **Interfaces** — CLI, text reports, HTML report, synthetic demo
6. **Stretch** — OFX/QIF import, multi-currency with FX table, rule suggestion mining
   (find frequent n-grams in user corrections and propose rules), local web UI,
   split transactions, encrypted DB.

## 7. Testing strategy
* Unit tests per algorithm with hand-crafted edge cases (ambiguous dates, `1.234,56`,
  preambles, BOMs, DR/CR columns).
* Property-style round-trip: synthetic generator writes the same ledger in three bank
  formats → importing each must reproduce identical transactions.
* Idempotency: importing overlapping statements twice inserts each transaction once.
* End-to-end: `demo` run asserts subscriptions are found and transfers are paired.
