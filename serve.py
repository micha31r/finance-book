#!/usr/bin/env python3
"""Export the database and serve the frontend.

    .venv/bin/python serve.py [port]

Exports fresh JSON from backend/finance.db every time it starts, so the page
never shows stale numbers, then serves frontend/ on localhost.
"""
import http.server
import json
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "bin" / "python"

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


def apply_rules_now():
    """Relabel every transaction, so an added or deleted rule takes effect."""
    conn = backend_db.connect()
    # reclassify rebuilds types as well as categories, so deleting a rule that
    # set a type puts its rows back.
    reconcile.reclassify(conn)
    conn.commit()
    conn.close()


def is_date(value):
    """Whether value is a real date written YYYY-MM-DD, like 2026-09-15."""
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    python = str(PYTHON) if PYTHON.exists() else sys.executable

    result = subprocess.run([python, "export.py"], cwd=ROOT / "backend")
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

    Holdings are things no statement covers, like a term deposit or a
    Sharesies balance. They have to be typed in. Rules relabel transactions.
    And /api/apply runs a change the agent proposed and you approved, to a
    rule, a manual type or a holding. Nothing that came from a bank document
    is editable here.
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

        A POST must also be JSON. Another site cannot send JSON without a CORS
        preflight, and this server never answers one.
        """
        if not super().parse_request():
            return False
        port = self.server.server_port
        if self.headers.get("Host") not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            self._send({"error": f"open http://localhost:{port}/ instead"}, 403)
            return False
        origins = (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
        if (self.command in ("POST", "DELETE") and "Origin" in self.headers
                and self.headers["Origin"] not in origins):
            self._send({"error": "requests from other sites are refused"}, 403)
            return False
        if self.command == "POST" and self.headers.get_content_type() != "application/json":
            self._send({"error": "Content-Type must be application/json"}, 415)
            return False
        return True

    def end_headers(self):
        # data.json is rewritten after every change, but on a reload the browser
        # may reuse its cached copy, for hours if the file was old when fetched.
        # no-cache makes it check first. A malformed request has no path, so
        # the command is tested before it.
        if self.command == "GET" and urlsplit(self.path).path == "/data.json":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _query(self, sql):
        # db.connect, for its REGEXP that never runs a pattern that could hang.
        conn = backend_db.connect()
        rows = [dict(r) for r in conn.execute(sql)]
        conn.close()
        return rows

    def _rules(self):
        return self._query(
            "SELECT r.id, r.pattern, r.category,"
            " (SELECT COUNT(*) FROM txn WHERE description REGEXP r.pattern) AS matches"
            " FROM rule r ORDER BY r.id")

    def _holdings(self):
        return self._query("SELECT id, kind, name, institution, balance, as_at, note"
                           " FROM holding ORDER BY kind, name")

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

    def _rebuild_then_send(self, payload):
        """Rewrite data.json, then send `payload`, or export's error if it failed.

        The page updates itself from the response, but data.json is what a
        reload reads. The change is already saved when export runs, so the
        error says that. Otherwise a retry would save it twice.

        Export exits 3 when it wrote data.json but a derived balance disagrees
        with a printed one. The save still worked, so that warning goes to the
        terminal and the page gets its answer.
        """
        python = str(PYTHON) if PYTHON.exists() else sys.executable
        with EXPORT_LOCK:
            export = subprocess.run([python, "export.py"], cwd=ROOT / "backend",
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

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/holdings", "/api/rules", "/api/chat", "/api/apply", "/api/stop"):
            return self.send_error(404)
        item = self._body()
        if item is None:
            return
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
        if path == "/api/apply":
            try:
                import agent                   # inside the try: it fails without the agent SDK
                changed = agent.apply_proposal(item.get("sql", ""))
            except Exception as error:
                return self._send({"error": str(error)}, 400)
            return self._rebuild_then_send({"changed": changed, "rules": self._rules()})
        if path == "/api/rules":
            return self._add_rule(item)
        balance, as_at = item.get("balance"), item.get("as_at")
        # type, not isinstance: isinstance(True, int) holds, so true would save as 1 cent.
        # SQLite integers are 8 bytes. A larger number would crash the insert.
        if not item.get("name") or type(balance) is not int or not -2**63 <= balance < 2**63:
            return self._send({"error": "name and integer balance in cents required"}, 400)
        # JSON can send a list or an object where text belongs, and SQLite can't store those.
        if not isinstance(item["name"], str) or any(
                item.get(key) is not None and not isinstance(item[key], str)
                for key in ("institution", "note")):
            return self._send({"error": "name, institution and note must be text"}, 400)
        if item.get("id") is not None and type(item["id"]) is not int:
            return self._send({"error": "id must be an integer"}, 400)
        if as_at is not None and not is_date(as_at):
            return self._send({"error": "as_at must be a YYYY-MM-DD date or null"}, 400)
        kind = item.get("kind") or "term deposit"
        if kind not in ("term deposit", "investment"):
            return self._send({"error": "kind must be 'term deposit' or 'investment'"}, 400)
        conn = backend_db.connect()
        values = (kind, item["name"], item.get("institution"), balance, as_at)
        if item.get("id"):
            conn.execute("UPDATE holding SET kind=?, name=?, institution=?, balance=?, as_at=?"
                         " WHERE id=?", values + (item["id"],))
            # The page's form has no note field, so an edit from it keeps the note.
            if "note" in item:
                conn.execute("UPDATE holding SET note=? WHERE id=?", (item["note"], item["id"]))
        else:
            conn.execute("INSERT INTO holding(kind, name, institution, balance, as_at, note)"
                         " VALUES (?,?,?,?,?,?)", values + (item.get("note"),))
        conn.commit()
        conn.close()
        return self._rebuild_then_send(self._holdings())

    def _add_rule(self, item):
        pattern, category, note = item.get("pattern") or "", item.get("category") or "", item.get("note")
        # As with holdings, a list or an object can arrive where text belongs.
        if not isinstance(pattern, str) or not isinstance(category, str) \
                or note is not None and not isinstance(note, str):
            return self._send({"error": "pattern, category and note must be text"}, 400)
        pattern, category = pattern.strip(), category.strip()
        if not pattern or not category:
            return self._send({"error": "pattern and category required"}, 400)
        # risky_pattern also refuses an invalid pattern. A risky one, like
        # (\w+ ?)+, can run for hours, so it is never saved.
        problem = backend_db.risky_pattern(pattern)
        if problem:
            return self._send({"error": problem}, 400)
        # The analysis view joins hidden categories with ~ in its URL, so a
        # category holding one would come back as two.
        if "~" in category:
            return self._send({"error": "a category cannot contain ~"}, 400)
        conn = backend_db.connect()
        conn.execute("INSERT INTO rule(pattern, category, note, created_at) VALUES (?,?,?,?)",
                     (pattern, category, note,
                      datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        apply_rules_now()
        return self._rebuild_then_send(self._rules())

    def do_DELETE(self):
        path = urlsplit(self.path).path
        conn = backend_db.connect()
        if path.startswith("/api/holdings/"):
            conn.execute("DELETE FROM holding WHERE id = ?", (path.rsplit("/", 1)[1],))
            conn.commit()
            conn.close()
            return self._rebuild_then_send(self._holdings())
        if path.startswith("/api/rules/"):
            conn.execute("DELETE FROM rule WHERE id = ?", (path.rsplit("/", 1)[1],))
            conn.commit()
            conn.close()
            apply_rules_now()
            return self._rebuild_then_send(self._rules())
        conn.close()
        return self.send_error(404)


if __name__ == "__main__":
    sys.exit(main())
