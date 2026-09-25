#!/usr/bin/env python3
"""Replace every amount and account number with a random one, for a demo.

This changes finance.db for good. Keep a copy of the file to put back, or load
your statements again afterwards.

Dates, types, categories, transfer pairs and the words of every description
stay as they are. The new numbers agree with each other, so the page shows no
warnings:

- an amount keeps its sign and its number of digits: $4.50 becomes something
  from $1.00 to $9.99
- both legs of a transfer, and every row of a repeating plan, share one amount
- running balances and every document's opening and closing balance are
  worked out again from the new amounts. Each account starts high enough that
  it never drops below zero.
- each of your account numbers and BSBs gets one random stand-in, used
  everywhere it appears, so transfers still point at the right account
- an amount written inside a description, like "12.99 USD", is replaced too
"""
import argparse
import random
import re
import sys
from collections import defaultdict

import db
from parsers.shared.money import match_key

# An amount with cents inside a description, like 12.99 or 1,234,567.00.
MONEY = re.compile(r"(?<![\d.,])\d[\d,]*\.\d\d(?![\d.,])")


def random_like(token):
    """A random number shaped like `token`, which starts with a digit.

    Commas and the dot stay where they were. The first digit is never 0, so
    the number keeps its size.
    """
    rest = re.sub(r"\d", lambda _: random.choice("0123456789"), token[1:])
    return random.choice("123456789") + rest


def swap(text, stand_in):
    """`text` with your account numbers swapped and any amounts replaced.

    Only a whole run of digits is swapped, so a reference that happens to
    contain an account number stays. A BSB written straight before an account
    number, as in 012345123456789, is swapped half by half.
    """
    if text is None:
        return None

    def number(match):
        run = match[0]
        if run in stand_in:
            return stand_in[run]
        if run[:6] in stand_in and run[6:] in stand_in:
            return stand_in[run[:6]] + stand_in[run[6:]]
        return run

    return MONEY.sub(lambda match: random_like(match[0]), re.sub(r"\d+", number, text))


def scramble_text(conn):
    """Swap every account number and BSB, and the amounts written in text.

    Counterparties are included, since one names an account you hold, and so
    are the deposit numbers in holding names. Shorter numbers there, like a
    year, stay.
    """
    numbers = {row[0] for row in conn.execute(
        "SELECT number FROM account UNION SELECT bsb FROM account"
        " UNION SELECT counterparty FROM txn")} - {None}
    for (name,) in conn.execute("SELECT name FROM holding"):
        numbers.update(re.findall(r"\d{6,}", name))
    stand_in = {number: random_like(number) for number in numbers}
    for table, column in (("account", "number"), ("account", "bsb"), ("txn", "counterparty"),
                          ("txn", "description"), ("rule", "pattern"), ("holding", "name")):
        rows = conn.execute(f"SELECT id, {column} FROM {table}").fetchall()
        conn.executemany(f"UPDATE {table} SET {column} = ? WHERE id = ?",
                         [(swap(value, stand_in), row_id) for row_id, value in rows])


def scramble_amounts(conn):
    """Give every row a random amount, then work the balances out again."""
    rows = conn.execute(
        "SELECT id, account_id, date, amount, description, occurrence, balance, whatif, series"
        " FROM txn ORDER BY account_id, date, sequence, id").fetchall()
    # The match key comes from the description, which now holds new numbers.
    keys = {row["id"]: match_key(row["description"]) for row in rows}
    partner = dict(conn.execute(
        "SELECT to_txn_id, from_txn_id FROM transfer WHERE confirmed = 1").fetchall())
    groups = defaultdict(list)
    for row in rows:
        groups[row["series"] or partner.get(row["id"], row["id"])].append(row)

    # Rows are unique on (account, date, amount, match key, occurrence), so an
    # amount that would give two rows one key is drawn again.
    amount, taken = {}, set()
    for group in groups.values():
        while True:
            size = int(random_like(str(abs(group[0]["amount"]))))
            signed = {row["id"]: size if row["amount"] > 0 else -size for row in group}
            unique = {(row["account_id"], row["date"], signed[row["id"]], keys[row["id"]],
                       row["occurrence"]) for row in group}
            if len(unique) == len(group) and not unique & taken:
                break
        taken |= unique
        amount.update(signed)

    by_account = defaultdict(list)
    for row in rows:
        if not row["whatif"]:       # a plan moves no balance
            by_account[row["account_id"]].append(row)
    balance = {}
    for (account,) in conn.execute("SELECT id FROM account").fetchall():
        real = by_account[account]
        running, lowest, after = 0, 0, {}
        for row in real:
            running += amount[row["id"]]
            after[row["id"]] = running
            lowest = min(lowest, running)
        # High enough that the balance never drops below zero, plus $100 to $5,000.
        start = random.randint(10_000, 500_000) - lowest
        balance.update({row_id: start + moved for row_id, moved in after.items()})
        # A document states the balance at the start of its first day and at the
        # end of its last, or states none.
        for doc in conn.execute(
                "SELECT id, period_start, period_end, opening_balance, closing_balance"
                " FROM document WHERE account_id = ?", (account,)).fetchall():
            opening, closing = doc["opening_balance"], doc["closing_balance"]
            if opening is not None:
                opening = start + sum(amount[row["id"]] for row in real
                                      if row["date"] < doc["period_start"])
            if closing is not None:
                closing = start + sum(amount[row["id"]] for row in real
                                      if row["date"] <= doc["period_end"])
            conn.execute("UPDATE document SET opening_balance = ?, closing_balance = ? WHERE id = ?",
                         (opening, closing, doc["id"]))

    # Every row first moves far above any real amount, so no new amount lands
    # on a key another row still has while they change one by one.
    conn.execute("UPDATE txn SET amount = amount + 1000000000000000")
    conn.executemany(
        "UPDATE txn SET amount = ?, match_key = ?, balance = ? WHERE id = ?",
        [(amount[row["id"]], keys[row["id"]],
          None if row["balance"] is None else balance[row["id"]], row["id"]) for row in rows])


def scramble(conn):
    """Replace every amount and account number. The caller commits."""
    scramble_text(conn)
    scramble_amounts(conn)
    holdings = conn.execute("SELECT id, balance FROM holding").fetchall()
    # The page accepts a negative holding, so the sign is kept.
    conn.executemany("UPDATE holding SET balance = ? WHERE id = ?",
                     [(int(random_like(str(abs(h["balance"])))) * (-1 if h["balance"] < 0 else 1),
                       h["id"]) for h in holdings])


def main():
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    try:
        answer = input(f"This replaces every amount and account number in {db.DEFAULT_PATH.name}"
                       " for good. Type yes to go on: ")
    except EOFError:
        answer = ""         # no terminal, or the input closed
    if answer.strip().lower() != "yes":
        print("nothing changed")
        return 1
    conn = db.connect()
    scramble(conn)
    conn.commit()
    conn.close()
    print("done. Run export.py, or restart serve.py, to see it on the page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
