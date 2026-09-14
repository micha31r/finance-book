"""The analysis agent.

Runs through the Claude Agent SDK, which drives your installed `claude` CLI, so
it uses the Claude subscription you already have rather than an API key.

The agent never adds up money itself. It writes SQL, the database does the
arithmetic, and the numbers come back. That is the whole point: a model asked to
sum 3,000 amounts in its head will be close and wrong, and close and wrong is
useless for money.

Reading happens in two steps, so a question can be answered without the
descriptions of every transaction being sent anywhere:

    run_query   you get the count and the totals
    read_rows   you get the rows themselves, if you still need them

Account numbers and BSBs are never returned by either.
"""
import contextvars
import json
import re
import sqlite3
import uuid
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              SystemMessage, TextBlock, ThinkingBlock, ToolUseBlock,
                              create_sdk_mcp_server, query, tool)

DB = Path(__file__).parent / "finance.db"

# Columns that identify a bank account rather than describe a transaction. The
# agent has no use for them and they are the most sensitive thing in the file.
SECRET = {"bsb", "number", "account_number", "counterparty"}

# Tables a proposed change is allowed to touch. Everything here is a label or a
# hand-entered balance; nothing here is a parsed bank record.
WRITABLE = {"rule", "manual_type", "holding"}

# What the current request is collecting: UI moves and proposed changes, which
# reach the page as events rather than as text in the reply.
sink = contextvars.ContextVar("sink")
results = {}          # handle -> {"sql", "columns", "rows"}


def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def money(cents):
    if cents is None:
        return None
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def check_select(sql):
    """Allow one plain SELECT and nothing else.

    The connection is opened read-only so a write cannot succeed anyway. This is
    the second lock: it keeps ATTACH, PRAGMA and stacked statements out, so the
    only thing a bad query can do is return the wrong rows.
    """
    stripped = re.sub(r"--[^\n]*", " ", sql)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip().rstrip(";")
    if ";" in stripped:
        return "one statement at a time, please"
    if not re.match(r"^\s*(select|with)\b", stripped, re.I):
        return "only SELECT (or WITH ... SELECT) can be run here"
    banned = re.search(r"\b(attach|pragma|insert|update|delete|drop|alter|create|vacuum)\b",
                       stripped, re.I)
    return f"{banned.group(1).upper()} is not allowed here" if banned else None


def visible(columns):
    return [i for i, name in enumerate(columns) if name.lower() not in SECRET]


def text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str, indent=1)}]}


# ---------------------------------------------------------------- tools

@tool("schema", "The tables, columns and the rules for reading them correctly. "
                "Call this before writing any SQL.", {"type": "object", "properties": {}})
async def schema_tool(_args):
    conn = connect()
    tables = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        name = row[0]
        if name.startswith("sqlite_"):
            continue
        columns = [c[1] for c in conn.execute(f"PRAGMA table_info({name})")
                   if c[1].lower() not in SECRET]
        tables[name] = {
            "columns": columns,
            "rows": conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0],
        }
    conn.close()
    notes = (Path(__file__).parent / "SCHEMA.md").read_text()
    return text({"tables": tables, "how_to_read_it": notes,
                 "account_numbers": "not available, and not needed"})


@tool("run_query",
      "Run one SELECT and get back how many rows matched and what the numeric columns "
      "total. This is how you work out any figure: let the database add up, never add "
      "up yourself. Amounts are integer cents. Use read_rows afterwards if you need to "
      "see the transactions behind the number.",
      {"type": "object",
       "properties": {"sql": {"type": "string"},
                      "purpose": {"type": "string",
                                  "description": "one line, shown to the user"}},
       "required": ["sql"]})
async def run_query_tool(args):
    sql = args["sql"]
    problem = check_select(sql)
    if problem:
        return text({"error": problem})
    conn = connect()
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(5001)
    except sqlite3.Error as error:
        conn.close()
        return text({"error": str(error), "hint": "call schema if you are unsure of a column"})
    conn.close()

    keep = visible(columns)
    dropped = [c for c in columns if c.lower() in SECRET]
    columns = [columns[i] for i in keep]
    rows = [[r[i] for i in keep] for r in rows]

    handle = f"q{len(results) + 1}_{uuid.uuid4().hex[:6]}"
    results[handle] = {"sql": sql, "columns": columns, "rows": rows}

    totals = {}
    for i, name in enumerate(columns):
        values = [r[i] for r in rows if isinstance(r[i], (int, float))]
        if values and len(values) == len([r for r in rows if r[i] is not None]):
            total = sum(values)
            totals[name] = {"sum": total, "min": min(values), "max": max(values)}
            if name in ("amount", "balance", "total", "spent", "cents"):
                totals[name]["sum_money"] = money(total)

    sink.get().append({"type": "query", "sql": sql,
                       "purpose": args.get("purpose", ""), "rows": len(rows)})
    answer = {"handle": handle, "columns": columns, "row_count": len(rows), "totals": totals}
    if dropped:
        answer["columns_withheld"] = dropped
    if len(rows) > 5000:
        answer["note"] = "stopped at 5000 rows; narrow the query"
    # A handful of rows is the answer itself, not a sample of it.
    if len(rows) <= 3:
        answer["rows"] = rows
    return text(answer)


@tool("read_rows",
      "Read the actual rows a previous run_query matched, including descriptions and "
      "categories. Only call this when the wording of the transactions matters to the "
      "answer. Account numbers are never included.",
      {"type": "object",
       "properties": {"handle": {"type": "string"},
                      "offset": {"type": "integer"},
                      "limit": {"type": "integer", "description": "at most 200"}},
       "required": ["handle"]})
async def read_rows_tool(args):
    found = results.get(args["handle"])
    if not found:
        return text({"error": "no such handle; run the query again"})
    offset = max(0, int(args.get("offset") or 0))
    limit = min(200, max(1, int(args.get("limit") or 50)))
    window = found["rows"][offset:offset + limit]
    sink.get().append({"type": "read", "rows": len(window), "of": len(found["rows"])})
    return text({"columns": found["columns"], "offset": offset,
                 "returned": len(window), "total": len(found["rows"]), "rows": window})


@tool("set_view",
      "Put the page in front of the user on a particular view, so they can see what you "
      "are describing. Pass only the parameters you want to change; the rest are left "
      "alone. Use this whenever your answer is about something the page can show.",
      {"type": "object",
       "properties": {
           "view": {"type": "string",
                    "description": "'all', 'analysis', 'holdings', 'investments', 'rules', "
                                   "or an account id"},
           "year": {"type": "string", "description": "'2026', or 'all'"},
           "period": {"type": "string",
                      "description": "30d, 3m, 6m, 12m, ytd or all (spending analysis)"},
           "from": {"type": "string"}, "to": {"type": "string"},
           "accts": {"type": "string", "description": "comma separated account ids"},
           "hide": {"type": "string",
                    "description": "categories to leave OUT of the totals, separated by ~. "
                                   "To show only some categories, hide all the others."},
           "cat": {"type": "string", "description": "open this category's drill-down"},
           "q": {"type": "string", "description": "search text for the transaction table"},
       }})
async def set_view_tool(args):
    params = {k: str(v) for k, v in args.items() if v not in (None, "")}
    sink.get().append({"type": "view", "params": params})
    return text({"applied": params})


@tool("propose_change",
      "Suggest a change to the user's labels or entries. It is NOT applied: the user sees "
      "your reasoning and an Apply button. Use it for a category rule, a transaction the "
      "bank's wording got wrong, or a holding balance. Never for parsed bank records.",
      {"type": "object",
       "properties": {"title": {"type": "string", "description": "one line, what changes"},
                      "why": {"type": "string", "description": "why, with the numbers"},
                      "sql": {"type": "string",
                              "description": "the INSERT or UPDATE that would do it"}},
       "required": ["title", "why", "sql"]})
async def propose_change_tool(args):
    table = re.search(r"\b(?:into|update)\s+([a-z_]+)", args["sql"], re.I)
    if not table or table.group(1).lower() not in WRITABLE:
        return text({"error": f"only {', '.join(sorted(WRITABLE))} can be changed"})
    proposal = {"type": "proposal", "id": uuid.uuid4().hex[:8], "title": args["title"],
                "why": args["why"], "sql": args["sql"]}
    sink.get().append(proposal)
    return text({"shown_to_user": True,
                 "note": "waiting on the user; do not assume it was applied"})


TOOLS = [schema_tool, run_query_tool, read_rows_tool, set_view_tool, propose_change_tool]
NAMES = [f"mcp__book__{t.name}" for t in TOOLS]

SYSTEM = """You are the analysis agent inside Finance Book, a personal budgeting app.
You are talking to the person whose money it is.

Work from the database, never from memory. Call `schema` first, then `run_query`.
Let SQL do every calculation. Amounts are integer cents: -1575 is $15.75 spent.

The one thing that ruins these numbers is mixing types. `type` is 'income',
'expense' or 'transfer'. A transfer is money moving between the user's own
accounts and is neither income nor spending. Never add 'expense' and 'transfer'
together. Spending is -SUM(amount) WHERE type='expense'.

`date` is the bank's posting date. `spent_on` is when the money actually moved,
and is the right one for "when did I spend this".

Answer in a sentence or two, with the figure. Show your reasoning only when it
changes what the number means. If a question depends on something only the user
knows — which payee is a relative, what counts as "eating out" — ask them rather
than guessing. Say plainly when the data cannot answer something.

When your answer is about something the page can show, call `set_view` so they
are looking at it. To show only some categories, `hide` all the others."""


async def stream(prompt, session=None):
    """Yield events for one turn. `session` continues an earlier conversation.

    A session the CLI no longer has — cleared, expired, or started on another
    machine — is not worth an error. The turn quietly starts a new session, and
    the page picks the new id up from the events like any other.
    """
    started = False
    try:
        async for event in _turn(prompt, session):
            started = True
            yield event
    except Exception as error:
        # The CLI reports it as "No conversation found with session ID: ...",
        # before sending anything. Retrying only when nothing has gone out yet
        # means a failure partway through an answer is never shown twice.
        if session and not started and "No conversation found" in str(error):
            async for event in _turn(prompt, None):
                yield event
        else:
            raise


async def _turn(prompt, session):
    """One attempt at a turn."""
    events = []
    sink.set(events)
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM,
        mcp_servers={"book": create_sdk_mcp_server(name="book", tools=TOOLS)},
        allowed_tools=NAMES,
        permission_mode="bypassPermissions",   # the tools are the only way in, and they are read-only
        setting_sources=[],                    # ignore this repo's CLAUDE.md and settings
        max_turns=30,
        cwd=str(Path(__file__).parent),
        resume=session,
    )
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    # The CLI reports a dead login as ordinary assistant text, so
                    # it arrives here rather than as an exception. Say what to do
                    # about it instead of passing the raw line through.
                    if "authenticate" in block.text.lower() and "oauth" in block.text.lower():
                        yield {"type": "text",
                               "text": "I can't reach Claude: the `claude` CLI is signed out.\n"
                                       "Run `claude` in a terminal and sign in, then ask again."}
                        continue
                    yield {"type": "text", "text": block.text}
                elif isinstance(block, ThinkingBlock):
                    yield {"type": "thinking"}
                elif isinstance(block, ToolUseBlock):
                    yield {"type": "tool", "name": block.name.rsplit("__", 1)[-1]}
        elif isinstance(message, SystemMessage):
            if getattr(message, "subtype", "") == "init":
                yield {"type": "session", "id": message.data.get("session_id")}
        elif isinstance(message, ResultMessage):
            for event in events:
                yield event
            events.clear()
            yield {"type": "done", "session": message.session_id,
                   "error": message.is_error, "turns": message.num_turns}
        # Anything the tools recorded goes out as soon as it appears, so the page
        # moves while the answer is still being written.
        while events:
            yield events.pop(0)


def apply_proposal(sql):
    """Run a change the user approved. Same table restriction as the proposal."""
    table = re.search(r"\b(?:into|update)\s+([a-z_]+)", sql, re.I)
    if not table or table.group(1).lower() not in WRITABLE:
        raise ValueError(f"only {', '.join(sorted(WRITABLE))} can be changed")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("one statement at a time")
    import db as backend_db
    import reconcile
    conn = backend_db.connect()
    changed = conn.execute(sql).rowcount
    reconcile.reclassify(conn)       # a new rule has to be applied to be worth anything
    conn.commit()
    conn.close()
    return changed
