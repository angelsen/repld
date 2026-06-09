"""Canonical user-facing docs for repld.

`build_instructions()` composes the MCP `initialize.instructions` dynamically
based on kernel state (browser connected? which gists available?). `OVERVIEW`
and `_TOPICS` back the `repld help` command / `browser.help()`. Four surfaces,
no overlap:

  INSTRUCTIONS (dynamic)  → behavioral model for the agent (terse, always loaded)
  Tool descriptions       → per-tool what + gotchas (lives in protocol.py)
  Topics                  → pure API reference for the human user
  GUIDE                   → MCP resource (repld://docs/guide) — working guide
                            with patterns and conventions; read on demand
"""

import json
import sys
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
    "every(seconds)(fn) schedules fn to run periodically; "
    "fn.cancel() stops it. every.list() shows active tickers. "
    "ask()/confirm()/choose() block on human input in the kernel pane. "
    "When you see a task that could run continuously — monitoring, polling, "
    "watching for changes — suggest wiring it with defer() + notify() or @every. "
    "The kernel persists; one-shot work can become background automation."
)

_BROWSER_MODEL = (
    "Browser model: "
    "Watch by URL pattern. Short target IDs (9222:a1b2c3). "
    "Mutations (click/type/navigate/key/open) settle then return "
    "tree + network delta + console delta. "
    "Tree crosses iframes. Network separates API calls from assets. "
    "Read workflow: network → request → body. "
    "browser object available in exec for chaining. "
    "For repeated browser interactions, write a gist (gists/*.py) to capture "
    "the API pattern. tab.pin() guards the session; tab.confirm()/choose() "
    "gate mutations in the browser. "
    "Run `repld help browser` for the full Python API (Tab, network queries, fetch)."
)

_GISTS_MODEL = (
    "Gists: ~/.repld/gists/ and ./gists/ on sys.path. Auto-reload on re-import.\n"
    "Before using a gist, read repld://gists/{name} for the full API — constructor args, "
    "method signatures, and usage patterns.\n"
    "Stable gists can register as MCP tools via __repld_tools__ — callable "
    "directly without exec, discoverable in tools/list.\n"
    'Gists declare deps via __repld_deps__ = ["httpx>=0.27"]; '
    "kernel prompts to install missing ones at boot."
)

_REFERENCE = "Reference: `repld help <topic>` — topics: exec, browser, gists, gates\nRead repld://docs/guide for exec patterns, browser workflows, and gist conventions."


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
            mod = sys.modules.get(name)
            usage = (
                str(mod.__repld_usage__)
                if mod and hasattr(mod, "__repld_usage__")
                else None
            )
            if usage and sig:
                # Usage override — show import of class name + usage hint
                class_name = sig.split("(")[0]
                hint = f"from {name} import {class_name}; {usage}"
            elif sig:
                hint = f"from {name} import {sig}"
            else:
                hint = f"import {name}"
            lines.append(f"  {hint:<55s} {doc}")
        parts.append("\n".join(lines))

    # Gist-registered tools
    gist_tools = gists.scan_tools()
    if gist_tools:
        names = [t["name"] for t in gist_tools]
        parts.append(
            "Gist tools: "
            + ", ".join(names)
            + " — call directly as MCP tools (no exec needed)."
        )

    # Dependency management hint
    from pathlib import Path

    if (Path.cwd() / "uv.lock").exists():
        parts.append(
            "Dependencies: this is a uv project. "
            "Add packages with `uv add <pkg>`, then restart the kernel. "
            "Gists can also declare __repld_deps__ for auto-install at boot."
        )
    else:
        parts.append(
            'Dependencies: gists can declare __repld_deps__ = ["pkg"] '
            "for boot-time install into the tool venv. "
            "Stdlib and pre-installed packages are always available."
        )

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

every(seconds, label=)(fn)  → fn    periodic ticker; fn.cancel() stops
every.list()                → list  active EveryHandles
every.cancel_all()          → None  stop all tickers

get_task(task_id)  → {done, text, spill_path, ...}
cancel(task_id)    → {cancelled: bool}

Channel kinds:
  task_done             exec or defer finished
  user                  notify() from user code
  every                 periodic tick result or error (kind=every, label=fn_name)
  awaiting_human        ask/confirm/choose pending
  bg_task_error         uncaught exception in background task
  loop_blocked          asyncio loop blocked > 5s
  loop_kill             watchdog cancelled a stuck task
  init_error            --init file failed
""",
    "browser": """\
Tab (async unless noted):
  tab.js(code, await_promise=)                     → any
  tab.tree()                                       → list[str]
  tab.click(selector)                              → None (auto-waits 2s, mouse event)
  tab.tap(selector_or_x, y=)                       → None (touch event, 3s timeout)
  tab.swipe(x1, y1, x2, y2, steps=, duration_ms=)  → None (touch scroll)
  tab.type_text(selector, text, enter=)            → None (clears first, auto-waits)
  tab.wait_for(selector, timeout=5)                → None (wait for element to appear)
  tab.wait_for_idle(timeout=5, quiet=0.5)          → int  (network idle; returns settle ms)
  tab.fetch(url, method=, body=, headers=)         → {status, ok, body}
  tab.navigate(url)                                → None
  tab.reload()                                     → None
  tab.screenshot(full_page=)                       → bytes
  tab.cookies()                                    → list[dict]
  tab.cdp(method, **params)                        → dict

Tab — pin + gate bridge:
  tab.pin(reason="")                 → None  inject pill + beforeunload guard; idempotent
  tab.unpin()                        → None  remove pill + guard
  tab.confirm(prompt, **kw)          → bool  gate routed to pill UI
  tab.choose(prompt, options, **kw)  → str   gate routed to pill UI
  tab.ask(prompt, **kw)              → str   terminal only (no pill UI for text input)

  Pill: bottom-center floating pill, green dot when connected, amber when awaiting input.
  Clicking pill expands panel with status, hostname, reason, and gate prompt + buttons.
  Gates queue — active gate on top, resolve pops next. Terminal and browser resolve same
  Future; first resolution wins.
  Lifecycle: heartbeat every 5s. Pill self-destructs if beats stop (detach/crash/shutdown).
  Same-origin reload auto-reinjects. Cross-origin navigation unpins + pushes channel.

Tab (sync — DuckDB queries):
  tab.network(url=, method=, status=, type=, since=, include_assets=)  → Rows
  tab.request(request_id)                                              → dict
  tab.body(request_id)                                                 → dict
  tab.console(level=, source=, since=)                                 → Rows
  tab.clear()                                                          → None

  row.body()                             → dict (response body for a Row)

Browser:
  browser.get(target, timeout=, fresh=, ready=)  → Tab  (glob or target ID; skips workers for globs)
  browser.watch(pattern)                         → str  (watch all matching, auto-attach new)
  browser.open(url)                              → Tab
  browser.tabs                                   → list[Tab]
  browser.pages()                                → list[dict]
  browser.detach(pattern=)                       → str
  browser.clear(target=)                         → str
  browser.disconnect()                           → None

  ready= takes a CSS selector. Tab waits for the element to appear before
  returning. On session loss (HMR/navigation), re-attaches and waits again.
  navigate() and reload() also wait for the ready selector before returning.
  Convention: add data-testid to your root layout component.

Selectors (click/tap/type_text):
  .css-class, #id, [attr]               CSS (no focus steal — pure CDP path)
  [data-testid='name']                   CSS (no focus steal — recommended for own code)
  text=Submit                            visible text match (JS eval)
  role=button[name="Save"]              ARIA role + name (JS eval)
  label=Username                        input by label (JS eval)
  button:has-text('OK')                 CSS + text filter (JS eval)

  CSS selectors use DOM.querySelector + DOM.getBoxModel (no JS eval, no focus steal).
  Custom selectors (text=, role=, label=, :has-text) use Runtime.evaluate.

Touch vs mouse:
  tab.click()  — Input.dispatchMouseEvent (works everywhere)
  tab.tap()    — Input.dispatchTouchEvent (fires touchstart/touchend)
  tab.swipe()  — touch sequence for scrolling

  Touch events may hang on complex apps (React, Messenger) where JS handlers
  block. tap/swipe have a 3s timeout and raise TimeoutError cleanly.

Target IDs: "{port}:{6-hex}" (e.g. 9222:887d3d). Stable across navigation.
Browser(port=N) creates a standalone instance for non-default ports (e.g. ADB-forwarded).
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

Tool registration:
  Set __repld_tools__ = [...] in module for MCP tool schemas.
  Name handlers _tool_{name}(args: dict) → str | dict.
  Tools appear in tools/list automatically; no exec round-trip needed.
  Scaffold: repld gist <name>

  Example:
    __repld_tools__ = [
        {"name": "foo_query", "description": "...",
         "inputSchema": {"type": "object", "properties": {...}, "required": [...]}},
    ]
    async def _tool_foo_query(args: dict) -> str:
        return json.dumps({"result": ...})

Dependencies:
  __repld_deps__ = ["httpx>=0.27", "beautifulsoup4"]
  Kernel scans at boot, prompts to install missing packages into the venv.
  Lost on `uv tool upgrade`; next boot re-scans (gist file is source of truth).

Writing gists:
  Prefer async — use httpx.AsyncClient, async def methods, await tab.fetch().
  Async gists yield to the event loop between calls: browser stays responsive,
  multiple tasks can interleave, no "loop blocked" warnings.
  Sync gists work (auto-threaded) but can't interleave with async work.
  Set __repld_usage__ = "sd = await SD.connect()" for a custom listing line.
""",
    "gates": """\
await ask(prompt, *, default=None, timeout=None)              → str
await confirm(prompt, *, default=None, timeout=None)          → bool
await choose(prompt, options, *, default=None, timeout=None)  → str

Blocks cell on human input in kernel pane.
TimeoutError if no default and timeout expires.
Emits awaiting_human channel while blocked.

notify(content, **meta)
  One-shot channel push to all MCP sessions.
""",
}


GUIDE = """\
repld — working guide

repld is a persistent Python kernel exposed over MCP. One asyncio loop, one
__main__ namespace shared between the human (terminal) and the agent (MCP).
The kernel stays alive across cells — state, background tasks, and browser
sessions persist. Everything you assign to a variable stays alive for the
next cell, the next turn, the next hour.

== How to think about exec ==

exec is the primary tool. It runs Python in __main__ and returns the result.
For anything beyond a single action, use exec with Python control flow
instead of chaining individual MCP tool calls — one exec cell can do what
would otherwise take many separate tool calls, and you get variables,
conditionals, loops, and error handling for free.

  # One cell — connect, fetch, filter, report:
  tab = await browser.get("*app.example.com*")
  users = (await tab.fetch("/api/users"))["body"]
  active = [u for u in users if u["status"] == "active"]
  f"{len(active)} active users out of {len(users)}"

State persists across cells. Build up context over a conversation:

  # Cell 1: connect and explore
  tab = await browser.get("*salesforce*")
  reqs = tab.network(url="*/api/*")

  # Cell 2: use what you found (tab and reqs are still alive)
  accounts = (await tab.fetch(reqs[0].url))["body"]

  # Cell 3: process the data
  big = [a for a in accounts if a["revenue"] > 1_000_000]

The kernel is a workspace, not a calculator. Treat it like a persistent
REPL session — import libraries, build up objects, iterate.

=== Timing and deferred tasks ===

If code finishes within timeout (default 2s), result is returned inline.
Otherwise exec returns {task_id, done:false} and pushes a channel
notification when done. Output spills to a file; the inline response
shows a head+tail preview with a path to the full output. Use Read/Grep
on that path for the full result.

For intentionally long work, use defer():

  defer(download_all_invoices(), label="invoice sync")

This returns the task_id immediately. The channel notification arrives
when the coroutine completes (or fails).

=== Top-level await ===

Top-level await is supported. No need to wrap in async def:

  data = await tab.fetch("/api/data")
  import asyncio
  result = await asyncio.gather(fetch_a(), fetch_b())

_ / _N history works — _ is the last expression, _1, _2, etc. for earlier.

== Project context ==

When repld runs inside a project (via uv run repld or an activated venv),
exec has access to everything in the project environment — your app
modules, ORM models, config, database sessions, API clients.

Note: this only works when repld is installed in the project's environment
(uv add --dev repld-tool). A globally-installed repld (uv tool install)
cannot see project dependencies.

  # FastAPI project — query the DB directly
  from myapp.db import async_session_maker
  from myapp.models import User
  from sqlalchemy import select
  async with async_session_maker() as s:
      users = (await s.execute(select(User).where(User.active == True))).scalars().all()

  # Django project — set up Django first, then query
  import django; django.setup()
  from myapp.models import Invoice
  from datetime import date
  overdue = list(Invoice.objects.filter(due_date__lt=date.today(), paid=False))

  # Direct SQL — stdlib, always available
  import sqlite3
  conn = sqlite3.connect("data/app.db")
  conn.execute("SELECT count(*) FROM events").fetchone()

No API layer, no HTTP, no serialization — you're in the process. Faster
than any external tool for ad-hoc queries, data inspection, and debugging.

== Live introspection with --init ==

repld --init repl.py runs a Python file at kernel startup, then keeps the
kernel alive. If repl.py starts a server, worker, or any long-running
process, that process lives inside __main__ — and exec can reach into it
at any time without restarting.

This is a dev-time decision, not a production architecture. Your service
doesn't depend on repld — it just runs inside it during development so
you can inspect it live.

  # repl.py — boot your service inside the kernel
  from myapp.server import create_app
  import asyncio

  app = create_app()
  runner = asyncio.create_task(app.start())
  print(f"server running, app and runner in __main__")

Now from exec (agent or human):

  # Inspect live server state — no restart, no debugger
  app.active_connections
  app.config["feature_flags"]
  list(app.sessions.keys())

  # Debug a specific session
  s = app.sessions["abc123"]
  s.state, s.last_activity, s.pending_messages

  # Poke at internals — test a handler directly
  result = await app.handle_request({"type": "test", "data": "hello"})

  # Patch something at runtime
  app.config["rate_limit"] = 100

This pattern works for any long-running Python process: HTTP servers
(FastAPI, aiohttp, Flask), queue workers, WebSocket servers, CLI daemons.
The service doesn't know it's inside repld — it just sees a normal asyncio
loop and a normal __main__ namespace. repld adds the ability to exec into
it mid-flight.

The human can also introspect from a terminal:

  repld exec 'list(app.sessions.keys())'    # one-shot query
  repld exec                                 # interactive REPL

Both the agent and the human see the same live objects.

== Builtins ==

Injected into __main__:

  notify(content, **meta)      push a channel notification to the agent
  defer(coro, label=)          fire-and-forget; channel push on completion
  every(seconds)(fn)           periodic ticker; fn.cancel() stops it
  ask(prompt) / confirm(prompt) / choose(prompt, options)
                               block on human input in the kernel terminal

== Browser ==

browser is lazy-injected into __main__. Connects to Chrome on first use
(requires --remote-debugging-port=9222).

=== Getting tabs ===

  tab = await browser.get("*example.com*")   # find by URL glob
  tab = await browser.get("9222:a1b2c3")     # find by target ID
  await browser.watch("*example.com*")       # watch pattern, auto-attach
  tab = await browser.open("https://...")     # open new tab

  # Ready signal — wait for element before returning
  tab = await browser.get("*localhost*", ready="[data-testid='app-root']")

browser.get() raises RuntimeError if no matching tab is found.
browser.open() opens a new tab and navigates to the URL.
tab.navigate(url) navigates an existing tab (use for same-site navigation;
use browser.open() when you need a fresh tab).

ready= takes a CSS selector. The tab waits for that element to appear before
returning. On session loss (HMR, navigation), re-attaches to the same target
and waits for the ready signal again. navigate() and reload() also wait.
Convention: add data-testid to your root layout component.

=== Tab API (async) ===

  tab.js(code)                        evaluate JS in page context
  tab.fetch(url, method=, body=, headers=)
                                      in-page fetch (inherits session/cookies)
  tab.tree()                          accessibility tree as text lines
  tab.click(selector)                 click element (mouse, auto-waits 2s)
  tab.tap(selector_or_x, y=)          touch tap (fires touchstart/touchend)
  tab.swipe(x1, y1, x2, y2)          touch scroll
  tab.type_text(selector, text)       clear + type (auto-waits 2s)
  tab.wait_for(selector, timeout=5)   wait for element to appear
  tab.wait_for_idle(timeout=5, quiet=0.5)
                                      wait for network idle; returns settle ms
  tab.pin(reason)                     inject status pill + beforeunload guard
  tab.confirm(prompt) → bool          gate routed to pill UI
  tab.choose(prompt, options) → str   gate routed to pill UI

CSS selectors (#id, .class, [data-testid]) use pure CDP calls (no JS eval,
no focus steal). Custom selectors (text=, role=, label=) use Runtime.evaluate.
For own code, prefer [data-testid='name'] to keep keyboard/focus intact.

=== tab.fetch() return shape ===

tab.fetch() returns a dict:
  {"status": 200, "headers": {...}, "body": <parsed JSON or text>}

body is auto-parsed as JSON if the content-type is application/json,
otherwise returned as a string. Access the data with ["body"].

If the request fails (network error, timeout), tab.fetch() raises
RuntimeError with the error message.

=== Tab API (sync, DuckDB-backed) ===

  tab.network(url=, method=, status=) query captured requests (list of rows)
  tab.request(request_id)             full request details
  tab.body(request_id)                response body (str or bytes)
  tab.clear()                         reset captured network/console data

Each row from tab.network() has: .id, .url, .method, .status,
.request_headers, .response_headers, .timestamp, .duration_ms.

=== Selectors ===

CSS, text=Label, role=button[name='OK'], label=Name,
button:has-text('OK'). Same syntax across click, type_text, wait_for.

=== When to use exec vs browser MCP tools ===

Use exec with the browser object when you need to:
  - Chain multiple operations (fetch → filter → fetch again)
  - Use Python logic (conditionals, loops, error handling)
  - Build up state across steps
  - Do anything with the results beyond displaying them

Use the browser MCP tools (browser_click, browser_network, etc.) for:
  - Quick single inspections ("what's on this page?")
  - One-off actions where you don't need the result in Python

=== API discovery workflow ===

When working with a new web app:

  # 1. Attach and watch traffic
  await browser.watch("*app.example.com*")
  # → user clicks around in the app to generate traffic

  # 2. See what API calls the app makes
  tab = await browser.get("*app.example.com*")
  tab.network(url="*/api/*")

  # 3. Inspect a specific request
  r = tab.network(url="*/api/users*")[0]
  r.url, r.method, r.status
  r.request_headers    # see auth headers
  tab.body(r.id)       # see response body

  # 4. Replay the call via tab.fetch() — inherits the browser session
  users = (await tab.fetch("/api/users"))["body"]

  # 5. Clear old traffic before exploring more
  tab.clear()

=== Building clients from captured traffic ===

For APIs that use bearer tokens or API keys (auth not tied to cookies):

  # Extract auth from captured traffic
  r = tab.network(url="*/api/*")[0]
  token = r.request_headers["Authorization"]

  # Build a standalone client — works outside the browser
  import urllib.request, json
  req = urllib.request.Request("https://api.example.com/data",
      headers={"Authorization": token})
  data = json.loads(urllib.request.urlopen(req).read())

For APIs that rely on cookies or session state — use tab.fetch(). The
browser maintains the session; you just call through it.

== Gists ==

Gists are Python modules in ~/.repld/gists/ (global) or ./gists/ (per-project).
Both directories are on sys.path at kernel startup. Edit a file, re-import it,
and the fresh version loads — auto-reload via mtime tracking.

Gists wrap anything into a callable API — web apps via the browser, databases,
graph stores, embedding indexes, internal services.

Module docstring first line → auto-shown in MCP instructions.
__repld_usage__ = "app = await App.connect()" → custom listing line.
__repld_deps__ = ["httpx>=0.27"] → kernel prompts to install at boot.
Type hints + one-line docstrings on public methods → auto-introspected.
Document return shapes in docstrings with -> {key, key, ...} so the agent
knows the dict structure without trial and error:
  async def search(self, query: str) -> list[dict]:
      \"""Search things. -> [{id, name, status, created_at, ...}]\"""

=== Writing a browser-connected gist ===

Template:

  \"""AppName — what it does.\"""

  __repld_deps__ = ["httpx>=0.27"]  # optional: auto-installed at boot
  __repld_usage__ = "app = await AppName.connect()"


  class AppName:
      \"""AppName — feature X, feature Y.\"""

      def __init__(self, tab) -> None:
          self._tab = tab

      @classmethod
      async def connect(cls) -> "AppName":
          \"""Find or open the app and return a ready instance.\"""
          import repld

          try:
              tab = await repld.browser.get("*app.example.com*")
          except RuntimeError:
              tab = await repld.browser.open("https://app.example.com")
              await tab.wait_for("role=main", timeout=10)
          await tab.pin("AppName — repld integration")
          return cls(tab)

      async def list_things(self) -> list[dict]:
          \"""List all things. -> [{id, name, status, created_at}]\"""
          return (await self._tab.fetch("/api/things"))["body"]

      async def create_thing(self, name: str) -> dict:
          \"""Create a thing (gated).\"""
          ok = await self._tab.confirm(f"Create \\"{name}\\"?")
          if not ok:
              raise RuntimeError("Cancelled")
          return (await self._tab.fetch(
              "/api/things", method="POST", body={"name": name}
          ))["body"]

=== Conventions ===

Import kernel builtins via `import repld` at module top level. Access as
repld.browser, repld.notify, repld.defer, repld.every. Module-level import
is auto-reload safe (attribute lookup on each call, not a frozen reference).

Async by default. All methods async def, use await tab.fetch(). Async gists
yield to the event loop — browser stays responsive, multiple gists can
interleave, no "loop blocked" warnings.

connect() classmethod. Finds or opens the app, returns a ready instance.
Pattern: try browser.get() → except RuntimeError → browser.open() + wait_for().

tab.pin(reason) in connect(). Injects a floating pill UI + beforeunload
guard. Prevents accidental tab close. The pill also serves as a gate
surface for confirm/choose prompts.

Gate write operations. Anything that mutates state should call
tab.confirm(prompt) or tab.choose(prompt, options) first. The gate appears
in both the terminal and the pill UI — first resolution wins.

For apps that don't need browser auth (public APIs), use httpx (declare it
in __repld_deps__) or stdlib urllib. No browser tab needed.

=== Multi-tab gists (embedded apps) ===

When the app lives in an iframe (e.g., Shopify embedded apps), hold both tabs:
  - admin tab for navigation (host page)
  - iframe tab for fetch/js (app context with auth)

After navigating the admin tab, re-acquire the iframe with
browser.get(pattern, timeout=10) — iframes reload on host navigation.
Never navigate an iframe directly — it destroys the embedded session.

=== Tool registration ===

Stable gists can register MCP tools callable without exec:

  __repld_tools__ = [
      {"name": "myapp_query", "description": "...",
       "inputSchema": {"type": "object", "properties": {...}, "required": [...]}},
  ]

  async def _tool_myapp_query(args: dict) -> str:
      import json
      return json.dumps({"result": ...})

Handler convention: _tool_{name}(args) → str | dict.
Tools appear in tools/list automatically. Scaffold: repld gist <name>.

== Background automation ==

The kernel persists. One-shot work can become continuous:

  @every(30)
  async def check():
      data = await app.poll()
      if data["changed"]:
          notify(f"Change detected: {data}")

  # Or fire-and-forget:
  defer(some_long_coroutine(), label="nightly sync")

  # List active tickers:
  every.list()

  # Stop a ticker:
  check.cancel()

Combine with project context for dev workflows:

  # Monitor your app's error rate (project-local repld)
  from datetime import datetime, timedelta
  from sqlalchemy import select, func
  from myapp.db import async_session_maker
  from myapp.models import ErrorLog

  @every(60)
  async def error_monitor():
      cutoff = datetime.utcnow() - timedelta(minutes=5)
      async with async_session_maker() as s:
          count = (await s.execute(
              select(func.count()).where(ErrorLog.created > cutoff)
          )).scalar()
          if count > 10:
              notify(f"{count} errors in last 5 min", kind="alert")

  # Watch a web app for price changes
  price_history = {}

  @every(300)
  async def price_watch():
      tab = await browser.get("*competitor.com*")
      products = (await tab.fetch("/api/products"))["body"]
      for p in products:
          prev = price_history.get(p["id"])
          if prev is not None and p["price"] != prev:
              notify(f"{p['name']}: {prev} → {p['price']}", kind="price_change")
          price_history[p["id"]] = p["price"]
"""


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
