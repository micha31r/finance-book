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
            # A what-if is a plan, not money that moved between the two balances.
            "SELECT COALESCE(SUM(amount), 0) AS net, COUNT(*) AS n FROM txn"
            " WHERE account_id = ? AND date > ? AND date < ? AND whatif = 0",
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
# The row's counterparty is an account we hold. Same bank only: account numbers
# are not unique across banks, so a Westpac number could otherwise match the
# tail of an ANZ counterparty.
HELD_COUNTERPARTY = (
    "EXISTS (SELECT 1 FROM account mine JOIN account holder ON holder.id = txn.account_id"
    " WHERE mine.number = txn.counterparty AND mine.bank_id = holder.bank_id)")


def match_by_reference(conn):
    """Pair internal transfers that share a trace reference.

    Both ANZ banks print the same reference on both legs. A bare number is weak
    evidence on its own, so a pair must also sit in one bank, within a week, and
    carry opposite amounts. Without those guards an invoice number and a refund
    number that happen to match would be netted out of your spending silently.

    ANZ Plus sometimes reuses a reference a year or more later, so one number
    can cover several transfers. A debit pairs with the one credit that fits it,
    as long as no other debit fits that credit too.
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

    def fits(source, target):
        gap = abs((date.fromisoformat(source["date"]) - date.fromisoformat(target["date"])).days)
        return (source["amount"] < 0 and target["amount"] == -source["amount"]
                and source["account_id"] != target["account_id"] and gap <= TRANSFER_WINDOW_DAYS)

    matched = 0
    for legs in groups.values():
        for source in legs:
            targets = [leg for leg in legs if fits(source, leg)]
            if len(targets) != 1 or sum(fits(leg, targets[0]) for leg in legs) != 1:
                continue
            matched += conn.execute(
                "INSERT OR IGNORE INTO transfer(from_txn_id, to_txn_id, method, confirmed)"
                " VALUES (?,?,'reference',1)", (source["id"], targets[0]["id"])).rowcount
    return matched


# A card purchase, ATM withdrawal or interest posting is never one leg of a
# transfer between your own accounts, whatever the amount happens to match.
NOT_A_TRANSFER = re.compile(r"VISA DEBIT|EFTPOS|\bATM\b|INTEREST", re.I)
TRANSFER_WORDING = re.compile(r"\bTFR\b|\bTRANSFER\b|FUNDS TFER", re.I)
# Where a payment prints the other side's name: who a debit paid, and who a
# credit came from. ANZ Plus, ANZ, then Westpac's Osko wording.
PAYEE = re.compile(r"^PAYMENT TO |BANKING PAYMENT \d+ TO |^WITHDRAWAL-OSKO PAYMENT \d+ ", re.I)
PAYER = re.compile(r"^PAYMENT FROM |^DEPOSIT-OSKO PAYMENT \d+ ", re.I)
SELF_TRANSFER_WINDOW = 3


def match_by_amount(conn):
    """Pair a debit in one of your accounts with the credit in another.

    Banks outside ANZ share no reference between the two legs, so the amount
    is the main link. Leaving these unpaired is what makes spending look
    roughly twice its real size. But an amount alone proves nothing: a friend
    paying you $300 two days after an unrelated $300 bill would pair. So the
    wording has to back the pair up too (see `evidence`).

    Only conclusive pairs are linked: exact opposite amounts, within a few
    days, neither side a card purchase, one best credit for the debit, and no
    rival debit that has nowhere else to go. Anything else is left for
    `review.py`.

    Returns (linked, ambiguous).
    """
    aliases = owner_aliases(conn)
    # Pairs you turned down in review.py. Only that combination is ruled out,
    # so each leg can still pair with its real partner.
    rejected = {(row["from_txn_id"], row["to_txn_id"]) for row in conn.execute(
        "SELECT from_txn_id, to_txn_id FROM transfer WHERE confirmed = 0")}
    total = 0
    while True:
        linked, ambiguous = _match_amount_pass(conn, aliases, rejected)
        total += linked
        if not linked:
            return total, ambiguous


def _match_amount_pass(conn, aliases, rejected):
    """One sweep. Linking a pair takes both rows out of the pool, which can
    leave a previously ambiguous debit with a single candidate, so the caller
    repeats this until it stops finding anything."""
    # The pool comes from the data, not from stored types, so loading every
    # file in one run pairs the same rows as loading them over several. A row
    # whose type is already settled, by a held counterparty, a typed rule or
    # your own decision, is not up for pairing. Nor is a what-if: it is a
    # plan, and no money moved for it.
    rows = conn.execute(
        "SELECT id, account_id, date, amount, description FROM txn WHERE whatif = 0"
        "   AND id NOT IN (SELECT from_txn_id FROM transfer WHERE confirmed = 1"
        "                  UNION SELECT to_txn_id FROM transfer WHERE confirmed = 1)"
        "   AND id NOT IN (SELECT txn_id FROM manual_type)"
        f"  AND NOT {HELD_COUNTERPARTY}"
        "   AND NOT EXISTS (SELECT 1 FROM rule WHERE rule.type IS NOT NULL"
        "                   AND txn.description REGEXP rule.pattern)").fetchall()

    by_amount = defaultdict(list)
    for row in rows:
        if not NOT_A_TRANSFER.search(row["description"]):
            by_amount[abs(row["amount"])].append(row)

    def strength(debit, credit):
        gap = abs((date.fromisoformat(credit["date"]) - date.fromisoformat(debit["date"])).days)
        if debit["account_id"] == credit["account_id"] or gap > SELF_TRANSFER_WINDOW \
                or (debit["id"], credit["id"]) in rejected:
            return 0
        return evidence(debit["description"], credit["description"], aliases)

    used, linked, ambiguous = set(), 0, 0
    for group in by_amount.values():
        debits = sorted((g for g in group if g["amount"] < 0), key=lambda g: g["date"])
        credits = [g for g in group if g["amount"] > 0]
        for debit in debits:
            if debit["id"] in used:
                continue
            scored = [(strength(debit, c), c) for c in credits if c["id"] not in used]
            best = max((score for score, _ in scored), default=0)
            if not best:
                continue
            options = [c for score, c in scored if score == best]
            if len(options) > 1 and interchangeable(options):
                options = options[:1]
            if len(options) != 1:
                ambiguous += 1
                continue
            credit = options[0]
            # The same question from the credit's side. A rival is another
            # debit that fits this credit at least as well. The pair is safe
            # only when every rival has another credit that fits it as well,
            # and no third debit wants that credit as much. Otherwise which
            # pair links would depend on row order, so the credit is left for
            # review: a payment to a relative must not take the credit your own
            # transfer produced. Two equal transfers a few days apart still link.
            rivals = [d for d in debits if d["id"] not in used and not interchangeable([debit, d])
                      and strength(d, credit) >= best]
            others = [c for c in credits if c["id"] not in used and c["id"] != credit["id"]]

            def elsewhere(rival):
                return any(strength(rival, c) >= strength(rival, credit) and not any(
                    strength(d, c) >= strength(rival, c) for d in debits
                    if d["id"] not in used and d["id"] not in (rival["id"], debit["id"]))
                    for c in others)

            if not all(elsewhere(rival) for rival in rivals):
                ambiguous += 1
                continue
            used.update((debit["id"], credit["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO transfer(from_txn_id, to_txn_id, method, confirmed)"
                " VALUES (?,?,'amount',1)", (debit["id"], credit["id"]))
            linked += 1
    return linked, ambiguous


def evidence(debit, credit, aliases):
    """How strongly two descriptions say the money stayed yours: 2, 1 or 0.

    2 when both legs use transfer wording, or when the name the debit paid is
    the name the credit came from ("PAYMENT TO A SMITH" and "PAYMENT FROM MR A
    SMITH"). 1 when only one leg uses transfer wording or names you. 0 when
    neither does, and no pair is made. A 1 only wins where no 2 competes.
    """
    payee, payer = _name(debit, PAYEE), _name(credit, PAYER)
    if (TRANSFER_WORDING.search(debit) and TRANSFER_WORDING.search(credit)) \
            or (payee and payer and (payee <= payer or payer <= payee)):
        return 2
    return int(any(TRANSFER_WORDING.search(text) or names_owner(text, aliases)
                   for text in (debit, credit)))


def _name(description, wording):
    """The words of the name after a payment's wording, or an empty set.

    The name stops at a reference, a date or an effective-date note, so
    "PAYMENT TO A SMITH #123 Effective Date 01/02/2025" gives {A, SMITH}.
    """
    found = wording.search(description)
    if not found:
        return frozenset()
    name = re.split(r"#|\d|EFFECTIVE DATE", description[found.end():], maxsplit=1, flags=re.I)[0]
    return frozenset(re.findall(r"[A-Z]+", name.upper()))


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


def reclassify(conn, ids=None):
    """Set every transaction's type, then its category, from the whole picture.

    Run after loading, so the answer does not depend on file order. A leg is
    only a transfer when its counterparty is an account we actually hold, or
    when a transfer link has been confirmed. Anything else is real money in or
    out, even if it names you: 'PAYMENT FROM <your name>' arriving from a bank we
    have no statements for is income until you confirm otherwise.

    `ids` limits every step to those rows. An edit on the page passes the rows
    it changed and their confirmed transfer partners (see with_partners): no
    other row can come out differently, and relabelling all of them takes two
    seconds the page would wait for.
    """
    # `1` keeps every row in, so each statement below reads the same either way.
    only = "1" if ids is None else f"id IN ({','.join('?' * len(ids))})"
    args = tuple(ids or ())
    conn.execute("UPDATE txn SET type = CASE WHEN amount > 0 THEN 'income' ELSE 'expense' END"
                 f" WHERE {only}", args)
    conn.execute(f"UPDATE txn SET type = 'transfer' WHERE {HELD_COUNTERPARTY} AND {only}", args)
    conn.execute(
        "UPDATE txn SET type = 'transfer' WHERE id IN"
        " (SELECT from_txn_id FROM transfer WHERE confirmed = 1"
        f"  UNION SELECT to_txn_id FROM transfer WHERE confirmed = 1) AND {only}", args)
    # Rules can force a type as well as a category: a bank's own wording for
    # "this went into a term deposit" is knowable, and should not need tagging
    # by hand every time new statements arrive.
    rules = conn.execute("SELECT pattern, category, type FROM rule ORDER BY id").fetchall()
    for rule in rules:
        if rule["type"]:
            conn.execute(f"UPDATE txn SET type = ? WHERE description REGEXP ? AND {only}",
                         (rule["type"], rule["pattern"], *args))
    # Your own decisions go last, so nothing automatic can undo them.
    conn.execute("UPDATE txn SET type = (SELECT type FROM manual_type WHERE txn_id = txn.id)"
                 f" WHERE id IN (SELECT txn_id FROM manual_type) AND {only}", args)
    # A pair only nets out while both legs are transfers. If your decision or a
    # typed rule made one leg income or spending, the other leg is no longer
    # half of a transfer, and left as one it would count nowhere. So it takes
    # its own sign too, unless you typed that leg yourself.
    conn.execute(
        "UPDATE txn SET type = CASE WHEN amount > 0 THEN 'income' ELSE 'expense' END"
        " WHERE type = 'transfer' AND id NOT IN (SELECT txn_id FROM manual_type) AND id IN"
        " (SELECT p.from_txn_id FROM transfer p JOIN txn leg ON leg.id = p.to_txn_id"
        "   WHERE p.confirmed = 1 AND leg.type != 'transfer'"
        "  UNION SELECT p.to_txn_id FROM transfer p JOIN txn leg ON leg.id = p.from_txn_id"
        f"   WHERE p.confirmed = 1 AND leg.type != 'transfer') AND {only}", args)

    # Categories come last, once types are final. A category says where money
    # came from or went, which `type` cannot: salary and money from your
    # parents are both income. The last matching rule wins, so a broad rule can
    # come first and a later one narrow it. A plain rule skips transfers, since
    # money moved between your own accounts was neither earned nor spent. A
    # rule that sets `type` still labels them: "Term deposit" is what they are.
    conn.execute(f"UPDATE txn SET category = NULL WHERE {only}", args)
    for rule in rules:
        plain = "" if rule["type"] else " AND type != 'transfer'"
        conn.execute(f"UPDATE txn SET category = ? WHERE description REGEXP ?{plain} AND {only}",
                     (rule["category"], rule["pattern"], *args))
    # Your own category goes last too, so no rule can undo it.
    conn.execute(
        "UPDATE txn SET category = (SELECT category FROM manual_category WHERE txn_id = txn.id)"
        f" WHERE id IN (SELECT txn_id FROM manual_category) AND {only}", args)


def with_partners(conn, ids):
    """Those rows plus the other leg of each one's confirmed transfer.

    Retyping one leg decides what the other is (see reclassify's last type
    step), so an edit hands both to reclassify and no more.
    """
    marks = ",".join("?" * len(ids))
    legs = conn.execute(
        "SELECT from_txn_id, to_txn_id FROM transfer WHERE confirmed = 1"
        f" AND (from_txn_id IN ({marks}) OR to_txn_id IN ({marks}))", (*ids, *ids)).fetchall()
    partners = {i for leg in legs for i in (leg["from_txn_id"], leg["to_txn_id"])}
    return list(set(ids) | partners)


def unheld_counterparties(conn):
    """Accounts our statements transfer to that we have no statements for.

    Money moved there is counted as spending, which is the safe default. If it
    is your account, load its statements and it reclassifies itself.
    """
    return conn.execute(
        "SELECT counterparty, COUNT(*) n, SUM(amount) net FROM txn"
        f" WHERE counterparty IS NOT NULL AND NOT {HELD_COUNTERPARTY}"
        " GROUP BY counterparty ORDER BY COUNT(*) DESC").fetchall()


def transfer_candidates(conn, aliases, window_days=3):
    """Propose cross-bank self-transfers for confirmation.

    No shared key exists between banks, so these are guesses. They are only
    ever suggestions: nothing is netted out of income or spending until you
    confirm it.
    """
    linked = {i for row in conn.execute(
        "SELECT from_txn_id, to_txn_id FROM transfer WHERE confirmed = 1")
              for i in (row["from_txn_id"], row["to_txn_id"])}
    # A pair you turned down is not offered again. Each leg still can be.
    rejected = {(row["from_txn_id"], row["to_txn_id"]) for row in conn.execute(
        "SELECT from_txn_id, to_txn_id FROM transfer WHERE confirmed = 0")}
    rows = [r for r in conn.execute(
        # A what-if is a plan, so it is no leg of a transfer that happened.
        "SELECT t.id, t.account_id, t.date, t.amount, t.description, a.name, a.number"
        " FROM txn t JOIN account a ON a.id = t.account_id"
        " WHERE t.type != 'transfer' AND t.whatif = 0")
        if r["id"] not in linked]

    credits = [r for r in rows if r["amount"] > 0 and names_owner(r["description"], aliases)]
    debits = [r for r in rows if r["amount"] < 0 and names_owner(r["description"], aliases)]
    used, candidates = set(), []
    for credit in credits:
        best = None
        for debit in debits:
            if debit["id"] in used or debit["account_id"] == credit["account_id"] \
                    or (debit["id"], credit["id"]) in rejected:
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
            # A what-if moved no money, so it explains none of the statement's.
            f"SELECT COALESCE(SUM(amount), 0) net FROM txn"
            f" WHERE account_id = ? AND date {lower} ? AND date <= ? AND whatif = 0",
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


def names_owner(text, aliases):
    """True when a description carries one of the account holder's names."""
    words = set(re.findall(r"[A-Za-z]+", text.upper()))
    return any(alias <= words for alias in aliases)
