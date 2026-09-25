"""ANZ Plus: monthly Account Statements and the current-month Transaction List.

Both use one table: Date | Description | Credit | Debit | Balance. Credit and
Debit extract to the same place in the text stream, so only the x-coordinate
tells them apart.
"""
import re
from datetime import date

from .shared import pdf
from .shared.money import MONTHS, cents, is_money, resolve_year
from .shared.model import Document, Transaction

COLUMNS = ["Credit", "Debit", "Balance"]
LABELS = ["Branch Number (BSB)", "Account Number", "Opening Balance", "Closing Balance",
          "Interest Earned (Current FY)", "Interest Earned", "Total Interest Paid",
          "Account Name"]
# ANZ renames its products. The same savings account has been "ANZ Save" and
# "ANZ Plus Growth Saver", and the everyday account has been plain "ANZ Plus".
# So a document is identified by its shape, never by the product branding.
TITLES = ("Account Statement", "Transaction List")
ISSUER = "Australia and New Zealand Banking Group"

PERIOD = re.compile(r"^(\d{1,2}) (\w+) (\d{4}) - (\d{1,2}) (\w+) (\d{4})$")
COMMENCING = re.compile(r"^Commencing (\d{1,2}) (\w+) (\d{4})$")
GENERATED = re.compile(r"generated on (\d{1,2}) (\w+) (\d{4})")
DAY_MONTH = re.compile(r"^(\d{1,2}) ([A-Z][a-z]{2})$")
TRANSFER = re.compile(r"TRANSFER (?:TO|FROM) (\d{6})-(\d{9}) #(\d+)")
REFERENCE = re.compile(r"#(\d{4,})")
EFFECTIVE = re.compile(r"Effective Date (\d{2})/(\d{2})/(\d{4})")


def detect(path) -> bool:
    if not str(path).lower().endswith(".pdf"):
        return False
    head = pdf.open_pdf(path)[0].get_text()
    first = next((line.strip() for line in head.split("\n") if line.strip()), "")
    # ANZ classic opens with its own product line ("ANZ ACCESS ADVANTAGE
    # STATEMENT", "Transaction Report") and uses Withdrawals/Deposits columns,
    # so the title alone separates the two ANZ families.
    return first in TITLES and ISSUER in head


def _labelled_values(page_lines):
    """Read the summary block, where a row of labels sits above a row of values.

    Values are matched to the nearest label by x, because the block's column
    count changes: savings accounts add 'Interest Earned' and sometimes
    'Interest Earned (Current FY)'.
    """
    found = {}
    for i, (_, items) in enumerate(page_lines):
        tokens = [(x0, t) for x0, _, t in items]
        labels, j = [], 0
        while j < len(tokens):
            for label in LABELS:
                words = label.split()
                if [t for _, t in tokens[j:j + len(words)]] == words:
                    labels.append((label, tokens[j][0]))
                    j += len(words)
                    break
            else:
                j += 1
        if not labels or i + 1 >= len(page_lines):
            continue
        for label, lx in labels:
            owned = [t for x0, _, t in page_lines[i + 1][1]
                     if min(labels, key=lambda L: abs(L[1] - x0))[0] == label]
            if owned:
                found.setdefault(label, " ".join(owned))
    return found


def _product(page_lines):
    """The product name sits on the line above the account summary block."""
    for i, (_, items) in enumerate(page_lines):
        if " ".join(t for _, _, t in items).startswith("Branch Number (BSB)") and i:
            return " ".join(t for _, _, t in page_lines[i - 1][1])
    return None


def _period(page_lines):
    """Statements state a closed period. A Transaction List states the day it
    commences and, in the footer of its last page, the day it was generated.
    That day bounds the year of every row; the list's period ends with its
    last row, which parse sets once the rows are read."""
    start = end = None
    for _, items in page_lines:
        text = " ".join(t for _, _, t in items)
        m = PERIOD.match(text)
        if m:
            return (date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1])),
                    date(int(m[6]), MONTHS[m[5][:3].lower()], int(m[4])), False)
        m = COMMENCING.match(text)
        if m:
            start = date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))
        m = GENERATED.search(text)
        if m:
            end = date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))
    return start, end or date.today(), True


def parse(path) -> list[Document]:
    doc = pdf.open_pdf(path)
    pages = [pdf.lines(page) for page in doc]

    meta = _labelled_values(pages[0])
    start, end, provisional = _period([ln for page in pages for ln in page])
    product = _product(pages[0])

    if start is None:
        raise ValueError(f"{path.name}: could not read the statement period")

    transactions = []
    for left, words, cells in pdf.table_rows(pages, [COLUMNS]):
        day_month = DAY_MONTH.match(" ".join(left))
        if day_month and cells.get("Balance") and (cells.get("Credit") or cells.get("Debit")):
            amount = cents(cells["Credit"]) if cells.get("Credit") else -cents(cells["Debit"])
            # Rows run newest first, so the previous row's date is the upper
            # bound for this one when the period spans more than a year. The
            # first row is bounded by the period's end.
            before = transactions[-1].date if transactions else end
            when = resolve_year(int(day_month[1]), MONTHS[day_month[2].lower()], start, end,
                                before=before)
            transactions.append(Transaction(date=when, description=" ".join(words),
                                            amount=amount, balance=cents(cells["Balance"])))
        elif transactions and pdf.is_continuation(left, words, cells):
            transactions[-1].description += " " + " ".join(words)

    for txn in transactions:
        _enrich(txn)
    if provisional:
        # A list ends with its last row, not with the day it was generated:
        # the same list downloaded again a day later was a second document.
        end = max((t.date for t in transactions), default=start)

    # Savings accounts label this "Interest Earned"; older ones say
    # "Total Interest Paid". Both mean the same thing.
    interest = (meta.get("Interest Earned") or meta.get("Total Interest Paid") or "").replace("+ ", "")
    return [Document(
        bank="ANZ Plus",
        kind="list" if provisional else "statement",
        number=(meta.get("Account Number") or "").replace(" ", ""),
        bsb=(meta.get("Branch Number (BSB)") or "").replace(" ", "") or None,
        account_name=meta.get("Account Name"),
        product=product,
        period_start=start, period_end=end,
        # A Transaction List has no period of its own: its end is only its
        # last row. Saying so stops the stale sweep deleting rows that a
        # shorter, later list simply did not cover.
        derived_period=provisional,
        opening_balance=cents(meta["Opening Balance"]) if "Opening Balance" in meta else None,
        closing_balance=cents(meta["Closing Balance"]) if "Closing Balance" in meta else None,
        stated_interest=cents(interest) if is_money(interest) else None,
        transactions=list(reversed(transactions)),   # emit oldest first
        source_name=path.name,
    )]


def _enrich(txn: Transaction):
    """Pull the structured bits out of the description text."""
    m = EFFECTIVE.search(txn.description)
    if m:
        txn.effective_date = date(int(m[3]), int(m[2]), int(m[1]))
    m = TRANSFER.search(txn.description)
    if m:
        txn.counterparty = m[2]
    ref = REFERENCE.search(txn.description)
    if ref:
        txn.reference = ref[1]
