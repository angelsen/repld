# Design: repld[browser]

## Architecture Overview

New `src/repld/browser/` subpackage. Six modules, ~1000 LOC. Lazy-loaded on first `browser.attach()` / `browser.find()` call — no import cost for non-browser users.

```
kernel.py
  └─ setattr(__main__, "browser", LazyBrowser())
       └─ first access triggers import of repld.browser
            └─ BrowserSession (one WS per Chrome port)
                 └─ CDPSession per attached target (owns DuckDB)
                      └─ Tab facade (user-facing API)
                      └─ FetchHandler (body capture)

protocol.py
  └─ TOOLS list extended with 12 browser tool dicts
  └─ _tools_call() dispatches to _browser_* handlers
  └─ handlers call into browser module via __main__.__dict__["browser"]
```

## Exploration Findings

**Protocol integration (protocol.py):**
- `TOOLS` is a flat list of `{"name", "description", "inputSchema"}` dicts (lines 14-57). Add 12 more.
- `_tools_call()` is a simple if-chain on `params["name"]` (lines 116-125). Add 12 branches.
- Each handler returns `{"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}`.

**Kernel injection (kernel.py:382-388):**
- Builtins injected via `setattr(__main__, "browser", obj)`. Browser uses same pattern.
- Loop is `asyncio.new_event_loop()` on a daemon thread (line 374-376). Browser schedules work via `asyncio.run_coroutine_threadsafe(coro, loop)`.
- Shutdown: `atexit.register()` for cleanup. Signal handlers set `stop` event (lines 428-429).

**Channel push (kernel.py:94-108):**
- `push_channel(content, meta)` → `ipc.broadcast_channel(jsonrpc_msg)` + `events.emit(ChannelPush(...))`.
- Browser emits `tab_attached` / `tab_detached` via this path.

**Fetch handler (webtap services/fetch.py):**
- `Fetch.enable({patterns: [{urlPattern: "*", requestStage: "Request"}, {urlPattern: "*", requestStage: "Response"}]})` on attach.
- Request vs response detection: `params.get("responseStatusCode") is not None`.
- Request stage: extract `postData`, store as `Network.requestBodyCaptured`, call `Fetch.continueRequest`.
- Response stage: call `Fetch.getResponseBody` (5s timeout), store as `Network.responseBodyCaptured` with `{ok, error, source}` metadata, call `Fetch.continueResponse` in finally block.
- Skip redirects (301/302/303/307/308) — no body per CDP spec.
- Skip SSE (text/event-stream) — fast continue.
- webtap uses ThreadPoolExecutor because WS thread can't block. We don't need this — our handler is async on the loop.

**ichrome Listener (async_utils.py:3311-3328):**
- Composite key: `"id=N@sessionId"` for command responses, `"method=X@sessionId"` for events.
- `pop_future()` resolves the matching `asyncio.Future`.

**DuckDB threading:**
- NOT thread-safe per connection. But we're single-writer on the asyncio event handler (no threads). Synchronous inserts are microseconds. No worker thread needed.

## Component Changes

### New files: `src/repld/browser/`

**`__init__.py`** — public namespace + lazy bootstrap
- `LazyBrowser` descriptor injected into `__main__`. On first attribute access, imports the real `Browser` class, connects to Chrome, replaces itself.
- `Browser` class: owns `BrowserSession`, manages watch patterns, resolves `find()`.

**`session.py`** — async BrowserSession
- Single `websockets` connection to `ws://localhost:{port}/devtools/browser/{id}`.
- `_recv_loop()` asyncio task: dispatches by message shape (command response → Future, session event → CDPSession, browser event → target lifecycle).
- Pending commands: `dict[int, asyncio.Future]` keyed by msg_id. Session-scoped commands include `sessionId` in the wire message; Future lookup doesn't need composite key (msg_id is globally unique per WS).
- Target watching: `_watched_patterns: dict[str, set[str]]` maps glob pattern → set of target_ids matched. `_resolve_target(target_info)` checks target_id → URL → opener → pattern.
- `connect()`: `urllib.request.urlopen(f"http://localhost:{port}/json/version")` → extract `webSocketDebuggerUrl` → `websockets.connect()` → `Target.setDiscoverTargets({discover: True})`.
- `disconnect()`: close WS, cancel recv task.
- `attach(target_id)` → `Target.attachToTarget({targetId, flatten: True})` → returns sessionId → creates CDPSession.
- `execute(method, params, session_id=None)` → send, await Future.

**`cdp.py`** — CDPSession (per-target event store)
- Owns `duckdb.connect(":memory:")`. Synchronous writes on the event handler path.
- `events` table: `(event JSON, method VARCHAR, request_id VARCHAR, target VARCHAR)` with indexes on `method`, `request_id`.
- `_handle_event(data)`: insert into DuckDB, dispatch to FetchHandler if `Fetch.requestPaused`, track `_event_count`, prune at 50k.
- `_json_dumps_safe(data)` — surrogate sanitizer from webtap (regex, port verbatim).
- `query(sql, params)` → `db.execute(sql, params).fetchall()`.
- `fetch_body(request_id)` → check `Network.responseBodyCaptured` in DB first, fall back to `Network.getResponseBody` CDP call.
- Domain enablement on attach: `Page.enable`, `Network.enable`, `Runtime.enable`, `Log.enable`.
- Crash detection: on `Inspector.detached` with `"Render process gone."`, signal reattach.

**`capture.py`** — Fetch body capture handler
- `enable(session)`: `Fetch.enable({patterns: [{urlPattern: "*", requestStage: "Request"}, {urlPattern: "*", requestStage: "Response"}]})`.
- `handle_paused(session, params)`: async handler called from `cdp._handle_event`.
  - Detect stage: `params.get("responseStatusCode") is not None`.
  - **Request stage**: extract `postData` from params or call `Fetch.getRequestPostData`. Store as `Network.requestBodyCaptured`. Call `Fetch.continueRequest`.
  - **Response stage**: skip redirects (3xx) and SSE (`text/event-stream`). Call `Fetch.getResponseBody` (5s timeout). Store as `Network.responseBodyCaptured` with `{ok, error, elapsed_ms}`. Call `Fetch.continueResponse` in finally.
  - Track `paused_count` (increment on pause, decrement on continue/fail).

**`har.py`** — SQL view definitions
- `har_entries` view: port webtap's 14 CTEs. Fix redirect bug: instead of `GROUP BY request_id` with `MAX()`, detect `redirectResponse` in `Network.requestWillBeSent` and emit separate rows per hop with `redirect_index`.
- Add derived columns: `curl_command`, `initiator_type/url/function/line`, `auth_scheme`, `auth_cookies`, `csrf_token_header`, `mime_family`, `is_asset`, `loader_id`, `frame_id`.
- `har_summary` view: subset of `har_entries` for list display.
- `console_entries` view: union of `Runtime.consoleAPICalled` + `Runtime.exceptionThrown` + `Log.entryAdded` with columns `level, source, text, stack_url, stack_line, stack_function, timestamp`.

**`tab.py`** — Tab facade + Row dataclass
- `Tab`: wraps CDPSession. Methods delegate to session commands or DuckDB queries.
  - `js(expr, *, await_promise="auto", user_gesture=True)` → `Runtime.evaluate`.
  - `click(selector, *, button="left", click_count=1)` → resolve selector to coords via `Runtime.evaluate` + `DOM.getBoxModel`, then `Input.dispatchMouseEvent`.
  - `type_text(selector, text, *, delay_ms=0, press_enter=False)` → focus element via `Runtime.evaluate`, then `Input.dispatchKeyEvent` per character.
  - `network(**filters)` → SQL query against `har_summary` view, return `list[Row]`.
  - `console(**filters)` → SQL query against `console_entries` view.
  - `body(request_id)` → `session.fetch_body(request_id)`.
  - `cookies` property → `Network.getAllCookies` CDP call.
  - `screenshot(**kwargs)` → `Page.captureScreenshot`.
  - `events.query(sql, params)` → `session.query(sql, params)`.
  - `cdp(method, **params)` → `session.execute(method, params)`.
- `Row` dataclass: compact `__repr__`, attribute access, `.body()` and `.curl()` methods.

### Modified files

**`protocol.py`:**
- Add 12 tool dicts to `TOOLS` list.
- Add 12 `if name == "browser_*"` branches in `_tools_call()`.
- Each handler: extract args, call `__main__.__dict__["browser"].method(...)`, format result as MCP response.

**`kernel.py`:**
- Line ~385: `setattr(__main__, "browser", LazyBrowser())` — inject lazy browser.
- Line ~400: `atexit.register(browser_cleanup)` — disconnect BrowserSession on shutdown.

**`events.py`:**
- Add `BrowserTabAttached(target, url, title)` and `BrowserTabDetached(target)` event dataclasses.

**`display.py`:**
- Handle `BrowserTabAttached` / `BrowserTabDetached` in render loop (short log line).

**`pyproject.toml`:**
- Add `browser = ["websockets>=15.0", "duckdb>=1.0"]` to `[project.optional-dependencies]`.

**`cli.py`:**
- No change for browser. (`repld exec` is a separate spec.)

## Data Flow

```
Chrome (port 9222)
  │
  │  websockets (single WS, sessionId multiplex)
  ▼
BrowserSession._recv_loop()
  │
  ├─ command response (has "id") → resolve asyncio.Future
  │
  ├─ session event (has "sessionId" + "method")
  │   └─ CDPSession._handle_event(data)
  │       ├─ INSERT INTO events (event, method, request_id, target)
  │       ├─ if Fetch.requestPaused → FetchHandler.handle_paused()
  │       │   ├─ request stage: store body, continueRequest
  │       │   └─ response stage: getResponseBody, store, continueResponse
  │       └─ _event_count++ → prune at 50k
  │
  └─ browser event (has "method", no sessionId)
      ├─ Target.targetCreated → resolve watched → auto-attach
      ├─ Target.targetDestroyed → cleanup session
      ├─ Target.targetInfoChanged → URL now matches? attach
      └─ Inspector.detached "Render process gone" → reattach

Tab.network(url="*api*")
  └─ SELECT * FROM har_summary WHERE url LIKE '%api%'
      └─ list[Row]

Tab.js("document.title")
  └─ BrowserSession.execute("Runtime.evaluate", {expression, awaitPromise, ...}, sessionId)
      └─ send msg on WS → await Future → unwrap result or raise BrowserJSError
```

## Method Signatures

### BrowserSession

```python
class BrowserSession:
    def __init__(self, port: int = 9222): ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def execute(self, method: str, params: dict | None = None,
                      session_id: str | None = None, timeout: float = 30) -> dict: ...
    async def attach(self, target_id: str) -> CDPSession: ...
    async def detach(self, session_id: str) -> None: ...
    def add_pattern(self, pattern: str) -> list[str]: ...  # returns matched target_ids
    def remove_pattern(self, pattern: str) -> list[str]: ...  # returns detached target_ids
    async def list_targets(self) -> list[dict]: ...  # Target.getTargets
```

### CDPSession

```python
class CDPSession:
    def __init__(self, browser: BrowserSession, session_id: str,
                 target_info: dict, port: int): ...
    async def execute(self, method: str, params: dict | None = None,
                      timeout: float = 30) -> dict: ...
    def _handle_event(self, data: dict) -> None: ...  # sync, called from recv loop
    def query(self, sql: str, params: list | None = None) -> list: ...
    def fetch_body(self, request_id: str) -> dict: ...
    def clear_events(self) -> None: ...
    def cleanup(self) -> None: ...
```

### Tab

```python
class Tab:
    def __init__(self, session: CDPSession, target_id: str): ...
    async def js(self, expr: str, *, await_promise: str | bool = "auto",
                 user_gesture: bool = True) -> Any: ...
    async def click(self, selector: str, *, button: str = "left",
                    click_count: int = 1) -> None: ...
    async def type_text(self, selector: str, text: str, *,
                        delay_ms: int = 0, press_enter: bool = False) -> None: ...
    async def navigate(self, url: str) -> None: ...
    async def reload(self) -> None: ...
    async def screenshot(self, *, full_page: bool = False) -> bytes: ...
    def network(self, *, url: str | None = None, method: str | None = None,
                status: int | None = None, type: str | None = None,
                since: int | None = None, include_assets: bool = False) -> list[Row]: ...
    def console(self, *, level: str | None = None, source: str | None = None,
                since: int | None = None) -> list[Row]: ...
    def body(self, request_id: str | int) -> dict: ...
    def request(self, request_id: str | int) -> Row: ...
    @property
    def cookies(self) -> dict: ...
    capture_bodies: bool  # default True
    preserve_log: bool    # default True
```

### Browser (injected into __main__)

```python
class Browser:
    def attach(self, pattern: str) -> str: ...       # returns summary
    def find(self, pattern: str) -> Tab: ...          # errors if 0 or >1
    def detach(self, pattern: str | None = None) -> str: ...
    @property
    def tabs(self) -> list[Tab]: ...
    @property
    def pages(self) -> list[dict]: ...
    @property
    def patterns(self) -> list[str]: ...
    port: int  # default 9222, from REPLD_CHROME_PORT env
```

### Row

```python
@dataclass
class Row:
    id: int
    request_id: str
    method: str
    url: str
    status: int
    type: str
    size: int
    time_ms: int | None
    state: str
    request_headers: dict
    response_headers: dict
    initiator_url: str | None
    initiator_function: str | None
    auth_scheme: str | None
    mime_family: str
    # ... remaining HAR fields

    def body(self) -> str: ...
    def curl(self) -> str: ...
    def __repr__(self) -> str:
        return f"<Request {self.method} {self.url} → {self.status} ({self.time_ms}ms, {self.size})>"
```
