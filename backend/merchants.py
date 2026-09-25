"""Reducing a bank description to the merchant name.

Statements bury the merchant inside card numbers, payment-processor prefixes,
suburb names and effective dates. Stripping those is what lets one rule cover
every visit to the same shop.
"""
import re

# "VISA DEBIT PURCHASE CARD 1234 ...", "EFTPOS ...", "PAYMENT TO ..."
PREFIX = re.compile(
    r"^(?:VISA DEBIT (?:PURCHASE|DEPOSIT) CARD \d+|EFTPOS(?: PIN\*)?|PAYMENT TO"
    r"|WITHDRAWAL[- ]?\w*|DEPOSIT[- ]?\w*|BPAY|DIRECT DEBIT)\s+", re.I)
EFFECTIVE = re.compile(r"\s+EFFECTIVE DATE .*$", re.I)
# "22.00 USD INC O/S FEE $0.91"
FOREIGN = re.compile(r"\s+[\d,]+\.\d{2}\s+[A-Z]{3}\s+INC O/S FEE.*$", re.I)
REFERENCE = re.compile(r"\s*#\d+\s*$")
# A trailing state or country, sometimes several deep: "... MELBOURNE VIC AU"
TAIL = re.compile(r"\s+(AU|AUS|VIC|NSW|QLD|WA|SA|TAS|NT|ACT)$", re.I)
# Square, Zeller and friends put their own tag in front of the real merchant.
PROCESSOR = re.compile(r"^(SQ|ZLR|SMP|LS|SP|PAYPAL|PP|EZY|WWW)\s*\*\s*", re.I)


def merchant(description: str) -> str:
    text = PREFIX.sub("", description)
    for pattern in (EFFECTIVE, FOREIGN, REFERENCE):
        text = pattern.sub("", text)
    text = " ".join(text.split()).upper()
    for _ in range(3):
        text = TAIL.sub("", text)
    return PROCESSOR.sub("", text).strip()
