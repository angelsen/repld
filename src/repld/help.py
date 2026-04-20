"""Canonical user-facing docs for repld.

`INSTRUCTIONS` is the terse string MCP sends on `initialize`. `OVERVIEW` and
`_TOPICS` back the `repld help` command. One source of truth — `protocol.py`
re-exports INSTRUCTIONS so MCP clients and `!repld help` can never drift.
"""

import json
from pathlib import Path

from .ipc import _pid_alive

INSTRUCTIONS = (
    "Persistent Python runtime with a shared __main__ namespace. Use `exec` "
    "to run code; long tasks exceeding `timeout` return {task_id, done:false} "
    'and their completion arrives as <channel source="repld" kind="task_done" '
    'task_id="...">...</channel>. Inline output is a small head+tail preview; '
    "when truncated, the full output path is appended as `[full output: "
    "/path/to/spill.out]` — use the standard Read/Grep tools on that file. "
    "`get_task` polls a running task; `cancel` cancels an await-yielding task. "
    "`defer(coro, label=None)` schedules a coroutine as a tracked task, returns "
    "task_id immediately, and pushes task_done on completion. Visible to "
    "get_task and cancel. "
    "Top-level await is supported. The last expression auto-displays and "
    "binds to `_` / `__` / `___` (last three) and `_N` (N = execution count). "
    "`await ask(...)` / `await confirm(...)` / `await choose(...)` block the "
    "cell on human input in the kernel's pane. "
    "Browser CDP: `browser_attach` watches a URL pattern and attaches matching "
    "Chrome tabs. `browser_tabs` lists attached tabs with short target IDs "
    "(e.g. '9222:887d3d'). All other browser_* tools take a `target` parameter "
    "which is this short ID — it stays stable across page navigation. "
    "Network workflow: `browser_network` to scan requests → `browser_request` "
    "to inspect headers/auth/postData → `browser_body` for response content. "
    "The `browser` object is also available in exec for chaining operations in "
    "a single cell (e.g. `tab = browser.find(...); tab.network(url='api')`) — "
    "call `browser.help()` for the full Python API."
)


OVERVIEW = """\
repld — persistent Python kernel exposed to LLM agents over MCP.

Architecture:
  Terminal pane: `repld --init repl.py`   kernel + display
  Editor pane:   `claude` (or equivalent) agent talks to kernel via MCP

One asyncio loop, one __main__ namespace shared with the agent. Cells run
via the MCP `exec` tool. Long tasks defer; channel pushes wake the agent
when work completes, files change, webhooks fire, or human gates resolve.

Commands:
  repld                    Start a kernel in cwd
  repld --init FILE        Start a kernel, exec FILE first (project bootstrap)
  repld exec CODE          One-shot: run code in kernel, print result, exit
  repld exec               Interactive REPL (state persists in kernel)
  repld bridge             Stdio MCP bridge (Claude Code spawns this)
  repld init               Scaffold .mcp.json + .gitignore in cwd
  repld help [TOPIC]       This help (re-fetchable: agent can `!repld help`)

Topics:
  exec      How exec runs cells; timeout / nudge / channel push
  exec-cli  repld exec: one-shot and interactive REPL
  channel   Channel push notifications
  defer     defer(coro, label) — fire-and-forget with channel push
  notify    notify(), ask(), confirm(), choose() helpers
  init      What `repld init` writes
  gists     Personal SDK convention (planned)
  browser   CDP browser integration
"""


_TOPICS: dict[str, str] = {
    "exec": """\
exec(code, timeout=2.0)

Compiles `code` with PyCF_ALLOW_TOP_LEVEL_AWAIT and runs it on the kernel's
shared asyncio loop. Returns inline if the cell finishes within `timeout`
seconds; otherwise returns {task_id, done:false} and the completion arrives
later as a channel notification.

Output:
  - Last expression auto-displays and binds to `_`, `__`, `___` (last three
    results) plus `_N` (N = exec count, keyed per cell)
  - All stdout/stderr spills lazily to $XDG_RUNTIME_DIR/repld/{pid}-{tid}.out
    from byte 1 (no inline-vs-spill cutoff)
  - Inline response carries a head+tail preview + the absolute spill path
  - Use Read/Grep on spill_path for full output

Cancel a deferred task via the MCP `cancel` tool (takes task_id). Cancels
await-yielding code — cannot preempt tight sync loops.

Top-level await:
  await asyncio.sleep(0.1)             # works directly
  asyncio.create_task(coro())          # fire-and-forget; outlives the cell

State:
  __main__ namespace persists across cells. Helpers (notify, ask, confirm,
  choose) are pre-injected. Use `repld --init FILE` to pre-load project
  state (clients, sessions, app instances).
""",
    "exec-cli": """\
repld exec — human-facing CLI for the running kernel.

Usage:
  repld exec 'CODE'          one-shot: run code, print result, exit
  repld exec                 interactive REPL (Ctrl-D to exit)
  repld exec --json 'CODE'   JSON output for scripting (pipe to jq)

State persists in the kernel. Two successive one-shot calls share __main__:
  repld exec 'x = 42'
  repld exec 'print(x)'     # → 42

Interactive REPL:
  Multi-line blocks (def/class/if) work — incomplete input triggers a
  continuation prompt. Readline history saved to ~/.repld/history.
  Ctrl-C cancels an in-flight deferred task; Ctrl-D exits the client
  (kernel keeps running).

Long-running code:
  If the kernel defers (code takes > 30s), the CLI waits for the task_done
  channel notification and then prints the final output. Ctrl-C sends a
  cancel request.
""",
    "defer": """\
defer(coro, label=None) → task_id

Schedule an async coroutine as a tracked background task. Returns the task_id
synchronously. On completion (or failure), a task_done channel notification is
pushed to all connected MCP sessions.

  task_id = defer(scrape_all_pages(), label="scrape")

The task is immediately visible to get_task (for polling/snapshots) and cancel
(for cancellation). Stdout/stderr produced by the coroutine is captured to the
same spill files as exec cells.

Works from both sync and async contexts. The label appears in the channel
notification content and meta for easy identification.

Difference from exec with timeout:
  - exec blocks the MCP response for up to `timeout` seconds before deferring
  - defer returns immediately — zero blocking
  - exec takes source code (string); defer takes a coroutine object
""",
    "channel": """\
Channel push: server-initiated notifications/claude/channel sent over the
MCP session. The agent receives them as <channel source="repld" ...>...
</channel> injections in the next turn — no polling.

repld emits channels for:
  task_done        a deferred exec finished (success or error)
  user             user code called notify() with custom meta
  loop_blocked     bg asyncio loop blocked > REPLD_LOOP_BLOCK_THRESHOLD (5s)
  awaiting_human   user code awaited ask()/confirm()/choose()
  bg_task_error    unretrieved exception from asyncio.create_task()
  init_error       --init file failed to load

From user code:
  notify("hello", kind="info", color="blue")     # meta = XML attrs

Buffering:
  Channels emitted before the client sends notifications/initialized are
  queued in repld and flushed on initialize.
""",
    "notify": """\
notify(content, **meta)              # one-off channel push to all sessions

await ask(prompt, *, default=None, timeout=None)               # free-form
await confirm(prompt, *, default=None, timeout=None)           # yes/no
await choose(prompt, options, *, default=None, timeout=None)   # pick one

ask/confirm/choose are async — `await` them. The awaiting cell yields the
loop (uvicorn / bg tasks keep running) until the human answers in the
kernel's pane, or `timeout` elapses (raising TimeoutError if no `default`).
While waiting, kernel emits an awaiting_human channel push so the agent
sees the gate.

These helpers are pre-injected into __main__ — call them directly without
import.
""",
    "init": """\
repld init — scaffold a project for repld.

Writes:
  .mcp.json     MCP config so Claude Code spawns `repld bridge`
  .gitignore    appends .pyrepl.lock + .pyrepl.sock (creates if missing)

Idempotent. Won't overwrite an existing .mcp.json — adds a repld entry if
the file is present without one, or warns if the file isn't valid JSON.

Doesn't create repl.py — that's project-specific. Write your own to pre-load
state (clients, sessions, app handles). See examples/fastapi/repl.py for the
shape.

Suggested CLAUDE.md addition for the project:

  ## repld
  This project uses repld. Bring up the kernel with `repld --init repl.py`.
  Long cells return done:false; channel push on completion.
  For agent docs: `!repld help`.
""",
    "gists": """\
gists (planned) — the personal SDK convention.

Service-specific code lives in gists, not in repld core:
  ~/.repld/gists/*.py     global, available everywhere
  ./gists/*.py            per-project, versioned with the repo

Workflow:
  1. Attach a logged-in browser tab: await browser.attach("*elhub.no*")
  2. Agent reads the page's network traffic via tab.network()
  3. Agent runs tab.js() against the live page with your session
  4. Once endpoints work, agent writes gists/<service>.py with a class
  5. Reuse: `from gists.elhub import Elhub`

The gist is the cached, named version of a reverse-engineering session. If
the site changes, the agent reruns the loop with the existing class as the
diff baseline.

Not implemented yet. See README "Status" section.
""",
    "browser": """\
browser — CDP integration for Chrome DevTools Protocol.

Requires Chrome launched with --remote-debugging-port=9222.

Workflow:
  1. browser_attach(pattern="*example.com*")   watch + attach matching tabs
  2. browser_tabs                              list attached tabs with short IDs
  3. browser_js(target="9222:a1b2c3", ...)     use short ID for all operations

Target IDs:
  Format: "{port}:{6-char-hex}" (e.g. "9222:887d3d"). Derived from Chrome's
  internal target ID. Stable across page navigation — the ID doesn't change
  when the page redirects or reloads.

MCP tools:
  browser_attach(pattern)                  attach tabs matching URL glob
  browser_detach(pattern?)                 detach by pattern, or all
  browser_tabs                             list attached tabs
  browser_pages                            list all Chrome targets
  browser_js(target, code)                 eval JS in tab
  browser_click(target, selector)          click element by CSS selector
  browser_type(target, selector, text)     type into element
  browser_network(target, url?, method?)   scan requests (compact list)
  browser_request(target, request_id)      inspect headers/auth/postData
  browser_body(target, request_id)         fetch response body
  browser_console(target, level?)          query console messages
  browser_screenshot(target)               capture PNG screenshot
  browser_cdp(target, method, params?)     raw CDP passthrough

From exec (Python API — `await` is optional, auto-detected):
  browser.attach("*example.com*")          returns summary string
  tab = browser.find("9222:a1b2c3")        resolve Tab by short ID
  tab.js("document.title")                 eval JS
  tab.network(url="api")                   scan requests (sync, returns Rows)
  tab.request(request_id)                  inspect full HAR entry (dict)
  tab.console(level="error")               query console (sync)
  row.body()                               fetch response body for a Row
  tab.cookies()                            get cookies via CDP
  tab.cdp("Page.navigate", url=...)        raw CDP passthrough
  browser.tabs                             list attached Tab objects
  browser.pages()                          list all Chrome targets
  browser.detach("*pattern*")              detach by pattern
  browser.disconnect()                     close WS connection

Network workflow (progressive disclosure):
  1. Scan:    browser_network / tab.network()   → compact list, pick by rid
  2. Inspect: browser_request / tab.request(rid) → headers, auth, postData
  3. Content: browser_body / row.body()          → response body

  Network events are stored per-tab in DuckDB. Fetch interception captures
  request POST bodies and response bodies automatically.
""",
}


def _check_state(cwd: Path) -> dict:
    state: dict = {
        "lock_exists": (cwd / ".pyrepl.lock").exists(),
        "lock_alive": False,
        "mcp_configured": False,
        "repl_py_exists": (cwd / "repl.py").exists(),
    }
    if state["lock_exists"]:
        try:
            lock = json.loads((cwd / ".pyrepl.lock").read_text())
            state["lock_alive"] = _pid_alive(lock.get("pid", -1))
        except (OSError, json.JSONDecodeError):
            pass
    mcp = cwd / ".mcp.json"
    if mcp.exists():
        try:
            cfg = json.loads(mcp.read_text())
            servers = cfg.get("mcpServers", {})
            state["mcp_configured"] = "repld" in servers
        except (OSError, json.JSONDecodeError):
            pass
    return state


def _suggestion(cwd: Path) -> str:
    s = _check_state(cwd)
    if not s["mcp_configured"]:
        return (
            "Suggested next step:\n"
            "  repld init   # scaffold .mcp.json + .gitignore in cwd\n"
        )
    if s["lock_alive"]:
        return "Kernel running in cwd. Open Claude Code: `claude`\n"
    if s["lock_exists"] and not s["lock_alive"]:
        return (
            "Stale .pyrepl.lock detected (kernel pid not alive).\n"
            "  rm .pyrepl.lock   # then `repld` to start fresh\n"
        )
    cmd = "repld --init repl.py" if s["repl_py_exists"] else "repld"
    return f"Suggested next step:\n  {cmd}   # start the kernel\n"


def run_help(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(OVERVIEW)
        return 0
    if not argv:
        print(OVERVIEW)
        print(_suggestion(Path.cwd()))
        return 0
    topic = argv[0]
    if topic not in _TOPICS:
        print(f"Unknown topic: {topic}")
        print(f"Topics: {', '.join(sorted(_TOPICS))}")
        return 2
    print(_TOPICS[topic])
    return 0
