# Finance Book

A personal ledger built from real bank statements. Two parts:

```
backend/    parses statements, validates them, stores them in SQLite
frontend/   a static page that reads the data and draws it
serve.py    exports fresh data and serves the page
```

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

## Load statements

```sh
cd backend
../.venv/bin/python ingest.py            # prompts, drag files from Finder
../.venv/bin/python ingest.py FILE...
../.venv/bin/python review.py            # confirm cross-bank transfers
```

Re-running is safe. Nothing is written unless the file passes its checks.
See `backend/README.md` for what is supported and what is checked.

## Look at it

```sh
python3 serve.py          # http://localhost:8000
```

Exports the database to `frontend/data.json`, then serves the page. Run it
again after loading new statements.

## Documentation

- `backend/README.md` — supported banks, validation, idempotency
- `backend/SCHEMA.md` — the database, and the rules for reading it correctly

## Privacy

Statements, the database and the exported JSON are all gitignored. Nothing with
your name or account numbers in it is tracked.
