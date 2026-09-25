# Finance Book

A personal ledger built from real bank statements. Three parts:

```
backend/    parses statements, validates them, stores them in SQLite
frontend/   a static page that reads the data and draws it
serve.py    exports fresh data and serves the page
```

## Setup

Needs Python 3.11 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

## Load statements

On the page, open All accounts and click Upload statements. Pick any mix of
PDFs and CSVs. Each file is identified by reading it, checked, then loaded,
and the page shows the report. From a terminal:

```sh
cd backend
../.venv/bin/python ingest.py            # prompts, drag files from Finder
../.venv/bin/python ingest.py FILE...
../.venv/bin/python review.py            # confirm cross-bank transfers
../.venv/bin/python rules.py seed        # the starting categories, run once
```

Re-running is safe. Nothing is written unless the file passes its checks.
See `backend/README.md` for what is supported and what is checked.

The starting rules in `backend/category_seed.py` name the shops in the
author's own statements. Edit them for yours, or add rules on the Rules view.

## Look at it

```sh
.venv/bin/python serve.py    # http://localhost:8000
```

Exports the database to `frontend/data.json`, then serves the page. Run it
again after loading new statements.

The views are All accounts, one ledger per account, Spending analysis, Cashflow
(free cash day by day, with the what-ifs ahead), Term deposits, Investments and
Rules. The URL holds the view, so a link or a
bookmark opens the same place.

The bar at the top of the page asks an AI agent about your money. It needs the
`claude` CLI installed and signed in, so run `claude` once in a terminal
first. It reads the database without account numbers, and changes nothing
until you click Apply.

You can double-click a cell in any transaction table to edit its description,
type or category. Each ledger table ends with a "+ add" row for money the bank
has not listed yet. That is a real payment, or a what-if: a plan you want to
see the effect of. What-ifs are tagged, and a toggle in the sidebar footer
hides them. Rows you add can be edited and deleted.

The bank's rows keep their dates and amounts. A real row you add is replaced
when the bank's next balance covers its day. A CSV export with no balance adds
the bank's row beside yours instead, so delete yours then.

## Documentation

- `backend/README.md`: supported banks, validation, idempotency
- `backend/SCHEMA.md`: the database, and the rules for reading it correctly

## Privacy

Statements, the database and the exported JSON are all gitignored. Nothing with
your name or account numbers in it is tracked.

To demo the page, copy `backend/finance.db` somewhere safe, then run
`backend/scramble.py`. It replaces every amount and account number with a
random one and keeps everything else. Put the copy back afterwards.
