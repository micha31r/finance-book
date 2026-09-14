"""Checks that decide whether parsed data is trustworthy.

Nothing is written unless the checks a source supports all pass. Sources
differ in what they can prove, so each check is skipped when its inputs are
absent rather than counted as a pass.
"""
import re
from collections import defaultdict
from datetime import date, timedelta

from parsers.shared.money import money_str

MAX_REPORTED = 5


def check_document(doc):
    """Return (problems, verified).

    'verified' means the running balance was actually checked against stated
    balances, not merely that nothing failed.
    """
    problems = []
    txns = doc.transactions                       # oldest first
    running = doc.opening_balance
    if running is None and txns and txns[0].balance is not None:
        running = txns[0].balance - txns[0].amount

    checked = 0
    if running is not None:
        for txn in txns:
            running += txn.amount
            if txn.balance is None:
                continue                          # ANZ classic omits a $0.00 balance
            checked += 1
            if running != txn.balance and len(problems) < MAX_REPORTED:
                problems.append(
                    f"balance {txn.date} {txn.description[:38]!r}: "
                    f"derived {money_str(running)}, stated {money_str(txn.balance)}")
        if doc.closing_balance is not None and running != doc.closing_balance:
            problems.append(f"closing balance: derived {money_str(running)}, "
                            f"stated {money_str(doc.closing_balance)}")

    deposits = sum(t.amount for t in txns if t.amount > 0)
    withdrawals = -sum(t.amount for t in txns if t.amount < 0)
    if doc.stated_deposits is not None and deposits != doc.stated_deposits:
        problems.append(f"total deposits: summed {money_str(deposits)}, "
                        f"stated {money_str(doc.stated_deposits)}")
    if doc.stated_withdrawals is not None and withdrawals != doc.stated_withdrawals:
        problems.append(f"total withdrawals: summed {money_str(withdrawals)}, "
                        f"stated {money_str(doc.stated_withdrawals)}")
    if doc.stated_interest is not None:
        earned = sum(t.amount for t in txns if "INTEREST" in t.description.upper() and t.amount > 0)
        if earned != doc.stated_interest:
            problems.append(f"interest earned: summed {money_str(earned)}, "
                            f"stated {money_str(doc.stated_interest)}")
    return problems, checked > 0


def check_daily_balances(doc, daily):
    """Compare a document against an independent daily balance series.

    This is the only check that can prove a transaction is missing. If the
    balance moved on a day with no transaction, something was dropped.
    """
    if not daily or not doc.transactions:
        return [], 0
    problems = []
    end_of_day = {}
    for txn in doc.transactions:
        if txn.balance is not None:
            end_of_day[txn.date] = txn.balance
    first, last = doc.transactions[0].date, doc.transactions[-1].date

    matched = 0
    for day, balance in sorted(daily.items()):
        if not first <= day <= last:
            continue
        if day in end_of_day:
            matched += 1
            if end_of_day[day] != balance and len(problems) < MAX_REPORTED:
                problems.append(f"daily balance {day}: export {money_str(end_of_day[day])}, "
                                f"accounts file {money_str(balance)}")

    days = sorted(d for d in daily if first <= d <= last)
    for previous, day in zip(days, days[1:]):
        if daily[day] != daily[previous] and day not in end_of_day and len(problems) < MAX_REPORTED:
            problems.append(f"balance moved on {day} "
                            f"({money_str(daily[previous])} -> {money_str(daily[day])}) "
                            f"but no transaction covers it")
    return problems, matched


def missing_statements(conn, acct_id):
    """Statement numbers are sequential, so a hole means a statement was never loaded."""
    numbers = sorted(r["statement_no"] for r in conn.execute(
        "SELECT statement_no FROM document WHERE account_id = ? AND statement_no IS NOT NULL",
        (acct_id,)))
    if not numbers:
        return []
    return [n for n in range(min(numbers), max(numbers) + 1) if n not in numbers]


def gap_checks(conn, acct_id):
    """Verify undocumented periods against the balances either side of them.

    Where a statement is missing, rows from a CSV export still have to explain
    the balance move between the surrounding statements. This is what removes
    any need for a manually typed opening balance.
    """
    statements = conn.execute(
        "SELECT period_start, period_end, opening_balance, closing_balance FROM document"
        " WHERE account_id = ? AND kind = 'statement' AND period_start IS NOT NULL"
        " ORDER BY period_start", (acct_id,)).fetchall()
    results = []
    for previous, following in zip(statements, statements[1:]):
        if _adjacent(previous["period_end"], following["period_start"]):
            continue                                    # contiguous or overlapping
        if previous["closing_balance"] is None or following["opening_balance"] is None:
            continue
        expected = following["opening_balance"] - previous["closing_balance"]
        rows = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS net, COUNT(*) AS n FROM txn"
            " WHERE account_id = ? AND date > ? AND date < ?",
            (acct_id, previous["period_end"], following["period_start"])).fetchone()
        results.append({
            "start": previous["period_end"], "end": following["period_start"],
            "expected": expected, "actual": rows["net"], "count": rows["n"],
            "ok": rows["net"] == expected,
        })
    return results


def contiguity(conn, acct_id):
    """Adjacent statements must agree: one's closing is the next one's opening."""
    rows = conn.execute(
        "SELECT period_start, period_end, opening_balance, closing_balance, source_name"
        " FROM document WHERE account_id = ? AND kind = 'statement'"
        " AND period_start IS NOT NULL ORDER BY period_start", (acct_id,)).fetchall()
    problems = []
    for previous, following in zip(rows, rows[1:]):
        if not _adjacent(previous["period_end"], following["period_start"]):
            continue
        if previous["closing_balance"] is None:
            continue
        if following["opening_balance"] is not None and \
                previous["closing_balance"] != following["opening_balance"]:
            problems.append(
                f"{previous['period_end']}: closing {money_str(previous['closing_balance'])} "
                f"does not meet next opening {money_str(following['opening_balance'])}")
    return problems


def _adjacent(end, start):
    """True when two statement periods touch.

    Banks express a handover either way: ANZ classic repeats the boundary date
    (27 Feb to 27 Apr, then 27 Apr to 27 Jun) while ANZ Plus starts the next
    day (1 to 31 March, then 1 April). Both mean no data is missing.
    """
    return date.fromisoformat(start) <= date.fromisoformat(end) + timedelta(days=1)


PLACEHOLDER_REFERENCE = re.compile(r"^(\d)\1*$")   # 0000000 and friends
TRANSFER_WINDOW_DAYS = 7


def match_by_reference(conn):
    """Pair internal transfers that share a trace reference.

    Both ANZ banks print the same reference on both legs. A bare number is weak
    evidence on its own, so a pair must also sit in one bank, within a week, and
    carry opposite amounts. Without those guards an invoice number and a refund
    number that happen to match would be netted out of your spending silently.
    """
    rows = conn.execute(
        "SELECT t.id, t.account_id, t.date, t.amount, t.reference, a.bank_id"
        " FROM txn t JOIN account a ON a.id = t.account_id"
        " WHERE t.reference IS NOT NULL").fetchall()
    groups = defaultdict(list)
    for row in rows:
        if PLACEHOLDER_REFERENCE.match(row["reference"]):
            continue
        groups[(row["bank_id"], row["reference"])].append(row)

    matched = 0
    for legs in groups.values():
        if len(legs) != 2:
            continue
        a, b = legs
        if a["account_id"] == b["account_id"] or a["amount"] != -b["amount"]:
            continue
        gap = abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days)
        if gap > TRANSFER_WINDOW_DAYS:
            continue
        source, target = (a, b) if a["amount"] < 0 else (b, a)
        changed = conn.execute(
            "INSERT OR IGNORE INTO transfer(from_txn_id, to_txn_id, method, confirmed)"
            " VALUES (?,?,'reference',1)", (source["id"], target["id"])).rowcount
        matched += changed
    return matched


# A card purchase, ATM withdrawal or interest posting is never one leg of a
# transfer between your own accounts, whatever the amount happens to match.
NOT_A_TRANSFER = re.compile(r"VISA DEBIT|EFTPOS|\bATM\b|INTEREST", re.I)
SELF_TRANSFER_WINDOW = 3


def match_by_amount(conn):
    """Pair a debit in one of your accounts with the credit in another.

    Banks outside ANZ share no reference between the two legs, so the amount
    is the only link. That is weak evidence in general, but not here: both
    ends are accounts you hold, so the money demonstrably never left. Leaving
    these unpaired is what makes spending look roughly twice its real size.

    Only conclusive pairs are linked: exact opposite amounts, within a few
    days, neither side a card purchase, and exactly one possible partner. Any
    amount with more than one candidate is left for `review.py`.

    Returns (linked, ambiguous).
    """
    total = 0
    while True:
        linked, ambiguous = _match_amount_pass(conn)
        total += linked
        if not linked:
            return total, ambiguous


def _match_amount_pass(conn):
    """One sweep. Linking a pair takes both rows out of the pool, which can
    leave a previously ambiguous debit with a single candidate, so the caller
    repeats this until it stops finding anything."""
    linked_ids = {i for row in conn.execute("SELECT from_txn_id, to_txn_id FROM transfer")
                  for i in (row["from_txn_id"], row["to_txn_id"])}
    rows = [r for r in conn.execute(
        "SELECT id, account_id, date, amount, description FROM txn"
        " WHERE type IN ('income', 'expense')") if r["id"] not in linked_ids]

    by_amount = defaultdict(list)
    for row in rows:
        if not NOT_A_TRANSFER.search(row["description"]):
            by_amount[abs(row["amount"])].append(row)

    used, linked, ambiguous = set(), 0, 0
    for group in by_amount.values():
        debits = sorted((g for g in group if g["amount"] < 0), key=lambda g: g["date"])
        credits = [g for g in group if g["amount"] > 0]
        for debit in debits:
            if debit["id"] in used:
                continue
            options = [c for c in credits
                       if c["id"] not in used and c["account_id"] != debit["account_id"]
                       and abs((date.fromisoformat(c["date"])
                                - date.fromisoformat(debit["date"])).days) <= SELF_TRANSFER_WINDOW]
            if len(options) > 1 and interchangeable(options):
                options = options[:1]
            if len(options) != 1:
                ambiguous += len(options) > 1
                continue
            credit = options[0]
            used.update((debit["id"], credit["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO transfer(from_txn_id, to_txn_id, method, confirmed)"
                " VALUES (?,?,'amount',1)", (debit["id"], credit["id"]))
            linked += 1
    return linked, ambiguous


def interchangeable(options):
    """True when it does not matter which of these candidates we pick.

    Candidates already share an amount. If they also share an account and a
    date then they are the same money in the same place on the same day, and
    every way of pairing them off gives the same totals. Refusing these was
    losing real transfers: two identical payments from one account into another
    on one day made each leg look ambiguous, so both pairs were dropped and
    the money was counted as income and as spending at the same time.
    """
    first = options[0]
    return all(o["account_id"] == first["account_id"] and o["date"] == first["date"]
               for o in options)


def reclassify(conn):
    """Set every transaction's type from the whole picture, not row by row.

    Run after loading, so the answer does not depend on file order. A leg is
    only a transfer when its counterparty is an account we actually hold, or
    when a transfer link has been confirmed. Anything else is real money in or
    out, even if it names you: 'PAYMENT FROM <your name>' arriving from a bank we
    have no statements for is income until you confirm otherwise.
    """
    conn.execute("UPDATE txn SET type = CASE WHEN amount > 0 THEN 'income' ELSE 'expense' END")
    # Same bank only. Account numbers are not unique across banks, so a Westpac
    # number could otherwise match the tail of an ANZ counterparty.
    conn.execute(
        "UPDATE txn SET type = 'transfer' WHERE EXISTS ("
        "  SELECT 1 FROM account mine JOIN account holder ON holder.id = txn.account_id"
        "  WHERE mine.number = txn.counterparty AND mine.bank_id = holder.bank_id)")
    conn.execute(
        "UPDATE txn SET type = 'transfer' WHERE id IN"
        " (SELECT from_txn_id FROM transfer WHERE confirmed = 1"
        "  UNION SELECT to_txn_id FROM transfer WHERE confirmed = 1)")
    # Rules can force a type as well as a category: a bank's own wording for
    # "this went into a term deposit" is knowable, and should not need tagging
    # by hand every time new statements arrive.
    apply_rules(conn)
    # Your own decisions go last, so nothing automatic can undo them.
    conn.execute("UPDATE txn SET type = (SELECT type FROM manual_type WHERE txn_id = txn.id)"
                 " WHERE id IN (SELECT txn_id FROM manual_type)")


def apply_rules(conn):
    """Label transactions by matching their description against your patterns.

    A category says where money came from or went, which `type` cannot. Salary
    and a transfer from your parents are both income, and worth telling apart.

    Rules run in the order they were added and the last match wins, so a broad
    rule can be written first and narrowed by a later one. A rule may also set
    `type`, for wording whose meaning the bank has already settled: money going
    into a term deposit is a transfer, not spending, whoever imports it.
    """
    conn.execute("UPDATE txn SET category = NULL")
    labelled = 0
    for rule in conn.execute("SELECT pattern, category, type FROM rule ORDER BY id"):
        labelled += conn.execute("UPDATE txn SET category = ? WHERE description REGEXP ?",
                                 (rule["category"], rule["pattern"])).rowcount
        if rule["type"]:
            conn.execute("UPDATE txn SET type = ? WHERE description REGEXP ?",
                         (rule["type"], rule["pattern"]))
    return labelled


def unheld_counterparties(conn):
    """Accounts our statements transfer to that we have no statements for.

    Money moved there is counted as spending, which is the safe default. If it
    is your account, load its statements and it reclassifies itself.
    """
    return conn.execute(
        "SELECT counterparty, COUNT(*) n, SUM(amount) net FROM txn"
        " WHERE counterparty IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM account mine JOIN account holder ON holder.id = txn.account_id"
        "  WHERE mine.number = txn.counterparty AND mine.bank_id = holder.bank_id)"
        " GROUP BY counterparty ORDER BY COUNT(*) DESC").fetchall()


def transfer_candidates(conn, aliases, window_days=3):
    """Propose cross-bank self-transfers for confirmation.

    No shared key exists between banks, so these are guesses. They are only
    ever suggestions: nothing is netted out of income or spending until you
    confirm it.
    """
    linked = {i for row in conn.execute("SELECT from_txn_id, to_txn_id FROM transfer")
              for i in (row["from_txn_id"], row["to_txn_id"])}
    rows = [r for r in conn.execute(
        "SELECT t.id, t.account_id, t.date, t.amount, t.description, a.name, a.number"
        " FROM txn t JOIN account a ON a.id = t.account_id WHERE t.type != 'transfer'")
        if r["id"] not in linked]

    def mentions_owner(text):
        words = set(re.findall(r"[A-Za-z]+", text.upper()))
        return any(alias <= words for alias in aliases)

    credits = [r for r in rows if r["amount"] > 0 and mentions_owner(r["description"])]
    debits = [r for r in rows if r["amount"] < 0 and mentions_owner(r["description"])]
    used, candidates = set(), []
    for credit in credits:
        best = None
        for debit in debits:
            if debit["id"] in used or debit["account_id"] == credit["account_id"]:
                continue
            if debit["amount"] != -credit["amount"]:
                continue
            delta = abs((date.fromisoformat(credit["date"])
                         - date.fromisoformat(debit["date"])).days)
            if delta <= window_days and (best is None or delta < best[0]):
                best = (delta, debit)
        if best:
            used.add(best[1]["id"])
            candidates.append({"credit": credit, "debit": best[1], "days": best[0]})
    return candidates


def verify_stored(conn, acct_id):
    """Re-derive every statement's movement from the rows now in the database.

    check_document proves a file in isolation, before writing. This proves the
    result afterwards, so a later file that adds or duplicates rows inside an
    already-proven period cannot pass unnoticed.
    """
    statements = conn.execute(
        "SELECT period_start, period_end, opening_balance, closing_balance, source_name"
        " FROM document WHERE account_id = ? AND kind = 'statement'"
        "   AND period_start IS NOT NULL AND opening_balance IS NOT NULL"
        "   AND closing_balance IS NOT NULL ORDER BY period_start", (acct_id,)).fetchall()
    problems, previous_end = [], None
    for statement in statements:
        # ANZ classic repeats the boundary date on both statements, so a row
        # dated exactly there belongs to the earlier one. ANZ Plus starts a day
        # later and has no such overlap.
        lower = ">" if previous_end == statement["period_start"] else ">="
        net = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) net FROM txn"
            f" WHERE account_id = ? AND date {lower} ? AND date <= ?",
            (acct_id, statement["period_start"], statement["period_end"])).fetchone()["net"]
        expected = statement["closing_balance"] - statement["opening_balance"]
        if net != expected:
            problems.append(
                f"{statement['period_start']} to {statement['period_end']}: statement moves "
                f"{money_str(expected)}, stored rows move {money_str(net)}")
        previous_end = statement["period_end"]
    return problems


def owner_aliases(conn):
    """Name variants for the account holder, used to spot self-transfers.

    A name arrives spelled several ways across banks and cases, so each alias is
    kept as a set of words and matched without regard to order.
    """
    aliases = set()
    for row in conn.execute("SELECT DISTINCT name FROM account WHERE name IS NOT NULL"):
        words = frozenset(re.findall(r"[A-Za-z]+", row["name"].upper()))
        if len(words) >= 2:
            aliases.add(words)
    return aliases
