#!/usr/bin/env python3
"""Export the database and serve the frontend.

Exports fresh JSON from backend/finance.db every time it starts, so the page
never shows stale numbers, then serves frontend/ on localhost.
"""
import argparse
import calendar
import http.server
import json
import sqlite3
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent

# The backend's modules import each other by bare name, so their folder goes
# on the path once, before any of them is imported.
sys.path.insert(0, str(ROOT / "backend"))
import db as backend_db
import reconcile

# In bytes. Questions, rules, holdings and SQL changes are all far smaller.
MAX_BODY = 1_000_000

# Two saves close together each rebuild data.json. Run one export at a time, so
# the file left on disk always comes from the newest database.
EXPORT_LOCK = threading.Lock()

# How far apart the rows of a repeating plan sit: days, or calendar months.
STEP_DAYS = {"weekly": 7, "fortnightly": 14}
STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}
TYPES = ("automatic", *backend_db.TYPES)
# The note on manual_type rows set from the page. classify.py stores one you type.
NOTE = "entered by hand"


def months_later(day, months):
    """`day` moved on by whole months, the day of the month clamped to the end.

    Each step counts from the day given, not from the clamped one before it,
    so the 31st lands on 28 February and back on 31 March.
    """
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def series_dates(start, repeat, until):
    """Every day a repeating plan lands on, from `start` up to `until`."""
    days, n = [], 0
    while True:
        day = (start + timedelta(days=n * STEP_DAYS[repeat]) if repeat in STEP_DAYS
               else months_later(start, n * STEP_MONTHS[repeat]))
        if day > until:
            return days
        days.append(day)
        n += 1


def misdated(conn, account, day):
    """Why a real row cannot sit on `day`, or None.

    The bank's figure settles the balance up to the last day it stated one. A
    real row on or before that day would move it, and cannot. One after today
    has not happened yet, which makes it a plan. A what-if is a plan and moves
    nothing, so it may sit anywhere.
    """
    known = backend_db.last_known_balance(conn, account)
    if known and day <= known["period_end"]:
        return (f"a real row must be dated after {known['period_end']}, the last day this"
                " account's balance is known; tick what-if for an earlier plan")
    if day > date.today().isoformat():
        return "a real row cannot be dated after today; tick what-if for a plan"
    return None


def sign_error(kind, amount):
    """Why a type you chose cannot go with the amount, or None.

    Totals pick rows by type, so income marked on money out would be added as
    money in. 'automatic', 'transfer' and no type at all fit either sign.
    """
    if kind == "expense" and amount > 0 or kind == "income" and amount < 0:
        return "an expense is negative and income positive"
    return None


def series_ids(conn, row):
    """The row's id, or every id in its repeating plan."""
    if not row["series"]:
        return [row["id"]]
    return [r["id"] for r in conn.execute("SELECT id FROM txn WHERE series = ?", (row["series"],))]


def add_txn(conn, item):
    """Insert a row typed in on the page, or a repeating series of them.

    Returns {"added": n}, or {"error": why} with nothing written.
    """
    account, day, description = item.get("account"), item.get("date"), item.get("description")
    amount, kind, category = item.get("amount"), item.get("type"), item.get("category")
    whatif, repeat, until = item.get("whatif", True), item.get("repeat", "once"), item.get("until")
    if type(account) is not int or not conn.execute(
            "SELECT 1 FROM account WHERE id = ?", (account,)).fetchone():
        return {"error": "account must be the id of one of your accounts"}
    if not backend_db.is_date(day):
        return {"error": "date must be a real day written YYYY-MM-DD"}
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
        return {"error": "description must be 1 to 200 characters of text"}
    if backend_db.bad_cents(amount) or amount == 0:
        return {"error": "amount must be a non-zero integer in cents"}
    if kind not in TYPES:
        return {"error": "type must be automatic, income, expense or transfer"}
    if category is None:
        category = ""
    problem = sign_error(kind, amount) or backend_db.category_error(category)
    if problem:
        return {"error": problem}
    if not isinstance(whatif, bool):
        return {"error": "whatif must be true or false"}
    if repeat not in ("once", *STEP_DAYS, *STEP_MONTHS):
        return {"error": "repeat must be once, weekly, fortnightly, monthly, quarterly or yearly"}
    start = date.fromisoformat(day)
    days = [start]
    if repeat != "once":
        if not backend_db.is_date(until) or not start <= date.fromisoformat(until) <= months_later(start, 120):
            return {"error": "until must be a date from the first row's date to ten years after it"}
        days = series_dates(start, repeat, date.fromisoformat(until))
    if not whatif:
        # The days climb, so the first and the last bound every row: a series
        # that runs past today is a plan, however it starts.
        problem = misdated(conn, account, day) or misdated(conn, account, days[-1].isoformat())
        if problem:
            return {"error": problem}

    ids = [backend_db.add_manual(conn, account, d.isoformat(), description.strip(), amount, whatif)
           for d in days]
    if len(ids) > 1:
        # The first row's id names the series, on every row including itself.
        conn.execute(f"UPDATE txn SET series = ? WHERE id IN ({','.join('?' * len(ids))})",
                     (ids[0], *ids))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if kind != "automatic":
        conn.executemany("INSERT INTO manual_type(txn_id, type, note, set_at) VALUES (?,?,?,?)",
                         [(i, kind, NOTE, now) for i in ids])
    if category.strip():
        conn.executemany("INSERT INTO manual_category(txn_id, category, set_at) VALUES (?,?,?)",
                         [(i, category.strip(), now) for i in ids])
    reconcile.reclassify(conn, ids)
    conn.commit()
    return {"added": len(ids)}


def edit_txn(conn, txn_id, item):
    """Change one field of a row, and of every other row in its series.

    Returns {"changed": n}, or {"error": why} with nothing written.
    """
    # SQLite would read "1e3" as 1000; a path holds a row id or nothing.
    if not str(txn_id).isdecimal():
        return {"error": "no such transaction"}
    if len(item) != 1:
        return {"error": "send exactly one field to change"}
    field, value = next(iter(item.items()))
    if field not in ("description", "type", "category", "date", "amount"):
        return {"error": "only description, type, category, date and amount can be changed"}
    row = conn.execute(
        "SELECT t.id, t.account_id, t.series, t.amount, t.match_key, t.whatif,"
        " d.kind = 'manual' AS manual, m.type AS manual_type"
        " FROM txn t JOIN document d ON d.id = t.document_id"
        " LEFT JOIN manual_type m ON m.txn_id = t.id WHERE t.id = ?", (txn_id,)).fetchone()
    if row is None:
        return {"error": "no such transaction"}
    if field in ("date", "amount") and not row["manual"]:
        return {"error": "the bank set this row's date and amount;"
                         " only a row entered by hand can change them"}
    # A repeating plan changes as one, except for its dates: a date moves one row.
    targets = [row["id"]] if field == "date" else series_ids(conn, row)
    marks = ",".join("?" * len(targets))
    # Retyping one leg of a transfer decides what the other is, so the partners
    # are found now, before an amount edit breaks the pair below.
    partners = reconcile.with_partners(conn, targets)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if field == "description":
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
            return {"error": "description must be 1 to 200 characters of text"}
        # match_key keeps the bank's spelling, so a file loaded again still
        # finds the row instead of inserting it a second time.
        conn.execute(f"UPDATE txn SET description = ? WHERE id IN ({marks})",
                     (value.strip(), *targets))
    elif field == "type":
        if value not in TYPES:
            return {"error": "type must be automatic, income, expense or transfer"}
        # Every row of a series has the same amount, so one row's sign speaks for all.
        problem = sign_error(value, row["amount"])
        if problem:
            return {"error": problem}
        if value == "automatic":
            conn.execute(f"DELETE FROM manual_type WHERE txn_id IN ({marks})", targets)
        else:
            conn.executemany(
                "INSERT INTO manual_type(txn_id, type, note, set_at) VALUES (?,?,?,?)"
                " ON CONFLICT(txn_id) DO UPDATE SET type=excluded.type, note=excluded.note,"
                " set_at=excluded.set_at", [(i, value, NOTE, now) for i in targets])
    elif field == "category":
        problem = backend_db.category_error(value)
        if problem:
            return {"error": problem}
        if not value.strip():
            conn.execute(f"DELETE FROM manual_category WHERE txn_id IN ({marks})", targets)
        else:
            conn.executemany(
                "INSERT INTO manual_category(txn_id, category, set_at) VALUES (?,?,?)"
                " ON CONFLICT(txn_id) DO UPDATE SET category=excluded.category,"
                " set_at=excluded.set_at", [(i, value.strip(), now) for i in targets])
    elif field == "date":
        if not backend_db.is_date(value):
            return {"error": "date must be a real day written YYYY-MM-DD"}
        problem = None if row["whatif"] else misdated(conn, row["account_id"], value)
        if problem:
            return {"error": problem}
        # The date is part of the row's key, so it takes the next occurrence there.
        conn.execute("UPDATE txn SET date = ?, occurrence = ? WHERE id = ?",
                     (value, backend_db.manual_occurrence(
                         conn, row["account_id"], value, row["amount"], row["match_key"]),
                      row["id"]))
    else:
        if backend_db.bad_cents(value) or value == 0:
            return {"error": "amount must be a non-zero integer in cents"}
        # A type you chose stays, so the new amount has to fit it.
        problem = sign_error(row["manual_type"], value)
        if problem:
            return {"error": problem}
        # The amount is part of each row's key, so each takes the next occurrence there.
        for target in conn.execute(f"SELECT id, date, match_key FROM txn WHERE id IN ({marks})",
                                   targets).fetchall():
            conn.execute("UPDATE txn SET amount = ?, occurrence = ? WHERE id = ?",
                         (value, backend_db.manual_occurrence(
                             conn, row["account_id"], target["date"], value, target["match_key"]),
                          target["id"]))
        # A transfer pairs two equal amounts. Ingest may have paired a real row
        # typed in on the page with a bank row; with a new amount it no longer
        # fits, so the pair goes and the partner is retyped below.
        conn.execute(f"DELETE FROM transfer WHERE from_txn_id IN ({marks}) OR to_txn_id IN ({marks})",
                     (*targets, *targets))
    # Sign, rules and your decisions again, for these rows and their transfer partners.
    reconcile.reclassify(conn, partners)
    conn.commit()
    return {"changed": len(targets)}


def delete_txn(conn, txn_id):
    """Delete a row typed in on the page, with every other row in its series.

    Returns {"deleted": n}, or {"error": why} with nothing written.
    """
    if not str(txn_id).isdecimal():
        return {"error": "no such transaction"}
    row = conn.execute(
        "SELECT t.id, t.series, d.kind = 'manual' AS manual FROM txn t"
        " JOIN document d ON d.id = t.document_id WHERE t.id = ?", (txn_id,)).fetchone()
    if row is None:
        return {"error": "no such transaction"}
    if not row["manual"]:
        return {"error": "the bank's rows cannot be deleted; only a row entered by hand can"}
    targets = series_ids(conn, row)
    # Ingest may have paired a real hand row with a bank row as a transfer. That
    # row is retyped, or it would stay half a transfer and count nowhere.
    partners = [i for i in reconcile.with_partners(conn, targets) if i not in targets]
    # Their manual_type, manual_category and transfer rows go too (ON DELETE CASCADE).
    conn.execute(f"DELETE FROM txn WHERE id IN ({','.join('?' * len(targets))})", targets)
    if partners:
        reconcile.reclassify(conn, partners)
    conn.commit()
    return {"deleted": len(targets)}


def holdings(conn):
    """Every holding, which the Term deposits and Investments tabs draw from."""
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, name, institution, balance, as_at, note FROM holding"
        " ORDER BY kind, name")]


def add_holding(conn, item):
    """Insert a holding, or change the one whose id is given.

    Returns every holding, or {"error": why} with nothing written.
    """
    name, institution, note = item.get("name"), item.get("institution"), item.get("note")
    balance, as_at, holding_id = item.get("balance"), item.get("as_at"), item.get("id")
    # JSON can send a list or an object where text belongs, and SQLite can't store those.
    if not isinstance(name, str) or any(
            value is not None and not isinstance(value, str) for value in (institution, note)):
        return {"error": "name, institution and note must be text"}
    name = name.strip()
    if not 1 <= len(name) <= 100 or len(institution or "") > 100 or len(note or "") > 500:
        return {"error": "name must be 1 to 100 characters, institution at most 100 and note 500"}
    if backend_db.bad_cents(balance):
        return {"error": "balance must be an integer in cents"}
    if as_at is not None and not backend_db.is_date(as_at):
        return {"error": "as_at must be a YYYY-MM-DD date or null"}
    kind = item.get("kind") or "term deposit"
    if kind not in ("term deposit", "investment"):
        return {"error": "kind must be 'term deposit' or 'investment'"}
    if holding_id is not None and type(holding_id) is not int:
        return {"error": "id must be an integer"}
    values = (kind, name, institution, balance, as_at)
    if holding_id is None:
        conn.execute("INSERT INTO holding(kind, name, institution, balance, as_at, note)"
                     " VALUES (?,?,?,?,?,?)", values + (note,))
    else:
        if not conn.execute("UPDATE holding SET kind=?, name=?, institution=?, balance=?, as_at=?"
                            " WHERE id=?", values + (holding_id,)).rowcount:
            return {"error": "no such holding"}
        # The page's form has no note field, so an edit from it keeps the note.
        if "note" in item:
            conn.execute("UPDATE holding SET note=? WHERE id=?", (note, holding_id))
    conn.commit()
    return holdings(conn)


def delete_holding(conn, holding_id):
    """Delete a holding. Returns every holding left, or {"error": why}."""
    # SQLite would read "1e3" as 1000; a path holds a row id or nothing.
    if not str(holding_id).isdecimal() or not conn.execute(
            "DELETE FROM holding WHERE id = ?", (holding_id,)).rowcount:
        return {"error": "no such holding"}
    conn.commit()
    return holdings(conn)


def rules(conn, hits=False):
    """Every rule, with how many real rows each matches when `hits` is set.

    Counting runs every pattern over every row, nearly two seconds. A change
    answers without the counts: the page reloads and asks GET /api/rules for
    them once. What-ifs are left out, as rules.py leaves them out.
    """
    if not hits:
        return [dict(r) for r in conn.execute("SELECT id, pattern, category FROM rule ORDER BY id")]
    return [dict(r) for r in conn.execute(
        "SELECT r.id, r.pattern, r.category, (SELECT COUNT(*) FROM txn"
        " WHERE whatif = 0 AND description REGEXP r.pattern) AS matches"
        " FROM rule r ORDER BY r.id")]


def add_rule(conn, item):
    """Insert a rule and relabel every transaction by it.

    Returns every rule, or {"error": why} with nothing written.
    """
    pattern, category, note = item.get("pattern") or "", item.get("category") or "", item.get("note")
    # As with holdings, a list or an object can arrive where text belongs.
    if not isinstance(pattern, str) or not isinstance(category, str) \
            or note is not None and not isinstance(note, str):
        return {"error": "pattern, category and note must be text"}
    pattern, category = pattern.strip(), category.strip()
    if not pattern or not category:
        return {"error": "pattern and category required"}
    if len(pattern) > 500 or len(note or "") > 500:
        return {"error": "pattern and note are at most 500 characters"}
    # risky_pattern also refuses an invalid pattern. A risky one, like
    # (\w+ ?)+, can run for hours, so it is never saved.
    problem = backend_db.risky_pattern(pattern) or backend_db.category_error(category)
    if problem:
        return {"error": problem}
    conn.execute("INSERT INTO rule(pattern, category, note, created_at) VALUES (?,?,?,?)",
                 (pattern, category, note,
                  datetime.now(timezone.utc).isoformat(timespec="seconds")))
    # In the same transaction, so the rule and its labels land together or not
    # at all. reclassify rebuilds types as well as categories, so a rule that
    # sets a type takes effect now, and deleting one puts its rows back.
    reconcile.reclassify(conn)
    conn.commit()
    return rules(conn)


def delete_rule(conn, rule_id):
    """Delete a rule and relabel its rows by the rules that remain.

    Returns every rule left, or {"error": why} with nothing written.
    """
    if not str(rule_id).isdecimal() or not conn.execute(
            "DELETE FROM rule WHERE id = ?", (rule_id,)).rowcount:
        return {"error": "no such rule"}
    reconcile.reclassify(conn)
    conn.commit()
    return rules(conn)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", type=int, default=8000, help="default 8000")
    port = parser.parse_args().port

    result = subprocess.run([sys.executable, "export.py"], cwd=ROOT / "backend")
    # 3 means data.json was written with a warning, which export printed above.
    if result.returncode not in (0, 3):
        print("export failed, not serving")
        return result.returncode

    url = f"http://localhost:{port}/"
    print(f"\nserving {url}   (ctrl-c to stop)")
    handler = partial(QuietHandler, directory=str(ROOT / "frontend"))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


# Turns in progress, so a Stop from another request can reach the right one.
# Each is its cancel scope plus the token of the event loop running it: the
# stop arrives on a different thread and has to be handed back to that loop.
turns = {}


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the page, plus a small API for the few things it can change.

        GET    /api/rules           every rule with its hit count
        POST   /api/holdings        add a holding, or change the one whose id is sent
        DELETE /api/holdings/<id>   delete a holding
        POST   /api/rules           add a rule and relabel every row
        DELETE /api/rules/<id>      delete a rule and relabel every row
        POST   /api/txn             add a row entered by hand, or a repeating series
        POST   /api/txn/<id>        change one field of a row, and of its series
        DELETE /api/txn/<id>        delete a row entered by hand, with its series
        POST   /api/chat            one turn of the agent, as server-sent events
        POST   /api/stop            cancel a turn
        POST   /api/apply           run a change the agent proposed and you approved

    A holding is money no statement covers, like a term deposit or a Sharesies
    balance, so it is typed in. A bank row can be renamed, retyped and given a
    category, but its date and amount are the bank's, and it stays. A change
    the agent proposed is to a rule, a manual type or category, or a holding.
    Every write rebuilds data.json before it answers, so a reload shows it.
    """

    # Seconds one read or write on the socket may block. Without it, a request
    # that promises more body than it sends holds its thread forever. A quiet
    # stretch in a chat answer is not cut off: waiting on the agent is not a
    # socket read or write.
    timeout = 30

    def log_message(self, fmt, *args):
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            super().log_message(fmt, *args)

    def parse_request(self):
        """Parse the request line and headers, then refuse other sites.

        Every request must name this server in its Host header. Another site
        can point its own hostname at 127.0.0.1 (DNS rebinding) and read what
        comes back, but its requests still carry that hostname.

        A POST or DELETE must come from this page. Browsers send Origin with
        every cross-site one, even when that site cannot read the answer.

        Another site cannot read an /api/ answer either, but the server would
        still do the work, and GET /api/rules runs every pattern over every
        row. Browsers say where a request came from in Sec-Fetch-Site:
        same-origin is this page and none is the address bar. Any other
        sender is refused.

        A POST must also be JSON. Another site cannot send JSON without a CORS
        preflight, and this server never answers one.
        """
        if not super().parse_request():
            return False
        port = self.server.server_port
        # A browser leaves the port off the Host and Origin when it is the default.
        hosts = [f"localhost:{port}", f"127.0.0.1:{port}"]
        if port == 80:
            hosts += ["localhost", "127.0.0.1"]
        if self.headers.get("Host") not in hosts:
            self._send({"error": f"open http://localhost:{port}/ instead"}, 403)
            return False
        if (self.command in ("POST", "DELETE") and "Origin" in self.headers
                and self.headers["Origin"] not in [f"http://{host}" for host in hosts]):
            self._send({"error": "requests from other sites are refused"}, 403)
            return False
        site = self.headers.get("Sec-Fetch-Site", "same-origin")
        if urlsplit(self.path).path.startswith("/api/") and site not in ("same-origin", "none"):
            self._send({"error": "requests from other sites are refused"}, 403)
            return False
        if self.command == "POST" and self.headers.get_content_type() != "application/json":
            self._send({"error": "Content-Type must be application/json"}, 415)
            return False
        return True

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """The JSON object a POST carries, or None once an error has been sent."""
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal():
            self._send({"error": "Content-Length missing or invalid"}, 400)
            return None
        # Compare lengths first: int() refuses strings over 4,300 digits.
        if len(length) > len(str(MAX_BODY)) or int(length) > MAX_BODY:
            self._send({"error": "request body over 1 MB"}, 413)
            return None
        try:
            item = json.loads(self.rfile.read(int(length)))
        except (ValueError, RecursionError):       # not JSON, not UTF-8, or nested too deep
            item = None
        if not isinstance(item, dict):
            self._send({"error": "request body must be a JSON object"}, 400)
            return None
        return item

    def _write(self, change, *args):
        """Run one change to the database, then rebuild data.json for it.

        BEGIN IMMEDIATE takes the write lock before the change reads anything,
        so two adds at once cannot pick the same occurrence, and a rule and
        the relabel it causes land together or not at all. The change commits
        itself when it goes through. Closing without a commit throws a refused
        or failed one away, so nothing half-done is kept.
        """
        conn = backend_db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = change(conn, *args)
        except (sqlite3.Error, OverflowError) as error:
            # ingest.py holding the lock, or a number too big for SQLite.
            return self._send({"error": f"not saved: {error}"}, 500)
        finally:
            conn.close()
        # Holdings and rules answer with their whole table, a list. A refusal
        # is a dict: 404 for a row that is not there, 400 for bad input.
        if isinstance(result, dict) and "error" in result:
            return self._send(result, 404 if result["error"].startswith("no such") else 400)
        return self._rebuild_then_send(result)

    def _rebuild_then_send(self, payload):
        """Rewrite data.json, then send `payload`, or export's error if it failed.

        The page updates itself from the response, but data.json is what a
        reload reads. The change is already saved when export runs, so the
        error says that. Otherwise a retry would save it twice.

        Export exits 3 when it wrote data.json but a derived balance disagrees
        with a printed one. The save still worked, so that warning goes to the
        terminal and the page gets its answer.
        """
        with EXPORT_LOCK:
            export = subprocess.run([sys.executable, "export.py"], cwd=ROOT / "backend",
                                    capture_output=True, text=True)
        output = (export.stdout + export.stderr).strip()
        if export.returncode == 3:
            print(output)
        elif export.returncode:
            return self._send({"error": f"Saved, but data.json was not rebuilt: {output}"}, 500)
        return self._send(payload)

    def _chat(self, item):
        """Stream one turn of the agent as server-sent events.

        The reply is written out as it arrives rather than collected, so the
        page can show text and move the view while the agent is still working.
        ThreadingHTTPServer means holding this socket open blocks nothing else.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event):
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        # Imported here, not at the top: the rest of the app works fine without
        # the agent SDK installed, and should keep working.
        try:
            import anyio
            import anyio.lowlevel
            import agent
        except ImportError:
            return emit({"type": "done", "error": True,
                         "message": "Install the agent first: .venv/bin/pip install claude-agent-sdk"})

        turn = uuid.uuid4().hex[:12]

        async def run():
            with anyio.CancelScope() as scope:
                turns[turn] = (scope, anyio.lowlevel.current_token())
                events = agent.stream(item.get("message", ""), item.get("session"))
                try:
                    emit({"type": "turn", "id": turn})
                    async for event in events:
                        emit(event)
                finally:
                    turns.pop(turn, None)
                    # Close the stream even when cancelled, shielded so the close is
                    # not itself cancelled. Closing reaches the SDK's own cleanup,
                    # which terminates the claude process instead of leaving the
                    # model working, and billing, for an answer nobody will read.
                    with anyio.CancelScope(shield=True):
                        await events.aclose()
            if scope.cancelled_caught:
                emit({"type": "done", "stopped": True})

        try:
            anyio.run(run)
        except BrokenPipeError:
            pass                                   # the user navigated away mid-answer
        except Exception as error:
            message = str(error)
            if "authenticate" in message.lower():
                message = ("The claude CLI is not signed in. Run `claude` in a terminal "
                           "and sign in, then try again.")
            try:
                emit({"type": "done", "error": True, "message": message})
            except BrokenPipeError:
                pass

    def do_GET(self):
        # Only the rules page shows hit counts, so it asks here; see rules().
        if urlsplit(self.path).path == "/api/rules":
            # db.connect, for its REGEXP that never runs a pattern that could hang.
            conn = backend_db.connect()
            try:
                return self._send(rules(conn, hits=True))
            finally:
                conn.close()
        super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        txn_id = None
        if path.startswith("/api/txn/"):                # /api/txn/<id> changes one row
            path, txn_id = "/api/txn/", path[len("/api/txn/"):]
        if path not in ("/api/holdings", "/api/rules", "/api/chat", "/api/apply", "/api/stop",
                        "/api/txn", "/api/txn/"):
            return self.send_error(404)
        item = self._body()
        if item is None:
            return
        if path == "/api/txn":
            return self._write(add_txn, item)
        if path == "/api/txn/":
            return self._write(edit_txn, txn_id, item)
        if path == "/api/holdings":
            return self._write(add_holding, item)
        if path == "/api/rules":
            return self._write(add_rule, item)
        if path == "/api/chat":
            return self._chat(item)
        if path == "/api/stop":
            turn = item.get("turn")
            found = turns.get(turn) if isinstance(turn, str) else None   # a list is no dict key
            if not found:
                return self._send({"stopped": False})      # already finished, or never existed
            import anyio.from_thread
            scope, token = found
            try:
                anyio.from_thread.run_sync(scope.cancel, token=token)
            except RuntimeError:
                return self._send({"stopped": False})      # finished in the moment between
            return self._send({"stopped": True})
        try:
            import agent                   # inside the try: it fails without the agent SDK
            changed = agent.apply_proposal(item.get("sql", ""))
        except Exception as error:
            return self._send({"error": str(error)}, 400)
        return self._rebuild_then_send({"changed": changed})

    def do_DELETE(self):
        path = urlsplit(self.path).path
        for prefix, change in (("/api/txn/", delete_txn), ("/api/holdings/", delete_holding),
                               ("/api/rules/", delete_rule)):
            if path.startswith(prefix):
                return self._write(change, path[len(prefix):])
        return self.send_error(404)


if __name__ == "__main__":
    sys.exit(main())
