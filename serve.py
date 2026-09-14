#!/usr/bin/env python3
"""Export the database and serve the frontend.

    python3 serve.py [port]

Exports fresh JSON from backend/finance.db every time it starts, so the page
never shows stale numbers, then serves frontend/ on localhost.
"""
import http.server
import json
import re
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "bin" / "python"


def apply_rules_now():
    """Relabel every transaction, then rewrite data.json for the page."""
    sys.path.insert(0, str(ROOT / "backend"))
    import db as backend_db
    import reconcile
    conn = backend_db.connect()
    # reclassify, not apply_rules: a rule can force a `type`, and apply_rules
    # never resets type, so deleting such a rule through the page would leave
    # its rows typed as it left them. reclassify rebuilds type from scratch.
    reconcile.reclassify(conn)
    conn.commit()
    conn.close()
    rebuild()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    python = str(PYTHON) if PYTHON.exists() else sys.executable

    result = subprocess.run([python, "export.py"], cwd=ROOT / "backend")
    if result.returncode:
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


DB = ROOT / "backend" / "finance.db"


def rebuild():
    """Re-apply rules and rewrite data.json, so the page sees the change."""
    python = str(PYTHON) if PYTHON.exists() else sys.executable
    subprocess.run([python, "export.py"], cwd=ROOT / "backend", capture_output=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the page, plus a small API for holdings.

    Holdings are things no statement covers — a term deposit, a Sharesies
    balance — so they have to be typed in. Everything else in the database
    comes from a bank document and is never editable here.
    """

    def log_message(self, fmt, *args):
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            super().log_message(fmt, *args)

    def _query(self, sql):
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        conn.create_function("regexp", 2,
                             lambda p, t: bool(re.search(p, t or "", re.I)))
        rows = [dict(r) for r in conn.execute(sql)]
        conn.close()
        return rows

    def _rules(self):
        return self._query(
            "SELECT r.id, r.pattern, r.category, r.note,"
            " (SELECT COUNT(*) FROM txn WHERE description REGEXP r.pattern) AS matches"
            " FROM rule r ORDER BY r.id")

    def _holdings(self):
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, name, institution, balance, as_at, note"
            " FROM holding ORDER BY kind, name")]
        conn.close()
        return rows

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/holdings":
            return self._send(self._holdings())
        if self.path == "/api/rules":
            return self._send(self._rules())
        return super().do_GET()

    def do_POST(self):
        if self.path not in ("/api/holdings", "/api/rules"):
            return self.send_error(404)
        size = int(self.headers.get("Content-Length", 0))
        item = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/api/rules":
            return self._add_rule(item)
        if not item.get("name") or not isinstance(item.get("balance"), int):
            return self._send({"error": "name and integer balance in cents required"}, 400)
        kind = item.get("kind") or "term deposit"
        if kind not in ("term deposit", "investment"):
            return self._send({"error": "kind must be 'term deposit' or 'investment'"}, 400)
        conn = sqlite3.connect(DB)
        values = (kind, item["name"], item.get("institution"), item["balance"],
                  item.get("as_at"), item.get("note"))
        if item.get("id"):
            conn.execute("UPDATE holding SET kind=?, name=?, institution=?, balance=?,"
                         " as_at=?, note=? WHERE id=?", values + (item["id"],))
        else:
            conn.execute("INSERT INTO holding(kind, name, institution, balance, as_at, note)"
                         " VALUES (?,?,?,?,?,?)", values)
        conn.commit()
        conn.close()
        # The page updates itself from this response, but data.json is what a
        # reload reads. Without rebuilding it the new holding disappears.
        rebuild()
        return self._send(self._holdings())

    def _add_rule(self, item):
        pattern, category = (item.get("pattern") or "").strip(), (item.get("category") or "").strip()
        if not pattern or not category:
            return self._send({"error": "pattern and category required"}, 400)
        try:
            re.compile(pattern)
        except re.error as error:
            return self._send({"error": f"not a valid regular expression: {error}"}, 400)
        conn = sqlite3.connect(DB)
        conn.execute("INSERT INTO rule(pattern, category, note, created_at) VALUES (?,?,?,?)",
                     (pattern, category, item.get("note"),
                      datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        apply_rules_now()
        return self._send(self._rules())

    def do_DELETE(self):
        conn = sqlite3.connect(DB)
        if self.path.startswith("/api/holdings/"):
            conn.execute("DELETE FROM holding WHERE id = ?", (self.path.rsplit("/", 1)[1],))
            conn.commit()
            conn.close()
            rebuild()
            return self._send(self._holdings())
        if self.path.startswith("/api/rules/"):
            conn.execute("DELETE FROM rule WHERE id = ?", (self.path.rsplit("/", 1)[1],))
            conn.commit()
            conn.close()
            apply_rules_now()
            return self._send(self._rules())
        conn.close()
        return self.send_error(404)


if __name__ == "__main__":
    sys.exit(main())
