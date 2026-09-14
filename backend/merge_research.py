#!/usr/bin/env python3
"""Turn researched merchant/category pairs into rules.

Reads /tmp/result_*.tsv (merchant, category, confidence) and writes one rule
per category, whose pattern is an alternation of that category's merchants.
One rule per category rather than per merchant keeps the rules list something a
person can actually read and edit.

    python merge_research.py            report only
    python merge_research.py --write    add the rules
"""
import collections
import glob
import re
import sys
from datetime import datetime, timezone

import db
import reconcile
from merchants import merchant

CONFIDENT = ("high", "medium")


def load():
    rows = []
    for path in sorted(glob.glob("/tmp/result_*.tsv")):
        for line in open(path, encoding="utf-8"):
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3 and parts[0].strip():
                rows.append((parts[0], parts[1].strip(), parts[2].strip().lower()))
    return rows


def generalise(name):
    """Drop the trailing suburb so one pattern covers every branch.

    "GONG CHA ELIZABETH MELBOURNE" and "GONG CHA QV CENTRE MELBOURNE" are the
    same business. Only the last word goes: dropping more starts merging
    genuinely different shops.
    """
    words = name.split()
    return " ".join(words[:-1]) if len(words) > 2 else name


def main():
    rows = load()
    if not rows:
        print("no /tmp/result_*.tsv files found")
        return 1

    by_confidence = collections.Counter(c for _, _, c in rows)
    print(f"{len(rows)} researched merchants: " +
          ", ".join(f"{n} {c}" for c, n in by_confidence.most_common()))

    conn = db.connect()
    known = {merchant(r["description"])
             for r in conn.execute("SELECT description FROM txn WHERE type = 'expense'")}
    # A curated rule beats research. Merchants that already have a category got
    # one after the research was commissioned, so leave them alone: the agents
    # filed ATM withdrawals under Bank fees, which the seed now handles better.
    settled = {merchant(r["description"])
               for r in conn.execute("SELECT description FROM txn"
                                     " WHERE type = 'expense' AND category IS NOT NULL")}
    missing = [name for name, _, _ in rows if name not in known]
    if missing:
        print(f"WARNING: {len(missing)} researched names are not in the data, "
              f"e.g. {missing[:3]}")

    patterns = collections.defaultdict(list)
    for name, category, confidence in rows:
        if confidence in CONFIDENT and category != "UNKNOWN" and name not in settled:
            patterns[category].append(re.escape(generalise(name)))

    print(f"\n{len(patterns)} categories to add or extend:")
    for category, names in sorted(patterns.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(names):4d}  {category}")

    if "--write" not in sys.argv:
        print("\nnothing written, pass --write to apply")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for category, names in sorted(patterns.items()):
        pattern = "|".join(sorted(set(names)))
        existing = conn.execute(
            "SELECT id FROM rule WHERE category = ? AND note = 'researched'",
            (category,)).fetchone()
        if existing:
            conn.execute("UPDATE rule SET pattern = ? WHERE id = ?", (pattern, existing["id"]))
        else:
            conn.execute("INSERT INTO rule(pattern, category, note, created_at)"
                         " VALUES (?,?,'researched',?)", (pattern, category, now))
    reconcile.apply_rules(conn)
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM txn WHERE type = 'expense'").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM txn WHERE type = 'expense'"
                        " AND category IS NOT NULL").fetchone()[0]
    print(f"\ncoverage now {done}/{total} ({100 * done / total:.0f}%)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
