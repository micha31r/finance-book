"""CSV reading shared by the ANZ and Westpac exports."""
import csv
from datetime import date, datetime


def rows(path, has_header=False):
    """Read a CSV, dropping blank lines. Returns dicts if has_header, else lists."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        if has_header:
            # A row with surplus cells keeps them in a list, and a short row
            # fills in None. Neither is text worth keeping a row for.
            return [r for r in csv.DictReader(fh)
                    if any(isinstance(v, str) and v.strip() for v in r.values())]
        return [r for r in csv.reader(fh) if any(v.strip() for v in r)]


def first_line(path) -> str:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return fh.readline()


def au_date(text: str) -> date:
    """Australian exports are all DD/MM/YYYY."""
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()
