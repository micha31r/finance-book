# Database schema

SQLite, at `backend/finance.db`. Eight tables.

**All money is signed integer cents.** Negative means money left the account.
`-1575` is $15.75 spent. Divide by 100 only when displaying.

**All dates are `YYYY-MM-DD` strings.** They sort and compare correctly as text.

```
bank ──< account ──< document ──< txn >── transfer
                                    └──── manual_type
holding   (standalone: money no statement covers)
```

## bank

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| name | TEXT | `ANZ Plus`, `ANZ`, `Westpac`. Unique. |

`ANZ Plus` and `ANZ` are different banks here. They are separate products with
separate statements and separate account numbers.

## account

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| bank_id | INTEGER | → bank.id |
| bsb | TEXT | 6 digits, may be null |
| number | TEXT | digits only. Unique per bank, not globally. |
| name | TEXT | account holder, may be null |
| product | TEXT | `ANZ Plus Everyday`, `ANZ ACCESS ADVANTAGE`, may be null |

Use `product` for a display label and fall back to `name`, then `number`.
Account numbers repeat across banks, so never match on `number` alone.

## document

One ingested file, or one account's slice of a multi-account file.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| account_id | INTEGER | → account.id |
| kind | TEXT | `statement`, `export`, `report`, `list` |
| period_start / period_end | TEXT | date, may be null |
| statement_no | INTEGER | sequential per account, statements only |
| opening_balance / closing_balance | INTEGER | cents, may be null |
| source_name | TEXT | original filename, display only |
| ingested_at | TEXT | ISO timestamp |

`statement` is final and authoritative. `export` is a CSV download. `report`
and `list` are provisional current-period listings that a later statement
replaces.

**Current balance of an account** is the `closing_balance` of its newest
document that has one. There is no stored balance field on `account`, so that
loading older history never disturbs the present.

## txn

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| account_id | INTEGER | → account.id |
| document_id | INTEGER | → document.id, where this row was last seen |
| date | TEXT | posting date |
| effective_date | TEXT | when it actually happened, may be null |
| description | TEXT | as printed by the bank |
| amount | INTEGER | signed cents. Negative is money out. |
| balance | INTEGER | running balance after this row, **null when the source had no balance column** |
| type | TEXT | `income`, `expense`, `transfer` |
| category | TEXT | where it came from, set by `rule`. Null if no rule matches. |
| counterparty | TEXT | the other account number, internal transfers only |
| reference | TEXT | bank trace number, pairs the two legs of a transfer |
| provisional | INTEGER | 1 if from a `report` or `list` |
| verified | INTEGER | 1 if covered by a balance check |
| sequence | INTEGER | position within its document |
| occurrence, match_key | | deduplication keys, ignore when reading |

`sequence` is how two rows on the same day are ordered. Row `id` follows
insertion order, which stops matching the statement the moment anything is
re-ingested. Only the document that owns a row sets its sequence, because a CSV
export covering the same day lists things in its own order. Read transactions
with `ORDER BY date, sequence, id`.

### type is the field that matters for any total

- `income` — money in that is really yours
- `expense` — money out that is really spent
- `transfer` — money moved between two accounts you own, **in either direction**

**Never sum `expense` and `transfer` together.** A transfer is not spending.
Moving $5,000 from Everyday to Growth Saver produces one `transfer` row of
`-500000` and another of `+500000`. Counting the negative one as an expense is
the single most common way these numbers go wrong.

So:

```sql
spending = -SUM(amount) WHERE type = 'expense'
income   =  SUM(amount) WHERE type = 'income'
```

`type` is recomputed across the whole database after every ingest, so it never
depends on the order files were loaded.

## transfer

Links the two legs of one movement.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| from_txn_id | INTEGER | → txn.id, the negative leg |
| to_txn_id | INTEGER | → txn.id, the positive leg |
| method | TEXT | `reference` (exact, shared trace number) or `cross-bank` (you confirmed it) |
| confirmed | INTEGER | 1 = a real transfer, 0 = you rejected the suggestion |

A leg can be typed `transfer` without a row here, when the other side has not
been loaded yet. Both legs of a confirmed pair always sum to zero.

## manual_type

Your decision about one transaction, when the bank's wording cannot settle it.

| column | type | notes |
|---|---|---|
| txn_id | INTEGER | → txn.id, primary key |
| type | TEXT | overrides `txn.type` |
| note | TEXT | why |
| set_at | TEXT | ISO timestamp |

Applied last by `reclassify`, so no automatic rule can undo it. Set it with
`classify.py`. Needed because some movements name no counterparty at all:
`DETAILS ADVISED SEPARATELY` and Westpac's bare `WITHDRAWAL ONLINE <ref> TFR`
are both money going into a term deposit, and nothing in the text says so.

## rule

Labels transactions by matching a regular expression against the description.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| pattern | TEXT | regular expression, matched case-insensitively |
| category | TEXT | what to call it, e.g. `family` |
| type | TEXT | optional. Forces `income`, `expense` or `transfer` too. |
| note | TEXT | may be null |
| created_at | TEXT | ISO timestamp |

Rules run in id order and the last match wins, so a broad rule can be written
first and narrowed by a later one. `rules.py seed` installs a starting set of
spending rules from `category_seed.py`; after that they are ordinary rows and
you edit them like any other.

A category never changes `type`. It answers a different question:

- `type` — is this money in, money out, or moving between your own accounts
- `category` — what kind of thing was it

Salary and money from your parents are both income. Groceries and rent are both
spending. The category is what tells them apart.

Categories used for spending are deliberately narrow, because "Eating out"
hides the difference between a $4 coffee and a $60 dinner. `rules.py todo`
lists merchants that no rule matches yet, busiest first.

### How each bank words a term deposit

Worked out from the statements, and encoded as rules so new imports need no
tagging. Money into a deposit is a transfer, not spending; interest is income.

| bank | meaning | wording |
|---|---|---|
| Westpac | money in | `WITHDRAWAL ONLINE <ref> TFR` |
| Westpac | principal back | `PRINCIPAL PAID ON 0000000 TERM DEPOSIT <number>` |
| Westpac | interest | `INTEREST PAID ON 0000000 TERM DEPOSIT <number>` |
| ANZ | money in | `DETAILS ADVISED SEPARATELY` |
| ANZ | principal back | `PRINCIPAL TRANSFERRED FROM <deposit>` |
| ANZ | interest | `CREDIT INTEREST FROM <deposit>` |

Westpac names no destination on the way in, which is what separates it from
`WITHDRAWAL MOBILE <ref> TFR Westpac Cho` — that one names the account and is
an ordinary internal transfer. Westpac pays interest monthly during the term.
ANZ pays it in lumps, usually at six-month points, and sometimes bundles it
into the maturity line instead.

**A deposit will not reconcile to the cent, and that is expected.** Interest
accrues inside an open deposit and is only paid at maturity, so the balance is
worth more than the amount that went in. Some payouts arrive with interest
already included, overstating the principal returned. And a deposit opened
before your statements begin shows money coming back that never went out.

## holding

Money you hold that no statement covers: a term deposit, a Sharesies balance.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| kind | TEXT | `term deposit` or `investment`. Decides which tab it appears under, nothing else. |
| name | TEXT | e.g. `Term deposit 12345`, `a broker portfolio` |
| institution | TEXT | may be null |
| balance | INTEGER | cents |
| as_at | TEXT | date, may be null |
| note | TEXT | may be null |

Counts towards net worth. Never towards income or spending. Edited from the
Term deposits and Investments tabs, which write through `serve.py`.

The "money parked elsewhere" figure covers both kinds together, since a
transfer to a broker leaves your accounts the same way one to a term deposit
does.

## Things worth knowing

**Net worth** is the sum of each account's current balance plus every
`holding.balance`. It is not the sum of all transactions, because history
rarely starts at zero.

**The books balance like this**, and it is worth checking after any change:

```
income - spending - money parked in accounts you hold no statements for
    = what is sitting in your bank accounts

that + every holding.balance
    = net worth
```

The left side is bank accounts only. Adding the holdings is the second step, so
do not expect the first line to reach net worth on its own.

This identity cannot catch one transfer counted as both income and spending: it
adds to one side and subtracts from the other, and the difference is unchanged.
Check `reconcile.py`'s pairing for that, not this.

Money parked elsewhere is the net of `transfer` rows with no confirmed partner.
If term deposits are the only such place, that figure is what should be sitting
in them, which makes it a direct check on what you typed into `holding`.

**`balance` is often null.** CSV exports and ANZ Transaction Reports carry no
running balance. Do not build a balance chart from that column alone. Derive it
by running the amounts forward from a document's `opening_balance`.

**Provisional rows can change.** Anything with `provisional = 1` came from a
current-period listing and will be replaced when the statement arrives.

**Amounts are exact.** Never convert to float for arithmetic. Sum the integers,
divide by 100 at the end.
