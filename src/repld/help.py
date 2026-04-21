"""Canonical user-facing docs for repld.

`build_instructions()` composes the MCP `initialize.instructions` dynamically
based on kernel state (browser connected? which gists available?). `OVERVIEW`
and `_TOPICS` back the `repld help` command / `browser.help()`. Three surfaces,
no overlap:

  INSTRUCTIONS (dynamic)  → behavioral model for the agent
  Tool descriptions       → per-tool what + gotchas (lives in protocol.py)
  Topics                  → pure API reference for the human user
"""

import json
from pathlib import Path

from .ipc import _pid_alive

# ---------------------------------------------------------------------------
# Composable instruction blocks (agent-facing, behavioral model only)
# ---------------------------------------------------------------------------

_EXEC_MODEL = (
    "Execution model: "
    "exec runs code in shared __main__. If it exceeds timeout, returns "
    "{task_id, done:false} and pushes channel on completion. "
    "Output: head+tail preview; full at [full output: /path] — use Read/Grep. "
    "_ / _N history. Top-level await. "
    "defer(coro, label) schedules a background task, returns task_id immediately, "
    "pushes channel on completion. "
    "ask()/confirm()/choose() block on human input in the kernel pane."
)

_BROWSER_MODEL = (
    "Browser model: "
    "Attach by URL pattern. Short target IDs (9222:a1b2c3). "
    "Mutations (click/type/navigate/key/open) settle then return "
    "tree + network delta + console delta. "
    "Tree crosses iframes. Network separates API calls from assets. "
    "Read workflow: network → request → body. "
    "browser object available in exec for chaining."
)

_GISTS_MODEL = (
    "Gists: ~/.repld/gists/ and ./gists/ on sys.path. Auto-reload on re-import.\n"
    "Before using a gist, read repld://gists/{name} for the full API — constructor args, "
    "method signatures, and usage patterns."
)

_REFERENCE = "Reference: `repld help <topic>` — topics: exec, browser, gists, gates"


def build_instructions() -> str:
    """Compose INSTRUCTIONS dynamically based on kernel state."""
    import __main__

    from . import gists

    parts = [_EXEC_MODEL]

    # Browser section — only if browser object exists in namespace
    if "browser" in __main__.__dict__:
        parts.append(_BROWSER_MODEL)

    # Gists base + available gists (with constructor signatures)
    parts.append(_GISTS_MODEL)
    available = gists.scan()
    if available:
        lines = ["Available gists:"]
        for name, doc in available:
            sig = gists.signature(name)
            if sig:
                lines.append(f"  {sig:<35s} {doc}")
            else:
                lines.append(f"  {name:<35s} {doc}")
        parts.append("\n".join(lines))

    parts.append(_REFERENCE)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# OVERVIEW (repld help, no topic arg)
# ---------------------------------------------------------------------------

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
  exec      exec / defer / get_task / cancel + channel kinds
  browser   Tab and Browser Python API
  gists     Auto-reloading module directories
  gates     ask / confirm / choose + notify
"""


# ---------------------------------------------------------------------------
# Topics (pure API reference for user — no behavioral explanations)
# ---------------------------------------------------------------------------

_TOPICS: dict[str, str] = {
    "exec": """\
exec(code, timeout=2.0)
  Inline within timeout; else {task_id, done:false} + channel push.
  Spill: $XDG_RUNTIME_DIR/repld/{pid}-{tid}.out
  Preview: head+tail + [full output: /path]

  _ / __ / ___          last three results
  _N                    result of cell N
  Top-level await       supported

defer(coro, label=None) → task_id
  Fire-and-forget. Channel push on done. Visible to get_task/cancel.

get_task(task_id)       → {done, text, spill_path, ...}
cancel(task_id)         → {cancelled: bool}

Channel kinds:
  task_done             exec or defer finished
  user                  notify() from user code
  awaiting_human        ask/confirm/choose pending
  bg_task_error         uncaught exception in background task
  loop_blocked          asyncio loop blocked > 5s
  init_error            --init file failed
""",
    "browser": """\
Tab (async unless noted):
  tab.js(code, await_promise=)           → any
  tab.tree()                             → list[str]
  tab.click(selector)                    → None (auto-waits 2s)
  tab.type_text(selector, text, enter=)  → None (clears first, auto-waits)
  tab.fetch(url, method=, body=, headers=) → {status, ok, body}
  tab.navigate(url)                      → None
  tab.screenshot(full_page=)             → bytes
  tab.cookies()                          → list[dict]
  tab.cdp(method, **params)              → dict

Tab (sync — DuckDB queries):
  tab.network(url=, method=, status=, type=, include_assets=) → Rows
  tab.request(request_id)                → dict
  tab.body(request_id)                   → dict
  tab.console(level=, source=)           → Rows
  tab.clear()                            → None

  row.body()                             → dict (response body for a Row)

Browser:
  browser.attach(pattern)                → str
  browser.open(url)                      → Tab
  browser.find(target_id)                → Tab
  browser.tabs                           → list[Tab]
  browser.pages()                        → list[dict]
  browser.detach(pattern=)               → str
  browser.clear(target=)                 → str
  browser.disconnect()                   → None

Selectors (click/type_text):
  .css-class, #id, [attr]               CSS
  text=Submit                            visible text match
  role=button[name="Save"]              ARIA role + name
  label=Username                        input by label
  button:has-text('OK')                 CSS + text filter

Target IDs: "{port}:{6-hex}" (e.g. 9222:887d3d). Stable across navigation.
Requires: Chrome --remote-debugging-port=9222
""",
    "gists": """\
Paths:
  ~/.repld/gists/      global (all projects)
  ./gists/             per-project

Both on sys.path at kernel startup. Auto-reload: edit file, re-import → fresh module.

Discovery:
  Module docstring first line → shown in MCP instructions automatically.
  Override: set __repld_help__ = "..." in module for custom description.

Workflow:
  1. Write gists/foo.py (with docstring)
  2. import foo
  3. Edit → re-import → fresh module
""",
    "gates": """\
await ask(prompt, *, default=None, timeout=None)       → str
await confirm(prompt, *, default=None, timeout=None)   → bool
await choose(prompt, options, *, default=None, timeout=None) → str

Blocks cell on human input in kernel pane.
TimeoutError if no default and timeout expires.
Emits awaiting_human channel while blocked.

notify(content, **meta)
  One-shot channel push to all MCP sessions.
""",
}


# ---------------------------------------------------------------------------
# CLI helpers (repld help)
# ---------------------------------------------------------------------------


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
