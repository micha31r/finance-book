# Backend

Loads bank statements and exports into `finance.db`. Run from this folder.

```sh
../.venv/bin/python ingest.py               # prompts, drag files from Finder
../.venv/bin/python ingest.py FILE [FILE...]
../.venv/bin/python review.py               # confirm cross-bank transfers
../.venv/bin/python export.py               # write frontend/data.json
../.venv/bin/python classify.py list        # movements the bank did not explain
../.venv/bin/python rules.py list           # every category rule
../.venv/bin/python rules.py seed           # install the starting rules
../.venv/bin/python rules.py todo           # merchants no rule matches yet
../.venv/bin/python rules.py test "PATTERN" # preview before adding
../.venv/bin/python rules.py add Sports "BADMINTON|TENNIS"
../.venv/bin/python scramble.py             # random numbers for a demo, for good
```

`SCHEMA.md` documents the database and how to read it without
miscounting transfers.

Files are identified by reading them, not by their names, so the ANZ PDFs that
download as `209ec0c7-....pdf` sort themselves out. The extension still has to
be `.pdf` or `.csv`. Mixed banks and formats in one run are fine.

An ANZ CSV export contains no account number. If enough of its rows are already
in the database it is matched automatically. Otherwise you are asked once which
account it belongs to.

## Supported

| Bank | Files |
|---|---|
| ANZ Plus | Account Statement PDF, Transaction List PDF |
| ANZ | Statement PDF, Transaction Report PDF, CSV export |
| Westpac | data export CSV, ACCOUNTS CSV |

Westpac's `PAYMENTS` file is recognised and skipped. Every transaction in it is
already in the data export.

## Checks that reject a file

If one of these fails, nothing from that file is written and the offending row
is printed. Other files in the same run still load.

- running balance against every stated balance
- opening and closing balance
- stated totals for deposits, withdrawals and interest
- Westpac daily balances, which are the only thing that can catch a *missing*
  transaction rather than a wrong one

A source is only held to the checks it supports. An ANZ CSV export has no
balance column, so its rows are stored with `verified = 0` rather than being
refused. The balance-anchored gap check below is what earns them their trust.

`ingest.py` exits 1 when any file was rejected, not recognised or skipped, so a
script can tell.

## Checks reported after loading

These describe the database as a whole, so they warn rather than reject.

- statements re-derived from the rows actually stored, which catches a later
  file quietly adding or duplicating rows inside an already-proven period
- missing statement numbers
- adjacent statements agreeing at the boundary
- periods with no statement, checked against the balances either side
- transfers pointing at an account you have not loaded

## Re-running is safe

Re-loading the same file, or one that overlaps, changes nothing. Transactions
are keyed on `(account, date, amount, match_key, occurrence)`.

`match_key` is the description with capitalisation and any trailing
`EFFECTIVE DATE ...` removed. A PDF statement and a CSV export spell the same
transaction differently, and this gives both spellings one key. Reference
numbers stay in the key: two $100 transfers on one day differ only by their
reference and are separate transactions. The occurrence counter separates
transactions that really are identical, such as four $5.00 charges at the same
shop on one day.

A statement supersedes a provisional Transaction List or Report covering the
same period. Rows it matches are upgraded in place. Provisional rows it does
not match are deleted: items that never posted, or rows the statement words
differently. A listing loaded after its statement adds nothing inside that
period.

Transfers are paired after every run. Two legs inside one bank pair on their
shared reference. Then a debit and a credit of the same amount pair across
your accounts. Those amount pairs are re-derived each run, so loading files in
one run or in several gives the same pairs. A pair between banks is only
proposed, and `review.py` asks you to confirm it.

`db.connect()` upgrades an older database in place: it reads
`PRAGMA user_version`, runs each pending migration inside a transaction, and
sets the version.

## Entered by hand

The page adds rows to a ledger table with "+ add", and edits a cell when you
double-click it. Every account gets one `manual` document for the rows you
enter by hand. A what-if (`whatif = 1`) is a plan. The page tags it, counts it
apart in the sidebar footer, where you can hide them, and leaves it out of
every real-money total. Ingest never touches it. A real row (`whatif = 0`) is
provisional, like a Transaction List row: the next document that states a
balance replaces it. A CSV export with no balance does not, so delete the row
entered by hand when the export lists it. Rows entered by hand take
`occurrence` counting down from -1, so they never collide with the bank's
rows, which count up from 1.

Editing a bank row changes its description, type or category only. Dates and
amounts stay as the bank printed them. Type and category edits go to
`manual_type` and `manual_category`, which `reclassify` applies after the
rules. A type you set must agree with the sign: `income` on money in,
`expense` on money out.

## Money

Stored as signed integer cents. Negative is money out. No floats anywhere.

## Files

| file | does |
|---|---|
| `ingest.py` | entry point: detect, parse, validate, write |
| `review.py` | confirm proposed cross-bank transfers |
| `classify.py` | set the type of movements the bank's wording cannot explain |
| `rules.py` | label income and spending by category |
| `category_seed.py` | the starting merchant-to-category mapping |
| `merchants.py` | reduce a bank description to the merchant name |
| `export.py` | write `frontend/data.json` for the page |
| `agent.py` | the chat agent: read-only queries, page moves, proposed changes |
| `db.py` | schema, migrations and idempotent writes |
| `reconcile.py` | the checks, balance anchors, transfer matching |
| `scramble.py` | replace every amount and account number at random, for a demo |
| `parsers/shared/` | PDF geometry, CSV reading, money and dates |
| `parsers/{anzplus,anz,westpac}.py` | one per bank |
