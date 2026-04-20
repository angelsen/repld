# Implementation Tasks: repld[browser]

**Status:** Complete
**Spec:** [requirements.md](./requirements.md) | [design.md](./design.md)

## Local Resources

| Resource | Path | Explore For Tasks |
|----------|------|-------------------|
| webtap CDP | `/home/fredrik/Projects/Python/project-summer/tap-tools/packages/webtap/src/webtap/cdp/` | **Task 1, 2, 3**: session.py, browser.py, har.py as port sources |
| webtap Fetch | `/home/fredrik/Projects/Python/project-summer/tap-tools/packages/webtap/src/webtap/services/fetch.py` | **Task 4**: Full Fetch handler flow |
| ichrome | `/home/fredrik/.local/share/resources/github.com/ClericPy/ichrome/tree/HEAD/ichrome/async_utils.py` | **Task 1**: _recv_daemon, Listener pattern |
| CDP schema | `/home/fredrik/.local/share/resources/github.com/ChromeDevTools/devtools-protocol/tree/HEAD/json/browser_protocol.json` | **Task 3, 5**: Event field names for HAR SQL + derived columns |
| repld protocol | `src/repld/protocol.py` | **Task 7**: TOOLS list shape, _tools_call dispatch |
| repld kernel | `src/repld/kernel.py` | **Task 8**: Builtin injection, shutdown hooks |

## Task Breakdown

### Task 1: BrowserSession — async WS + sessionId multiplexing
**Description:** Port webtap's `browser.py` to async. Single `websockets` connection to Chrome, `_recv_loop` task dispatching by message shape, pending-command Futures keyed by msg_id, target discovery via `Target.setDiscoverTargets`.

**Explore First:**
- webtap `browser.py` connect flow (lines 64-120)
- ichrome `_recv_daemon` (async_utils.py:3003-3082) for async dispatch pattern

**Files:**
- `src/repld/browser/session.py` — new file, ~200 LOC

**Implementation:**
- `connect()`: stdlib `urllib.request.urlopen` for `/json/version`, `websockets.connect()` for WS
- `_recv_loop()`: asyncio task, dispatch by `"id"` (command response → Future), `"sessionId"+"method"` (session event), `"method"` only (browser event)
- `execute(method, params, session_id, timeout)`: send JSON, await Future with `asyncio.wait_for`
- `attach(target_id)` → `Target.attachToTarget({targetId, flatten: True})`, return sessionId
- `detach(session_id)` → `Target.detachFromTarget`
- `list_targets()` → `Target.getTargets`
- Target watching: `_watched_patterns` dict, `_resolve_target(target_info)` with four-path resolution (target_id → URL → opener → pattern)
- Handle `Target.targetCreated`, `Target.targetDestroyed`, `Target.targetInfoChanged`, `Inspector.detached` in browser event handler

**Acceptance:**
- [ ] Connects to Chrome on port 9222, WS handshake succeeds
- [ ] `execute("Target.getTargets")` returns target list
- [ ] `attach(target_id)` returns sessionId, session receives events
- [ ] Pattern-based watch auto-attaches matching tabs
- [ ] Disconnect cleans up WS + tasks

**Dependencies:** None
**Complexity:** High

---

### Task 2: CDPSession — per-target DuckDB event store
**Description:** Port webtap's `session.py` to async. Per-target in-memory DuckDB, synchronous event inserts, indexed columns, surrogate sanitization, FIFO pruning.

**Explore First:**
- webtap `session.py` (lines 46-120) for schema + insert path
- webtap `_json_dumps_safe` (lines 37-43) for surrogate regex

**Files:**
- `src/repld/browser/cdp.py` — new file, ~250 LOC

**Implementation:**
- `__init__`: create `duckdb.connect(":memory:")`, create `events` table with `(event JSON, method VARCHAR, request_id VARCHAR, target VARCHAR)`, indexes on method + request_id
- `_json_dumps_safe(data)`: port webtap's surrogate regex verbatim
- `_handle_event(data)`: sync, called from BrowserSession recv loop. Insert into DuckDB, extract request_id, increment event count, prune at 50k
- `execute(method, params, timeout)`: delegate to BrowserSession with sessionId
- `query(sql, params)`: `db.execute(sql, params).fetchall()`
- `fetch_body(request_id)`: check `Network.responseBodyCaptured` in DB first, fall back to CDP `Network.getResponseBody`
- `cleanup()`: close DuckDB connection
- Domain enablement on creation: schedule `Page.enable`, `Network.enable`, `Runtime.enable`, `Log.enable`

**Acceptance:**
- [ ] Events from attached target stored in DuckDB
- [ ] `query("SELECT COUNT(*) FROM events")` returns correct count
- [ ] Surrogate sanitization doesn't corrupt valid JSON
- [ ] Prune fires at 50k, DB stays bounded
- [ ] `fetch_body` returns body for a completed request

**Dependencies:** Task 1
**Complexity:** Medium

---

### Task 3: HAR view + console view SQL
**Description:** Port webtap's `har.py` with redirect fix and derived columns. Add `console_entries` view.

**Explore First:**
- webtap `har.py` (full file) for CTE topology
- CDP schema `browser_protocol.json` for `initiator` field structure in `Network.requestWillBeSent`

**Files:**
- `src/repld/browser/har.py` — new file, ~250 LOC (mostly SQL)

**Implementation:**
- Port all 14 CTEs from webtap's `_HAR_ENTRIES_SQL`
- **Fix redirect bug:** Detect `redirectResponse` in `Network.requestWillBeSent` events. Instead of `GROUP BY request_id` with `MAX()`, emit separate rows. Each redirect hop gets its own entry with a `redirect_index` column. Use `ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY rowid)` to assign indices.
- Add derived columns computed in SQL:
  - `initiator_type/url/function/line`: extract from `$.params.initiator` JSON
  - `curl_command`: reconstruct from method + url + headers + body
  - `auth_scheme`: pattern-match `Authorization` header
  - `auth_cookies`: extract cookie names from `Cookie` header
  - `csrf_token_header`: scan for X-CSRF-*, X-XSRF-*, Authenticity-Token
  - `mime_family`: bucket mime_type to json|html|js|css|image|font|media|other
  - `is_asset`: derive from Sec-Fetch-Dest or resource_type
  - `loader_id`, `frame_id`: from requestWillBeSent params
- `console_entries` view: UNION of `Runtime.consoleAPICalled` + `Runtime.exceptionThrown` + `Log.entryAdded`
- `_create_views(db_execute)`: called from CDPSession init

**Acceptance:**
- [ ] `SELECT * FROM har_entries` returns correlated request/response pairs
- [ ] Redirect chains produce separate rows with correct status per hop
- [ ] `curl_command` column produces valid curl strings
- [ ] `console_entries` surfaces console.log + exceptions + browser logs
- [ ] WebSocket entries appear alongside HTTP entries

**Dependencies:** Task 2
**Complexity:** High

---

### Task 4: Fetch body capture handler
**Description:** Port webtap's `fetch.py` body capture to async. Enable Fetch on attach, handle requestPaused for both request and response stages.

**Explore First:**
- webtap `services/fetch.py` lines 111-239 for full handler flow
- Redirect skip logic (301/302/303/307/308), SSE detection

**Files:**
- `src/repld/browser/capture.py` — new file, ~150 LOC

**Implementation:**
- `enable(session)`: `Fetch.enable({patterns: [{urlPattern: "*", requestStage: "Request"}, {urlPattern: "*", requestStage: "Response"}]})` — called from CDPSession on domain init
- `handle_paused(session, params)`: async, called from `_handle_event` when method is `Fetch.requestPaused`
  - Detect stage: `params.get("responseStatusCode") is not None`
  - **Request stage:** extract postData from params, or `await Fetch.getRequestPostData`. Store as `Network.requestBodyCaptured`. `await Fetch.continueRequest({requestId})`.
  - **Response stage:** skip if redirect (3xx) or SSE (content-type check). `await Fetch.getResponseBody` with 5s timeout. Store as `Network.responseBodyCaptured` with `{ok, error, elapsed_ms}`. `await Fetch.continueResponse({requestId})` in finally.
- `paused_count` tracking: increment on pause, decrement on continue
- `tab.capture_bodies` toggle: when False, immediately continue without capture

**Acceptance:**
- [ ] POST body captured for form submissions
- [ ] Response body captured for API responses
- [ ] Login redirect (302) body captured
- [ ] SSE responses fast-continued (not blocked)
- [ ] Capture toggle disables without breaking page load
- [ ] `Fetch.continueRequest/Response` always called (no hung requests)

**Dependencies:** Task 2
**Complexity:** High

---

### Task 5: Tab facade + Row dataclass
**Description:** User-facing `Tab` class wrapping CDPSession, plus `Row` dataclass for query results.

**Files:**
- `src/repld/browser/tab.py` — new file, ~250 LOC

**Implementation:**
- `Tab.__init__(session, target_id, browser_session)`: hold references
- **Interaction methods (async):**
  - `js(expr)`: `Runtime.evaluate` with auto-await detection (check if result has `type: "object", subtype: "promise"` → retry with `awaitPromise: True`), `returnByValue: True`, `userGesture: True`. Exception unwrap: if `exceptionDetails` in result, raise `BrowserJSError(text, stack, url, line)`.
  - `click(selector)`: `Runtime.evaluate` to get element → `DOM.getContentQuads` or `Runtime.evaluate` for `getBoundingClientRect()` → compute center → `Input.dispatchMouseEvent` (mousePressed + mouseReleased).
  - `type_text(selector, text)`: focus element via `Runtime.evaluate("document.querySelector(sel).focus()")`, then per-char `Input.dispatchKeyEvent` (keyDown + keyUp) with optional delay.
  - `navigate(url)`: `Page.navigate({url})`.
  - `reload()`: `Page.reload`.
  - `screenshot()`: `Page.captureScreenshot` → decode base64 → bytes.
- **Query methods (sync, DuckDB):**
  - `network(**filters)`: build SQL WHERE clause from kwargs, query `har_summary`, return `list[Row]`.
  - `console(**filters)`: query `console_entries`, return `list[Row]`.
  - `body(request_id)`: delegate to `session.fetch_body`.
  - `request(request_id)`: query `har_entries` WHERE request_id, return single `Row`.
  - `cookies` property: `await Network.getAllCookies`, return dict.
- **Row dataclass:** `__repr__` as `<Request METHOD url → status (time_ms, size)>`. `.body()` calls back to session. `.curl()` returns the `curl_command` column value.

**Acceptance:**
- [ ] `tab.js("1+1")` returns `2`
- [ ] `tab.js("await fetch('/api').then(r=>r.json())")` auto-awaits and returns dict
- [ ] `tab.js("throw new Error('x')")` raises `BrowserJSError`
- [ ] `tab.click("#btn")` dispatches trusted click
- [ ] `tab.network(status=200)` returns filtered rows
- [ ] `Row.__repr__` is compact and readable
- [ ] `row.curl()` produces valid curl command

**Dependencies:** Task 2, Task 3
**Complexity:** Medium

---

### Task 6: Browser namespace + lazy init
**Description:** `Browser` class injected into `__main__` as lazy descriptor. Manages watch patterns, resolves `find()`, exposes `tabs`/`pages`/`patterns`.

**Files:**
- `src/repld/browser/__init__.py` — new file, ~100 LOC

**Implementation:**
- `LazyBrowser`: descriptor or module-level `__getattr__`. On first access, imports `Browser`, creates `BrowserSession`, connects, replaces self in `__main__.__dict__`.
- `Browser.__init__(port)`: create BrowserSession, connect, store reference.
- `attach(pattern)`: add to `session._watched_patterns`, call `session.list_targets()`, attach matching ones, return summary string. Push `tab_attached` channel events.
- `find(pattern)`: filter `self.tabs` by URL glob, error if 0 or >1, return Tab.
- `detach(pattern=None)`: if pattern, remove from patterns + detach matching tabs. If None, detach all + clear patterns. Push `tab_detached` channel events.
- `tabs` property: `list[Tab]` from session's registered CDPSessions.
- `pages` property: `session.list_targets()` → list of dicts.
- `patterns` property: `list(session._watched_patterns.keys())`.
- `port`: from `REPLD_CHROME_PORT` env or default 9222.

**Acceptance:**
- [ ] `browser.attach("*example*")` attaches matching tabs
- [ ] `browser.find("*example*")` returns single Tab
- [ ] `browser.find("*nonexistent*")` raises with clear message
- [ ] `browser.tabs` lists attached tabs
- [ ] `browser.pages` lists all Chrome targets
- [ ] `browser.detach()` cleans up everything
- [ ] Lazy init: no import cost until first access

**Dependencies:** Task 1, Task 5
**Complexity:** Medium

---

### Task 7: MCP tool registration + handlers
**Description:** Add 12 browser tool definitions to `TOOLS` list and dispatch handlers in `protocol.py`.

**Files:**
- `src/repld/protocol.py` — modify existing

**Implementation:**
- Add 12 tool dicts to `TOOLS` list following existing shape (`name`, `description`, `inputSchema` with `properties` + `required`).
- Add 12 `if name == "browser_*"` branches in `_tools_call()`.
- Each handler: extract args, get `browser` from `__main__.__dict__`, call appropriate method, format result as `{"content": [{"type": "text", "text": json.dumps(result)}]}`.
- Tools that need async (js, click, type_text, screenshot, cdp): use `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)` since dispatcher runs on IPC thread.
- Tools:
  - `browser_attach(pattern)` → `browser.attach(pattern)`
  - `browser_detach(pattern?)` → `browser.detach(pattern)`
  - `browser_tabs()` → `browser.tabs` serialized
  - `browser_pages()` → `browser.pages`
  - `browser_js(target, code, await_promise?)` → `browser.find(target).js(code)`
  - `browser_network(target, url?, method?, status?, type?)` → `browser.find(target).network(**filters)` serialized
  - `browser_body(target, request_id)` → `browser.find(target).body(request_id)`
  - `browser_click(target, selector)` → `browser.find(target).click(selector)`
  - `browser_type(target, selector, text)` → `browser.find(target).type_text(selector, text)`
  - `browser_console(target, level?)` → `browser.find(target).console(**filters)` serialized
  - `browser_screenshot(target)` → `browser.find(target).screenshot()` → base64
  - `browser_cdp(target, method, params?)` → `browser.find(target).cdp(method, **params)`

**Acceptance:**
- [ ] `tools/list` MCP response includes all 12 browser tools with schemas
- [ ] `browser_attach` via MCP attaches tabs
- [ ] `browser_js` via MCP returns eval result
- [ ] `browser_network` via MCP returns filtered entries
- [ ] Error responses for missing target, bad pattern, etc.

**Dependencies:** Task 6
**Complexity:** Medium

---

### Task 8: Kernel integration + lifecycle
**Description:** Wire browser into kernel startup/shutdown. Inject lazy builtin, register cleanup, add event types.

**Files:**
- `src/repld/kernel.py` — modify (2 lines: setattr + atexit)
- `src/repld/events.py` — modify (2 new dataclasses)
- `src/repld/display.py` — modify (2 new render branches)

**Implementation:**
- `kernel.py` line ~385: `setattr(__main__, "browser", LazyBrowser())`
- `kernel.py` line ~400: `atexit.register(_browser_cleanup)` where `_browser_cleanup` checks if browser was initialized and disconnects.
- `events.py`: add `BrowserTabAttached(target: str, url: str, title: str)` and `BrowserTabDetached(target: str)` dataclasses.
- `display.py`: render `BrowserTabAttached` as `[browser] attached {target} {title}` and `BrowserTabDetached` as `[browser] detached {target}`.

**Acceptance:**
- [ ] `browser` available in `__main__` after kernel start
- [ ] Attach/detach events render in kernel pane
- [ ] Kernel shutdown disconnects browser cleanly
- [ ] No import of browser module until first access (lazy)

**Dependencies:** Task 6
**Complexity:** Low

---

### Task 9: Smoketest phase 6
**Description:** Extend `tests/smoketest.py` with a browser phase. Requires Chrome running with `--remote-debugging-port=9222`.

**Files:**
- `tests/smoketest.py` — extend existing

**Implementation:**
- Phase 6: browser integration
  - Start Chrome with remote debugging (or skip phase if Chrome not available)
  - Via bridge MCP: `browser_attach("*")` to attach any open tab
  - `browser_tabs` → verify at least one tab attached
  - `browser_js(target, "1+1")` → verify returns 2
  - `browser_network(target)` → verify returns list (may be empty)
  - `browser_detach` → verify clean detach
  - `browser_tabs` → verify empty

**Acceptance:**
- [ ] Phase 6 passes with Chrome running
- [ ] Phase 6 skips gracefully without Chrome
- [ ] All existing phases still pass

**Dependencies:** Task 7, Task 8
**Complexity:** Low

---

## Task Dependencies

```
Task 1: BrowserSession (WS + multiplex)
  ↓
Task 2: CDPSession (DuckDB event store)
  ↓           ↓
Task 3: HAR   Task 4: Fetch capture
  ↓     ↓
Task 5: Tab + Row
  ↓
Task 6: Browser namespace + lazy init
  ↓           ↓
Task 7: MCP   Task 8: Kernel integration
  ↓           ↓
Task 9: Smoketest phase 6
```

## Parallel Tracks

- **Track A (tasks 1→2→3):** Core CDP pipeline: WS → events → HAR view
- **Track B (task 4):** Fetch capture — can run in parallel with Task 3 once Task 2 is done
- **Track C (tasks 5→6):** User-facing API — depends on both tracks completing
- **Track D (tasks 7+8):** Integration — can run in parallel once Task 6 is done
- **Task 9:** Verification — runs last
