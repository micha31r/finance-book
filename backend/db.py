"""SQLite storage.

The important property here is idempotency. Re-running the same file, or a
file that overlaps one already loaded, must leave the database unchanged.
That rests on the unique key for a transaction:

    (account, date, amount, match_key, occurrence)

match_key is the description with case, spacing and a trailing EFFECTIVE DATE
ignored, so a statement and a CSV export of one transaction share a key. The
occurrence counter is what makes it safe. Real statements contain genuinely
identical transactions on the same day (four $5.00 EFTPOS charges at the same
shop, for example), so hashing the content alone would silently merge them.
"""
import functools
import re
import re._parser as sre_parse
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from re._constants import BRANCH, MAX_REPEAT, MIN_REPEAT

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


def _nodes(items):
    """Every (op, argument) in a parsed pattern, at any depth."""
    for op, av in items:
        yield op, av
        # An atomic group, (?>...), holds its body directly instead of in a tuple.
        for part in av if isinstance(av, (tuple, list)) else [av]:
            subs = part if isinstance(part, list) else [part]
            for sub in subs:
                if isinstance(sub, sre_parse.SubPattern):
                    yield from _nodes(sub)


def risky_pattern(pattern):
    """Why a rule pattern could hang the app, or None when it is safe to run.

    Python's re backtracks. A repeated group holding a variable-length repeat,
    like (\\w+ ?)+, tries every way of splitting a word and never finishes on
    an ordinary 40-character description. Alternatives that can match the same
    text, like (a|aa)+, do the same. While it runs it holds the whole server.
    So such a pattern is refused before it is saved, and never run.
    """
    try:
        tree = sre_parse.parse(pattern)
    except re.error as error:
        return f"not a valid regular expression: {error}"
    repeats = (MAX_REPEAT, MIN_REPEAT)
    for op, av in _nodes(tree):
        if op in repeats and av[1] > 1:
            inside = list(_nodes(av[2]))
            if any(o in repeats and a[0] != a[1] for o, a in inside):
                return "a repeated group that holds another repeat, like (\\w+ ?)+, can run for hours"
            # Which alternatives overlap is hard to tell, so all are refused. One
            # character each, like (a|b), parses as a character class and passes.
            if any(o == BRANCH for o, _ in inside):
                return "a repeated group that holds alternatives, like (a|aa)+, can run for hours"
    # Open-ended repeats, like the three in .*A.*B.*, try every way of sharing the
    # text between them. Two are fine. Three took minutes over all descriptions.
    # An optional character, like " ?", repeats at most once and is not counted.
    if sum(1 for op, av in _nodes(tree) if op in repeats and av[1] > 1 and av[0] != av[1]) > 2:
        return "more than two open-ended repeats, like .*A.*B.*, can run for minutes"
    return None


@functools.lru_cache(maxsize=None)
def _rule_regex(pattern):
    return None if risky_pattern(pattern) else re.compile(pattern, re.I)


def regexp(pattern, text):
    """SQLite's REGEXP. A risky pattern matches nothing instead of hanging."""
    compiled = _rule_regex(pattern)
    return compiled is not None and compiled.search(text or "") is not None


def connect(path=DEFAULT_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Rules are regexes, and more than one query needs to run them.
    conn.create_function("regexp", 2, regexp)
    conn.executescript(SCHEMA)
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

    A statement is complete and balance-checked for its period, so it replaces
    every provisional row dated inside it, whichever file arrives first. The
    keys alone would miss some: an ANZ Plus statement ends a card purchase with
    "Effective Date dd/mm/yyyy" and a Transaction List never does.

    Returns (inserted, upgraded, unchanged, removed).
    """
    counts = Counter()
    inserted = upgraded = unchanged = 0
    seen_ids = []
    statements = conn.execute(
        "SELECT period_start, period_end FROM document WHERE account_id = ? AND kind = 'statement'",
        (acct_id,)).fetchall() if doc.provisional else []
    for position, txn in enumerate(doc.transactions):
        day = txn.date.isoformat()
        key = (acct_id, day, txn.amount, match_key(txn.description))
        counts[key] += 1
        full = key + (counts[key],)
        row = conn.execute(
            "SELECT txn.id, txn.document_id, document.kind FROM txn"
            " JOIN document ON document.id = txn.document_id"
            " WHERE txn.account_id=? AND txn.date=? AND txn.amount=? AND txn.match_key=?"
            " AND txn.occurrence=?", full).fetchone()
        effective = txn.effective_date.isoformat() if txn.effective_date else None
        if row is None and any(s["period_start"] <= day <= s["period_end"] for s in statements):
            unchanged += 1      # a statement already has it, maybe spelled differently
        elif row is None:
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
    if doc.kind == "statement":
        # Rows this statement matched are no longer provisional, so any left in
        # its period are a listing's copies or items that never posted. Their
        # transfer links and manual types are deleted with them. Ingest pairs
        # the new rows again by reference and amount, but a cross-bank link or
        # a manual type has to be set again.
        removed += conn.execute(
            "DELETE FROM txn WHERE account_id = ? AND provisional = 1 AND date BETWEEN ? AND ?",
            (acct_id, doc.period_start.isoformat(), doc.period_end.isoformat())).rowcount
    return inserted, upgraded, unchanged, removed
