# Feature: repld[browser]

## Overview

CDP integration that gives repld authenticated access to any SaaS the user is logged into via their browser. Attach to Chrome tabs by URL pattern, capture all network traffic (including login redirect bodies) in DuckDB, eval JS, and expose the full surface as both kernel builtins and MCP tools. Combined with gists, this makes repld a standalone product for AI tooling against any web service — no API keys, no OAuth flows, no per-service MCP servers.

## Specification Heritage

- **Ports from:** webtap's CDP layer (`/home/fredrik/Projects/Python/project-summer/tap-tools/packages/webtap/src/webtap/cdp/`) — BrowserSession, CDPSession, HAR view SQL. Rewritten async-native.
- **References:** ichrome's flatten-mode Listener pattern, chrome-devtools-mcp's tool surface (as anti-pattern for tool count), CDP JSON schema from `ChromeDevTools/devtools-protocol`.
- **Design doc:** `docs/browser.md` in the repld repo captures the full brainstorm.

## What It Does

### Browser Discovery + Watch

- `browser.attach("*pattern*")` adds a persistent URL watch pattern. Currently-matching Chrome targets attach immediately; future tabs matching the pattern auto-attach on `Target.targetCreated`.
- `browser.find("*pattern*")` resolves one attached Tab by URL pattern. Errors if 0 or >1 match.
- `browser.tabs` lists currently attached tabs (display only — not a handle source).
- `browser.pages` lists all Chrome targets (attached or not).
- `browser.patterns` lists active watch patterns.
- `browser.detach("*pattern*")` removes a pattern and detaches its tabs. `browser.detach()` clears everything.
- Target matching uses dual-key resolution: target_id, URL exact match, opener match (for OAuth popups), URL substring pattern. All four paths are load-bearing.

### Per-Tab Interaction

- `tab.js(expr)` evaluates JavaScript via `Runtime.evaluate` with auto-await detection, `returnByValue: True`, `userGesture: True`, and exception unwrap to Python `BrowserJSError`.
- `tab.click(selector)` dispatches trusted mouse events via `Input.dispatchMouseEvent` (produces `event.isTrusted = true`).
- `tab.type_text(selector, text)` dispatches trusted keyboard events via `Input.dispatchKeyEvent` with proper keydown/keypress/input/keyup sequence.
- `tab.navigate(url)` and `tab.reload()` for page navigation.
- `tab.screenshot()` captures the page via `Page.captureScreenshot`.

### Network Capture + HAR View

- All CDP events stored as-is in per-session DuckDB (in-memory), indexed on `(method, request_id, target)`.
- `har_entries` SQL view joins `Network.requestWillBeSent` + `requestWillBeSentExtraInfo` + `responseReceived` + `responseReceivedExtraInfo` + `loadingFinished` + `loadingFailed` + `Fetch.requestPaused` + body-capture events + WebSocket lifecycle events.
- Redirect chains produce one row per hop (fixes webtap's `MAX() GROUP BY request_id` bug that collapsed redirects).
- Derived columns computed in SQL: `curl_command`, `initiator_type/url/function/line`, `auth_scheme`, `auth_cookies`, `csrf_token_header`, `mime_family`, `is_asset`, `loader_id`, `frame_id`.
- `tab.network(url=, method=, status=, type=, since=, include_assets=False)` queries the view, returns `list[Row]`.
- `tab.body(request_id)` fetches response body — checks captured store first, falls back to `Network.getResponseBody`.
- `tab.request(request_id)` returns full HAR entry as a `Row`.

### Body Capture via Fetch

- Fetch interception enabled on attach: `Fetch.enable({patterns: [{requestStage: "Request"}, {requestStage: "Response"}]})`.
- Request stage: capture full un-truncated POST body via `Fetch.getRequestPostData`, store as synthetic `Network.requestBodyCaptured` event, then `Fetch.continueRequest`.
- Response stage: capture body via `Fetch.getResponseBody`, store as `Network.responseBodyCaptured` with `{ok, error, elapsed_ms}` metadata, then `Fetch.continueRequest`.
- On by default. Per-tab opt-out via `tab.capture_bodies = False`.
- No request/response modification — peek and continue only.

### Console View

- `console_entries` SQL view collapses `Runtime.consoleAPICalled` + `Runtime.exceptionThrown` + `Log.entryAdded` into one queryable surface.
- `tab.console(level=, source=, since=)` queries it, returns `list[Row]`.

### MCP Tools

- 12 browser-specific MCP tools exposed alongside `exec`/`get_task`/`cancel`: `browser_attach`, `browser_detach`, `browser_tabs`, `browser_pages`, `browser_js`, `browser_network`, `browser_body`, `browser_click`, `browser_type`, `browser_console`, `browser_screenshot`, `browser_cdp`.
- Each tool takes a `target` parameter (e.g. `9222:abc123`) for per-tab operations.
- Tools call kernel builtins internally — one implementation, two surfaces.

### Row Dataclass

- Compact `repr`: `<Request POST https://app/api/login -> 302 (312ms, 2.1KB)>`.
- Attribute access for all HAR fields.
- `.body()` method for on-demand body fetch.
- `.curl()` method for curl command reconstruction.

### Escape Hatches

- `tab.events.query(sql, params=None)` — raw DuckDB SQL.
- `tab.cdp(method, **params)` — raw CDP passthrough.

## Constraints

- **Optional extra.** `repld[browser]` pulls `websockets` and `duckdb`. Base repld install stays stdlib-only.
- **Async-native.** All CDP communication on repld's shared asyncio loop via `websockets` lib. No threads for WS (webtap's thread-based approach is replaced). DuckDB writes stay synchronous on the event handler path (microsecond inserts); pruning offloaded to `asyncio.to_thread()` if needed.
- **Single WS per Chrome port.** BrowserSession multiplexes via sessionId. Not one socket per tab.
- **Per-session DuckDB.** Each Tab has its own in-memory DB. No cross-tab queries.
- **Lazy import.** `browser` doesn't load CDP until first `browser.find()` / `browser.attach()`. No cost for non-browser users.
- **Localhost only.** Chrome must be running with `--remote-debugging-port=9222`. Default port configurable via `REPLD_CHROME_PORT` env var.
- **Surrogate sanitization.** DuckDB JSON rejects lone `\uD800-\uDFFF` surrogates. Port webtap's regex sanitizer verbatim.
- **FIFO prune at ~50k events** per session to prevent unbounded growth.

## Out of Scope

- Reactive decorators (`@on_request`, `@on_console`). Compose `@every` + `notify` + queries instead. Revisit in v1.1 once usage patterns emerge.
- Request/response modification via Fetch. Peek-and-continue only.
- SSE message decode, DOM/Overlay events, accessibility tree.
- DevTools console bridge (`window.repld` injection). Dropped — `repld exec` covers the human-access story.
- TUI (`repld tui`). Deferred indefinitely.
- Gists layer (`~/.repld/gists/` + `./gists/`). Separate spec — ships alongside or after browser, but is independent.
- `repld exec` CLI subcommand. Separate spec — small enough to ship independently.
