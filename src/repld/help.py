"""Canonical user-facing docs for repld.

`build_instructions()` composes the MCP `initialize.instructions` dynamically
based on kernel state (browser connected? which gists available?). `OVERVIEW`
and `_TOPICS` back the `repld help` command / `browser.help()`. Four surfaces
at different depths, not duplicate content — Topics are terse signature
cheat-sheets, MCP resources are prose (rationale, quirks, internals). Where a
Topic and a resource cover the same API, the Topic stays signature-only and
points at the resource for behavior/internals (e.g. "browser" topic's pin +
gate bridge → BROWSER_GUIDE) rather than restating it. One sanctioned
exception: GUIDE's `== Builtins ==` section recaps the injected builtins so
the guide reads standalone; keep it in sync with `_EXEC_MODEL`. The surfaces:

  INSTRUCTIONS (dynamic)  → behavioral model for the agent (terse, always loaded)
  Tool descriptions       → per-tool what + gotchas (lives in protocol.py)
  Topics                  → pure API reference for the human user
  MCP resources           → on-demand docs, one constant each:
      GUIDE          (repld://docs/guide)      working guide — patterns + conventions
      BROWSER_GUIDE  (repld://docs/browser)    browser API reference + internals
      PLAYBOOK       (repld://docs/playbook)   workflow methodology:
                                               interactive → gist → trigger → production
      PRODUCTION     (repld://docs/production) graduation guide:
                                               gist → FastMCP/FastAPI with wiring examples
"""

from pathlib import Path

from . import cli_args
from .state import read_lock

# ---------------------------------------------------------------------------
# Composable instruction blocks (agent-facing, behavioral model only)
# ---------------------------------------------------------------------------

# Builtins recap also in GUIDE's `== Builtins ==` section — keep in sync.
_EXEC_MODEL = (
    "Execution model: "
    "exec runs code in shared __main__. If it exceeds timeout, returns "
    "{task_id, done:false} and pushes channel on completion. "
    "Output: head+tail preview; full at [full output: /path] — use Read/Grep. "
    "_ / _N history. Top-level await. "
    "no_display(value) returns value without re-printing it (still binds _/_N) — "
    "for functions that already print their own output. "
    "defer(coro, label) schedules a background task, returns task_id immediately, "
    "pushes channel on completion. "
    "every(seconds, delay=0)(fn) schedules fn to run periodically; the first "
    "tick is immediate unless delay= holds it back — use that when watching "
    "something you just started, or the health check races its warmup. "
    "fn.cancel() stops it. every.list() shows active tickers. "
    "ask()/confirm()/choose() block the cell on human input; the answer comes "
    "from the kernel's pane, a pinned tab's pill, or `repld gate answer <id>` "
    "when the kernel is headless (the usual case — the push tells you which). "
    "When you see a task that could run continuously — monitoring, polling, "
    "watching for changes — suggest wiring it with defer() + notify() or @every. "
    "The kernel persists; one-shot work can become background automation."
)

_BROWSER_MODEL = (
    "Browser model: "
    "Watch by URL pattern. Short target IDs (9222:a1b2c3). "
    "Multi-browser: browser.connect(port) adds Chrome instances; target IDs route by port prefix. "
    "Selectors resolve strictly (Playwright engine): >1 match with no single "
    "visible winner errors with a candidate list instead of guessing; input "
    "waits for visible/enabled/stable; click/type return a receipt line naming "
    "what was actually hit. type verifies the value landed, falling back to the "
    "native setter for framework-controlled inputs. browser_select drives "
    "dropdowns (native <select> or custom listbox; an aria-autocomplete field "
    "gets the option typed in as a filter, reaching virtualized lists). "
    "browser_hover parks the pointer on an element — selector or 'x,y' "
    "(hover-revealed UI stays up); browser_drag runs press→moves→release "
    "atomically in one call (selectors or 'x,y' endpoints, micro-stepped "
    "out of small origins). "
    "browser_tree default is an aria snapshot with [ref=eN] handles — reuse as "
    "aria-ref=eN selectors until the next snapshot or navigation; mode='ax' is "
    "the raw CDP tree; at='x,y' hit-tests a coordinate instead and returns a "
    "small elided tree rooted there, with real [ref=eN] handles usable the "
    "same way (an iframe hit reports the redirect instead). "
    "Mutations (click/type/select/hover/drag/navigate/key/open) settle then "
    "return receipt + a changes: AX-diff (what appeared/disappeared/changed "
    "state) + tree + network delta + console delta. "
    "Tree crosses iframes. Network separates API calls from assets. "
    "get()/open() capture request/response bodies; watch() attaches lightweight. "
    "Read workflow: network → request → body. "
    "browser object available in exec for chaining. "
    "For repeated browser interactions, write a gist (gists/*.py) to capture "
    "the API pattern. tab.pin() guards the session; tab.confirm()/choose() "
    "gate mutations in the browser. "
    "Controls: apps exposing window.controls get browser_controls (discover) and "
    "browser_invoke (act) MCP tools. Action observations push as channel messages. "
    "Console errors from watched tabs push as [console:error] channel messages automatically "
    "(cross-tab duplicates within 2s are collapsed; browser.suppress(substring) mutes matching errors). "
    "Read repld://docs/browser for the full API, internals, and workflow patterns."
)

_GISTS_MODEL = (
    "Gists: ~/.repld/gists/ and ./gists/ on sys.path. Auto-reload on re-import.\n"
    "Before using a gist, read repld://gists/{name} for the full API — constructor args, "
    "method signatures, and usage patterns.\n"
    "Stable gists can register as MCP tools via typed _tool_* functions — "
    "callable directly without exec, discoverable in tools/list. Schema is "
    "inferred from type hints and the docstring's first line.\n"
    'Gists declare deps via __repld_deps__ = ["httpx>=0.27"]; '
    'use "." to depend on the gist\'s own project; use "path:vendor/lib" to '
    "add a local (non-pip-installable) directory to sys.path, relative to "
    "the project root. Kernel prompts to install missing PyPI ones at boot.\n"
    "Read repld://gists/_registry to see gists written in other projects — an "
    "entry marked (already here) is already resolvable in this one, so skip "
    "it; otherwise the user can link one in with `repld gist add <name>` "
    "(no copy)."
)

_PLAYBOOK = (
    "Playbook: prototype interactive → extract gists from repetition → wire triggers "
    "when the pattern stabilizes → same gist runs headless in production. "
    "Read repld://docs/playbook for the full methodology."
)


# ---------------------------------------------------------------------------
# PLAYBOOK (repld://docs/playbook resource — workflow methodology)
# ---------------------------------------------------------------------------

PLAYBOOK = """\
The Playbook — how composable workflows get built

Discovered through practice, not designed upfront. Works for any automation —
fully automatic pipelines, human-in-the-loop processes, AI-augmented flows,
or plain data transforms.

== Principles ==

  One service, one job.
    Each function takes input, returns output. They compose through data,
    not through shared frameworks.

  UI is a view, not the system.
    Pipeline state lives in plain data: DB rows, JSON, spreadsheet cells.
    Any UI can read or write it. Swap the view without touching the pipeline.

  Prototype interactive, harden headless.
    Start with Claude Code + repld doing it manually. Each ad-hoc action
    becomes a gist. Each gist becomes a callable stage. Wire triggers when
    the pattern stabilizes. The gist is the portable unit — same code in
    repld (interactive) and FastAPI/Inngest (production).

  Human gates are just empty fields.
    A spreadsheet column waiting for input, a DB field set to null, a
    waitForEvent in a workflow engine — same concept. The pipeline pauses,
    a human fills in a value, the next stage picks it up.

  State is plain data.
    JSON in, JSON out. Spreadsheet columns, DB columns, and pipeline
    stage inputs are the same thing.

  Discover, don't design.
    Do the work manually first. The pipeline reveals itself through
    repetition. Extract stages only when you've done the same thing
    three times. Premature automation encodes the wrong workflow.

== Layers ==

  Layer          Interactive (prototype)         Production (headless)
  ─────          ──────────────────────          ────────────────────
  Capture        Web app, phone, manual entry    Same, or webhook/API trigger
  AI reasoning   Claude Code (human steers)      Claude API (prompt hardened)
  Glue           repld gist (ad-hoc, stateful)   FastAPI/Inngest (durable)
  State/UI       Spreadsheet, DB, file dump      Same — UI is just a view
  Integration    MCP tools (browser, APIs)       Same tools, called from code

== The portable unit: the gist ==

  A gist is a plain Python file with no framework dependency. Data in,
  data out. Where it runs doesn't matter:

    gists/                  # pure Python, no repld/fastapi dependency
      inventory.py          # ERP lookup, sheet write
      notify.py             # Slack / email dispatch

    repld                   # imports gists for interactive prototyping
    fastapi                 # imports gists for production endpoints
    inngest                 # calls gists as durable step functions

== Human gates ==

  Auto stages fill themselves; human stages wait. The UI can be anything:
  a spreadsheet column, a dashboard, a Slack message, or nothing at all.

    [Auto]   Finding    → system analyzes, writes result
    [Human]  Decision   → human reviews, writes action        # ← gate
    [Auto]   Execution  → system executes the action
    [Auto]   Status     → Pending → Done

== The progression ==

  Every workflow starts interactive and hardens incrementally. Not every
  workflow needs every phase. Each phase reuses the previous one's artifacts.

  1. Interactive
     You and Claude work a task in repld. Poke at the browser, hit an API,
     inspect the result. No plan — just doing.

  2. Scripted
     You repeated it three times. Extract a gist — a plain Python file the
     kernel hot-reloads. Same code, now importable.

  3. Semi-auto
     Wire a trigger: @every, a webhook, a queue. Human gates where ambiguity
     requires judgment.

  4. Production
     Event-driven, durable, monitored. Gists from phase 2 are now production
     steps. Nothing was thrown away.

== Applying the playbook ==

  When the user asks you to automate something, follow this progression:

  - Phase 1: Do it interactively first. Use exec, browser, fetch to
    understand the task. Don't write a gist yet.

  - Phase 2: If you've done the same sequence three times, suggest
    extracting a gist. The user decides; don't force it.

  - Phase 3: If the gist works reliably, suggest wiring a trigger —
    @every for polling, a webhook, a spreadsheet gate. Match the trigger
    to the cadence of the real workflow.

  - Phase 4: When the user is ready to go headless, the gist moves to
    FastAPI/Inngest unchanged. The repld session becomes the test harness.

  Never skip phases. Never design the pipeline before doing the work.
  The workflow reveals itself through repetition.

  Read repld://docs/production for concrete wiring patterns when
  graduating gists to FastMCP or FastAPI.
"""

# ---------------------------------------------------------------------------
# PRODUCTION (repld://docs/production resource — graduation guide)
# ---------------------------------------------------------------------------

PRODUCTION = """\
Graduating gists to production

A gist is a plain Python file that runs in repld during prototyping. The same
code runs in production — FastMCP, FastAPI, or any framework. This guide shows
how to wire it.

== The two-layer pattern ==

Write gists with two layers: core logic (portable) and repld wiring (shed on
graduation). The core function moves to production unchanged; the wiring gets
replaced by the target framework's decorator.

  Core logic — top of file:

    import os
    import httpx

    async def lookup(company_id: str) -> dict:
        \"""Look up a company. -> {name, address, ...}\"""
        async with httpx.AsyncClient() as c:
            resp = await c.get(
                f"https://api.example.com/company/{company_id}",
                headers={"Authorization": f"Bearer {os.environ['API_TOKEN']}"},
            )
            return resp.json()

  Browser-auth variant — accept a fetch callable so the core function works
  both with browser session auth (no token) and standalone (token from env):

    async def lookup(company_id: str, *, fetch=None) -> dict:
        \"""Look up a company. -> {name, address, ...}\"""
        if fetch is not None:
            return (await fetch(f"/api/company/{company_id}"))["body"]
        async with httpx.AsyncClient() as c:
            resp = await c.get(
                f"https://api.example.com/company/{company_id}",
                headers={"Authorization": f"Bearer {os.environ['API_TOKEN']}"},
            )
            return resp.json()

  repld wiring — bottom of file (shed on graduation):

    async def _tool_lookup(company_id: str) -> dict:
        \"""Look up a company.\"""
        import repld
        try:
            tab = await repld.browser.get("*example.com*")
            return await lookup(company_id, fetch=tab.fetch)
        except RuntimeError:
            return await lookup(company_id)

  Schema is inferred from the type hints and the docstring's first line.
  Per-parameter descriptions go in Annotated[T, "..."].

== Secrets and .env ==

Core logic reads secrets from os.environ — never hardcode tokens.

  token = os.environ["API_TOKEN"]

Where the env var comes from depends on context:
  - Interactive (repld): .env at project root, loaded at kernel boot
  - Production (FastAPI/FastMCP): .env at project root, loaded by framework
  - CI/deploy: platform secrets (Fly.io, Railway, etc.)

repld loads .env from the project directory (same place as gists/) at kernel
boot. Existing env vars are never overwritten.

Boot is the *only* time it is read — unlike gists/, which reload on mtime. A
value written to .env afterwards stays invisible until something asks:

  from repld import load_dotenv
  load_dotenv()

And because existing vars win, a name captured while it was empty stays empty.
Clear it first when correcting one:

  import os
  os.environ.pop("API_TOKEN", None)
  load_dotenv()

For browser-auth APIs — no token at all. The browser session IS the
credential. Use the fetch= callable pattern above.

== Three graduation tiers ==

  Standalone (no repld dependency):
    Gist uses public APIs with token auth. No browser needed.
    Production deps: just the gist's __repld_deps__ (httpx, etc.)

  Browser-backed (repld dependency):
    Gist relies on browser session auth. Production service runs alongside
    repld + Chrome.
    Production deps: uv add repld-tool[browser]

  Hybrid (token + browser fallback):
    Token auth when available, browser fallback when not. The fetch=
    parameter pattern enables this — core logic doesn't care which path.

== FastMCP wiring ==

Shortest path — register the core function directly:

  from fastmcp import FastMCP
  from gists.acme import lookup

  mcp = FastMCP("my-service")
  mcp.add_tool(lookup)

FastMCP generates the input schema from type hints and the tool description
from the docstring. If the core function's signature already matches what
you want the tool to look like, this is all you need.

When you need a different name or want to adapt parameters:

  @mcp.tool
  async def company_lookup(company_id: str) -> dict:
      \"""Look up a company.\"""
      return await lookup(company_id)

== FastAPI wiring ==

  from fastapi import APIRouter
  from gists.acme import lookup

  router = APIRouter()

  @router.get("/company/{company_id}")
  async def company_lookup(company_id: str):
      return await lookup(company_id)

Same core function, different framework. Data in, data out.

== Scaffolding a production project ==

  # 1. Create the project
  uv init my-service && cd my-service

  # 2. Add framework
  uv add fastmcp                         # or: uv add fastapi uvicorn

  # 3. Add gist deps (from __repld_deps__)
  uv add httpx                           # each package the gist declared

  # 4. If browser-backed:
  uv add repld-tool[browser]

  # 5. Copy gists (vendor them)
  mkdir -p gists
  cp ~/other-project/gists/lookup.py gists/

  # 6. Write server.py — import core functions, wrap with decorators
  #    The _tool_* layer stays behind in the gist file.
  #    @mcp.tool / @router.get replaces it.

  # 7. Run
  uv run fastmcp run server.py           # FastMCP
  uv run uvicorn main:app                # FastAPI

== What stays, what goes ==

  Stays (portable):              Goes (repld-specific):
  ────────────────               ──────────────────────
  Core async functions           _tool_* handler functions
  __repld_deps__ (as reference)  import repld (in wiring)
  os.environ["TOKEN"]            tab.fetch / browser.get
  Data parsing helpers           __repld_usage__
  Type hints, docstrings
"""

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
  tab = await browser.acquire("*example.com*")        # get(), or open() on a miss
  await browser.watch("*example.com*")               # auto-attach current + future

  browser.tabs                                       # list[Tab] currently attached
  await browser.pages()                              # all Chrome targets (dict list)
  browser.patterns                                   # active watch patterns
  await browser.detach("*example.com*")              # detach pattern + tabs
  await browser.detach()                             # detach everything
  browser.clear(target=)                             # clear all captured data

  await browser.connect(9223)                        # add another Chrome instance
  await browser.connect(profile="/tmp/my-chrome")    # connect by user-data-dir
  await browser.disconnect()                         # unpin tabs, close all WebSockets
  await browser.disconnect(port=9222)                # unpin + close one Chrome instance

Quirks:
  - get(glob) skips workers (service_worker, shared_worker, worker). To reach
    a worker, use its target ID directly: get("9222:a1b2c3").
  - get() raises RuntimeError if no match found (with timeout=None, checks once).
  - fresh=True snapshots currently-matching targets at call time and excludes
    them — returns only tabs that appear *after* the call.
  - open() creates a tab via Target.createTarget, then waits the same ready=
    signal as get() before returning (default: document.readyState === 'complete').
  - acquire(pattern, open=, ready=, timeout=) is get() falling back to open()
    on a miss — the try-get/except-open dance every gist that returns to one
    recurring site ends up hand-rolling. open= is the URL to navigate to on
    a miss; defaults to pattern itself when pattern is already a real URL.
  - browser.connect(profile=path) reads DevToolsActivePort from a Chrome
    user-data-dir to discover the debug port.  Works with --remote-debugging-port=0
    (random port) — Chrome writes the actual port to that file on startup.

=== ready= parameter ===

  tab = await browser.get("*localhost*", ready="[data-testid='app-root']")

ready= stores a CSS selector or JS expression on the Tab.  It's used by:
  - get() / open() — waits after initial attach
  - navigate() / reload() — waits after page load
  - _reattach() — waits after session recovery (HMR, navigation)

Selectors use the same classification as click/type_text: '.', '#', '[',
'data-', a bare tag or custom-element name ('main', 'my-app'), and the
custom forms (text=, role=, label=, :has-text, aria-ref=) are polled as an
existence check through the selector engine every 100ms, 15s timeout —
existence only, never strictness: a ready signal that matched three nodes
has still fired.  Anything else — anything with a dot or an operator in
it — is evaluated as a JS expression via Runtime.evaluate, polled the same
way, and must return truthy.  An expression that *raises* is reported
immediately rather than polled out.

Default (no ready=): waits for document.readyState === 'complete'.

Convention: add data-testid to your root layout component.

SPA gotcha: readyState fires once the page shell has loaded, often well
before a client-rendered app has painted anything worth reading — a bare
get()/navigate() with no ready= can hand back a tab whose tree() is still
an empty shell.  Confirmed live against Shopify admin's product list (a
div-based Polaris grid, not a real <table>, so waiting on generic table/row
markup would never fire):

  tab = await browser.get(
      "*admin.shopify.com*/products*",
      ready="[href*='/products/']",   # first real row link, not just the shell
  )

get()/open() only apply ready= on the tab's *first* attach — re-calling
get() on a tab that's already attached skips the wait (the hot re-query
path stays cheap), so passing ready= there is a no-op.  Set it once, on
whichever call first attaches the tab.

Selector-vs-JS classification checks the string's *prefix*
(.,#,[,data-,...), not whether it contains selector syntax — a tag+attribute
combo like "a[href*='/products/']" doesn't start with any of those, so it's
read as JS and fails as a syntax error.  Drop the leading tag ("[href*=...]")
or lead with a recognized prefix instead.

get()/open() also fall back silently when no ready= is given — the fallback
is a best-effort guess (readyState + a network-idle settle), not a
confirmation the SPA gotcha above didn't just happen again.
tab.ready_confirmed (bool, read-only) tells you which one happened: True
only if an explicit ready= was polled and satisfied; False if the bare
default was used.  Lives on the underlying session, so a later re-`get()`
on an already-attached tab still reports the original attach's value.

== Tab API (async) ==

  tab.js(expr, *, await_promise=True, user_gesture=True)     → Any
      Evaluate JS in page context.  Results returned by value (deep-serialized).
      await_promise=True (default) awaits Promise results like the DevTools console.
      user_gesture=True makes isTrusted=true on events.
      Raises BrowserJSError on JS exceptions (with preserved stack trace).

  tab.click(selector, *, button='left', click_count=1)       → Receipt
      Mouse click: arrives (mouseMoved) then Input.dispatchMouseEvent
      (mousePressed + mouseReleased).  The arrival matters beyond hover
      styling — Chromium's native click synthesis can silently fail without
      it on some pages (drag-capable elements are the confirmed case);
      Playwright's own mouse implementation always moves before pressing
      for the same reason.  Produces isTrusted=true events.  Auto-waits up
      to 2s for the element, then for visible/enabled/stable
      (actionability).  Strict resolution:
      a selector matching >1 element with no single visible winner raises
      with a candidate digest instead of guessing.  The Receipt names what
      the click actually hit ("clicked: <button…> — #save (412,133)").  When
      an unrelated element intercepts the point, a plain left-click switches
      to element.click() on the resolved target (the coordinate path can
      start a drag on a pan layer; the DOM click reaches it the way
      assistive tech does — isTrusted=false, and the receipt says so);
      modified clicks keep the coordinate dispatch plus a warning.

  tab.type_text(selector, text, *, delay_ms=0, press_enter=False)  → Receipt
      Focus element, select-all existing content, type character-by-character
      via Input.dispatchKeyEvent.  Auto-waits + actionability like click.
      Verifies the value landed; if the keystrokes changed nothing (a
      framework-controlled input reverting them), falls back to the native
      prototype value setter + bubbled input/change events and re-verifies —
      the Receipt says which path the text took.
      delay_ms adds a pause between keystrokes (in milliseconds).
      press_enter sends an Enter key after the text (after verification).

  tab.select_option(selector, option)                        → Receipt
      Select an option in a dropdown.  Native <select>: finds the option by
      label (else value), sets it via the prototype setter + input/change
      events.  Custom widgets (react-select-style): clicks the field, waits
      for a role=option matching the name (exact, then substring), clicks
      it.  When the click focuses an element declaring aria-autocomplete,
      the option text is typed in first and resolution runs against the
      filtered listbox — reaches options a virtualized list hasn't rendered.
      A miss lists the visible options' accessible names.

  tab.hover(selector)                                        → Receipt
      Move the mouse over an element — or an (x, y) / 'x,y' point — and
      leave it parked there: :hover styling and mouseenter-revealed UI
      (menus, toolbars, tooltips) stay up for a following click, and the
      observation's changes: section reports what the hover revealed.
      The selector form resolves strictly + waits visible/stable like
      click.  Coordinates are the route to visual-only targets whose
      accessible proxy sits elsewhere (SVG diagram nodes): hover has no
      element.click()-style fallback, so a proxy hover parks on the proxy.

  tab.drag(source, to, *, steps=12, duration_ms=400, dwell_ms=150)  → Receipt
      Mouse drag in one call: press on source, paced mouseMoved events with
      the button held, dwell at the drop point, release on to.  Endpoints
      are selectors (strictly resolved, scrolled into view) or (x, y)
      tuples / 'x,y' strings.  The first moves are 2px micro-steps, so a
      small origin (a 10px SVG port) arms its drag-slop threshold before
      the pointer leaves it.  dwell_ms holds the pointer at the drop point
      with repeated same-position moves before releasing — a debounced
      drop handler needs move events to see the pointer as hovered before
      a release counts as a drop; 0 disables it for gestures where it's
      pure overhead (sliders, whole-node moves).  A drop target that only
      appears once the drag starts is re-resolved mid-gesture.  Covers
      pointer/mouse-event drag (SVG editors, sliders, drag libraries);
      native HTML5 draggable=true DnD rides a different browser pipeline
      these events don't start — an observation saying "changes: none"
      after dragging one is the tell.  The Receipt names both endpoints
      and warns if the drop point was occluded.

  tab.key(key)                                               → None
      Dispatch one key press: named keys ("Enter", "Backspace", "ArrowLeft",
      "F5", "Space"), printable characters ("a"), and Ctrl/Alt/Shift/Cmd
      combos ("Ctrl+A", "Shift+Tab"; trailing "+" is the plus key).  Carries
      the virtual key code, so editing actions actually happen — Backspace
      deletes, arrows move the caret — and Enter/Space carry their character
      so native button/form activation fires.

  tab.keys(keys, *, delay_ms=0)                              → None
      A sequence of key()/combo presses in one call — one round trip for a
      keyboard flow ("Ctrl+A", "Backspace", "ArrowDown", "Enter") instead of
      one per keypress.  delay_ms pauses between presses for apps that
      debounce key handling.

  tab.tap(selector_or_x, y=None)                             → Receipt
      Touch tap via Input.dispatchTouchEvent (touchstart/touchend).
      Accepts a selector string OR (x, y) coordinates.  The selector form
      resolves strictly and waits for visible/stable like click.
      3s timeout — raises TimeoutError if the page's touch handler blocks
      (common on complex apps like Messenger/React).

  tab.set_viewport(width, height)                             → None
      Emulate a fixed viewport at deviceScaleFactor 1
      (Emulation.setDeviceMetricsOverride), so screenshot coordinates are
      page pixels with no multiplier math.  browser_open's viewport= param
      calls this on open.  Use a fresh tab per distinct size.

  tab.front()                                                 → None
      Bring this tab's window to the front (Page.bringToFront).
      click/tap/type_text/key already call this automatically when Chrome
      has the tab backgrounded — coordinate/key input silently does nothing
      on a hidden tab otherwise (Chrome drops it, no error). Call directly
      to raise a tab for its own sake, or to skip the per-call visibility
      check in a script doing a lot of input against one tab.

  tab.swipe(x1, y1, x2, y2, *, steps=10, duration_ms=300)   → None
      Touch swipe: touchStart → touchMove × steps → touchEnd.
      For scrolling on mobile Chrome via ADB.

  tab.scroll(selector, dy=0, dx=0, *, steps=10, duration_ms=300) → None
      Touch-scroll a container by (dx, dy) pixels. Sugar over swipe() —
      resolves selector to its center, swipes the opposite direction
      (scrollBy semantics: positive dy scrolls down, positive dx scrolls
      right). Auto-waits up to 2s for the element.

  tab.tree(mode='aria', *, at=None, in_crop=None)             → list[str]
      Accessibility snapshot as text lines.  mode='aria' (default): Playwright
      ariaSnapshot with [ref=eN] handles, reusable as aria-ref=eN selectors
      in click/type_text until the next snapshot, navigation, or reattach.
      mode='ax': the raw CDP accessibility tree (pierces same-process
      iframes, no refs).  Crosses OOPIF iframes either way — discovers
      attached iframe children via DOM.getFrameOwner, inlines their trees.
      Standalone read (no settle, no observation pipeline).
      at=(x, y) / 'x,y' hit-tests that point instead (ignores mode) and
      returns a small elided tree rooted at the nearest meaningful ancestor
      of the hit — for "what's actually here" from a coordinate rather than
      a selector.  Rendered through the same engine as mode='aria', so its
      nodes carry real [ref=eN] handles too.  Always also captures a
      screenshot cropped to the hit (path in the result's `screenshot` key)
      — the tree's text isn't always enough to disambiguate (visual-only
      state, near-identical rows).  A hit on an iframe reports the redirect
      (target id + translated local coordinate) instead of a tree.
      in_crop=<path> reinterprets at= as a pixel *within* an earlier seed's
      screenshot instead of a page coordinate — the crop embeds its own
      translation, so no scale/offset math is needed: pass the point you
      see in the image straight through.

  tab.fetch(url, *, method='GET', body=None, headers=None)    → dict
      In-page JS fetch() — inherits the browser's cookies, session, and CORS
      origin.  NOT a separate HTTP call.
      Returns: {"status": int, "ok": bool, "body": Any}
      body is auto-parsed as JSON whenever it's valid JSON, regardless of
      content-type (plenty of real APIs mislabel JSON as text/plain or
      text/html).  A non-JSON text body is returned as-is.
      Auto-sets Content-Type: application/json for a dict body,
      application/x-www-form-urlencoded for a string body.
      Caller headers override auto-set headers (e.g. for raw JSON text).
      Raises RuntimeError (via BrowserJSError) on network errors.

  tab.navigate(url)                                           → None
      Navigate to URL.  Waits for ready signal after page load.

  tab.reload()                                                → None
      Reload page.  Waits for ready signal after load.

  tab.close()                                                 → None
      Close this tab (Target.closeTarget).  Session cleanup follows from the
      resulting Target.targetDestroyed event, same as a user closing it.

  tab.wait_for(selector, *, timeout=5.0)                      → None
      Wait for element to appear.  Polls every 100ms.
      Same selector syntax as click/type_text.

  tab.wait_for_idle(*, timeout=5.0, quiet=0.5)                → int
      Wait for network idle.  Returns settle time in ms.
      See "Settle loop" below for what "idle" means.

  tab.screenshot(*, full_page=False, force=False, path=None)  → dict
      Capture a native-resolution PNG. Returns {path, width, height, bytes}.
      Raises if the capture exceeds the vision API's token budget instead
      of silently shrinking it — pass force=True to send it natively
      anyway, or crop to a smaller region instead (browser_tree(at=...)
      does this). Budget is checked against the PNG's real pixel
      dimensions, which diverge from the CSS-pixel viewport size by the
      device pixel ratio.

  tab.cookies()                                               → list[dict]
      All cookies for this tab via Network.getCookies.

  tab.http_client(*, base_url=None, **kwargs)                 → httpx.AsyncClient
      Copy this tab's current cookies into a plain httpx.AsyncClient.
      Requires the `http` extra (RuntimeError with an install hint if
      missing). base_url defaults to the tab's own origin. Use this once a
      site's data lives behind cookie-authenticated JSON — the tab is only
      needed to establish the session; every read after that is a plain
      concurrent-safe HTTP call, no shared-tab-state races and no per-call
      CDP round trip the way tab.fetch() pays.

  tab.cdp(method, **params)                                   → dict
      Raw CDP passthrough — escape hatch for anything not wrapped.

=== Pin + gate bridge ===

  tab.pin(reason='', guard_unload=True)  → None
      Inject floating pill UI + beforeunload guard.  Idempotent.
      guard_unload=False skips the beforeunload guard — for a tab you're
      actively driving against a live-reloading dev server, since the
      guard fires on ANY unload (same-origin included) and blocks a
      framework's own HMR reload behind a native confirm dialog no CDP
      driver can dismiss, indistinguishable from a hung tab.
      Pill shows green dot when connected, amber when awaiting input.
      Prevents accidental tab close.

  tab.unpin()                       → None
      Remove pill + guard + heartbeat.

  tab.label = "text"                → None
      Colored identification bar at the top of the page.  Auto-color;
      ("text", "#hex") picks the color, None removes.  Injected via
      Page.addScriptToEvaluateOnNewDocument, so it survives navigation.
      Read tab.label for the current text (or None).

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

  since= is epoch seconds on all four — pass time.time(). The underlying
  CDP clocks differ (epoch s / epoch ms / monotonic); the conversion is
  done for you, so one base is all you need.
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

=== Full HAR entry (via tab.request()) ===

A nested dict, not a flat Row — keys are omitted entirely when empty:

  {"request":  {method, url, headers, postData, cookies},
   "response": {status, statusText, headers, mimeType},
   "state", "type", "size", "time_ms", "timing", "error_text",
   "auth_scheme", "auth_cookies", "csrf_token_header",
   "loader_id", "frame_id", "curl_command",
   "initiator": {type, url, function, line}}

Everything except the response body — use tab.body(request_id) for that.
Row-level fields (id, redirect_index, target, is_asset, ...) stay on the Row
from tab.network(); this is the per-request detail view, not a superset.

== Tab properties ==

  tab.url            str   current URL (from target_info, see staleness note)
  tab.title          str   page title (from target_info)
  tab.type           str   "page", "iframe", "service_worker", etc.
  tab.target_id      str   short ID in "{port}:{6-hex}" format, stable across nav
  tab.parent_frame_id str  parent frame for iframes
  tab.capture_bodies bool  toggle Fetch body capture (True on get/open tabs, False on watch tabs)
  tab.ready_confirmed bool True if the last ready-wait used an explicit ready=;
                              False if it fell back to document.readyState (best-effort guess)

Staleness: tab.url and tab.title are read from a cached target_info dict,
updated only on Target.targetInfoChanged events.  They can be briefly stale
after navigation — if you need the live URL, use tab.js("location.href").

== Multi-browser ==

browser.connect(port) adds a Chrome instance to the pool.  Call it multiple
  times for multi-browser setups (e.g. two test browsers on different ports).
  Target IDs include the port prefix (42829:abc123 vs 43213:def456), so all
  tab-scoped tools route to the right Chrome automatically.

  await browser.connect(42829)
  await browser.connect(43213)
  await browser.connect(profile="/tmp/my-chrome")  # port from DevToolsActivePort
  await browser.watch("*localhost:5200*")   # watches across both
  browser.tabs                              # tabs from all instances

Browser state (connected ports + watch patterns) persists in the kernel's
  dashboard hint file ($XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.dashboard)
  across kernel restarts.  On boot, repld prompts on the terminal before
  reconnecting/re-watching ([Y/n], default yes); headless boot (--no-display)
  or non-tty stdin skips the restore.

== Controls protocol ==

Apps exposing window.controls (a ControlRegistry) get automatic discovery
  and invocation from repld.

  tab.controls()                            → dict | None
      Snapshot window.controls.describeAll().  Returns full schema: actions
      with param types, properties with current values, state per control.
      Returns None if the tab has no controls.  Async.

  tab.invoke(control, action, args=None)    → dict
      Call window.controls.invoke(control, action, args).  Returns
      {returned, stateBefore, stateAfter, duration}.  Async.

  tab.control_observations()               → list[dict]
      Parsed __controls__ observations from console.debug messages.
      History of actions that fired, with state transitions.

MCP tools:
  browser_controls(target)                   Discover controls on a tab
  browser_invoke(target, control, action, args)  Invoke with observation pipeline

Channel push: apps that wire setObservationSink to console.debug('__controls__',
  JSON.stringify(obs)) push action observations as channel messages automatically:
    [controls] thread.goto(id: "abc") — state: "none" → "abc" (42ms)

== Console error push ==

Console errors (console.error) and uncaught exceptions (Runtime.exceptionThrown)
  from watched tabs push as [console:error] channel messages immediately.
  No polling needed — the agent sees errors the moment they happen.

    [console:error] 9222:af5ae1: TypeError: Cannot read property 'x' of null

Cross-tab dedup (always on):
  When the same error fires from multiple tabs/iframes within 2 seconds,
  only the first pushes immediately. Duplicates are collapsed into one
  follow-up message:  [console:error] 9222:af5ae1: ... (×14 tabs)

Suppress filter (opt-in):
  browser.suppress("[vite] failed to connect")    mute matching errors
  browser.unsuppress("[vite] failed to connect")   un-mute
  browser.suppressed                                list active patterns
  Suppressed patterns persist across kernel restarts.

Fetch capture exemption (opt-in):
  browser.no_capture("*asusrouter.com*")   skip Fetch body capture for matching tab URLs
  browser.capture_ok("*asusrouter.com*")   undo — re-enable capture for matching tabs
  browser.no_capture_patterns                list active exemption globs
  For servers with no CORS/CORP headers (old embedded-device admin UIs are the
  common case), Chrome's Fetch.continueResponse/fulfillRequest replay path
  applies CORB/ORB more strictly than a natively-fetched response, which
  false-positives as a CORS block on resources that are actually same-origin.
  Exempting the host skips Fetch interception entirely on matching tabs —
  tab.network()/tab.body() still work via the Tier-1 on-demand path, only the
  proactive capture is lost. Exemption patterns persist across kernel restarts.
  Unlike suppress/unsuppress, which apply live to every open tab immediately,
  no_capture/capture_ok only take effect on the *next* attach — a tab that
  already has Fetch enabled keeps capturing until it's re-attached.
  tab.disable_capture() is still the retroactive per-tab knob for that case.

== Selectors ==

Same syntax across click, tap, type_text, select_option, hover, drag, wait_for:

  .css-class, #id, [attr], tag                        CSS
  [data-testid='name']                                CSS (recommended for own code)
  text=Submit                                         exact text match
  role=button[name="Save"]                            ARIA role + accessible name
  label=Username                                      input by label
  placeholder=Search or create                        input by placeholder
  testid=workflow.add-status                          data-testid shorthand
  button:has-text('OK')                               CSS + text filter
  aria-ref=e12                                        ref from tab.tree() / browser_tree
  getByRole('button', { name: 'OK' })                 Playwright locator call — what the
                                                      strict-mode error suggests, pasteable
                                                      as-is (also getByTestId/getByText/
                                                      getByLabel/getByPlaceholder/locator)

Resolution runs through a vendored build of Playwright's InjectedScript
engine, evaluated once per document in an isolated world (main world on the
few pages that refuse one — same semantics, no tamper isolation).  Every form
pierces open shadow roots.  role= computes real implicit ARIA roles and
accessible names (W3C accname), so labels, alt text, and aria-labelledby all
resolve; hidden elements are excluded from role= matches.

Strict resolution:
  0 matches  → auto-wait (poll 100ms, 2s), then "Element not found"
  1 match    → used, visible or not (apps keep real controls off-screen
                behind styled proxies — a lone invisible match is the target)
  >1 matches  → filtered by visibility; exactly one visible wins (the receipt
                notes "N matches, 1 visible"); otherwise the call raises with
                a candidate digest (preview + generated selector for each),
                so a wrong-element click is impossible rather than diagnosable.

role= name operators:
  role=button[name="Save"]     exact accessible-name match (case-sensitive)
  role=button[name*="Save"]    contains
  role=button[name^="Save"]    starts with

text= matching: exact (whitespace-normalized) text content, with an exact
  aria-label match as fallback when the text form matches nothing.

aria-ref= lifetime: refs come from the last tab.tree() / browser_tree aria
  snapshot and die on the next snapshot, navigation, or reattach — a dead ref
  errors telling you to take a fresh snapshot, it never polls.

Auto-wait: selectors auto-wait up to 2s (click/type_text) or the specified
  timeout (wait_for), polling every 100ms, then actionability waits for
  visible/enabled/stable (rAF-based) before dispatching.  Under MCP tools the
  previous mutation already settled the page (network idle + tree rebuilt), so
  the 2s poll is just a safety net for DOM that lags behind network quiet
  (lazy renders, setTimeout callbacks).  For first interactions or known-slow
  elements, call wait_for(selector, timeout=10) before click/type_text.

== Internals ==

=== Network body capture ===

Two tiers of body access:

  Tier 1 — on-demand (all tabs):
    tab.body(request_id) calls Network.getResponseBody, a CDP call that fetches
    the body from Chrome's resource cache.  Works on any attached tab without
    Fetch enabled.  Best-effort: Chrome may evict the response from cache during
    rapid redirect chains or high-traffic flows.

  Tier 2 — proactive Fetch capture (get/open tabs):
    browser.get() and browser.open() enable Fetch domain interception
    automatically.  browser.watch() tabs are lightweight — no Fetch overhead.

    When enabled, Fetch intercepts all requests/responses:
    - Request stage: POST/PUT/PATCH bodies captured via Fetch.getRequestPostData
      (full body, not the ~64KB-truncated Network.requestWillBeSent.postData)
    - Response stage: bodies under 500KB stored in DuckDB as synthetic
      Network.responseBodyCaptured events.  Skips redirects (CDP limitation)
      and SSE (infinite stream).  Captured bodies replayed via fulfillRequest.
    - Non-captured responses use fire-and-forget continue commands (no roundtrip)

    tab.body() checks DuckDB first (microsecond lookup), falls back to
    Network.getResponseBody if not proactively captured.

  Opt-in/out on any tab:
    tab.capture_bodies = True            # fire-and-forget Fetch enable
    tab.capture_bodies = False           # fire-and-forget Fetch.disable
    await tab.enable_capture()           # awaitable enable
    await tab.disable_capture()          # awaitable disable

=== Settle loop ===

wait_for_idle() and the MCP observation pipeline use the same settle logic:

  Polls each tab's in-memory set of open requestIds every 50ms, across all
  tabs (including iframe children).  No DuckDB query — the count is kept by
  the CDP event handler, so the poll never touches the kernel loop's DB.

  A request enters on Network.requestWillBeSent and leaves on
  loadingFinished/loadingFailed.  WebSockets never enter (no
  requestWillBeSent).  Streamed responses (text/event-stream) leave as soon as
  their headers arrive: the body stays open by design, and waiting for it
  would mean this tab never settles again.  Anything still open after 60s is
  aged out as stuck.

  Returns when the inflight count is 0 for a continuous quiet period
  (default 0.5s).  Timeout default is 5s.

  Returns settle time in milliseconds.

=== MCP tools vs exec — settle behavior ===

MCP browser tools (browser_click, browser_type, browser_navigate, etc.) run
  the full observation pipeline: pre_observe → mutate → settle → post_observe.
  They automatically wait for network idle and return a changes: section (an
  AX-tree diff of what the mutation made appear/disappear/change state —
  "changes: none" means the page visibly ignored the action; a
  presentational reveal the AX tree can't see falls back to a
  "dom: +N −M elements" line from a MutationObserver) + tree +
  network delta + console delta.  This means each MCP call returns only
  after the page is stable — the next call's auto-wait (2s) rarely fires
  because the element is already in the DOM.

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
    3. Wait for ready signal (CSS or JS, 15s timeout)
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

For APIs that rely on cookies or session state — use tab.fetch(), or, once
the session is established, tab.http_client() to keep calling it without the
tab (plain concurrent httpx calls instead of one at a time through the page).

=== Multi-tab gists (embedded apps) ===

When the app lives in an iframe (e.g., Shopify embedded apps), hold both tabs:
  - admin tab for navigation (host page)
  - iframe tab for fetch/js (app context with auth)

After navigating the admin tab, re-acquire the iframe with
browser.get(pattern, timeout=10) — iframes reload on host navigation.
Never navigate an iframe directly — it destroys the embedded session.
"""


def _deps_hint() -> str:
    """The dependency paragraph. Reads cwd only — no kernel needed."""
    if (Path.cwd() / "uv.lock").exists():
        return (
            "Dependencies: this is a uv project. "
            "Add packages with `uv add <pkg>`, then restart the kernel. "
            "Gists can also declare __repld_deps__ for auto-install at boot."
        )
    return (
        'Dependencies: gists can declare __repld_deps__ = ["pkg"] '
        "for boot-time install into a shared, interpreter-versioned dir "
        "(never the project venv, which uv sync would prune). "
        "Stdlib and pre-installed packages are always available."
    )


def static_instructions() -> str:
    """`build_instructions` minus everything that needs a live kernel.

    The bridge answers `initialize` from here when no kernel has ever run in
    this project and there is no cache to read, so this is the instruction
    text for exactly the sessions that start cold. It was `_EXEC_MODEL` alone,
    which left the first session in a project — the one deciding how to
    structure the work — never told that ./gists is on sys.path, that _tool_*
    functions become MCP tools, or that __repld_deps__ exists. Nothing
    re-sends `instructions` after the lazy spawn, so it stayed missing for
    that whole session.

    Deliberately adjacent to `build_instructions`: the two compose the same
    constants in the same order, and what's absent here is only what genuinely
    depends on kernel state — the browser model, the gist listing, and
    registered gist tools.
    """
    # The join-a-list shape deliberately mirrors build_instructions below.
    return "\n\n".join(  # noqa: FLY002
        [_EXEC_MODEL, _GISTS_MODEL, _deps_hint(), _PLAYBOOK, _reference()]
    )


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
            hint = gists.import_hint(name)
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

    parts.append(_deps_hint())
    parts.append(_PLAYBOOK)
    parts.append(_reference())
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# OVERVIEW (repld help, no topic arg)
# ---------------------------------------------------------------------------

OVERVIEW = """\
repld — persistent Python kernel exposed to LLM agents over MCP.

Setup (once per project, per machine):
  uv tool install repld-tool
  claude mcp add repld -- repld bridge
  Local scope, so nothing repld-shaped lands in the project directory.

Architecture:
  Editor pane:   `claude` (or equivalent) — the bridge starts a headless
                 kernel for the cwd if one isn't running, and heals it if
                 it dies. Nothing to start by hand.
  Terminal pane: `repld`   kernel + live display, when you want to watch
                 it; `repld log -f` works either way.

A `repld_init.py` in the project root is executed into __main__ at boot —
by every kernel for that project, however it was started.

Runtime state lives in $XDG_RUNTIME_DIR/repld/projects/<slug>/, never in
the project directory.

One asyncio loop, one __main__ namespace shared with the agent. Cells run
via the MCP `exec` tool. Long tasks defer; channel pushes wake the agent
when work completes, files change, webhooks fire, or human gates resolve.

Commands:
  repld                    Start a kernel in cwd (with the live display)
  repld exec CODE          One-shot: run code in kernel, print result, exit
  repld exec               Interactive REPL (state persists in kernel)
  repld log [-f]           Recent kernel activity; -f streams it live
  repld status [--json]    pid / uptime / dashboard + live kernels elsewhere
  repld stop [--all]       Stop this project's kernel (or every one)
  repld restart            Stop, then start a fresh headless kernel
  repld dashboard          Open the kernel's web control panel (supplies its API
                           token; the page is not served without one)
  repld gate               List gates waiting on a human answer
  repld gate answer ID VAL Answer one (the route for a headless kernel)
  repld bridge             Stdio MCP bridge (Claude Code spawns this)
  repld gist new NAME      Scaffold a tool gist in ./gists/NAME.py
  repld gist fetch URL     Download a GitHub gist into ./gists (--global)
  repld gist add NAME      Link a gist registered in another project
  repld gist rm NAME       Unlink a gist (--stale drops all dead links)
  repld gist list          Show local + linked gists
  repld gist lint [NAME]   Check gist(s) for common authoring gaps
  repld browser ARGS...    Re-exec with browser deps (duckdb/websockets/pillow)
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

no_display(value) → value
  Return a value from a cell without auto-display re-printing it (still
  binds _/_N). For functions that already print(...) their own output.

defer(coro, label=None) → task_id
  Fire-and-forget. Channel push on done. Visible to get_task/cancel.
  coro can also be a zero-arg callable that builds the awaitable —
  defer(lambda: asyncio.gather(a(), b())) — so gather()/create_task()
  construct on the kernel's loop instead of a sync cell's own thread.
  A non-None return value is printed (into the push) and recoverable
  afterward as get_task(task_id)["result"] (a bounded repr — there's no
  cell number to bind _/_N to instead). no_display() suppresses the print
  but not the get_task recovery, same as it does for an exec cell.

every(seconds, label=, delay=0)(fn) → fn   periodic ticker; fn.cancel() stops
  delay= defers the first tick (default: tick now)
every.list()        → list  active EveryHandles
every.cancel_all()  → None  stop all tickers

get_task(task_id)  → {done, text, spill_path, ...}
cancel(task_id)    → {cancelled: bool}

Channel kinds:
  task_done             exec or defer finished
  every                 periodic tick result or error (kind=every, label=fn_name)
  awaiting_human        ask/confirm/choose pending
  bg_task_error         uncaught exception in background task
  loop_blocked          asyncio loop blocked > 5s
  loop_kill             watchdog cancelled a stuck task
  init_loaded           repld_init.py ran at boot (__main__ pre-populated)
  init_error            repld_init.py raised
  browser_connect       dashboard connected to Chrome (port in meta)
  browser_watch         dashboard watched a pattern (pattern in meta)
  browser_unwatch       dashboard unwatched a pattern
  controls              window.controls action observation (control, action, state in meta)
  console_error         console.error or uncaught exception from watched tab
  pin_lost              pinned tab navigated cross-origin — pin contract broken (target in meta)
  browser_disconnect    dashboard disconnected a Chrome connection or tab
  venv                  a project venv was adopted onto the running kernel

  A bare notify("...") carries no kind at all — meta is whatever keywords you
  passed, and {} if you passed none. Pass kind= yourself to filter on it.
""",
    "browser": """\
Tab (async unless noted):
  tab.js(expr, await_promise=, user_gesture=)      → any
  tab.tree(mode="aria", at=None, in_crop=None)     → list[str] (aria: [ref=eN] snapshot; "ax": raw CDP tree; at="x,y": elided [ref=eN] tree + cropped screenshot, hit-tested at a point; in_crop=path: at= is a pixel within that earlier crop, no scale math needed)
  tab.click(selector, button=, click_count=)       → Receipt (strict resolve + actionability wait; names what was hit)
  tab.tap(selector_or_x, y=)                       → Receipt (touch event, 3s timeout)
  tab.set_viewport(width, height)                  → None (fixed viewport at scale 1 — screenshot px == page px)
  tab.front()                                      → None (Page.bringToFront; click/tap/type_text/key auto-call this on a hidden tab)
  tab.swipe(x1, y1, x2, y2, steps=, duration_ms=)  → None (touch scroll)
  tab.scroll(selector, dy=, dx=, steps=, duration_ms=) → None (touch-scroll container)
  tab.type_text(selector, text, delay_ms=, press_enter=)  → Receipt (clears first; verifies value, native-setter fallback for controlled inputs)
  tab.select_option(selector, option)                     → Receipt (native <select> or custom listbox; types into an aria-autocomplete filter to reach virtualized options; a miss lists the options)
  tab.hover(selector)                                     → Receipt (park the pointer on an element or 'x,y' point; hover-revealed UI stays up)
  tab.drag(source, to, steps=, duration_ms=, dwell_ms=)   → Receipt (press→micro-steps→paced moves→dwell→release; endpoints are selectors or (x,y)/'x,y')
  tab.key(key)                                            → None (named keys, chars, "Ctrl+A" combos; VK-coded so Backspace/arrows actually edit)
  tab.keys(keys, delay_ms=)                               → None (sequence of presses in one call)
  tab.wait_for(selector, timeout=5)                       → None (wait for element to appear)
  tab.wait_for_idle(timeout=5, quiet=0.5)                 → int  (network idle; returns settle ms)
  tab.fetch(url, method=, body=, headers=)                → {status, ok, body, base64Encoded}
  tab.navigate(url)                                       → None
  tab.reload()                                            → None
  tab.close()                                             → None (Target.closeTarget)
  tab.controls()                                          → dict | None
  tab.invoke(control, action, args=)                      → dict
  tab.screenshot(full_page=, force=, path=)               → dict {path, width, height, bytes} (native res; raises over the vision token budget unless force=True)
  tab.cookies()                                           → list[dict]
  tab.http_client(base_url=None, **kwargs)                → httpx.AsyncClient (requires `http` extra; copies tab's cookies for plain concurrent HTTP calls)
  tab.cdp(method, **params)                               → dict

Tab — pin + gate bridge (see repld://docs/browser for pill lifecycle + heartbeat detail):
  tab.pin(reason="", guard_unload=True)  → None  inject pill + guard (guard_unload=False for dev-loop tabs); idempotent
  tab.unpin()                            → None  remove pill + guard
  tab.confirm(prompt, **kw)              → bool  gate routed to pill UI
  tab.choose(prompt, options, **kw)      → str   gate routed to pill UI
  tab.ask(prompt, **kw)                  → str   terminal only (no pill UI for text input)

Tab (sync — DuckDB queries):
  tab.network(url=, method=, status=, type=, since=, include_assets=)  → Rows
  tab.request(request_id)                                              → dict
  tab.body(request_id)                                                 → dict
  tab.console(level=, source=, since=)                                 → Rows
  tab.sse(url=, event_name=, since=)                                   → Rows
  tab.lifecycle(name=, since=)                                         → Rows
  since= on all four is epoch seconds (time.time()).
  tab.clear()                 → None
  tab.control_observations()  → list[dict]

  row.body()                             → dict (response body for a Row)

Tab (async — Fetch capture control):
  await tab.enable_capture()   → None
  await tab.disable_capture()  → None

Tab properties:
  tab.url / tab.title / tab.type         str   target info (type: page/iframe/worker)
  tab.target_id / tab.parent_frame_id    str   short ID; parent frame for iframes
  tab.capture_bodies                      bool  Fetch body capture (True on get/open, False on watch)
  tab.ready_confirmed                     bool  True = explicit ready= polled and satisfied; False = best-effort fallback
  tab.label                               str | None  colored ID bar; set "text" / ("text", "#hex") / None to clear; survives navigation

Browser:
  browser.connect(port=, profile=)               → Browser  (add Chrome instance; profile= reads DevToolsActivePort)
  browser.get(target, timeout=, fresh=, ready=)  → Tab  (glob or target ID; skips workers for globs)
  browser.watch(pattern)                         → str  (watch all matching, auto-attach new)
  browser.open(url)                              → Tab
  browser.acquire(pattern, open=, ready=, timeout=) → Tab  (get(), falling back to open() on a miss)
  browser.tabs                  → list[Tab]
  browser.pages()               → list[dict]
  browser.detach(pattern=)      → str
  browser.patterns              → list[str]  active watch patterns
  browser.clear(target=)        → str
  browser.disconnect(port=)     → str  (unpins tabs first; port=None disconnects all)
  browser.suppress(pattern)     → str  mute console errors matching substring
  browser.unsuppress(pattern)   → str  un-mute
  browser.suppressed            → list[str]  active suppress patterns
  browser.no_capture(url_glob)  → str  skip Fetch body capture for matching tabs
  browser.capture_ok(url_glob)  → str  undo — re-enable capture
  browser.no_capture_patterns   → list[str]  active exemption globs

  ready= takes a selector or a JS expression, classified exactly as
  click/type_text classify theirs: '.', '#', '[', 'data-', a bare tag or
  custom-element name, and text=/role=/label=/:has-text/aria-ref= → polled as
  an engine existence check (never strictness); anything else is evaluated as
  a JS expression and must return truthy (one that raises is reported, not
  polled out). Tab waits for the signal before returning; on session loss
  (HMR/navigation) it re-attaches and waits again. navigate() and reload()
  also wait for it. Convention: add data-testid to your root layout component.

Selectors (click/tap/type_text/select_option/hover/drag):
  .css-class, #id, [attr], tag           CSS
  [data-testid='name']                   CSS (recommended for own code)
  text=Submit                            exact text match
  role=button[name="Save"]              ARIA role + accessible name (accname)
  label=Username                        input by label
  placeholder=Search                    input by placeholder
  testid=x                              data-testid shorthand
  button:has-text('OK')                 CSS + text filter
  aria-ref=e12                          ref from tab.tree() (dies on next snapshot/navigation)
  getByRole('button', { name: 'OK' })   Playwright locator call — ambiguity-error suggestions paste as-is

  All forms resolve through the vendored Playwright engine (isolated world,
  once per document) and pierce open shadow roots. Strict: >1 match with no
  single visible winner errors with a candidate digest; a lone match is used
  visible or not; input then waits for visible/enabled/stable.

Touch vs mouse:
  tab.click()  — Input.dispatchMouseEvent (works everywhere)
  tab.tap()    — Input.dispatchTouchEvent (fires touchstart/touchend)
  tab.swipe()  — touch sequence for scrolling
  tab.scroll() — swipe sugar: scroll a container by (dx, dy) pixels

  Touch events may hang on complex apps (React, Messenger) where JS handlers
  block. tap/swipe/scroll have a 3s timeout and raise TimeoutError cleanly.

Mobile viewport testing:
  Emulation.setDeviceMetricsOverride (via tab.cdp) can leave viewport metrics
  inconsistent if reapplied on a tab that already has a different override —
  confirmed on real hardware: document.documentElement.clientWidth and
  window.innerWidth can disagree on the same page load, which a real browser
  never does on a fresh one. Use a fresh tab per distinct size, and verify
  clientWidth === innerWidth before trusting anything measured or captured.

  For definitive results, or when emulation disagrees with itself, a real
  device over ADB sidesteps emulation entirely: `adb forward tcp:PORT
  localabstract:chrome_devtools_remote`, then Browser(port=PORT) connects to
  the device's actual Chrome — same Tab API, real rendering. Worth a gist for
  repeated use (connect, forward, `adb shell screencap` for true
  native-resolution screenshots instead of CDP's).

Target IDs: "{port}:{6-hex}" (e.g. 9222:887d3d). Stable across navigation.
Browser(port=N) creates a standalone instance for non-default ports (e.g. ADB-forwarded).
Requires: Chrome 140+ with --remote-debugging-port=9222
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
  Name handlers _tool_{name}(param: type = default, ...) → str | dict.
  Schema is inferred from type hints + defaults; first docstring line →
  tool description. Tools appear in tools/list automatically; no exec
  round-trip needed.
  Type map: str→string, int→integer, float→number, bool→boolean,
  list→array, dict→object. No annotation → string. No default → required.
  Describe a *parameter* with Annotated[T, "..."] — the docstring's first
  line is spent on the tool description, so this is the only place to say
  what a date format or an id refers to. Composes with `| None`.
  Scaffold: repld gist new <name>

  Example:
    async def _tool_foo_query(
        term: Annotated[str, "Search term, matched against name + tags"],
        since: Annotated[str | None, "From date YYYY-MM-DD"] = None,
        limit: int = 10,
    ) -> dict:
        \"""Search foo for term.\"""
        return {"result": ...}

  A _tool_* function is the only way to register a tool; a __repld_tools__
  list is ignored entirely, and `repld gist lint` flags one still lying
  around.

Dependencies:
  __repld_deps__ = ["httpx>=0.27", "beautifulsoup4"]
  Use "." to depend on the gist's own project (editable install when linked elsewhere).
  Use "path:vendor/lib" to prepend a local, non-pip-installable directory to
  sys.path (relative to the project root; absolute paths pass through as-is).
  No install step; modules imported from it auto-reload like gists do. A
  missing directory warns with a git-submodule hint instead of failing later
  with a bare ModuleNotFoundError.
  Kernel scans at boot and prompts to install missing PyPI packages into a
  shared, interpreter-versioned dir (~/.local/share/repld/deps/pyX.Y) -- never
  the project venv, which `uv sync` would prune. It goes on sys.path *after*
  the project's own packages, so a project install always shadows a gist dep.

Linting:
  repld gist lint [name...]   Check gist(s) for common authoring gaps
  Rules: firstline (module docstring's first line must stand alone),
  shape (dict/list-returning public methods need -> {shape} in their
  docstring's first line), deps (non-stdlib imports need __repld_deps__),
  legacy (a __repld_tools__ list, which is silently ignored -- nothing
  else reports one).
  Suppress one: # gistlint: ignore=<rule> on the flagged line.

Getting a gist you didn't write:
  repld gist fetch <gist-url> [--global] [--name NAME] [--force]
  Downloads a GitHub gist's .py files into ./gists (or ~/.repld/gists with
  --global) with a `# source:` header. A snapshot, not a link — nothing
  tracks the gist afterwards, and the file is yours to edit. Sibling of
  `new`, not of `add`: `rm` unlinks, so delete a fetched file directly.
  Declared deps are reported but not installed — read the code first; the
  next kernel boot prompts for them wherever there is a terminal.

Cross-project links:
  repld gist list             local + linked gists in this project
  repld gist add <name>       link a gist registered in another project
  repld gist rm <name>        unlink (--stale drops all dead links)
  Every import is recorded in a central registry; `add` resolves a name to its
  path, follows same-dir sibling imports, and records absolute paths in a
  committed ./gists/.links manifest — no copy. Read repld://gists/_registry to
  browse every gist seen across projects; a name marked (already here) is
  already resolvable in this project and doesn't need `add`.
  A linked gist's own imports carry over too — one that does `import repld`
  at module level only works inside a kernel, whatever project it's linked
  into. `add` warns when it links a file shaped that way; `repld gist lint`
  won't catch it on its own (`import repld` isn't flagged as a dependency).

Writing gists:
  Prefer async — use httpx.AsyncClient, async def methods, await tab.fetch().
  Async gists yield to the event loop between calls: browser stays responsive,
  multiple tasks can interleave, no "loop blocked" warnings.
  Sync gists work (auto-threaded) but can't interleave with async work.
  Set __repld_usage__ = "sd = await SD.connect()" for a custom listing line.
""",
    "gates": """\
await ask(prompt, *, tab=None, default=None, timeout=None)      → str
await confirm(prompt, *, tab=None, default=None, timeout=None)  → bool
await choose(prompt, options, *, tab=None, default=None, timeout=None) → str

Blocks the cell until a human answers. Three surfaces can answer, and
they race — first one wins:
  kernel pane      only if the kernel was started as `repld` in a terminal
  pin pill         pass tab= (requires tab.pin()); confirm/choose only
  repld gate       any kernel, from the project dir — the headless answer
An auto-spawned kernel (Claude Code's bridge, `repld restart`) has no pane,
so `repld gate` is the usual route; the awaiting_human push says so and
carries the gate id. `repld gate` lists what's pending, `repld gate answer
<id> <value>` resolves it, and `repld log -f` shows gates as they open.
ask() accepts tab= for symmetry, but the pill has no text input.
TimeoutError if no default and timeout expires — pass timeout= for any gate
that must not park a cell indefinitely.
Emits awaiting_human channel while blocked.

notify(content, **meta)
  One-shot channel push to all MCP sessions.
""",
    "migration": """\
Why a repld project has no state files (0.1.x → 0.2).

repld 0.2 writes nothing into a project directory. Files an older setup
created are absent on purpose — none of these are missing, they are gone:

  .pyrepl.lock/.sock/.dashboard  runtime state; now
                                 $XDG_RUNTIME_DIR/repld/projects/<slug>/ as
                                 kernel.lock/.sock/.dashboard. `repld status`
                                 prints the live paths.
  .mcp.json                      never written now. Register the server with
                                 `claude mcp add repld -- repld bridge`.
  CLAUDE.md repld:start block    gone; that content is the MCP `initialize`
                                 instructions, composed fresh each session.
  repl.py + --init               now ./repld_init.py, auto-detected and run by
                                 every kernel, whoever started it.
  __repld_tools__                removed in 0.2 and ignored, so a file still
                                 declaring one has silently lost its tools.
                                 `repld gist lint` is what reports it.

Still in the project and yours to commit: ./gists/, ./gists/.links, ./.env,
./repld_init.py.

If you FIND a .pyrepl.* file, it is 0.1.x leftover, not something this version
made. .pyrepl.lock names a pid that may still be running — check it with
`ps -p <pid> -o pid,command` before signalling, since a stale lockfile can name
a reused pid. .pyrepl.dashboard holds a dead dashboard's API token; it was only
gitignored where `repld init` ran, so check `git ls-files | grep pyrepl`.
Cleanup: https://angelsen.github.io/repld/docs/guides/upgrading/
""",
}


def _reference() -> str:
    """The always-loaded pointer block. Topic list derives from _TOPICS.

    It was a literal listing four topics by hand, which is a second copy of
    `sorted(_TOPICS)` in the one string every session loads — so adding a topic
    silently published a list that omitted it.
    """
    return (
        f"Reference: `repld help <topic>` — topics: {', '.join(sorted(_TOPICS))}\n"
        "Read repld://docs/guide for exec patterns and gist conventions. "
        "Read repld://docs/browser for the full browser API and internals.\n"
        "Read repld://docs/production when graduating gists to FastMCP/FastAPI."
    )


# `== Builtins ==` below recaps _EXEC_MODEL's builtins — keep in sync.
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

Fanning out with asyncio.gather()/create_task() inside defer() needs one
more step: those reach for a running loop when *built*, not just when
awaited, and a sync cell (no top-level await) runs in a worker thread with
no loop of its own. Pass defer() a zero-arg callable instead of a
pre-built awaitable, so the gather() call happens on the kernel's loop:

  defer(lambda: asyncio.gather(one("a"), one("b"), return_exceptions=True))

This returns the task_id immediately. The channel notification arrives
when the coroutine completes (or fails).

=== Top-level await ===

Top-level await is supported. No need to wrap in async def:

  data = await tab.fetch("/api/data")
  import asyncio
  result = await asyncio.gather(fetch_a(), fetch_b())

_ / _N history works — _ is the last expression, _1, _2, etc. for earlier.

== Project context ==

When repld runs in a project directory, exec has access to everything in
the project environment — your app modules, ORM models, config, database
sessions, API clients.

A globally-installed repld (uv tool install repld-tool, the recommended
setup) still sees them: entry points that run a kernel re-exec under
./.venv's interpreter first, so the kernel is bound to the project's Python
rather than to repld's own. Nothing needs to be added to the project's
dependencies. The one hard requirement is that ./.venv match repld's minor
version — a cross-version venv is refused outright rather than spliced in
half, and imports from it then fail with an error saying so.

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

== Live introspection with repld_init.py ==

A repld_init.py in the project root runs at kernel startup, then the kernel
stays alive. If it starts a server, worker, or any long-running process,
that process lives inside __main__ — and exec can reach into it at any time
without restarting.

It is a file rather than a flag because the kernel that matters is usually
one nobody started by hand: the bridge spawns it when Claude Code first
calls a repld tool, and `repld restart` respawns it. Every one of those runs
repld_init.py, so the namespace is populated no matter who booted it.

It runs *after* the socket binds, so a slow bootstrap (a tunnel, a server
waiting on a port) doesn't stall the connection. Watch for the init_loaded
channel message before assuming __main__ is furnished; a bootstrap that
raises pushes init_error and leaves the kernel up so you can fix it.

This is a dev-time decision, not a production architecture. Your service
doesn't depend on repld — it just runs inside it during development so
you can inspect it live.

  # repld_init.py — boot your service inside the kernel
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
  every(seconds, delay=0)(fn)  periodic ticker; fn.cancel() stops it.
                               delay= defers the first tick — a watchdog
                               registered as a resource comes up would
                               otherwise check it at its most fragile
  no_display(value)            return value from a cell without auto-display
                                re-printing it (still binds _/_N)
  ask(prompt) / confirm(prompt) / choose(prompt, options)
                               block the cell on human input. Answered from
                               the kernel's pane, from a pinned tab's pill
                               (confirm/choose, via tab=), or with
                               `repld gate answer <id> <value>` — the last
                               is the only one a headless kernel has, and
                               headless is the usual case. Pass timeout=
                               for a gate that must not park a cell forever.

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
  # __repld_deps__ = ["path:vendor/lib"]  # local dir added to sys.path, not pip-installed
  __repld_usage__ = "app = await AppName.connect()"


  class AppName:
      \"""AppName — feature X, feature Y.\"""

      def __init__(self, tab) -> None:
          self._tab = tab

      @classmethod
      async def connect(cls) -> "AppName":
          \"""Find or open the app and return a ready instance.\"""
          import repld

          tab = await repld.browser.acquire(
              "*app.example.com*", open="https://app.example.com", ready="role=main"
          )
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
repld.browser, repld.notify, repld.defer, repld.every, repld.no_display. Module-level import
is auto-reload safe (attribute lookup on each call, not a frozen reference).
Exception: if this gist might be linked into another project (repld gist add)
and doesn't otherwise need a kernel to run, keep `import repld` local to the
one function that needs it instead — a module-level import makes the whole
file fail outside a kernel, even for a caller who only wanted the rest of it.
`repld gist add` warns when it links a file shaped that way.

Async by default. All methods async def, use await tab.fetch(). Async gists
yield to the event loop — browser stays responsive, multiple gists can
interleave, no "loop blocked" warnings.

connect() classmethod. Finds or opens the app, returns a ready instance.
Pattern: browser.acquire(pattern, open=url, ready=selector) — ready= only
applies on the fresh open, so re-attaching an already-open tab stays cheap.
Check tab.ready_confirmed when the gist depends on real content being
loaded — False means it fell back to readyState alone; log rather than
proceed silently against a possibly-empty page.

tab.pin(reason) in connect(). Injects a floating pill UI + beforeunload
guard. Prevents accidental tab close. The pill also serves as a gate
surface for confirm/choose prompts. Pass guard_unload=False for a tab
driven against a live-reloading dev server — the guard fires on any
unload, same-origin included, so it otherwise blocks the framework's own
HMR reload behind a native confirm dialog nothing can dismiss.

Gate write operations. Anything that mutates state should call
tab.confirm(prompt) or tab.choose(prompt, options) first. The gate appears
in both the terminal and the pill UI — first resolution wins.

For apps that don't need browser auth (public APIs), use httpx (declare it
in __repld_deps__) or stdlib urllib. No browser tab needed. For apps that
do — once connect() has a tab, tab.http_client() hands back a plain
httpx.AsyncClient carrying its cookies, for concurrent calls with no
further browser round trips.

Normalize responses. Parse provider payloads into flat dicts with stable
keys (_parse_* module helpers) instead of returning raw API JSON — terse
output, stable downstream code, and a shape that fits in a docstring.

Module-level state resets on reload. Globals (clients, caches) re-initialize
when the gist auto-reloads; stale connections are not closed. Keep such
state disposable — lazy-init clients, caches that can rebuild.

Lint before calling it done. `repld gist lint [name...]` checks: the module
docstring's first line stands alone (it's what gets truncated into tool
listings and instructions); public methods returning dict/list document the
shape on the docstring's first line (`-> {key, ...}`); every non-stdlib
import is covered by `__repld_deps__`; no lingering `__repld_tools__` (an
ignored tool-registration list). Suppress a specific finding with
`# gistlint: ignore=<rule>` on the flagged line (or the line above).

=== Writing portable gists ===

When a gist might graduate to production (FastMCP, FastAPI), use the two-layer
pattern: core logic as pure async functions at the top of the file, repld
wiring (typed _tool_* functions) at the bottom. The core functions move
to production unchanged; the wiring gets replaced by @mcp.tool or @router.get.

For secrets, use os.environ["TOKEN"] — never hardcode. The kernel loads .env
from the project root at boot.

For browser-auth APIs, accept a fetch= callable parameter in the core function.
In repld, pass tab.fetch. In production, pass an httpx client or use token
auth instead. The core function doesn't care which path.

Read repld://docs/production for the full graduation guide with wiring
examples and scaffolding steps.

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
    from . import paths

    lock_path = paths.lock_path(cwd)
    state: dict = {
        "lock_exists": lock_path.exists(),
        "lock_alive": False,
    }
    if state["lock_exists"]:
        state["lock_alive"] = isinstance(read_lock(lock_path), dict)
    return state


def _suggestion(cwd: Path) -> str:
    """What to do next here, from what can actually be observed.

    Registration is deliberately not probed: `claude mcp add` records at local
    scope in the client's own config, not in a project file, and reaching into
    client internals to guess would be worse than saying nothing. A live kernel
    is observable, and it implies registration anyway.
    """
    s = _check_state(cwd)
    if s["lock_alive"]:
        return (
            "Kernel running for this project. Open Claude Code: `claude`\n"
            "  repld log -f    # watch what it's doing\n"
            "  repld status    # pid, uptime, dashboard\n"
        )
    # A stale lockfile is no longer something to clean up by hand: the flock
    # mutex settles ownership, and the next bridge overwrites it on spawn.
    # Both commands are equivalent as far as the project bootstrap goes —
    # repld_init.py runs either way, which is the point of it being a file
    # rather than an argument.
    return (
        "No kernel running. Either is fine:\n"
        "  claude   # the bridge starts a headless kernel for you\n"
        "  repld    # start one yourself, with the live display\n"
        "\n"
        "If `claude` doesn't see repld here, it isn't registered yet:\n"
        "  claude mcp add repld -- repld bridge\n"
    )


def _usage() -> str:
    """Usage for the argv checks, listing the topics that actually exist."""
    return (
        "repld help — agent/human docs\n"
        "\n"
        "  repld help [TOPIC]\n"
        "\n"
        f"  TOPIC   one of: {', '.join(sorted(_TOPICS))}\n"
        "\n"
        "  With no TOPIC, prints the overview.\n"
    )


def run_help(argv: list[str]) -> int:
    # Via cli_args like every other subcommand, rather than the `argv[0] in
    # (-h, --help)` this open-coded: that only ever looked at the first
    # argument, so `repld help gists --help` printed the gists topic instead of
    # usage — the exact case `wants_help`'s scan-every-argument rule exists for.
    if cli_args.wants_help(argv):
        print(OVERVIEW)
        return 0
    if not argv:
        print(OVERVIEW)
        print(_suggestion(Path.cwd()))
        return 0
    # No flags: `repld help` takes a topic and nothing else, so `--socket` (or
    # anything else borrowed from a sibling command) is refused rather than
    # silently ignored on the way to printing a topic.
    bad = cli_args.check_args("repld help", argv, _usage(), positionals=1)
    if bad is not None:
        return bad
    topic = argv[0]
    if topic not in _TOPICS:
        print(f"Unknown topic: {topic}")
        print(f"Topics: {', '.join(sorted(_TOPICS))}")
        return 2
    print(_TOPICS[topic])
    return 0
