#!/usr/bin/env python3
"""Label income and spending by where it came from.

`type` says whether money is income, spending or a transfer. It cannot say that
one deposit is salary and another is your parents sending money. A rule matches
a regular expression against the description and attaches a category.

    python rules.py list
    python rules.py add "family" "INTL PAYMENT FROM (FIRST NAME|SECOND NAME)"
    python rules.py test "FIRST NAME"        what would this match
    python rules.py set 32 --pattern "..."   correct one, keeping its place
    python rules.py remove 3

Rules run in the order added and the last match wins, so write a broad rule
first and narrow it with a later one. Totals leave out transfers, as the page
does: money moved between your own accounts is neither income nor spending.
"""
import argparse
import sys
from datetime import datetime, timezone

import db
import reconcile
from parsers.shared.money import money_str


def preview(conn, pattern, limit=12):
    rows = conn.execute(
        # A what-if is a plan, so it is left out of every sum here.
        "SELECT date, amount, type, description FROM txn WHERE description REGEXP ?"
        " AND whatif = 0 ORDER BY ABS(amount) DESC", (pattern,)).fetchall()
    total = sum(r["amount"] for r in rows if r["type"] != "transfer")
    print(f"  matches {len(rows)} transactions, {money_str(total)} net excluding transfers")
    for r in rows[:limit]:
        print(f"    {r['date']} {money_str(r['amount']):>13} {r['type']:8s} {r['description'][:56]}")
    if len(rows) > limit:
        print(f"    ... and {len(rows) - limit} more")
    return rows


def refusal(pattern, category=""):
    """Why a rule cannot be saved, or None. The same rules the page applies."""
    return db.risky_pattern(pattern) or db.category_error(category)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    todo = sub.add_parser("todo")
    todo.add_argument("--min", type=int, default=1, help="only merchants seen this often")
    todo.add_argument("--limit", type=int, default=40)
    sub.add_parser("seed").add_argument("--replace", action="store_true",
                                        help="rewrite the seeded rules from category_seed.py")
    add = sub.add_parser("add")
    add.add_argument("category")
    add.add_argument("pattern")
    add.add_argument("--note")
    sub.add_parser("test").add_argument("pattern")
    sub.add_parser("remove").add_argument("id", type=int)
    # Correcting a rule in place rather than removing and re-adding it. Rules
    # run in id order and the last match wins, so re-adding would move the rule
    # to the end and silently change what it overrides.
    edit = sub.add_parser("set")
    edit.add_argument("id", type=int)
    edit.add_argument("--pattern")
    edit.add_argument("--category")

    args = parser.parse_args()
    conn = db.connect()

    if args.command == "list":
        rows = conn.execute("SELECT * FROM rule ORDER BY id").fetchall()
        if not rows:
            print("no rules yet")
            return 0
        for r in rows:
            # What this pattern matches, not what shares its category. 30
            # categories are used by more than one rule, and counting by
            # category made each of them report the whole category's rows.
            n = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(CASE WHEN type != 'transfer'"
                             " THEN amount END), 0) t FROM txn WHERE description REGEXP ?"
                             " AND whatif = 0",
                             (r["pattern"],)).fetchone()
            print(f"  [{r['id']}] {r['category']:<14} /{r['pattern']}/"
                  f"   {n['c']} rows, {money_str(n['t'])}")
        return 0

    if args.command == "todo":
        import collections
        from merchants import merchant
        # Income as well as spending: a salary no rule labels is as much a gap
        # as a shop. Real rows only: a what-if is a plan you typed in yourself.
        counts = collections.Counter(
            merchant(r["description"]) for r in conn.execute(
                "SELECT description FROM txn WHERE type != 'transfer' AND category IS NULL"
                " AND whatif = 0"))
        items = [(n, v) for n, v in counts.most_common() if n and v >= args.min]
        total = conn.execute("SELECT COUNT(*) FROM txn WHERE type != 'transfer'"
                             " AND whatif = 0").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM txn WHERE type != 'transfer'"
                            " AND category IS NOT NULL AND whatif = 0").fetchone()[0]
        print(f"{done}/{total} income and spending rows categorised ({100 * done / total:.0f}%)")
        print(f"{len(items)} merchants and payers still unlabelled, {sum(v for _, v in items)} rows\n")
        for name, seen in items[:args.limit]:
            print(f"  {seen:4d}  {name}")
        if len(items) > args.limit:
            print(f"  ... and {len(items) - args.limit} more")
        return 0

    if args.command == "seed":
        from category_seed import SEED
        # An entry is (category, pattern) or (category, pattern, type).
        entries = [(e[0], e[1], e[2] if len(e) > 2 else None) for e in SEED]
        if args.replace:
            # Rewrite the seeded rules in place, in the file's order. Deleting
            # and re-adding them would move every one after your own rules,
            # where a broad one like Paying people would override yours.
            others = {r["pattern"] for r in conn.execute(
                "SELECT pattern FROM rule WHERE note IS NOT 'seed'")}
            entries = [e for e in entries if e[1] not in others]
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM rule WHERE note = 'seed' ORDER BY id")]
            kept = ids[:len(entries)]
            conn.executemany("UPDATE rule SET category = ?, pattern = ?, type = ? WHERE id = ?",
                             [(*entry, rule_id) for rule_id, entry in zip(kept, entries)])
            conn.executemany("DELETE FROM rule WHERE id = ?", [(i,) for i in ids[len(kept):]])
            entries = entries[len(kept):]
            print(f"rewrote {len(kept)} seeded rules in place")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = {r["pattern"] for r in conn.execute("SELECT pattern FROM rule")}
        added = [(p, c, t, "seed", now) for c, p, t in entries if p not in existing]
        conn.executemany("INSERT INTO rule(pattern, category, type, note, created_at)"
                         " VALUES (?,?,?,?,?)", added)
        reconcile.reclassify(conn)
        conn.commit()
        print(f"added {len(added)} rules")
        return 0

    if args.command == "test":
        problem = refusal(args.pattern)
        if problem:
            print(problem)
            return 1
        preview(conn, args.pattern)
        return 0

    if args.command == "set":
        rule = conn.execute("SELECT * FROM rule WHERE id = ?", (args.id,)).fetchone()
        if not rule:
            print("no rule with that id")
            return 1
        pattern = args.pattern or rule["pattern"]
        category = args.category or rule["category"]
        problem = refusal(pattern, category)
        if problem:
            print(problem)
            return 1
        conn.execute("UPDATE rule SET pattern = ?, category = ? WHERE id = ?",
                     (pattern, category, args.id))
        print(f"  [{args.id}] was {rule['category']} /{rule['pattern']}/")
        print(f"       now {category} /{pattern}/")
        preview(conn, pattern)
        reconcile.reclassify(conn)
        conn.commit()
        return 0

    if args.command == "add":
        problem = refusal(args.pattern, args.category)
        if problem:
            print(problem)
            return 1
        rows = preview(conn, args.pattern)
        if not rows:
            print("  nothing matches, rule not added")
            return 1
        conn.execute("INSERT INTO rule(pattern, category, note, created_at) VALUES (?,?,?,?)",
                     (args.pattern, args.category, args.note,
                      datetime.now(timezone.utc).isoformat(timespec="seconds")))
    else:
        if not conn.execute("DELETE FROM rule WHERE id = ?", (args.id,)).rowcount:
            print("no rule with that id")
            return 1
        print(f"removed rule {args.id}")

    reconcile.reclassify(conn)
    conn.commit()
    for r in conn.execute("SELECT category, COUNT(*) c, SUM(amount) t FROM txn"
                          " WHERE category IS NOT NULL AND type != 'transfer' AND whatif = 0"
                          " GROUP BY category"):
        print(f"  {r['category']:<14} {r['c']:>4} rows  {money_str(r['t'])}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
