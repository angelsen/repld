"""Canonical user-facing docs for repld.

`INSTRUCTIONS` is the terse string MCP sends on `initialize`. `OVERVIEW` and
`_TOPICS` back the `repld help` command. One source of truth — `protocol.py`
re-exports INSTRUCTIONS so MCP clients and `!repld help` can never drift.
"""

import json
import os
from pathlib import Path

INSTRUCTIONS = (
    "Persistent Python runtime with a shared __main__ namespace. Use `exec` "
    "to run code; long tasks exceeding `timeout` return {task_id, done:false} "
    'and their completion arrives as <channel source="repld" kind="task_done" '
    'task_id="...">...</channel>. Inline output is a small head+tail preview; '
    "when truncated, the full output path is appended as `[full output: "
    "/path/to/spill.out]` — use the standard Read/Grep tools on that file. "
    "`get_task` polls a running task; `cancel` cancels an await-yielding task. "
    "Top-level await is supported. The last expression auto-displays and "
    "binds to `_` / `__` / `___` (last three) and `_N` (N = execution count). "
    "`await ask(...)` / `await confirm(...)` / `await choose(...)` block the "
    "cell on human input in the kernel's pane."
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
  repld bridge             Stdio MCP bridge (Claude Code spawns this)
  repld init               Scaffold .mcp.json + .gitignore in cwd
  repld help [TOPIC]       This help (re-fetchable: agent can `!repld help`)

Topics:
  exec      How exec runs cells; timeout / nudge / channel push
  channel   Channel push notifications
  notify    notify(), ask(), confirm(), choose() helpers
  init      What `repld init` writes
  gists     Personal SDK convention (planned)
  browser   CDP browser attach (planned)
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
  1. Attach a logged-in browser tab via CDP (planned: browser.find)
  2. Agent reads the page's network traffic via HAR capture
  3. Agent runs Runtime.evaluate against the live page with your session
  4. Once endpoints work, agent writes gists/<service>.py with a class
  5. Reuse: `from gists.elhub import Elhub`

The gist is the cached, named version of a reverse-engineering session. If
the site changes, the agent reruns the loop with the existing class as the
diff baseline.

Not implemented yet. See README "Status" section.
""",
    "browser": """\
browser (planned) — CDP attach to logged-in tabs.

browser.find(url_pattern)                  attach one tab matching pattern
browser.auto_attach(pattern, as_="name")   auto-attach matching tabs as
                                            they appear in Chrome

Requires Chrome launched with --remote-debugging-port=9222.

Tabs land in __main__ as named handles:
  await tabs.elhub.eval("await fetch('/api/...').then(r => r.json())")
  tabs.elhub.har.last(50)                  query recent network traffic

Not implemented yet. See README "Status" section.
""",
}


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — still alive.
        return True


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
