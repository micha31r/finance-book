#!/usr/bin/env python3
"""Load bank statements and exports into finance.db.

Drop any mix of files on it. Each file is identified by its contents, not its
name, because the ANZ PDFs arrive named like
'209ec0c7-108d-43f7-9c3a-3f0b623d8eb5.pdf' and folder names change.

    python ingest.py                 # prompts for paths
    python ingest.py FILE [FILE...]
"""
import argparse
import shlex
import sys
from collections import Counter
from pathlib import Path

import db
import reconcile
from parsers import anz, anzplus, westpac
from parsers.shared.money import match_key, money_str

PARSERS = [anzplus, anz, westpac]


# An overlap match needs real evidence before it skips the prompt. Two
# coincidental rows are not enough to attach a whole export to an account, and
# getting it wrong is self-reinforcing: the next copy then matches the mistake.
MIN_OVERLAP = 10
DOMINANCE = 5


def identify(path):
    """The parser that recognises this file, or None with the last error hit."""
    error = None
    for parser in PARSERS:
        try:
            if parser.detect(path):
                return parser, None
        except Exception as caught:
            # Unreadable or corrupt for this parser: try the next, and keep
            # the reason in case none takes it.
            error = str(caught) or type(caught).__name__
    return None, error


def read_paths():
    print("Paste file paths (drag from Finder), then press Enter:")
    try:
        line = input("> ")
    except EOFError:
        return []
    return [Path(p) for p in shlex.split(line)]


def resolve_account(conn, doc):
    """Give an ANZ CSV export an account.

    The export carries no account number. If its rows already exist we can say
    which account it is; otherwise the only honest answer is to ask.
    """
    if doc.number:
        return True
    candidates = conn.execute(
        "SELECT a.id, a.number, a.name, a.bsb FROM account a JOIN bank b ON b.id = a.bank_id"
        " WHERE b.name = ? ORDER BY a.number", (doc.bank,)).fetchall()

    scores = []
    for account in candidates:
        hits = sum(1 for txn in doc.transactions if conn.execute(
            # A what-if is no evidence that an export's row is already known.
            "SELECT 1 FROM txn WHERE account_id = ? AND date = ? AND amount = ? AND match_key = ?"
            " AND whatif = 0 LIMIT 1",
            (account["id"], txn.date.isoformat(), txn.amount, match_key(txn.description))).fetchone())
        scores.append((hits, account))
    scores.sort(key=lambda s: -s[0])
    best = scores[0][0] if scores else 0
    runner_up = scores[1][0] if len(scores) > 1 else 0
    if best >= MIN_OVERLAP and best >= (runner_up + 1) * DOMINANCE:
        doc.number = scores[0][1]["number"]
        doc.bsb = doc.bsb or scores[0][1]["bsb"]
        print(f"    account matched by overlap: {doc.number} "
              f"({best} of {len(doc.transactions)} rows already known, "
              f"next best {runner_up})")
        return True

    if not sys.stdin.isatty():
        print(f"    SKIPPED {doc.source_name}: cannot tell which account this is "
              f"(best overlap {best} rows, need {MIN_OVERLAP})")
        return False
    print(f"\n  Which account is {doc.source_name}? "
          f"(best overlap {best} rows, too weak to decide)")
    for i, account in enumerate(candidates, 1):
        print(f"    [{i}] {account['bsb'] or '?'}-{account['number']} {account['name'] or ''}")
    print("    or type the 9-digit account number, or press Enter to skip")
    while True:
        choice = input("  > ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            doc.number = candidates[int(choice) - 1]["number"]
            doc.bsb = doc.bsb or candidates[int(choice) - 1]["bsb"]
            return True
        # ANZ prints account numbers as 1234-56789. Anything else, like a
        # mistyped choice, would quietly create an account that isn't yours.
        number = choice.replace("-", "").replace(" ", "")
        if len(number) == 9 and number.isdigit():
            doc.number = number
            return True
        if not choice:
            print(f"    SKIPPED {doc.source_name}")
            return False
        print("    not a listed choice or a 9-digit account number, try again")


def main(argv):
    cli = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("files", nargs="*", type=Path,
                     help="statements and exports; prompts for them when none are given")
    paths = cli.parse_args(argv).files or read_paths()
    if not paths:
        print("nothing to do")
        return 0

    status = 0          # 1 once any file could not be loaded
    documents = []
    print("\n  detected")
    for file_no, path in enumerate(paths):
        if not path.exists():
            print(f"    {path.name[:40]:42s} missing file")
            status = 1
            continue
        parser, error = identify(path)
        if parser is None:
            why = f" ({error})" if error else ""
            print(f"    {path.name[:40]:42s} not recognised{why}")
            status = 1
            continue
        try:
            parsed = parser.parse(path)
        except Exception as error:
            print(f"    {path.name[:40]:42s} FAILED to parse: {error}")
            status = 1
            continue
        if not parsed:
            print(f"    {path.name[:40]:42s} nothing to load (already covered elsewhere)")
            continue
        for doc in parsed:
            documents.append((file_no, doc))
            where = f"{doc.bsb or '?'}-{doc.number}" if doc.number else "account unknown"
            print(f"    {path.name[:40]:42s} {doc.bank} {doc.kind:9s} {where}")

    # Account files carry names and daily balances, so load them first.
    documents.sort(key=lambda item: 0 if item[1].kind == "accounts" else 1)
    daily = {}

    conn = db.connect()
    totals = Counter()
    print()
    checked = []
    for file_no, doc in documents:
        if doc.kind == "accounts":
            daily[(doc.bank, doc.number)] = doc.daily_balances
            db.account_id(conn, doc)
            print(f"  {doc.bank} {doc.number} {doc.account_name}: "
                  f"{len(doc.daily_balances)} daily balances for cross-checking")
            continue
        # Only Westpac has daily balances, and its exports name their accounts,
        # so this runs before an ANZ CSV export is asked which account it is.
        problems, verified = reconcile.check_document(doc)
        extra, matched_days = reconcile.check_daily_balances(doc, daily.get((doc.bank, doc.number)))
        checked.append((file_no, doc, problems + extra, verified, matched_days))

    # A Westpac export holds several accounts. All of them are checked before
    # any is written, so a file that fails anywhere leaves nothing behind. Files
    # are told apart by their place in this run, because two can share a name.
    failed = {file_no for file_no, _, problems, _, _ in checked if problems}
    for file_no, doc, problems, verified, matched_days in checked:
        if not resolve_account(conn, doc):
            status = 1
            continue
        label = (f"  {doc.bank} {doc.number} {doc.kind}"
                 f"{f' #{doc.statement_no}' if doc.statement_no else ''} "
                 f"{doc.period_start} to {doc.period_end}")
        if file_no in failed:
            print(f"{label}\n    REJECTED, {len(doc.transactions)} transactions not written")
            for problem in problems or ["another account in this file failed its checks"]:
                print(f"      {problem}")
            status = 1
            continue

        acct_id = db.account_id(conn, doc)
        doc_id = db.document_id(conn, acct_id, doc)
        counts = db.write_transactions(conn, acct_id, doc_id, doc, verified)
        totals.update(dict(zip(('new', 'upgraded', 'known', 'removed'), counts)))
        checks = ["balance verified" if verified else "no balance to verify"]
        if matched_days:
            checks.append(f"checked against {matched_days} days of balances")
        print(f"{label}\n    {len(doc.transactions):4d} transactions   "
              f"{counts[0]} new, {counts[2]} already present"
              f"{f', {counts[1]} upgraded' if counts[1] else ''}"
              f"{f', {counts[3]} removed' if counts[3] else ''}   {', '.join(checks)}")
    conn.commit()

    by_reference, by_amount, ambiguous = reconcile.pair_transfers(conn)
    reconcile.reclassify(conn)
    conn.commit()

    print(f"\n  written    {totals['new']} new, {totals['known']} already present, "
          f"{totals['upgraded']} upgraded, {totals['removed']} removed")
    print(f"  transfers  {by_reference} newly paired by shared reference, "
          f"{by_amount} paired by matching amount across your accounts")
    if ambiguous:
        print(f"             {ambiguous} amounts had more than one possible partner, left for review")

    for account in conn.execute(
            "SELECT a.id, a.number, a.name, b.name AS bank FROM account a"
            " JOIN bank b ON b.id = a.bank_id ORDER BY b.name, a.number"):
        notes = []
        for problem in reconcile.verify_stored(conn, account["id"]):
            notes.append(f"STORED ROWS DISAGREE: {problem}")
        holes = reconcile.missing_statements(conn, account["id"])
        if holes:
            notes.append("missing statements " + ", ".join(f"#{n}" for n in holes))
        for problem in reconcile.contiguity(conn, account["id"]):
            notes.append(problem)
        for gap in reconcile.gap_checks(conn, account["id"]):
            verdict = "verified by surrounding balances" if gap["ok"] else \
                f"UNEXPLAINED, expected net {money_str(gap['expected'])}, got {money_str(gap['actual'])}"
            notes.append(f"gap {gap['start']} to {gap['end']}: {gap['count']} rows, {verdict}")
        if notes:
            print(f"\n  {account['bank']} {account['number']} {account['name'] or ''}")
            for note in notes:
                print(f"    {note}")

    unheld = reconcile.unheld_counterparties(conn)
    if unheld:
        print("\n  transfers to accounts you have not loaded (counted as spending):")
        for row in unheld:
            print(f"    {row['counterparty']}  {row['n']} transfers, net {money_str(row['net'])}")

    pending = reconcile.transfer_candidates(conn, reconcile.owner_aliases(conn))
    if pending:
        print(f"\n  {len(pending)} cross-bank candidate(s) need review: run `python review.py`")
    conn.close()
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
