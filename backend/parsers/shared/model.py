"""Data shapes shared by every parser.

Money is always signed integer cents. Negative means money left the account.
Using cents avoids float rounding, which matters because the whole system
proves itself with exact balance arithmetic.
"""
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Transaction:
    date: date
    description: str
    amount: int
    balance: int | None = None      # running balance, when the source prints one
    effective_date: date | None = None
    reference: str | None = None    # shared trace id, e.g. ANZ "#123456"
    counterparty: str | None = None # other account number, for internal transfers


# Ranks decide who wins when two documents describe the same transaction.
# A final statement beats an export, which beats a provisional listing. A
# 'manual' document holds rows typed in on the page. They never share a key
# with a bank row (see db.manual_occurrence), so its rank is never compared;
# it is here so RANK[kind] holds for every document kind.
RANK = {"statement": 3, "export": 2, "report": 1, "list": 1, "manual": 0}


@dataclass
class Document:
    """One parsed file, or one account's slice of a multi-account file."""
    bank: str
    kind: str                       # statement | export | report | list
    number: str                     # account number, digits only
    transactions: list[Transaction]
    source_name: str
    bsb: str | None = None
    account_name: str | None = None
    product: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: int | None = None
    closing_balance: int | None = None
    statement_no: int | None = None
    # True when the period was inferred from the rows rather than stated by the
    # bank. Two exports can then share a period without being the same document,
    # so such a document never sweeps away rows it happens not to contain.
    derived_period: bool = False

    # Optional totals a source prints about itself. Used as extra checks.
    stated_deposits: int | None = None
    stated_withdrawals: int | None = None
    stated_interest: int | None = None
    # date -> closing balance, from Westpac's ACCOUNTS file
    daily_balances: dict[date, int] = field(default_factory=dict)

    @property
    def provisional(self) -> bool:
        return self.kind in ("report", "list")

    @property
    def rank(self) -> int:
        return RANK[self.kind]
