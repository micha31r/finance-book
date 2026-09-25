"""ANZ classic: PDF statements, PDF Transaction Reports, and the CSV export.

One module because they are the same bank and the same accounts. The CSV has
no account number in it, so the account identity learned from a PDF is what
names the CSV later.
"""
import re
from datetime import date, timedelta

from .shared import csvfile, pdf
from .shared.money import MONTHS, cents, is_money, resolve_year
from .shared.model import Document, Transaction

STATEMENT_COLUMNS = ["Withdrawals", "Deposits", "Balance"]
REPORT_COLUMNS = ["Withdrawals", "Deposits"]

PERIOD = re.compile(r"^(\d{1,2}) ([A-Za-z]+) (\d{4}) to (\d{1,2}) ([A-Za-z]+) (\d{4})$", re.I)
STATEMENT_NO = re.compile(r"^STATEMENT NUMBER (\d+)$")
AS_OF = re.compile(r"Balance as of (\d{1,2}) (\w{3}) (\d{4})")
DAY_MONTH = re.compile(r"^(\d{1,2}) ([A-Z]{3})$")
YEAR = re.compile(r"^\d{4}$")
EFFECTIVE = re.compile(r"EFFECTIVE DATE (\d{2}) ([A-Z]{3}) (\d{4})")
TRANSFER = re.compile(r"TRANSFER (\d+)\s+(TO|FROM)\s+(\d+)")
PAYMENT_REF = re.compile(r"\bPAYMENT (\d{6})\b")
SUMMARY_LABELS = [("opening", "Opening Balance:"), ("closing", "Closing Balance:"),
                  ("deposits", "Total Deposits:"), ("withdrawals", "Total Withdrawals:")]
CSV_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def detect(path) -> bool:
    name = str(path).lower()
    if name.endswith(".pdf"):
        head = pdf.open_pdf(path)[0].get_text()
        if "STATEMENT" in head and ("ACCESS ADVANTAGE" in head or "SAVER ACCOUNT" in head):
            return True
        if "Transaction Report" in head:
            return True
    elif name.endswith(".csv"):
        rows = csvfile.rows(path)
        # No header, and the first cell is a date. Westpac exports carry a
        # header line, so they never look like this.
        if rows and len(rows[0]) >= 3 and CSV_DATE.match(rows[0][0].strip()):
            try:
                float(rows[0][1].strip())
                return True
            except ValueError:
                return False
    return False


def parse(path) -> list[Document]:
    if str(path).lower().endswith(".csv"):
        return [_parse_csv(path)]
    return [_parse_pdf(path)]


def _value_below(page_lines, index, x, within=45.0):
    """Find the money value printed under a label.

    The summary block is two columns of label-above-value, and the x position
    of those columns changes between account products, so the value is found
    by proximity rather than a fixed offset. '$' and '0.00' sometimes land on
    separate lines, so a bare '$' is skipped.
    """
    top = page_lines[index][0]
    for y, items in page_lines[index + 1:]:
        if y - top > within:
            break
        for x0, _, text in items:
            if abs(x0 - x) < 60 and is_money(text):
                return cents(text)
    return None


def _parse_pdf(path) -> Document:
    doc = pdf.open_pdf(path)
    pages = [pdf.lines(page) for page in doc]
    is_report = "Transaction Report" in doc[0].get_text()[:200]

    meta, start, end = {}, None, None
    for i, (_, items) in enumerate(pages[0]):
        texts = [t for _, _, t in items]
        text = " ".join(texts)
        m = PERIOD.match(text)
        if m:
            start = date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))
            end = date(int(m[6]), MONTHS[m[5][:3].lower()], int(m[4]))
        m = STATEMENT_NO.match(text)
        if m:
            meta["statement_no"] = int(m[1])
        for key, label in SUMMARY_LABELS:
            words = label.split()
            for j in range(len(texts) - len(words) + 1):
                if texts[j:j + len(words)] == words and key not in meta:
                    same = [t for _, _, t in items[j + len(words):] if is_money(t)]
                    meta[key] = cents(same[0]) if same else _value_below(pages[0], i, items[j][0])
        if text.startswith("Branch Number (BSB)") and i + 1 < len(pages[0]):
            meta["bsb"] = next((t.replace("-", "") for _, _, t in pages[0][i + 1][1]
                                if re.match(r"^\d{3}-?\d{3}$", t)), None)
        if "Account" in texts and "Number" in texts and i + 1 < len(pages[0]):
            for _, _, t in pages[0][i + 1][1]:
                if re.match(r"^\d{4}-\d{5}$|^\d{9}$", t):
                    meta.setdefault("number", t.replace("-", ""))
        m = AS_OF.search(text)
        if m:
            inline = [t for t in texts if is_money(t)]
            balance_x = next((x0 for x0, _, t in items if t == "Balance"), items[0][0])
            meta["as_of"] = cents(inline[0]) if inline else _value_below(pages[0], i, balance_x)

    if start is None or end is None:
        raise ValueError(f"{path.name}: could not read the statement period")

    # The first line names the product on a statement, but on a report it just
    # says "Transaction Report". Only statements get to name the account.
    title = doc[0].get_text().split("\n")[0].replace("STATEMENT", "").strip()
    product = None if is_report else (title or None)

    transactions, stated, pending = [], {}, []
    # A Fee Summary table with its own amount columns follows the transactions.
    for left, words, cells in pdf.table_rows(
            pages, [STATEMENT_COLUMNS, REPORT_COLUMNS], stop_at="TOTALS AT END OF"):
        description = " ".join(words)
        # 'blank' marks an empty cell, so it is not part of the date stamp. It
        # stays in `left` so a totals row is never read as a wrapped description.
        day_month = DAY_MONTH.match(" ".join(t for t in left if t != "blank"))
        has_amount = "Withdrawals" in cells or "Deposits" in cells
        if description == "Total" and has_amount:
            stated["withdrawals"] = cents(cells.get("Withdrawals", "0.00"))
            stated["deposits"] = cents(cells.get("Deposits", "0.00"))
        elif YEAR.match(" ".join(t for t in left if t != "blank")):
            # A year marker sits in the Date column. When a description wraps,
            # ANZ starts it on that same line and puts the date and amounts on
            # the next one, so this text belongs to the transaction below.
            pending = words
        elif day_month and has_amount:
            amount = (cents(cells["Deposits"]) if "Deposits" in cells
                      else -cents(cells["Withdrawals"]))
            description = " ".join(pending + words)
            pending = []
            day, month = int(day_month[1]), MONTHS[day_month[2].lower()]
            previous = transactions[-1].date if transactions else None
            # A statement prints oldest first, so the row above bounds this
            # date from below. A report prints newest first, so it bounds it
            # from above, and the period's end bounds the first row.
            if is_report:
                when = resolve_year(day, month, start, end, before=previous or end)
            else:
                when = resolve_year(day, month, start, end, after=previous)
            transactions.append(Transaction(
                date=when, description=description, amount=amount,
                balance=cents(cells["Balance"]) if "Balance" in cells else None))
        elif transactions and pdf.is_continuation(left, words, cells):
            transactions[-1].description += " " + description

    for txn in transactions:
        _enrich(txn)
    if is_report:
        transactions.reverse()              # reports print newest first
    # ANZ prints the previous closing day as the start, and the generation
    # stamp shows the end day is fully included: the period begins a day later.
    # Rows were resolved against the printed period, so one dated on the
    # printed start day still parses and the checks report it.
    if not is_report and meta.get("statement_no", 1) > 1:
        start += timedelta(days=1)

    return Document(
        bank="ANZ",
        kind="report" if is_report else "statement",
        number=meta.get("number", ""),
        bsb=meta.get("bsb"),
        product=product,
        period_start=start, period_end=end,
        opening_balance=meta.get("opening"),
        # "or" would discard a real $0.00 closing balance, which these accounts hit often
        closing_balance=meta["closing"] if meta.get("closing") is not None else meta.get("as_of"),
        statement_no=meta.get("statement_no"),
        stated_deposits=stated.get("deposits", meta.get("deposits")),
        stated_withdrawals=stated.get("withdrawals", meta.get("withdrawals")),
        transactions=transactions,
        source_name=path.name,
    )


def _parse_csv(path) -> Document:
    """Columns are positional: date, amount, description, then optional extras.

    Exports carry 3 to 8 columns depending on the options chosen, and the same
    account can produce either width, so only the first three are relied on.
    """
    transactions = []
    for row in csvfile.rows(path):
        txn = Transaction(date=csvfile.au_date(row[0]),
                          description=" ".join(row[2].split()),
                          amount=cents(row[1].strip()))
        _enrich(txn)
        transactions.append(txn)
    transactions.reverse()                  # exports print newest first
    dates = [t.date for t in transactions]
    return Document(
        bank="ANZ", kind="export", number="",   # resolved by ingest.py
        period_start=min(dates) if dates else None,
        period_end=max(dates) if dates else None,
        transactions=transactions, source_name=path.name,
        derived_period=True,
    )


def _enrich(txn: Transaction):
    upper = txn.description.upper()
    m = EFFECTIVE.search(upper)
    if m:
        txn.effective_date = date(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
    m = TRANSFER.search(upper)
    if m:
        txn.reference = m[1]
        txn.counterparty = m[3][-9:]        # sometimes prefixed with the BSB
        return
    m = PAYMENT_REF.search(upper)
    if m:
        txn.reference = m[1]
