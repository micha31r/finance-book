"""Up: the monthly statement PDF, and the part-month one the app makes mid-month.

There is no table header. Rows are grouped under day headers ("Thursday, 22nd
Oct") and each row has a line that starts with a time ("3:41pm"). That line
carries the bank's raw description, the amount and the balance. The tidy
merchant name and the card used sit on the lines around it and are not read.

Money in is printed "+$12.34" and money out "$12.34" with no sign, so the "+"
is the only thing that says which way the money went.
"""
import calendar
import re
from datetime import date

from .shared import pdf
from .shared.money import MONTHS, cents, resolve_year
from .shared.model import Document, Transaction

ISSUER = "Up is a brand of Bendigo and Adelaide Bank"
# "September 2026 Statement", or "October 2026 Interim Statement" for a
# statement made before the month ended.
TITLE = re.compile(r"^(\w+) (\d{4}) (Interim )?Statement\b")
WEEKDAY = re.compile(r"^(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,")
DAY = re.compile(r"^\w+day, (\d{1,2})(?:st|nd|rd|th) ([A-Z][a-z]{2,})$")
TIME = re.compile(r"^\d{1,2}:\d{2}(?:am|pm)$")
MONEY = re.compile(r"^[+-]?\$[\d,]+\.\d{2}$")
# The raw description starts at x≈218 and ends before x≈330. The tidy name
# can spill onto the time line left of it, and the payment type ("Direct
# Debit", "Salary") starts at x≈353 right of it. The amount and balance are
# the only money right of the description, so "$450.00" typed into a
# description is never taken for either.
DESCRIPTION_X = (210, 345)
# BSB and account number are printed top right of page 1.
DETAILS_X = 450
SUMMARY = ("opening", "closing", "in", "out")


def _cents(token):
    """'+$1,234.56' -> 123456. A '+' marks money in; anything else is money out."""
    value = cents(token)
    return value if token.startswith("+") else -abs(value)


def _text(items):
    return " ".join(t for _, _, t in items)


def detect(path) -> bool:
    if not str(path).lower().endswith(".pdf"):
        return False
    head = pdf.open_pdf(path)[0].get_text()
    return ISSUER in head and "Statement" in head


def _header(page_lines):
    """Month, year, part-month flag, BSB, account number and the summary."""
    found = {}
    for i, (_, items) in enumerate(page_lines):
        text = _text(items)
        m = TITLE.match(text)
        if m and "month" not in found:
            found["month"] = MONTHS[m[1][:3].lower()]
            found["year"] = int(m[2])
            found["partial"] = bool(m[3])
        right = [t for x0, _, t in items if x0 >= DETAILS_X]
        if right[:1] == ["BSB"]:
            found.setdefault("bsb", "".join(right[1:]))
        if right[:1] == ["Account"]:
            found.setdefault("number", "".join(right[1:]))
        money = [t for _, _, t in items if MONEY.match(t)]
        if text.startswith("Opening Balance") and money:
            found["opening"] = cents(money[0])
        if text.startswith("Closing Balance") and money:
            found["closing"] = cents(money[0])
        # "Money In  Money Out" labels, then "+$X  $X" on the next line.
        if "Money In" in text and "Money Out" in text and i + 1 < len(page_lines):
            values = [t for _, _, t in page_lines[i + 1][1] if MONEY.match(t)]
            if len(values) == 2:
                found["in"] = cents(values[0])
                found["out"] = cents(values[1])
    return found


def parse(path) -> list[Document]:
    doc = pdf.open_pdf(path)
    pages = [pdf.lines(page) for page in doc]
    head = _header(pages[0])
    # The summary is what proves no row was skipped, so a file without it is
    # refused rather than loaded unchecked.
    missing = [k for k in ("month", "number") + SUMMARY if k not in head]
    if missing:
        raise ValueError(f"{path.name}: could not read {', '.join(missing)}")

    start = date(head["year"], head["month"], 1)
    end = date(head["year"], head["month"], calendar.monthrange(head["year"], head["month"])[1])

    transactions, day = [], None
    for page_lines in pages:
        for _, items in page_lines:
            text = _text(items)
            m = DAY.match(text)
            if m:
                day = resolve_year(int(m[1]), MONTHS[m[2][:3].lower()], start, end)
                continue
            if WEEKDAY.match(text):
                # A day header we cannot read would leave the rows under it
                # with the day before's date, and no balance check notices that.
                raise ValueError(f"{path.name}: unreadable day header")
            money = [t for x0, _, t in items if x0 >= DESCRIPTION_X[1] and MONEY.match(t)]
            if not (items and TIME.match(items[0][2]) and len(money) == 2):
                continue
            if day is None:
                raise ValueError(f"{path.name}: a transaction appears before any date")
            description = " ".join(t for x0, _, t in items
                                   if DESCRIPTION_X[0] <= x0 < DESCRIPTION_X[1])
            transactions.append(Transaction(date=day, description=description,
                                            amount=_cents(money[0]),
                                            balance=cents(money[1])))

    partial = head["partial"]
    if partial:
        # A part-month statement is replaced by the full one later, so it is
        # loaded like a provisional list. Its period ends with its last row.
        end = max((t.date for t in transactions), default=start)

    return [Document(
        bank="Up",
        kind="list" if partial else "statement",
        number=head["number"],
        bsb=head.get("bsb"),
        period_start=start, period_end=end,
        derived_period=partial,
        opening_balance=head["opening"],
        closing_balance=head["closing"],
        stated_deposits=head["in"],
        stated_withdrawals=head["out"],
        transactions=list(reversed(transactions)),   # rows run newest first
        source_name=path.name,
    )]
