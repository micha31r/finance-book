# Database schema

SQLite, at `backend/finance.db`. Nine tables.

**All money is signed integer cents.** Negative means money left the account.
`-1575` is $15.75 spent. Divide by 100 only when displaying.

**All dates are `YYYY-MM-DD` strings.** They sort and compare correctly as text.

```
bank ──< account ──< document ──< txn >── transfer
                                    ├──── manual_type
                                    └──── manual_category
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
| kind | TEXT | `statement`, `export`, `report`, `list`, `manual` |
| period_start / period_end | TEXT | date, may be null |
| statement_no | INTEGER | sequential per account, statements only |
| opening_balance / closing_balance | INTEGER | cents, may be null |
| source_name | TEXT | original filename, display only |
| ingested_at | TEXT | ISO timestamp |

`statement` is final and authoritative. `export` is a CSV download. `report`
and `list` are provisional current-period listings that a later statement
replaces. `manual` is the one document per account for the rows you typed in
on the page. Its periods and balances are null and its `source_name` is
`entered by hand`.

**Current balance of an account** is the `closing_balance` of its newest
document that has one, plus the amounts of any rows dated after that
document's `period_end`. Those rows come from a source that states no balance,
such as an ANZ CSV export, or are real rows you typed in. A what-if never
counts. On a tie a statement wins. There is no stored balance field on
`account`, so that loading older history never disturbs the present.

## txn

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| account_id | INTEGER | → account.id |
| document_id | INTEGER | → document.id, where this row was last seen |
| date | TEXT | posting date |
| effective_date | TEXT | when it actually happened, may be null. The day money was spent is `COALESCE(effective_date, date)`. |
| description | TEXT | as printed by the bank |
| amount | INTEGER | signed cents. Negative is money out. |
| balance | INTEGER | running balance after this row, **null when the source had no balance column** |
| type | TEXT | `income`, `expense`, `transfer` |
| category | TEXT | where it came from, set by `rule`. Null if no rule matches. A transfer only gets one from a rule with a type. |
| counterparty | TEXT | the other account number, internal transfers only |
| reference | TEXT | bank trace number, pairs the two legs of a transfer |
| provisional | INTEGER | 1 if from a `report` or `list`, or a real row you typed in |
| verified | INTEGER | 1 if covered by a balance check |
| sequence | INTEGER | position within its document |
| whatif | INTEGER | 1 for a plan you typed in on the page, 0 for real money. Set when the row is added, never changed. |
| series | INTEGER | → txn.id of the first row of a repeating series, which points at itself. Null for a row on its own. A leftover from a replaced series keeps it; the export reports it as no series. |
| occurrence, match_key | | deduplication keys, ignore when reading |

`sequence` is how two rows on the same day are ordered. Row `id` follows
insertion order, which stops matching the statement the moment anything is
re-ingested. Only the document that owns a row sets its sequence, because a CSV
export covering the same day lists things in its own order. Read transactions
with `ORDER BY date, sequence, id`.

### Rows you type in

The page adds rows to a ledger table with "+ add". A what-if (`whatif = 1`) is
a plan. A real row (`whatif = 0`) is money the bank has not shown yet. Both go
in the account's `manual` document, with `sequence` 1000000 so they list after
the bank's rows on a shared day. A repeating one is a series: every row of it
carries `series` = the first row's id.

**You can** double-click a cell to edit the description, type or category of
any row, the bank's included. On a row you typed in you can also edit the date
and amount, and delete it. Type and category edits are stored in `manual_type`
and `manual_category` against the row's id, so a re-ingest keeps them. A
statement that replaces a provisional row deletes them with it, a real typed
row included: label the bank's row again, or write a rule. On a series row,
description, type, category and amount apply to the whole series. The date
applies to that row only. Deleting a series row deletes the whole series.

**You cannot** change a bank row's date or amount, or delete it. Nor can you
change the what-if tag: delete the row you typed and add it again.

How typed rows sit beside the bank's:

- `occurrence` counts down from -1 for typed rows and up from 1 for the bank's,
  over the same key `(account_id, date, amount, match_key)`. Two identical
  what-ifs on one day both insert, and an ingested row never collides with one.
- A real typed row is `provisional = 1`. It can only be dated after the
  account's last known balance and no later than today. The next document that
  states a balance replaces it, like a Transaction List row, and so does the
  statement for its period. A CSV export with no balance does not: it adds the
  bank's row beside yours, and yours shows as `covered`.
- Ingest never touches a what-if. One dated inside any bank document's period
  shows as `covered` on the page: delete it if it happened, or it is counted
  twice. A what-if dated after the day the balance is known as at shows a
  projected balance, that balance plus every what-if from then on. An earlier
  one shows none.
- Renaming a row changes its description only. The rules run again on the new
  wording. Transfer pairing does not: a pair found before stays paired, and no
  new pair is looked for.

### type is the field that matters for any total

- `income` — money in that is really yours
- `expense` — money out that is really spent
- `transfer` — money moved between two accounts you own, **in either direction**

**Never sum `expense` and `transfer` together.** A transfer is not spending.
Moving $5,000 from Everyday to Growth Saver produces one `transfer` row of
`-500000` and another of `+500000`. Counting the negative one as an expense is
the single most common way these numbers go wrong.

**Every total of real money needs `AND whatif = 0`.** A what-if is a plan the
user typed in, not money that moved. It sits in the same table with a `type`
like any other row, so it is counted unless you leave it out. Include what-ifs
only when the question is about plans, and say so.

So:

```sql
spending = -SUM(amount) WHERE type = 'expense' AND whatif = 0
income   =  SUM(amount) WHERE type = 'income'  AND whatif = 0
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
| method | TEXT | `reference` (shared trace number), `amount` (same amount between your own accounts within a few days, backed by transfer wording or your name), `cross-bank` (you confirmed it) or `rejected` (a suggestion you turned down) |
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
`classify.py`, or double-click the type cell on the page. Needed because some
movements name no counterparty at all, such as a payment to your own name at a
bank you have no statements for. Wording a bank always uses for the same thing,
like `DETAILS ADVISED SEPARATELY` for money into a term deposit, is better as a
rule with a type.

## manual_category

Your category for one transaction, when no rule fits or a rule gets it wrong.

| column | type | notes |
|---|---|---|
| txn_id | INTEGER | → txn.id, primary key |
| category | TEXT | overrides `txn.category`. Never contains `~`. |
| set_at | TEXT | ISO timestamp |

Applied after the rules by `reclassify`, exactly as `manual_type` is for type.
Set it by double-clicking the category cell on the page. An empty value removes
the row and the rules apply again. Wording the bank always uses for the same
thing is better as a rule, which labels every row that matches.

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
first and narrowed by a later one. `rules.py seed` installs a starting set from
`category_seed.py`, including the typed rules that mark term-deposit movements
as transfers and interest as income. After that they are ordinary rows and you
edit them like any other.

A rule without a type labels income and spending only. It skips transfers, so
money moved between your own accounts never carries a spending category.

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
current-period listing, or was typed in as a real row, and will be replaced
when the statement arrives. A statement deletes any it does not match, and a
listing loaded after its statement adds nothing inside that period.

**Amounts are exact.** Never convert to float for arithmetic. Sum the integers,
divide by 100 at the end.
