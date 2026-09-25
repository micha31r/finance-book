"""Money, date and description parsing shared by every parser."""
import re
from datetime import date
from decimal import Decimal

# Amounts may carry the minus on either side of the dollar sign.
MONEY_RE = re.compile(r"^-?\$?-?[\d,]+\.\d{2}$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# A statement writes "... COLES SYDNEY EFFECTIVE DATE 01 MAR 2025" where the
# CSV export writes "... COLES SYDNEY". Same transaction, two spellings. ANZ
# Plus writes "Effective Date 01/03/2025" on a statement's card purchases and
# nothing on a Transaction List's.
EFFECTIVE_SUFFIX = re.compile(r"\s+EFFECTIVE DATE (\d{1,2} \w{3} \d{4}|\d{2}/\d{2}/\d{4})\s*$")


def is_money(token: str) -> bool:
    return bool(MONEY_RE.match(token))


def cents(token: str) -> int:
    """'$1,234.56' -> 123456. Decimal first so no float ever touches a balance."""
    return int(Decimal(token.replace("$", "").replace(",", "")) * 100)


def money_str(value: int) -> str:
    return f"{value / 100:,.2f}"


def match_key(description: str) -> str:
    """The spelling-independent identity of a transaction description.

    The same transaction reaches us from a PDF statement and from a CSV export
    with different capitalisation and a different suffix. Without this, both
    copies are stored and the money is counted twice.

    Reference numbers stay in the key on purpose. Two $100 transfers on one day
    differ only by their reference, and they are separate transactions.
    """
    return " ".join(EFFECTIVE_SUFFIX.sub("", description.upper()).split())


def resolve_year(day: int, month: int, start: date, end: date,
                 after: date | None = None, before: date | None = None):
    """Statements print '31 Jul' with no year. Pick the year that lands in the period.

    Periods can straddle new year (27 Dec 2023 to 27 Feb 2024), so every year
    the period touches is tried. A period of a year or more makes more than one
    valid, so the previous row's date breaks the tie, because rows within a
    document run in date order.
    Pass it as `after` when rows run oldest first. Pass it as `before` when they
    run newest first, and pass the period's end for the first row.

    Raises ValueError rather than guessing. A wrong date silently becomes part of
    a transaction's identity, so a bad guess would duplicate the row later.
    """
    candidates = []
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue                          # 29 Feb in a non-leap year
        if start <= candidate <= end:
            candidates.append(candidate)
    if not candidates:
        raise ValueError(f"date {day:02d}/{month:02d} falls outside {start}..{end}")
    if after:
        forward = [c for c in candidates if c >= after]
        if forward:
            return min(forward)
    if before:
        backward = [c for c in candidates if c <= before]
        if backward:
            return max(backward)
    return min(candidates)
