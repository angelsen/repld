# repld[browser] — design

Design spec for the browser extra, written before it was built and kept for its
rationale — why the event store is DuckDB, why one WebSocket per port, why
`click` is a builtin rather than a `tab.js` call. It is **not** the API
reference: the shipped surface is `repld help browser` / `repld://docs/browser`
(and the [Browser API page](https://angelsen.github.io/repld/docs/reference/browser/)),
which are generated from and checked against the code. Where this document and
those disagree, those are right.

Sections that the implementation moved away from say so inline rather than
being edited into agreement, because the gap between what was planned and what
shipped is the part worth keeping.

## Premise

The browser is the one integration path that earns kernel-level support. Every other service (Slack, GitHub, Gmail, PowerOffice, internal apps) rides on top of a browser tab the user already has logged in — so if we make the browser a first-class substrate, we cover all of them without writing per-service MCPs.

The load-bearing primitive is the DevTools **Network tab** reconstructed over CDP events. Everything else — JS eval, console, cookies, screenshots — is small once the event pipeline is in place.

## Goals (v1)

1. **Network tab equivalent.** DuckDB HAR view over CDP events, including login redirects and WebSocket frames, with body capture via Fetch.
2. **`tab.js(...)`.** The agent's primary interaction surface. `Runtime.evaluate` with auto-await and exception unwrap.
3. **Console view.** `Runtime.consoleAPICalled` + `exceptionThrown` + `Log.entryAdded` collapsed into one queryable view.
4. **Query-first API.** `tab.network(...)` / `tab.console(...)` return rows, with raw SQL as the escape hatch beneath them. (Shipped as `tab.cdp(...)` instead — the SQL stayed internal; see the `Tab` section.)
5. **Async-native.** Lives on repld's shared asyncio loop. No threads, no Futures, no SSE coalescing.

## Non-goals (v1)

- Reactive decorators (`@on_request` etc.). Agents compose `@every` + `notify` + `tab.network(...)` — three lines, no new primitive. Revisit in v1.1 once usage patterns are visible.
- Request modification via Fetch (change headers/body/status). We peek for body capture and continue unmodified.
- SSE message decode, DOM/Overlay events, accessibility tree.
- Per-service helpers. Agent writes CDP-level code; we don't ship Slack/GitHub/Gmail wrappers.

## Architecture

```
Project cwd
 └─ kernel (asyncio loop, shared __main__)
     └─ browser module (lazy)
         └─ BrowserSession (one WS per Chrome debug port)
             ├─ session multiplexing (Target.attachToTarget, sessionId)
             └─ CDPSession per attached target
                 ├─ event stream → DuckDB (per-session, in-memory)
                 ├─ Fetch pause handler (body capture)
                 └─ Tab facade (user-facing API)
```

- **Extra**: `repld[browser]` pulls `websockets` + `duckdb` + `pillow`. Base install stays stdlib-only. All three are required and all three are imported eagerly — `png.py` pulls PIL at module scope for `Tab.screenshot`'s resize — so a two-of-three install reads as the extra being absent, not as a missing screenshot.
- **Lazy import.** `browser` doesn't load CDP until first `browser.get(...)` / `browser.watch(...)`. No cost for repld users who never touch it.
- **One WS per port.** Not one per tab. sessionId multiplexing keeps socket count bounded.
- **Per-session DuckDB.** Each tab has its own in-memory DB. Cross-tab queries are a deliberate non-goal; if needed, the agent unions manually.

## Core utilities (the real work)

### 1. BrowserSession — async port of `webtap/cdp/browser.py`

Single WebSocket to `/devtools/browser/<id>`, derived from `http://localhost:{port}/json/version`. Multiplexes multiple target attachments via sessionId. Owns:

- **Pending-command map**: `asyncio.Future` keyed by `(msg_id, session_id)` composite (same shape as ichrome's Listener, but on asyncio). Sessionless commands (e.g. `Target.getTargets`) key on `(msg_id, None)`.
- **Single `_recv_loop` task** on the shared loop. Reads messages, dispatches by shape:
  - `{"id": N, ...}` → resolve Future from pending map
  - `{"sessionId": X, "method": ...}` → route to `CDPSession._handle_event`
  - `{"method": ...}` (no sessionId) → browser-level event (targets, crashes)
- Session registry (sessionId → CDPSession) — explicit register/unregister (ichrome's `WeakValueDictionary` is slick but adds GC-order footguns; webtap's explicit pattern is clearer).
- Target-watch dict: **dual-key** (target_id + URL + opener + URL substring pattern). Needed because `Target.targetCreated` fires with empty URL for extension pages and popups; pattern/opener lookups fill the gap. Confirmed necessary — both webtap and ichrome hit this.
- `Target.setDiscoverTargets({discover: True})` on connect for lifecycle events.
- **Crash detection**: on `Inspector.detached` with reason `"Render process gone."`, unregister session and fire reattach via the matching watch entry. (Pattern from ichrome.)

**Changes from webtap:**
- `asyncio` throughout. `websockets` lib instead of `websocket-client`. No daemon thread, no `threading.Event`, no `Future.result(timeout=...)`.
- Drop `httpx` — use stdlib `urllib` for the one-shot `/json/version` fetch. Keeps the base-extra dep count minimal.
- Drop all lifecycle callback plumbing (`set_target_lifecycle_callbacks`, `_fire_callback`). Service-layer concerns from webtap that we don't need.
- Drop `_is_self_target` — Chrome-extension artifact.

### 2. CDPSession — async port of `webtap/cdp/session.py`

Per-attached-target. Receives routed events from BrowserSession, writes to DuckDB, dispatches registered handlers.

**Keep:**
- Store-as-is philosophy. One `events` table, JSON column, indexed on `(method, request_id, target)`.
- Surrogate sanitization regex (DuckDB JSON rejects lone `\uD800-\uDFFF` — webtap's regex is a real bug fix, port verbatim).
- Periodic FIFO prune at ~50k events.
- `has_body_capture()` / `fetch_body()` — lazy body fetch that checks captured store first, falls back to `Network.getResponseBody`.

**Drop:**
- DB worker thread. DuckDB isn't thread-safe, but we're single-writer on the asyncio loop — writes stay on the loop via an asyncio.Lock or just serial execution (DuckDB inserts are microseconds). If pruning gets heavy, offload to `asyncio.to_thread()`.
- `_trigger_state_broadcast`, `_STATE_AFFECTING_PREFIXES`, `set_broadcast_callback`. webtap's SSE coalescing for UI — our "broadcast" is already `notifications/claude/channel`.
- Sync `register_event_callback`/`unregister_event_callback` — if we add them at all, they're async.

### 3. Body capture via Fetch — the non-trivial one

Login flows are the canonical failure mode for `Network.getResponseBody`: Chrome evicts bodies on navigation commit, 302 intermediates get dropped, SW-intercepted responses miss the resource cache. Fetch is the only reliable path.

On attach, enable:
```python
await session.execute("Fetch.enable", {
    "patterns": [
        {"requestStage": "Request"},
        {"requestStage": "Response"},
    ],
    "handleAuthRequests": False,
})
```

Handler logic per `Fetch.requestPaused`:
- **Request stage.** If the request has a body (POST/PUT/PATCH), call `Fetch.getRequestPostData`, store as `Network.requestBodyCaptured` event (our synthetic), then `Fetch.continueRequest`. Full un-truncated body — `Network.requestWillBeSent.postData` caps at ~64KB.
- **Response stage.** Call `Fetch.getResponseBody`, store as `Network.responseBodyCaptured` with `{ok, error, elapsed_ms}` metadata, then `Fetch.continueRequest`. This is where login redirect bodies live.

**Cost:** ~5–15ms per request (one CDP round-trip added at each stage). Acceptable for dev-time; on by default; per-tab opt-out via `tab.capture_bodies = False`.

**Backpressure signal.** Track `paused_count` (incremented on `requestPaused`, decremented on `continueRequest`). Expose as `tab.capture_pending` so the agent can see when our handler is the bottleneck. *(Not built — no public counter exists. The pressure it would have measured is handled instead by `_should_capture_body` skipping assets outright, and by `_INFLIGHT_MAX_AGE` ageing out anything left open.)*

### 4. HAR view SQL — port `webtap/cdp/har.py` + fix + extend

webtap's view is ~90% of what we need. Port the shape verbatim, then:

**Fix the redirect bug.** webtap groups by `request_id` using `MAX(...)`, which collapses redirect chains into the final response only. Chrome reuses `requestId` across the chain; `Network.requestWillBeSent.redirectResponse` carries the *previous* response. Split each redirect into its own row keyed on `(request_id, redirect_index)`. Without this, "why did the login 302 to /error" is invisible — exactly the case we most need to debug.

**Add derived columns** (SQL-computed, queryable):
- `initiator_type, initiator_url, initiator_function, initiator_line` — flatten `initiator`; use `stack[0]` when type=script, URL+line when type=parser.
- `curl_command` — full reconstruction (method + URL + headers + `--data-binary` for bodies). SQL-computed so `WHERE curl_command LIKE '%authorization%'` works.
- `auth_scheme` — pattern-match `Authorization` → `bearer|basic|digest|null`.
- `auth_cookies` — cookie names from `Cookie` header (names only; values are sensitive).
- `csrf_token_header` — scan for `X-CSRF-*`, `X-XSRF-*`, `Authenticity-Token`, `X-Requested-With`.
- `mime_family` — bucket `mime_type` to `json|html|js|css|image|font|media|other`.
- `is_asset` — derived from `Sec-Fetch-Dest`. Default `tab.network()` filter strips assets.
- `loader_id`, `frame_id` — from `Page.frameNavigated` correlation. Enables "requests during this navigation" queries.

Two views exposed: `har_entries` (full detail) and `har_summary` (list-friendly subset, same as webtap).

### 5. Console view — new, ~30 lines SQL

```sql
CREATE VIEW console_entries AS
SELECT ...level, 'console' as source, text, stack_url, stack_line, stack_function, timestamp
  FROM events WHERE method = 'Runtime.consoleAPICalled'
UNION ALL
SELECT 'error' as level, 'exception' as source, text, ... FROM events WHERE method = 'Runtime.exceptionThrown'
UNION ALL
SELECT level, 'log' as source, text, url, lineNumber, null, timestamp
  FROM events WHERE method = 'Log.entryAdded'
```

One view, three sources. Columns: `level, source, text, stack_url, stack_line, stack_function, timestamp`.

### 6. JS eval — `tab.js(expr)`

Wraps `Runtime.evaluate` with:
- `awaitPromise="auto"` — detect Promise in return, set `awaitPromise: True` if so. Explicit `True`/`False` overrides.
- `returnByValue: True` by default (deep-serialize). Remote handles only when explicitly requested.
- `userGesture: True` by default so `.click()` etc. work without "user activation required" errors.
- Auto `Runtime.enable` on first use per session.
- Exception unwrap: `exceptionDetails` → Python `BrowserJSError(text, stack, url, line)` with preserved stack trace.

Cross-site iframes reach us as separate OOPIF targets (own `Tab` via `browser.watch`); same-site nested iframes can be reached via `document.querySelector('iframe').contentDocument.*` on the parent's session. No dedicated `context_id` kwarg in v1 — if someone needs same-site frame isolation, `tab.cdp("Runtime.evaluate", contextId=N, expression=...)` is the escape hatch.

```python
tab.js("document.title")                            # str
tab.js("await fetch('/api').then(r=>r.json())")     # dict (auto-await)
tab.js("window.__APOLLO_CLIENT__.cache.extract()")  # app internals
```

### 7. Target watching + attach

Port webtap's `_resolve_watched_target` logic (target_id → URL → opener → pattern). Four match paths are all load-bearing:
- **target_id**: direct handle.
- **URL**: ephemeral pages (chrome-extension://, about:blank+set).
- **opener**: popups opened by a watched tab (OAuth, SSO).
- **pattern**: URL substring match, re-attaches on nav to matching URL.

## Builtins

Everything the browser feature injects into `__main__`. Three levels: browser (discovery + watch), Tab (per-page interaction + query), Row (per-request detail).

### `browser` — discovery + watch management

```python
# watch patterns (persistent — auto-attach future matching tabs)
browser.watch("*gmail*")            # add pattern, attach current matches
browser.patterns                    # list active watch patterns
browser.detach("*gmail*")           # remove pattern, detach its tabs
browser.detach()                    # clear all patterns, detach everything

# resolve a handle
browser.get("*gmail*")              # → Tab  (glob — skips workers)
browser.get("9222:a81998")          # → Tab  (target ID — any type, attach on demand)

# inspect
browser.tabs                        # list[Tab] — currently attached tabs
browser.pages                       # list[TargetInfo] — ALL targets Chrome knows about

# config
browser.port = 9222                 # default from REPLD_CHROME_PORT env
```

`browser.tabs` is for display (`repld exec 'browser.tabs'`). Use `browser.get(pattern)` to get a stable handle. Index order is undefined and shifts on detach/reattach.

Two names in that block are drafts the implementation moved away from, and the
rest are async where this shows them sync — `watch`, `detach`, `get` and
`open` are all awaited. See the reference for the shipped forms.

- **`browser.pages`.** An `async` method returning plain dicts, not a property
  over a `TargetInfo` type — `await browser.pages()`. There is no
  `TargetInfo`; the draft's assumption that discovery needed its own value type
  didn't survive contact with `/json/list`, which already hands back the shape
  the agent wants. `browser.patterns` *is* a property, which is the asymmetry
  worth remembering.
- **`browser.port`.** Never existed on the pool. A port is a property of one
  Chrome instance, not of the multiplexer over N of them, so it lives on
  `Browser` and the pool exposes `browser.ports` (read-only) instead;
  `REPLD_CHROME_PORT` is still the default, read at `connect()` time. Assigning
  `browser.port` is the sharp edge: `LazyBrowser` has no `__setattr__` guard, so
  it silently binds an attribute on the descriptor that nothing ever reads.
  Pass the port to `browser.connect(port)`.

### `Tab` — per-page interaction + query

```python
# interaction
tab.js(expr, *, await_promise=True, user_gesture=True)
tab.click(selector, *, button="left", click_count=1)
tab.type_text(selector, text, *, delay_ms=0, press_enter=False)
tab.navigate(url)
tab.reload()
tab.screenshot(*, full_page=False, force=False, path=None) -> dict

# query (sync, DuckDB-backed; all return list[Row])
tab.network(url=None, method=None, status=None, type=None, since=None, include_assets=False)
tab.console(level=None, source=None, since=None)
tab.sse(url=None, event_name=None, since=None)
tab.lifecycle(name=None, since=None)
# since= is epoch seconds on every one of these — pass time.time().

# detail
tab.body(request_id) -> {body, base64Encoded[, capture]}
tab.request(request_id) -> dict (full HAR entry)
tab.cookies() -> list[dict]  (async; Network.getCookies, tab-scoped)

# config
tab.capture_bodies = True   # Fetch-based, captures login redirect bodies.
                            # True on get()/open() tabs, False on watch().

# escape hatch
tab.cdp(method, **params)
```

The shipped surface is much larger than this block — `fetch`, `key`, `tap`,
`swipe`, `scroll`, `tree`, `wait_for`, `pin` and the gate methods all arrived
after it was written. See the reference for those. What's worth recording here
is the four names in the original draft that did *not* ship:

- **`tab.ws` / `tab.ws_frames`.** WebSockets are in the HAR view — connection, handshake, close, and per-connection `frames_sent` / `frames_received` counts — so `tab.network()` already shows one and a dedicated method earned nothing. Individual frame *payloads* are the exception: they are stored as events, but with no public SQL surface nothing exposes them. That is a real gap rather than a decision. The two query methods that did earn their own signature, `tab.sse()` and `tab.lifecycle()`, are ones this draft never anticipated.
- **`preserve_log`.** Not a toggle. Events are always kept, bounded only by the FIFO prune at `MAX_EVENTS`, and cleared explicitly with `tab.clear()` / `browser.clear()`. They survive more than the draft assumed: `reattach_session` re-attaches *in place*, so the same `CDPSession` and the same DuckDB carry through navigation, HMR and a WebSocket reconnect alike. A fresh store appears only with a genuinely new target.
- **`tab.events.query(sql)` / `.subscribe()`.** The raw-SQL escape hatch is deliberately not public. `CDPSession.query` exists and takes a per-call cursor precisely so it can run off the loop, but exposing it would make the view schema a compatibility surface — and those views get reshaped for performance (the `ORDER BY` removal, the `har_summary` delta). `tab.cdp` is the escape hatch that shipped.
- **`capture_pending`.** No public counter. See the backpressure note under Fetch capture.

**Why `click` / `type_text` are builtins** (vs `tab.js`): the discovery loop — *trigger interaction → observe API call → synthesize client* — requires `event.isTrusted = true`. DOM `.click()` / `.value = 'x'` produce untrusted events that auth/CSRF-protected endpoints and debounced-input React components silently ignore. `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` produce real keyboard/mouse events. ~40 LOC combined, thin CDP wrappers.

### `Row` — per-request detail

Dataclass-like, not dict. Compact `repr`; attribute access for full fields; on-demand methods for body + curl.

```python
>>> r = browser.get("*app*").network(url="*login*")[0]
>>> r
<Request POST https://app/api/login → 302 (312ms, 2.1KB)>
>>> r.request_headers
{...}
>>> r.response_headers
{...}
>>> r.body()
'{"token": "..."}'
>>> r.curl()
"curl -X POST 'https://app/api/login' -H '...' --data-binary '...'"
>>> r.initiator_url
'https://app/static/login.js'
>>> r.auth_scheme
'bearer'
```

### `repld exec` — human CLI access to all builtins

```bash
$ repld exec 'browser.watch("*gmail*")'
→ watching "*gmail*" (1 tab attached)

$ repld exec 'browser.tabs'
target       type  title  url
9222:988492  page  Gmail  https://mail.google.com/...

$ repld exec 'browser.get("*gmail*").network(url="*search*")'
id  method  url                          status  time_ms  size
41  GET     /mail/u/0/s/?view=tl&...     200     142      8.2K

$ repld exec 'browser.get("*gmail*").body(41)'
{"threads": [...]}

$ repld exec 'browser.patterns'
["*gmail*"]

$ repld exec 'browser.detach()'
→ detached 1 tab, cleared 1 pattern
```

No args drops into interactive REPL (stdlib `code.InteractiveConsole` over IPC, `~/.repld/history`). Same builtins, prompt loop.

## File layout

Consumer-first — each module imports the ones below it:

```
src/repld/browser/
  __init__.py       — re-exports, nothing else
  pool.py           — BrowserPool over N Chrome instances + LazyBrowser descriptor
  browser.py        — Browser, one Chrome instance
  session.py        — BrowserSession (WS + sessionId multiplex)
  cdp.py            — CDPSession (event storage, dispatch, per-target DuckDB)
  tab.py            — Tab facade (JS/DOM)
  tab_query.py      — Tab's query surface (network/console/sse/lifecycle)
  row.py            — Row dataclass
  observe.py        — post-mutation observation pipeline
  capture.py        — Fetch.requestPaused handler + body-capture predicate
  har.py            — HAR view SQL + console view SQL
  selector.py       — Playwright-style selector resolution
  pin.py            — pill UI injection + label bar
  png.py            — screenshot resize / token grid (pulls PIL)
  target.py         — short-target-ID vocabulary + TabNotFoundError (leaf)
```

`target.py` is the leaf everything above may import; `__init__.py` is
re-exports only. Note what that means for tests: `BrowserPool` resolves
`Browser` through `pool.py`'s namespace, not the package re-export, so
patching `repld.browser.Browser` silently leaves the real class in play.

## Ported / improved / dropped — summary

**Ported from webtap (shape preserved):**
- BrowserSession single-WS multiplexing
- Dual-key target watching
- Store-as-is event schema with indexed `method`/`request_id`/`target`
- Surrogate-sanitization regex for DuckDB JSON
- FIFO prune at ~50k
- Lazy body fetch with capture-first fallback
- HAR view join topology (requestWillBeSent + ExtraInfo + responseReceived + ExtraInfo + loadingFinished/Failed + Fetch.requestPaused + bodyCaptured + WebSocket frames)

**Improved:**
- Async-native (`websockets` + asyncio loop, no threads, no Futures)
- Redirect chain: one row per hop (webtap collapses via `MAX` group-by)
- Derived columns: `curl_command`, `initiator_*`, `auth_scheme`, `auth_cookies`, `csrf_token_header`, `mime_family`, `is_asset`, `loader_id`, `frame_id`
- Exception unwrap on `tab.js` → typed `BrowserJSError`
- Auto-await detection on `Runtime.evaluate`
- Stdlib `urllib` replaces `httpx`

**Dropped:**
- DB worker thread (single writer on asyncio loop)
- SSE broadcast layer (`_trigger_state_broadcast`, `_STATE_AFFECTING_PREFIXES`, `set_broadcast_callback`)
- `_is_self_target` (Chrome-extension artifact)
- Target lifecycle callback plumbing (`set_target_lifecycle_callbacks`, `_fire_callback`)
- Fetch-based request modification (peek-and-continue only)
- Per-service helpers (agent writes CDP-level code)
- Reactive decorators (`@on_request` etc.) — deferred, compose `@every` + queries for now

## Implementation order

The build plan as written, kept as the record of it. All six landed, in this
order; the LOC estimates and the two names that didn't survive
(`tab.events.query`, `capture_pending`) are left as they were.

1. **BrowserSession + CDPSession core** — WS connect, sessionId multiplex, event → DuckDB. Plus target-watching. ~450 LOC. Smoketest: attach to Chrome, see events arrive in DB.
2. **HAR view + console view** — port + fix + extend. ~250 LOC SQL. Smoketest: browse to a site, query `har_entries`, verify redirects are separate rows.
3. **Tab facade** — `tab.js`, `tab.network`, `tab.console`, `tab.body`, `tab.cookies`, `tab.events.query`, `tab.cdp`. ~250 LOC. Smoketest: run JS, query filtered network.
4. **Fetch capture** — request + response stage handler, `capture_bodies` toggle, `capture_pending` counter. ~150 LOC. Smoketest: login flow, verify 302 body is captured.
5. **`browser` namespace + lazy injection** — kernel wires it into `__main__` on first access. ~50 LOC.
6. **Docs + smoketest phase 6.**

Total: ~1–2 days of focused work.

## Open questions

`since=` used to be listed here as undecided. It is decided: **epoch
seconds, on all four query methods**. Row_id was the cheaper option and is
what the observation pipeline uses internally (`pre_observe` cuts on
`MAX(rowid)`), but a caller cannot produce a row_id without first making a
query, whereas `time.time()` is always to hand. The cost is a per-view
conversion in `tab_query.py`, because the three source clocks disagree:
`har_summary.last_activity` is already epoch seconds, `Runtime.Timestamp`
(console) is epoch milliseconds, and `Network.MonotonicTime` (sse,
lifecycle) counts from an arbitrary origin and has to be offset against a
`requestWillBeSent` carrying both `wallTime` and `timestamp`. Until that
conversion existed each method compared the caller's number against its raw
column, so only `network()` behaved as documented — `console(since=...)`
matched every row and `sse`/`lifecycle` matched none.

The rest are decided too, and are kept with their answers rather than deleted:

- **`browser.get(...)` when multiple tabs match.** Returns the first match, in attach order — not alphabetical, and not an error. Tighten the pattern, or pass a `{port}:{6-hex}` target ID for an unambiguous handle.
- **Default `tab` when only one is attached.** Dropped. A module-level `tab` that delegates to "the" tab is wrong the moment a popup opens, and the failure is silent — the call goes to a real tab, just not the intended one. `browser.get(pattern)` is one line and says which.
- **Redirect entry ID.** Both, in the end. `redirect_index` is a `ROW_NUMBER()` over the hops of a `request_id`, so `(request_id, redirect_index)` reads meaningfully, while `rowid` stays available underneath for the SQL that needs a single monotonic key — `pre_observe` cuts its delta on exactly that.
- **DuckDB lifetime across navigation.** Answered in the `Tab` section above: the store belongs to the `CDPSession`, and reattach happens in place, so it survives navigation, HMR and reconnect. Only a new target starts a new store.
- **Cookie domain scoping.** Sidestepped rather than filtered. `tab.cookies()` calls `Network.getCookies`, which is already scoped to the tab's frames, so there was nothing to filter by registrable domain and no need for a `.all` escape hatch. `tab.cdp("Network.getAllCookies")` reaches the full jar for the rare caller who wants it.
