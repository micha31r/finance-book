"""SQLite storage.

The important property here is idempotency. Re-running the same file, or a
file that overlaps one already loaded, must leave the database unchanged.
That rests on the unique key for a transaction:

    (account, date, amount, description, occurrence)

The occurrence counter is what makes it safe. Real statements contain genuinely
identical transactions on the same day (four $5.00 EFTPOS charges at the same
shop, for example), so hashing the content alone would silently merge them.
"""
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from parsers.shared.model import RANK
from parsers.shared.money import match_key

DEFAULT_PATH = Path(__file__).parent / "finance.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS account (
    id      INTEGER PRIMARY KEY,
    bank_id INTEGER NOT NULL REFERENCES bank(id),
    bsb     TEXT,
    number  TEXT NOT NULL,
    name    TEXT,
    product TEXT,
    UNIQUE (bank_id, number)
);
CREATE TABLE IF NOT EXISTS document (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id),
    kind            TEXT NOT NULL,
    period_start    TEXT,
    period_end      TEXT,
    statement_no    INTEGER,
    opening_balance INTEGER,
    closing_balance INTEGER,
    source_name     TEXT,
    ingested_at     TEXT NOT NULL,
    UNIQUE (account_id, kind, period_start, period_end)
);
CREATE TABLE IF NOT EXISTS txn (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES account(id),
    document_id    INTEGER NOT NULL REFERENCES document(id),
    occurrence     INTEGER NOT NULL,
    date           TEXT NOT NULL,
    effective_date TEXT,
    description    TEXT NOT NULL,
    match_key      TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    balance        INTEGER,
    type           TEXT,
    category       TEXT,
    counterparty   TEXT,
    reference      TEXT,
    provisional    INTEGER NOT NULL DEFAULT 0,
    verified       INTEGER NOT NULL DEFAULT 0,
    sequence       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, date, amount, match_key, occurrence)
);
CREATE TABLE IF NOT EXISTS transfer (
    id          INTEGER PRIMARY KEY,
    from_txn_id INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
    to_txn_id   INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
    method      TEXT NOT NULL,
    confirmed   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (from_txn_id, to_txn_id)
);
-- Your decision about a transaction, when the bank's wording cannot tell us.
-- Applied after every automatic rule, so it always wins.
CREATE TABLE IF NOT EXISTS manual_type (
    txn_id  INTEGER PRIMARY KEY REFERENCES txn(id) ON DELETE CASCADE,
    type    TEXT NOT NULL,
    note    TEXT,
    set_at  TEXT NOT NULL
);
-- Labels income and spending by where it came from or went. A pattern is a
-- regular expression matched against the description, case-insensitively.
CREATE TABLE IF NOT EXISTS rule (
    id         INTEGER PRIMARY KEY,
    pattern    TEXT NOT NULL,
    category   TEXT NOT NULL,
    type       TEXT,          -- optional: also force income / expense / transfer
    note       TEXT,
    created_at TEXT NOT NULL
);
-- Money you hold that no statement covers: a term deposit, a Sharesies
-- balance. Counted in net worth, never in income or spending. `kind` only
-- decides which tab it appears under.
CREATE TABLE IF NOT EXISTS holding (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'term deposit',
    name        TEXT NOT NULL,
    institution TEXT,
    balance     INTEGER NOT NULL,
    as_at       TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS txn_account_date ON txn(account_id, date);
CREATE INDEX IF NOT EXISTS txn_reference ON txn(reference);
"""


def connect(path=DEFAULT_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Rules are regexes, and more than one query needs to run them.
    conn.create_function("regexp", 2,
                         lambda pattern, text: bool(re.search(pattern, text or "", re.I)))
    conn.executescript(SCHEMA)
    # Columns added after a database already existed.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(txn)")}
    if "sequence" not in have:
        conn.execute("ALTER TABLE txn ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")
    ruled = {r["name"] for r in conn.execute("PRAGMA table_info(rule)")}
    if "type" not in ruled:
        conn.execute("ALTER TABLE rule ADD COLUMN type TEXT")
    held = {r["name"] for r in conn.execute("PRAGMA table_info(holding)")}
    if "kind" not in held:
        conn.execute("ALTER TABLE holding ADD COLUMN kind TEXT NOT NULL"
                     " DEFAULT 'term deposit'")
    return conn


def account_id(conn, doc):
    """Find or create the account, filling in details as sources supply them."""
    bank = conn.execute("SELECT id FROM bank WHERE name = ?", (doc.bank,)).fetchone()
    if bank is None:
        bank_id = conn.execute("INSERT INTO bank(name) VALUES (?)", (doc.bank,)).lastrowid
    else:
        bank_id = bank["id"]
    row = conn.execute("SELECT id, name, bsb, product FROM account WHERE bank_id = ? AND number = ?",
                       (bank_id, doc.number)).fetchone()
    if row is None:
        return conn.execute(
            "INSERT INTO account(bank_id, bsb, number, name, product) VALUES (?,?,?,?,?)",
            (bank_id, doc.bsb, doc.number, doc.account_name, doc.product)).lastrowid
    # A later file often knows more than the first one did.
    conn.execute("UPDATE account SET name = COALESCE(?, name), bsb = COALESCE(?, bsb),"
                 " product = COALESCE(?, product) WHERE id = ?",
                 (doc.account_name, doc.bsb, doc.product, row["id"]))
    return row["id"]


def document_id(conn, acct_id, doc):
    """Upsert the document row. Same account, kind and period means same document."""
    start = doc.period_start.isoformat() if doc.period_start else None
    end = doc.period_end.isoformat() if doc.period_end else None
    existing = conn.execute(
        "SELECT id FROM document WHERE account_id = ? AND kind = ? AND period_start IS ?"
        " AND period_end IS ?", (acct_id, doc.kind, start, end)).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if existing:
        conn.execute("UPDATE document SET opening_balance=?, closing_balance=?, statement_no=?,"
                     " source_name=?, ingested_at=? WHERE id=?",
                     (doc.opening_balance, doc.closing_balance, doc.statement_no,
                      doc.source_name, now, existing["id"]))
        return existing["id"]
    return conn.execute(
        "INSERT INTO document(account_id, kind, period_start, period_end, statement_no,"
        " opening_balance, closing_balance, source_name, ingested_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (acct_id, doc.kind, start, end, doc.statement_no, doc.opening_balance,
         doc.closing_balance, doc.source_name, now)).lastrowid


def write_transactions(conn, acct_id, doc_id, doc, verified):
    """Insert new rows, upgrade rows from weaker sources, leave the rest alone.

    Returns (inserted, upgraded, unchanged, removed).
    """
    counts = Counter()
    inserted = upgraded = unchanged = 0
    seen_ids = []
    for position, txn in enumerate(doc.transactions):
        key = (acct_id, txn.date.isoformat(), txn.amount, match_key(txn.description))
        counts[key] += 1
        full = key + (counts[key],)
        row = conn.execute(
            "SELECT txn.id, txn.document_id, document.kind FROM txn"
            " JOIN document ON document.id = txn.document_id"
            " WHERE txn.account_id=? AND txn.date=? AND txn.amount=? AND txn.match_key=?"
            " AND txn.occurrence=?", full).fetchone()
        effective = txn.effective_date.isoformat() if txn.effective_date else None
        if row is None:
            new_id = conn.execute(
                "INSERT INTO txn(account_id, date, amount, match_key, occurrence, description,"
                " document_id, effective_date, balance, counterparty, reference,"
                " provisional, verified, type, sequence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                full + (txn.description, doc_id, effective, txn.balance, txn.counterparty,
                        txn.reference, int(doc.provisional), int(verified),
                        "income" if txn.amount > 0 else "expense", position)).lastrowid
            seen_ids.append(new_id)
            inserted += 1
        elif doc.rank > RANK[row["kind"]]:
            # A statement supersedes what a provisional listing said. COALESCE
            # keeps what the older source knew: a CSV export outranks a report
            # but carries no balance, and must not erase one.
            conn.execute(
                "UPDATE txn SET document_id=?, description=?,"
                " effective_date=COALESCE(?, effective_date), balance=COALESCE(?, balance),"
                " counterparty=COALESCE(?, counterparty), reference=COALESCE(?, reference),"
                " provisional=?, verified=MAX(verified, ?), sequence=? WHERE id=?",
                (doc_id, txn.description, effective, txn.balance, txn.counterparty,
                 txn.reference, int(doc.provisional), int(verified), position, row["id"]))
            seen_ids.append(row["id"])
            upgraded += 1
        else:
            # Row id follows insertion order, which stops matching the
            # statement as soon as anything is re-ingested, so position within
            # the document is what orders two rows on the same day. Only the
            # document that owns the row may set it: a CSV export covering the
            # same day lists things in its own order, and would otherwise
            # scramble what the statement printed.
            if row["document_id"] == doc_id:
                conn.execute("UPDATE txn SET sequence = ? WHERE id = ?", (position, row["id"]))
            seen_ids.append(row["id"])
            unchanged += 1

    removed = 0
    if not doc.derived_period:
        # Re-ingesting a corrected statement drops rows it no longer lists. Only
        # safe when the bank stated the period: an export's period comes from its
        # own rows, so a shorter export would otherwise delete the difference.
        placeholders = ",".join("?" * len(seen_ids))
        sql = f"DELETE FROM txn WHERE document_id = ? AND id NOT IN ({placeholders})" \
            if seen_ids else "DELETE FROM txn WHERE document_id = ?"
        removed = conn.execute(sql, [doc_id, *seen_ids]).rowcount
    return inserted, upgraded, unchanged, removed
