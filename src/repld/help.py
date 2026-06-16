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
  BROWSER_GUIDE            → MCP resource (repld://docs/browser) — comprehensive
                            browser API reference, internals, and workflows
"""

import json
import sys
from pathlib import Path

from .ipc import read_lock

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
    "Read repld://docs/browser for the full API, internals, and workflow patterns."
)

_GISTS_MODEL = (
    "Gists: ~/.repld/gists/ and ./gists/ on sys.path. Auto-reload on re-import.\n"
    "Before using a gist, read repld://gists/{name} for the full API — constructor args, "
    "method signatures, and usage patterns.\n"
    "Stable gists can register as MCP tools via __repld_tools__ — callable "
    "directly without exec, discoverable in tools/list.\n"
    'Gists declare deps via __repld_deps__ = ["httpx>=0.27"]; '
    'use "." to depend on the gist\'s own project. '
    "Kernel prompts to install missing ones at boot.\n"
    "Read repld://gists/_registry to see gists written in other projects; the "
    "user can link one in with `repld gist add <name>` (no copy)."
)

_REFERENCE = "Reference: `repld help <topic>` — topics: exec, browser, gists, gates\nRead repld://docs/guide for exec patterns and gist conventions. Read repld://docs/browser for the full browser API and internals."


# ---------------------------------------------------------------------------
# BROWSER_GUIDE (repld://docs/browser resource — comprehensive browser reference)
# ---------------------------------------------------------------------------

BROWSER_GUIDE = """\
repld browser — comprehensive guide

API reference, non-obvious behaviors, and workflow patterns for the browser
object.  Read this instead of diving into source code.

== Getting tabs ==

  tab = await browser.get("*example.com*")          # URL glob
  tab = await browser.get("9222:a1b2c3")            # target ID (any type)
  tab = await browser.get("*app*", fresh=True)       # only newly-appearing tabs
  tab = await browser.get("*app*", timeout=10)       # wait up to 10s for match
  tab = await browser.open("https://example.com")    # open new tab
  await browser.watch("*example.com*")               # auto-attach current + future

  browser.tabs                                       # list[Tab] currently attached
  browser.pages()                                    # all Chrome targets (dict list)
  browser.patterns()                                 # active watch patterns
  browser.detach("*example.com*")                    # detach pattern + tabs
  browser.detach()                                   # detach everything
  browser.clear(target=)                             # clear all captured data

  b = Browser.from_profile("/tmp/my-chrome")          # connect by user-data-dir
  browser.disconnect()                               # close WebSocket

Quirks:
  - get(glob) skips workers (service_worker, shared_worker, worker). To reach
    a worker, use its target ID directly: get("9222:a1b2c3").
  - get() raises RuntimeError if no match found (with timeout=None, checks once).
  - fresh=True snapshots currently-matching targets at call time and excludes
    them — returns only tabs that appear *after* the call.
  - open() creates a tab via Target.createTarget, waits for attach, sleeps 0.3s
    for the page to settle before returning.
  - Browser.from_profile(path) reads DevToolsActivePort from a Chrome
    user-data-dir to discover the debug port.  Works with --remote-debugging-port=0
    (random port) — Chrome writes the actual port to that file on startup.

=== ready= parameter ===

  tab = await browser.get("*localhost*", ready="[data-testid='app-root']")

ready= stores a CSS selector or JS expression on the Tab.  It's used by:
  - get() / open() — waits after initial attach
  - navigate() / reload() — waits after page load
  - _reattach() — waits after session recovery (HMR, navigation)

CSS selectors (starts with '.', '#', '[', 'data-') use DOM.querySelector,
polled every 100ms, 10s timeout.  Everything else is evaluated as a JS
expression via Runtime.evaluate, polled every 100ms, must return truthy.

Default (no ready=): waits for document.readyState === 'complete'.

Convention: add data-testid to your root layout component.

== Tab API (async) ==

  tab.js(expr, *, await_promise=True, user_gesture=True)     → Any
      Evaluate JS in page context.  Results returned by value (deep-serialized).
      await_promise=True (default) awaits Promise results like the DevTools console.
      user_gesture=True makes isTrusted=true on events.
      Raises BrowserJSError on JS exceptions (with preserved stack trace).

  tab.click(selector, *, button='left', click_count=1)       → None
      Mouse click via Input.dispatchMouseEvent (mousePressed + mouseReleased).
      Produces isTrusted=true events.  Auto-waits up to 2s for the element.

  tab.type_text(selector, text, *, delay_ms=0, press_enter=False)  → None
      Focus element, select-all existing content, type character-by-character
      via Input.dispatchKeyEvent.  Auto-waits up to 2s.
      delay_ms adds a pause between keystrokes (in milliseconds).
      press_enter sends an Enter key after the text.

  tab.tap(selector_or_x, y=None)                             → None
      Touch tap via Input.dispatchTouchEvent (touchstart/touchend).
      Accepts a selector string OR (x, y) coordinates.
      3s timeout — raises TimeoutError if the page's touch handler blocks
      (common on complex apps like Messenger/React).

  tab.swipe(x1, y1, x2, y2, *, steps=10, duration_ms=300)   → None
      Touch swipe: touchStart → touchMove × steps → touchEnd.
      For scrolling on mobile Chrome via ADB.

  tab.tree()                                                  → list[str]
      Compact accessibility tree as text lines.  Crosses iframes — discovers
      attached iframe children by matching parentFrameId, inlines their trees.
      Standalone read (no settle, no observation pipeline).

  tab.fetch(url, *, method='GET', body=None, headers=None)    → dict
      In-page JS fetch() — inherits the browser's cookies, session, and CORS
      origin.  NOT a separate HTTP call.
      Returns: {"status": int, "ok": bool, "body": Any}
      body is auto-parsed as JSON when content-type includes 'json'.
      Auto-sets Content-Type: application/json when body is a dict.
      Caller headers override auto-set headers.
      Raises RuntimeError (via BrowserJSError) on network errors.

  tab.navigate(url)                                           → None
      Navigate to URL.  Waits for ready signal after page load.

  tab.reload()                                                → None
      Reload page.  Waits for ready signal after load.

  tab.wait_for(selector, *, timeout=5.0)                      → None
      Wait for element to appear.  Polls every 100ms.
      Same selector syntax as click/type_text.

  tab.wait_for_idle(*, timeout=5.0, quiet=0.5)                → int
      Wait for network idle.  Returns settle time in ms.
      See "Settle loop" below for what "idle" means.

  tab.screenshot(*, full_page=False, path=None)               → bytes | Path
      Capture screenshot as PNG bytes.  full_page captures the full scrollable
      page.  If path is given, writes to file and returns the Path.

  tab.cookies()                                               → list[dict]
      All cookies for this tab via Network.getCookies.

  tab.cdp(method, **params)                                   → dict
      Raw CDP passthrough — escape hatch for anything not wrapped.

=== Pin + gate bridge ===

  tab.pin(reason='')                → None
      Inject floating pill UI + beforeunload guard.  Idempotent.
      Pill shows green dot when connected, amber when awaiting input.
      Prevents accidental tab close.

  tab.unpin()                       → None
      Remove pill + guard + heartbeat.

  tab.confirm(prompt, **kw)         → bool
      Gate routed to pill UI.  Also appears in terminal — first wins.

  tab.choose(prompt, options, **kw) → str
      Gate routed to pill UI.

  tab.ask(prompt, **kw)             → str
      Terminal only (no pill UI for text input).

Gates queue — only one rendered at a time in the pill.  Pending count shown.
Terminal and browser resolve the same Future; first resolution wins.

Heartbeat: kernel beats every 5s by setting window.__repld_hb = Date.now().
The pill checks every 5s and self-destructs if stale for > 15s.
Same-origin reload: pill auto-reinjects (heartbeat detects __repld_pill missing
but origin matches).
Cross-origin navigation: pin broken, pushes pin_lost channel, heartbeat exits.
3 consecutive heartbeat exceptions also exit the loop.

== Tab API (sync, DuckDB-backed) ==

  tab.network(url=, method=, status=, type=, since=, include_assets=False)
      → Rows (list[Row])
      Query captured requests from the HAR summary view.
      url uses LIKE matching — "*" becomes "%".
      Assets excluded by default (is_asset=false); pass include_assets=True
      to see them.  Returns max 500 rows, ordered newest-first.

  tab.console(level=, source=, since=)  → Rows
      Query console messages.  Returns max 200 rows.

  tab.sse(url=, event_name=, since=)    → Rows
      Query SSE (EventSource) messages.  Each row has: request_id,
      event_name, event_id, data, timestamp.  Chrome parses the stream
      and fires Network.eventSourceMessageReceived per message — no
      manual parsing needed.  Returns max 500 rows, oldest-first.
      NOTE: only captures EventSource API connections, not fetch()-based
      SSE streams (common in modern apps for POST/custom-header SSE).

  tab.request(request_id)               → dict
      Full HAR entry as a dict: request/response headers, postData, auth
      scheme, timing, initiator — everything except the response body.

  tab.body(request_id)                  → dict
      Response body for a request.  Checks DuckDB first (captured bodies),
      falls back to Network.getResponseBody CDP call.
      Returns: {"body": str, "base64Encoded": bool}
      If unavailable: {"error": "..."}

  row.body()                            → dict
      Shortcut — calls tab.body(self.request_id) on the row's session.

  tab.lifecycle(name=, since=)           → Rows
      Query Page.lifecycleEvent events.  Each row has: frame_id, loader_id,
      name, timestamp.  Requires Page.setLifecycleEventsEnabled (auto-enabled
      on attach).  Chrome replays already-fired events on late attach.
      Event names: init, DOMContentLoaded, load, firstPaint,
      firstContentfulPaint, firstImagePaint, firstMeaningfulPaintCandidate,
      firstMeaningfulPaint, networkAlmostIdle, networkIdle, InteractiveTime,
      commit (catch-up only).

  tab.clear()                           → None
      Clear all captured events for this tab.

=== Row fields ===

Network rows: id, request_id, redirect_index, protocol, method, status, url,
  type, size, time_ms, state, pause_stage, paused_id, frames_sent,
  frames_received, started_datetime, last_activity, target, body_status,
  mime_family, is_asset, initiator_type, initiator_url

Console rows: id, level, source, text, stack_url, stack_line, stack_function,
  timestamp, target

SSE rows: id, request_id, event_name, event_id, data, timestamp, target

Lifecycle rows: id, frame_id, loader_id, name, timestamp, target

Rows is a list subclass with one-entry-per-line repr for grep-friendly output.

=== Full HAR entry fields (via tab.request()) ===

All of the above plus: request_headers, post_data, response_headers, mime_type,
  timing, error_text, request_cookies, status_text, auth_scheme, auth_cookies,
  csrf_token_header, curl_command, loader_id, frame_id, initiator_function,
  initiator_line

== Tab properties ==

  tab.url            str   current URL (from target_info, see staleness note)
  tab.title          str   page title (from target_info)
  tab.type           str   "page", "iframe", "service_worker", etc.
  tab.target_id      str   short ID in "{port}:{6-hex}" format, stable across nav
  tab.parent_frame_id str  parent frame for iframes
  tab.capture_bodies bool  toggle Fetch-domain body capture (default True)

Staleness: tab.url and tab.title are read from a cached target_info dict,
updated only on Target.targetInfoChanged events.  They can be briefly stale
after navigation — if you need the live URL, use tab.js("location.href").

== Selectors ==

Same syntax across click, tap, type_text, wait_for:

  .css-class, #id, [attr], tag                        CSS (pure CDP, no focus steal)
  [data-testid='name']                                CSS (recommended for own code)
  text=Submit                                         visible text match (JS eval)
  role=button[name="Save"]                            ARIA role + name (JS eval)
  label=Username                                      input by label (JS eval)
  button:has-text('OK')                               CSS + text filter (JS eval)

CSS vs JS path:
  Plain CSS selectors use DOM.querySelector + DOM.getContentQuads for coordinate
  resolution — pure CDP, no JavaScript eval, no focus steal.  This means typing
  into a field found by CSS won't dismiss a dropdown or blur another element.

  Custom selectors (text=, role=, label=, :has-text) use Runtime.evaluate to
  find the element and getBoundingClientRect() for coordinates.  This runs JS
  in the page, which *can* trigger focus changes.

  For your own code, prefer [data-testid='name'] to keep keyboard/focus intact.

role= expansions:
  role=button  → button, [role="button"], input[type="button"], input[type="submit"]
  role=link    → a[href], [role="link"]
  role=textbox → input:not([type]), input[type="text"], ..., textarea, [role="textbox"]
  (and checkbox, radio, heading, listitem, tab, tabpanel, option, combobox)

role= name operators:
  role=button[name="Save"]     exact match (textContent, aria-label, title, value, labels)
  role=button[name*="Save"]    contains
  role=button[name^="Save"]    starts with

text= matching: finds visible elements (offsetWidth > 0) where textContent or
  aria-label matches exactly.  Returns shortest match (avoids matching a parent
  container that also contains the text).

label= resolution: finds <label> by text, then resolves to the input via
  htmlFor attribute or querySelector within the label element.

Auto-wait: all selectors auto-wait up to 2s (click/type_text) or the specified
  timeout (wait_for), polling every 100ms.

== Internals ==

=== Network body capture ===

Fetch domain interception captures request and response bodies proactively.
Enabled by default (tab.capture_bodies = True).

Request stage: ALL requests are intercepted.  POST/PUT/PATCH bodies are captured
  via Fetch.getRequestPostData and stored as synthetic Network.requestBodyCaptured
  events in DuckDB.  This gets the full un-truncated body (Network.requestWillBeSent
  .postData caps at ~64KB).

Response stage: ALL responses are intercepted.  Bodies are captured when:
    - Status is not a redirect (301-308 — Chrome puts redirects in
      kRedirectReceived state; Fetch.getResponseBody errors on them)
    - Content-type is not text/event-stream (SSE is an infinite stream —
      Fetch.getResponseBody would block forever)
    - Content-length is under 500KB (_MAX_BODY_SIZE = 500,000 bytes)

  Captured bodies are replayed to the page via Fetch.fulfillRequest (because
  Fetch.getResponseBody consumes the internal buffer).

  Non-captured responses (assets, redirects, SSE) use fire-and-forget continue
  commands (no roundtrip wait).  Body captures still await the CDP response.

  tab.capture_bodies = False disables Fetch interception entirely — all requests
  pass through without pausing.

=== Settle loop ===

wait_for_idle() and the MCP observation pipeline use the same settle logic:

  Polls DuckDB every 50ms across all tabs (including iframe children):
    SELECT COUNT(*) FROM har_entries
    WHERE state NOT IN ('complete', 'failed', 'redirect', 'closed')
    AND method != 'WS'

  WebSocket connections are excluded — they stay 'open' indefinitely and would
  block settle forever.

  Returns when the inflight count is 0 for a continuous quiet period
  (default 0.5s).  Timeout default is 5s.

  Returns settle time in milliseconds.

=== MCP tools vs exec — settle behavior ===

MCP browser tools (browser_click, browser_type, browser_navigate, etc.) run
  the full observation pipeline: pre_observe → mutate → settle → post_observe.
  They automatically wait for network idle and return tree + network delta +
  console delta.

exec-based mutations (calling tab.click(), tab.type_text() etc. in Python code)
  do NOT auto-settle.  The method returns as soon as the CDP command completes.
  If you need to wait for the page to settle after a mutation:
    await tab.click("button.submit")
    await tab.wait_for_idle()          # explicit settle

=== Session recovery ===

When Chrome invalidates a CDP session (HMR reload, same-origin navigation that
destroys the render process), tab methods detect the error and recover:

  Detection: error message contains "session with given id not found" or
  "no session with given id" (case-insensitive).  Any other RuntimeError
  propagates immediately.

  Recovery (_reattach):
    1. Detach old CDPSession from BrowserSession
    2. Re-attach to the same Chrome target ID (target ID is stable, only the
       session ID changes)
    3. Wait for ready signal (CSS or JS, 10s timeout)
    4. Sleep 0.3s for stability
    5. Retry the original CDP command once

  If the retry also fails, the error propagates.

=== WebSocket reconnect ===

On WebSocket connection loss (ConnectionClosed, OSError), BrowserSession
reconnects automatically on the next CDP command:
  - Opens a new WebSocket to the same Chrome debug port
  - Re-attaches all previously-tracked targets
  - Watch patterns survive reconnect
  - CDPSession objects and their DuckDB event stores are preserved —
    only Chrome session IDs change (remapped internally)
  - Serialized by an asyncio Lock to prevent concurrent reconnect races

=== DuckDB event store ===

Each attached tab has its own in-memory DuckDB connection.  All CDP events are
inserted synchronously on the asyncio loop (DuckDB inserts are microseconds).

  Event table: (event JSON, method VARCHAR, request_id VARCHAR, target VARCHAR)

  HAR views (har_entries, har_summary), console_entries, sse_entries, and
  lifecycle_entries are SQL views created on CDPSession init.

  FIFO prune: every 1000 event inserts, checks if count > 50,000.  If so,
  deletes the oldest batch (at least 5000 events).

  Events survive reconnect (DuckDB is on the CDPSession object, which is
  preserved).  Events do NOT survive tab close + re-attach — new attachment
  creates a new CDPSession with a fresh DB.

=== Attachment race guard ===

BrowserSession.attach() tracks in-flight attaches via an _attaching set.  If
attach() is called concurrently for the same target_id, the second call
returns None immediately.

== Workflow patterns ==

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
  tab.request(r.request_id)     # full headers, auth scheme, timing
  r.body()                      # response body (shortcut)

  # 4. Replay the call via tab.fetch() — inherits the browser session
  users = (await tab.fetch("/api/users"))["body"]

  # 5. Clear old traffic before exploring more
  tab.clear()

=== Building clients from captured traffic ===

For APIs that use bearer tokens or API keys (auth not tied to cookies):

  r = tab.network(url="*/api/*")[0]
  token = tab.request(r.request_id)["request_headers"]["Authorization"]

  import urllib.request, json
  req = urllib.request.Request("https://api.example.com/data",
      headers={"Authorization": token})
  data = json.loads(urllib.request.urlopen(req).read())

For APIs that rely on cookies or session state — use tab.fetch(). The
browser maintains the session; you just call through it.

=== Multi-tab gists (embedded apps) ===

When the app lives in an iframe (e.g., Shopify embedded apps), hold both tabs:
  - admin tab for navigation (host page)
  - iframe tab for fetch/js (app context with auth)

After navigating the admin tab, re-acquire the iframe with
browser.get(pattern, timeout=10) — iframes reload on host navigation.
Never navigate an iframe directly — it destroys the embedded session.
"""


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
  repld gist new NAME      Scaffold a tool gist in ./gists/NAME.py
  repld gist add NAME      Link a gist registered in another project
  repld gist rm NAME       Unlink a gist (--stale drops all dead links)
  repld gist list          Show local + linked gists
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
  tab.sse(url=, event_name=, since=)                                   → Rows
  tab.lifecycle(name=, since=)                                         → Rows
  tab.clear()                                                          → None

  row.body()                             → dict (response body for a Row)

Tab properties:
  tab.url / tab.title / tab.type         str   target info (type: page/iframe/worker)
  tab.target_id / tab.parent_frame_id    str   short ID; parent frame for iframes
  tab.capture_bodies = False             bool  toggle Fetch-domain body capture

Browser:
  Browser.from_profile(path)                     → Browser  (read port from DevToolsActivePort)
  browser.get(target, timeout=, fresh=, ready=)  → Tab  (glob or target ID; skips workers for globs)
  browser.watch(pattern)                         → str  (watch all matching, auto-attach new)
  browser.open(url)                              → Tab
  browser.tabs                                   → list[Tab]
  browser.pages()                                → list[dict]
  browser.detach(pattern=)                       → str
  browser.patterns()                             → list[str]  active watch patterns
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
  Scaffold: repld gist new <name>

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

Cross-project links:
  repld gist list             local + linked gists in this project
  repld gist add <name>       link a gist registered in another project
  repld gist rm <name>        unlink (--stale drops all dead links)
  Every import is recorded in a central registry; `add` resolves a name to its
  path, follows same-dir sibling imports, and records absolute paths in a
  committed ./gists/.links manifest — no copy. Read repld://gists/_registry to
  browse every gist seen across projects.

Writing gists:
  Prefer async — use httpx.AsyncClient, async def methods, await tab.fetch().
  Async gists yield to the event loop between calls: browser stays responsive,
  multiple tasks can interleave, no "loop blocked" warnings.
  Sync gists work (auto-threaded) but can't interleave with async work.
  Set __repld_usage__ = "sd = await SD.connect()" for a custom listing line.
""",
    "gates": """\
await ask(prompt, *, default=None, timeout=None)                       → str
await confirm(prompt, *, tab=None, default=None, timeout=None)         → bool
await choose(prompt, options, *, tab=None, default=None, timeout=None) → str

Blocks cell on human input in kernel pane.
Pass tab= to also surface the gate in that tab's pin pill (requires
tab.pin()); terminal and browser resolve the same gate — first wins.
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
                               block on human input in the kernel terminal;
                               confirm/choose accept tab= to also surface
                               the gate in that tab's pin pill

== Browser ==

browser is lazy-injected into __main__. Connects to Chrome on first use
(requires --remote-debugging-port=9222).

  tab = await browser.get("*example.com*")   # find by URL glob
  await browser.watch("*example.com*")       # watch pattern, auto-attach
  tab = await browser.open("https://...")     # open new tab

  tab.fetch(url, method=, body=, headers=)   # in-page fetch (inherits session)
  tab.network(url=, method=, status=)        # query captured requests (DuckDB)
  tab.tree()                                 # accessibility tree
  tab.click(selector)                        # click (auto-waits, mouse event)
  tab.type_text(selector, text)              # clear + type (auto-waits)
  tab.js(code)                               # evaluate JavaScript

Use exec with the browser object for multi-step operations (fetch, filter,
iterate). Use browser MCP tools for quick one-off inspections.

Read repld://docs/browser for the full API reference, internals (settle loop,
body capture patterns, selector dispatch, session recovery, DuckDB event
store), and workflow patterns (API discovery, building clients, multi-tab
gists).

== Gists ==

See `repld help gists` for the full API reference (paths, tool registration,
dependencies, cross-project links).

Gists wrap anything into a callable API — web apps via the browser, databases,
graph stores, embedding indexes, internal services.

Module docstring first line → auto-shown in MCP instructions.
__repld_usage__ = "app = await App.connect()" → custom listing line.
__repld_deps__ = ["httpx>=0.27"] → kernel prompts to install at boot.
  Use "." to depend on the gist's own project (editable install when linked elsewhere).
Type hints + one-line docstrings on public methods → auto-introspected.
Document return shapes in the docstring FIRST line with -> {key, key, ...}
(only the first line is surfaced) so the agent knows the dict structure
without trial and error:
  async def search(self, query: str) -> list[dict]:
      \"""Search things. -> [{id, name, status, created_at, ...}]\"""

Introspection is AST-based on the gist file alone — inherited methods and an
inherited __init__ are INVISIBLE in repld://gists/{name}. When subclassing a
library class, define an explicit __init__ and thin documented wrappers for
the methods agents should discover; list the rest in the class docstring.

=== Writing a browser-connected gist ===

Template:

  \"""AppName — what it does.\"""

  __repld_deps__ = ["httpx>=0.27"]  # PyPI packages, auto-installed at boot
  # __repld_deps__ = ["."]          # depend on the project itself (editable install)
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

Normalize responses. Parse provider payloads into flat dicts with stable
keys (_parse_* module helpers) instead of returning raw API JSON — terse
output, stable downstream code, and a shape that fits in a docstring.

Module-level state resets on reload. Globals (clients, caches) re-initialize
when the gist auto-reloads; stale connections are not closed. Keep such
state disposable — lazy-init clients, caches that can rebuild.

=== Multi-tab gists (embedded apps) ===

When the app lives in an iframe (e.g., Shopify embedded apps), hold both tabs:
  - admin tab for navigation (host page)
  - iframe tab for fetch/js (app context with auth)

After navigating the admin tab, re-acquire the iframe with
browser.get(pattern, timeout=10) — iframes reload on host navigation.
Never navigate an iframe directly — it destroys the embedded session.

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
        state["lock_alive"] = isinstance(read_lock(cwd / ".pyrepl.lock"), dict)
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
