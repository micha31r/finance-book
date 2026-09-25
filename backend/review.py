#!/usr/bin/env python3
"""Confirm cross-bank self-transfers.

Transfers inside one bank pair exactly on a shared reference. Between banks
there is no shared key, so these are proposals. Money is only excluded from
income and spending once you say yes.
"""
import argparse
import sys

import db
import reconcile
from parsers.shared.money import money_str


def describe(row):
    # Show the number as well as the name: three ANZ Plus accounts share the
    # holder name, so the name alone does not say which account this is.
    who = f"{row['number']} {row['name'] or ''}".strip()
    return f"{who:<26} {row['date']}  {money_str(row['amount']):>12}  {row['description'][:46]}"


def main():
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    conn = db.connect()
    candidates = reconcile.transfer_candidates(conn, reconcile.owner_aliases(conn))
    if not candidates:
        print("nothing to review")
        return 0

    print(f"{len(candidates)} cross-bank candidate(s)\n")
    linked = skipped = income = 0
    for i, pair in enumerate(candidates, 1):
        credit, debit, days = pair["credit"], pair["debit"], pair["days"]
        print(f"[{i}] {describe(credit)}")
        print(f"  ~ {describe(debit)}")
        print(f"    exact amount, {days} day(s) apart")
        try:
            choice = input("    [y] link  [n] keep as income  [s] skip > ").strip().lower()
        except EOFError:
            choice = "s"        # no terminal, or the input closed
        if choice == "y":
            conn.execute(
                "INSERT OR IGNORE INTO transfer(from_txn_id, to_txn_id, method, confirmed)"
                " VALUES (?,?,'cross-bank',1)", (debit["id"], credit["id"]))
            linked += 1
        elif choice == "n":
            # Remember the decision so the same pair is not proposed again.
            conn.execute(
                "INSERT OR IGNORE INTO transfer(from_txn_id, to_txn_id, method, confirmed)"
                " VALUES (?,?,'rejected',0)", (debit["id"], credit["id"]))
            income += 1
        else:
            skipped += 1
        print()
    reconcile.reclassify(conn)
    conn.commit()
    conn.close()
    print(f"{linked} linked, {income} kept as income/expense, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
