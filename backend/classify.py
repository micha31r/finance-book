#!/usr/bin/env python3
"""Set the type of transactions the bank's wording cannot explain.

Some movements name no counterparty. "DETAILS ADVISED SEPARATELY" and Westpac's
bare "WITHDRAWAL ONLINE <ref> TFR" are both money going into a term deposit,
but nothing in the text says so. Only you know. What you decide here is stored
and reapplied after every ingest.

    python classify.py list                        what is still unexplained
    python classify.py set transfer --like "DETAILS ADVISED SEPARATELY"
    python classify.py set transfer --id 4131 4132
    python classify.py clear --like "..."          undo
"""
import argparse
import sys
from datetime import datetime, timezone

import db
import reconcile
from parsers.shared.money import money_str

BIG = 100_000        # cents. Below this, a stray row barely moves a total.


def rows_matching(conn, pattern, ids):
    if ids:
        marks = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT t.id, t.date, t.amount, t.type, t.description, a.number, b.name bank"
            f" FROM txn t JOIN account a ON a.id=t.account_id JOIN bank b ON b.id=a.bank_id"
            f" WHERE t.id IN ({marks}) ORDER BY t.date", ids).fetchall()
    return conn.execute(
        "SELECT t.id, t.date, t.amount, t.type, t.description, a.number, b.name bank"
        " FROM txn t JOIN account a ON a.id=t.account_id JOIN bank b ON b.id=a.bank_id"
        " WHERE t.description LIKE ? ORDER BY t.date", (pattern,)).fetchall()


def show(rows):
    for r in rows:
        print(f"  {r['id']:>6} {r['bank']:8s} {r['number']:10s} {r['date']} "
              f"{money_str(r['amount']):>13}  {r['type']:8s} {r['description'][:52]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--min", type=int, default=BIG,
                                        help="cents, default 100000 ($1000)")
    for name in ("set", "clear"):
        p = sub.add_parser(name)
        if name == "set":
            p.add_argument("type", choices=["transfer", "income", "expense"])
        p.add_argument("--like", help="SQL LIKE pattern against the description")
        p.add_argument("--id", nargs="+", type=int)
        p.add_argument("--note")

    args = parser.parse_args()
    conn = db.connect()

    if args.command == "list":
        rows = conn.execute(
            "SELECT t.id, t.date, t.amount, t.type, t.description, a.number, b.name bank"
            " FROM txn t JOIN account a ON a.id=t.account_id JOIN bank b ON b.id=a.bank_id"
            " WHERE t.type IN ('income','expense') AND ABS(t.amount) >= ?"
            "   AND t.id NOT IN (SELECT txn_id FROM manual_type)"
            " ORDER BY ABS(t.amount) DESC", (args.min,)).fetchall()
        print(f"{len(rows)} unexplained movements over {money_str(args.min)}:")
        show(rows)
        print("\nTag them with:  python classify.py set transfer --like \"...\"")
        return 0

    if not args.like and not args.id:
        print("give --like or --id")
        return 1
    rows = rows_matching(conn, args.like, args.id)
    if not rows:
        print("nothing matched")
        return 1
    print(f"{len(rows)} transactions:")
    show(rows)

    if args.command == "set":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.executemany(
            "INSERT INTO manual_type(txn_id, type, note, set_at) VALUES (?,?,?,?)"
            " ON CONFLICT(txn_id) DO UPDATE SET type=excluded.type, note=excluded.note,"
            " set_at=excluded.set_at",
            [(r["id"], args.type, args.note, now) for r in rows])
        print(f"\nmarked as {args.type}")
    else:
        conn.executemany("DELETE FROM manual_type WHERE txn_id = ?", [(r["id"],) for r in rows])
        print("\ncleared, back to the automatic classification")

    reconcile.reclassify(conn)
    conn.commit()
    total = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount END),0) i,"
        " -COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) e FROM txn").fetchone()
    print(f"income now {money_str(total['i'])}, spending {money_str(total['e'])}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
