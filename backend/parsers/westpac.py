"""Westpac: the transaction data export and the ACCOUNTS balance file.

The data export holds several accounts in one file and carries a running
balance. The ACCOUNTS file is not a transaction source. It supplies account
names, which the export lacks, and a daily closing balance going back years.
Those daily balances are the only thing we have that can prove a transaction
is missing rather than merely wrong.
"""
import re
from collections import defaultdict

from .shared import csvfile
from .shared.money import cents
from .shared.model import Document, Transaction

EXPORT_HEADER = "Bank Account,Date,Narrative"
ACCOUNTS_HEADER = "As at date for closing balance"
PAYMENTS_HEADER = "Record Type,Name,Account Details"
REFERENCE = re.compile(r"\b(\d{7})\b")


def detect(path) -> bool:
    if not str(path).lower().endswith(".csv"):
        return False
    head = csvfile.first_line(path)
    return head.startswith(EXPORT_HEADER) or ACCOUNTS_HEADER in head or head.startswith(PAYMENTS_HEADER)


def parse(path) -> list[Document]:
    head = csvfile.first_line(path)
    if head.startswith(PAYMENTS_HEADER):
        # Every debit leg here is already in the data export. The only extra
        # information is the original foreign amount on international
        # payments, which nothing downstream uses yet.
        return []
    if ACCOUNTS_HEADER in head:
        return _parse_accounts(path)
    return _parse_export(path)


def _split_account(combined: str) -> tuple[str, str]:
    """'123456789012' -> ('123456', '789012'). BSB is always 6 digits."""
    combined = combined.strip()
    return combined[:6], combined[6:]


def _parse_export(path) -> list[Document]:
    by_account = defaultdict(list)
    for row in csvfile.rows(path, has_header=True):
        by_account[row["Bank Account"].strip()].append(row)

    documents = []
    for combined, rows in by_account.items():
        bsb, number = _split_account(combined)
        transactions = []
        for row in rows:
            debit = cents(row["Debit Amount"]) if row["Debit Amount"].strip() else 0
            credit = cents(row["Credit Amount"]) if row["Credit Amount"].strip() else 0
            narrative = " ".join(row["Narrative"].split())
            reference = REFERENCE.search(narrative)
            transactions.append(Transaction(
                date=csvfile.au_date(row["Date"]),
                description=narrative,
                amount=credit - debit,
                balance=cents(row["Balance"]) if row["Balance"].strip() else None,
                reference=reference[1] if reference else None,
            ))
        transactions.reverse()              # the export prints newest first
        documents.append(Document(
            bank="Westpac", kind="export", number=number, bsb=bsb,
            period_start=transactions[0].date if transactions else None,
            period_end=transactions[-1].date if transactions else None,
            closing_balance=transactions[-1].balance if transactions else None,
            transactions=transactions, source_name=path.name,
            derived_period=True,
        ))
    return documents


def _parse_accounts(path) -> list[Document]:
    """One row per account per day. Collapse to a name plus a daily balance series."""
    daily, names, products = defaultdict(dict), {}, {}
    for row in csvfile.rows(path, has_header=True):
        key = (row["BSB"].strip(), row["Account Number/Portfolio Number"].strip())
        names[key] = row["Account Nickname/Name"].strip()
        products[key] = row["Account Type"].strip()
        daily[key][csvfile.au_date(row["As at date for closing balance"])] = \
            cents(row["Closing Balance"])

    documents = []
    for key, balances in daily.items():
        bsb, number = key
        latest = max(balances)
        documents.append(Document(
            bank="Westpac", kind="accounts", number=number, bsb=bsb,
            account_name=names[key], product=products[key],
            period_start=min(balances), period_end=latest,
            closing_balance=balances[latest],
            daily_balances=balances, transactions=[], source_name=path.name,
        ))
    return documents
