#!/usr/bin/env python3
"""Write the database out as JSON for the frontend.

The frontend is a static page, so it cannot query SQLite. Everything it needs
is decided here, where the schema rules live: which balance is current, what an
account should be called, and a running balance for rows whose source printed
none.

It exits 0 once data.json is written, or 3 if it is written but a derived
balance disagrees with a printed one. An error stops it before the file is
replaced, and Python exits 1. Not 2: Python exits 2 when it cannot run the
script at all. serve.py reads these codes.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
from merchants import merchant

OUTPUT = Path(__file__).resolve().parent.parent / "frontend" / "data.json"
GENERIC_PRODUCTS = {"cash", "account", ""}


def label(bank, product, name, number):
    """A short name for an account, without the bank repeated in it."""
    for candidate in (product, name):
        if not candidate:
            continue
        text = candidate.strip()
        if text.upper().startswith(bank.upper()):
            text = text[len(bank):].strip()
        if text.isupper():
            text = text.title()
        for suffix in (" Account", " Statement"):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
        if text.lower() not in GENERIC_PRODUCTS:
            return text
    return number


def current_balance(conn, account_id):
    """The balance now, and the date it is as at.

    That is the newest closing balance a document states, plus any rows dated
    after it. Those come from a file that states no balance, like an ANZ CSV
    export downloaded after the last statement. Leaving them out would shift
    every running balance derived from this one. When a statement and a
    Transaction List end on the same day the statement wins, because the list
    may predate that day's interest.
    """
    row = conn.execute(
        "SELECT closing_balance, period_end FROM document"
        " WHERE account_id = ? AND closing_balance IS NOT NULL"
        " ORDER BY period_end DESC, kind = 'statement' DESC LIMIT 1", (account_id,)).fetchone()
    if row is None:
        return None, None
    later = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS net, MAX(date) AS last FROM txn"
        " WHERE account_id = ? AND date > ?", (account_id, row["period_end"])).fetchone()
    return row["closing_balance"] + later["net"], later["last"] or row["period_end"]


def export(conn):
    accounts, transactions = [], []
    for row in conn.execute(
            "SELECT a.id, a.number, a.bsb, a.name, a.product, b.name AS bank"
            " FROM account a JOIN bank b ON b.id = a.bank_id"
            " ORDER BY b.name, a.number"):
        balance, as_at = current_balance(conn, row["id"])
        rows = conn.execute(
            "SELECT date, effective_date, description, amount, type, category,"
            " balance, provisional"
            " FROM txn WHERE account_id = ? ORDER BY date, sequence, id", (row["id"],)).fetchall()

        # Walk backwards from the known current balance. Statements and CSV
        # exports disagree about whether they print a balance at all, so a
        # derived one is the only series that covers every row.
        running = balance
        derived = [None] * len(rows)
        for i in range(len(rows) - 1, -1, -1):
            derived[i] = running
            if running is not None:
                running -= rows[i]["amount"]

        for txn, balance_after in zip(rows, derived):
            transactions.append({
                "account": row["id"],
                # `date` is the bank's posting date: it orders the statement and
                # the running balance. `spent_on` is when the money actually
                # moved. They differ by up to 8 days, and the gap is why the
                # posting dates land on no weekend at all — a card tap on
                # Saturday posts on Monday.
                "date": txn["date"],
                "spent_on": txn["effective_date"] or txn["date"],
                "description": txn["description"],
                # The shop, with the card prefix, suburb and dates stripped.
                # Extracted here because the backend owns that logic.
                "merchant": merchant(txn["description"]),
                "amount": txn["amount"],
                "type": txn["type"],
                "category": txn["category"],
                "balance": balance_after,
                "provisional": txn["provisional"],
            })
        accounts.append({
            "id": row["id"],
            "bank": row["bank"],
            "label": label(row["bank"], row["product"], row["name"], row["number"]),
            "number": row["number"],
            "bsb": row["bsb"],
            "balance": balance,
            "as_at": as_at,
            "count": len(rows),
        })

    rules = [dict(r) for r in conn.execute(
        # Count what this pattern matches. Counting rows that merely share the
        # category made every rule in a shared category report the same
        # inflated number: 86 rules claimed 9,315 matches over 3,505 rows.
        "SELECT r.id, r.pattern, r.category,"
        " (SELECT COUNT(*) FROM txn WHERE description REGEXP r.pattern) AS matches"
        " FROM rule r ORDER BY r.id")]
    holdings = [dict(r) for r in conn.execute(
        "SELECT id, kind, name, institution, balance, as_at, note"
        " FROM holding ORDER BY kind, name")]

    # Money sent to an account we hold no statements for, less what came back.
    # If the only such accounts are your term deposits, this is what should be
    # sitting in them right now, and it is a direct check on what you enter.
    parked = conn.execute(
        # A pair only cancels out while both legs are transfers. If you typed one
        # leg as spending, the other is money that went somewhere unheld.
        "SELECT COALESCE(SUM(amount), 0) n FROM txn WHERE type = 'transfer' AND id NOT IN"
        " (SELECT p.from_txn_id FROM transfer p JOIN txn leg ON leg.id = p.to_txn_id"
        "   WHERE p.confirmed = 1 AND leg.type = 'transfer'"
        "  UNION SELECT p.to_txn_id FROM transfer p JOIN txn leg ON leg.id = p.from_txn_id"
        "   WHERE p.confirmed = 1 AND leg.type = 'transfer')").fetchone()["n"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounts": accounts,
        "holdings": holdings,
        "rules": rules,
        "parked": -parked,
        "transactions": transactions,
    }


def main():
    conn = db.connect()
    data = export(conn)

    # The derived balance must agree with every balance a bank actually printed.
    stated = {}
    for row in conn.execute("SELECT account_id, date, amount, balance FROM txn"
                            " WHERE balance IS NOT NULL"):
        stated.setdefault((row["account_id"], row["date"], row["amount"]), set()).add(row["balance"])
    mismatches = [t for t in data["transactions"]
                  if (t["account"], t["date"], t["amount"]) in stated
                  and t["balance"] not in stated[(t["account"], t["date"], t["amount"])]]
    conn.close()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Write a whole file beside the old one, then swap it in, so the page never
    # reads half of one. The pid keeps two exports running at once apart.
    temp = OUTPUT.with_name(f"{OUTPUT.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(temp, OUTPUT)
    size = OUTPUT.stat().st_size / 1_000_000
    print(f"{OUTPUT.name}: {len(data['transactions'])} transactions, "
          f"{len(data['accounts'])} accounts, {size:.1f} MB")
    net_worth = (sum(a["balance"] or 0 for a in data["accounts"])
                 + sum(h["balance"] for h in data["holdings"]))
    print(f"net worth: {net_worth / 100:,.2f}")
    if mismatches:
        print(f"WARNING: {len(mismatches)} derived balances disagree with the printed ones")
        for t in mismatches[:5]:
            print(f"  {t['date']} {t['amount'] / 100:,.2f}  {t['description'][:52]}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
