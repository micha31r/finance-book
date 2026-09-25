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
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
from merchants import merchant
from parsers.shared.money import money_str

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
    export downloaded after the last statement, or were typed in on the page.
    Leaving them out would shift every running balance derived from this one.
    """
    known = db.last_known_balance(conn, account_id)
    if known is None:
        return None, None
    later = conn.execute(
        # A what-if is a plan: the bank holds no such money yet.
        "SELECT COALESCE(SUM(amount), 0) AS net, MAX(date) AS last FROM txn"
        " WHERE account_id = ? AND date > ? AND whatif = 0",
        (account_id, known["period_end"])).fetchone()
    return known["closing_balance"] + later["net"], later["last"] or known["period_end"]


def export(conn):
    accounts, transactions = [], []
    # How many rows each repeating plan has, by the id of its first row. One
    # row left on its own, after a statement replaced the rest, is no series.
    series = {r["series"]: r["n"] for r in conn.execute(
        "SELECT series, COUNT(*) n FROM txn WHERE series IS NOT NULL"
        " GROUP BY series HAVING COUNT(*) >= 2")}
    for row in conn.execute(
            "SELECT a.id, a.number, a.bsb, a.name, a.product, b.name AS bank"
            " FROM account a JOIN bank b ON b.id = a.bank_id"
            " ORDER BY b.name, a.number"):
        balance, as_at = current_balance(conn, row["id"])
        rows = conn.execute(
            "SELECT t.id, t.date, t.effective_date, t.description, t.amount, t.type,"
            " t.category, t.balance, t.provisional, t.whatif, t.series,"
            " d.kind = 'manual' AS manual"
            " FROM txn t JOIN document d ON d.id = t.document_id"
            " WHERE t.account_id = ? ORDER BY t.date, t.sequence, t.id", (row["id"],)).fetchall()
        # A row typed in on the page and dated inside one of these is one the
        # bank has since reported on. The page flags it: if it happened, the
        # bank's own row is here too, and it is counted twice.
        periods = conn.execute(
            "SELECT period_start, period_end FROM document WHERE account_id = ?"
            " AND kind != 'manual' AND period_start IS NOT NULL", (row["id"],)).fetchall()

        # Walk backwards from the known current balance. Statements and CSV
        # exports disagree about whether they print a balance at all, so a
        # derived one is the only series that covers every row. A what-if is
        # skipped: it is a plan, and no real balance moved for it.
        running = balance
        derived = [None] * len(rows)
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["whatif"]:
                continue
            derived[i] = running
            if running is not None:
                running -= rows[i]["amount"]
        # A what-if dated after the day the balance is known as at shows where
        # it would go: that balance plus every what-if from then on. One dated
        # earlier shows nothing. The bank's figure for that day stands, and a
        # plan the bank has since reported on adds nothing to the ones after it.
        planned = 0
        for i, txn in enumerate(rows):
            if txn["whatif"] and as_at is not None and txn["date"] > as_at:
                planned += txn["amount"]
                derived[i] = balance + planned

        for txn, balance_after in zip(rows, derived):
            covered = 0
            if txn["manual"]:
                covered = int(any(p["period_start"] <= txn["date"] <= p["period_end"]
                                  for p in periods))
            transactions.append({
                "id": txn["id"],
                "account": row["id"],
                # `date` is the bank's posting date: it orders the statement and
                # the running balance. `spent_on` is when the money actually
                # moved. They differ by up to 8 days, and the gap is why the
                # posting dates land on no weekend at all: a card tap on
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
                "whatif": txn["whatif"],
                # Only a row typed in on the page can be deleted or have its
                # date or amount changed.
                "manual": txn["manual"],
                "series": series.get(txn["series"], 0),
                "covered": covered,
            })
        accounts.append({
            "id": row["id"],
            "bank": row["bank"],
            "label": label(row["bank"], row["product"], row["name"], row["number"]),
            "number": row["number"],
            "bsb": row["bsb"],
            "balance": balance,
            "as_at": as_at,
            # Real rows: a what-if is a plan, not a transaction the account had.
            "count": sum(1 for r in rows if not r["whatif"]),
        })

    # Hit counts are left out: running every pattern over every row took most
    # of export's two seconds, and only the rules page shows them. It asks
    # GET /api/rules, which counts them then.
    rules = [dict(r) for r in conn.execute("SELECT id, pattern, category FROM rule ORDER BY id")]
    holdings = [dict(r) for r in conn.execute(
        "SELECT id, kind, name, institution, balance, as_at, note"
        " FROM holding ORDER BY kind, name")]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounts": accounts,
        "holdings": holdings,
        "rules": rules,
        "transactions": transactions,
    }


def main():
    # Only for --help, and to refuse arguments it does not take.
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    conn = db.connect()
    data = export(conn)

    # The derived balance must agree with every balance a bank actually printed.
    stated = {r["id"]: r["balance"] for r in conn.execute(
        "SELECT id, balance FROM txn WHERE balance IS NOT NULL")}
    mismatches = [t for t in data["transactions"]
                  if t["id"] in stated and t["balance"] != stated[t["id"]]]
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
    print(f"net worth: {money_str(net_worth)}")
    if mismatches:
        print(f"WARNING: {len(mismatches)} derived balances disagree with the printed ones")
        for t in mismatches[:5]:
            print(f"  {t['date']} {money_str(t['amount'])}  {t['description'][:52]}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
