---
title: Browser API
description: Full Tab API reference — every method, property, and query interface.
---

## Getting tabs

```python
tab = await browser.get("*example.com*")          # URL glob
tab = await browser.get("9222:a1b2c3")            # target ID
tab = await browser.get("*app*", fresh=True)       # only newly-appearing tabs
tab = await browser.get("*app*", timeout=10)       # wait up to 10s
tab = await browser.get("*app*", ready="#root")    # wait for element after attach
tab = await browser.open("https://...")            # open new tab
await browser.watch("*pattern*")                   # auto-attach current + future

browser.tabs                                       # list[Tab] attached
browser.pages()                                    # all Chrome targets
browser.patterns()                                 # active watch patterns
browser.detach("*pattern*")                        # detach by pattern
browser.detach()                                   # detach everything
browser.clear(target=)                             # clear captured data

await browser.connect(9223)                        # add another Chrome instance
await browser.connect(profile="/path/to/profile")  # port from DevToolsActivePort
browser.disconnect()                               # unpin tabs, close all WebSockets
browser.disconnect(port=9222)                      # unpin + close one Chrome instance
```

### ready= parameter

Stores a CSS selector or JS expression on the Tab. Used by `get()`, `open()`, `navigate()`, `reload()`, and session recovery after HMR.

- CSS selectors (starts with `.`, `#`, `[`, `data-`) → `DOM.querySelector`, polled every 100ms
- Everything else → `Runtime.evaluate`, must return truthy
- Default (no `ready=`): waits for `document.readyState === 'complete'`

## Async methods

### js

```python
await tab.js(expr, *, await_promise=True, user_gesture=True) → Any
```

Evaluate JavaScript with REPL semantics. Top-level `await` works. Promise results are awaited by default. `let`/`const` can be redeclared across calls. Raises `BrowserJSError` on exceptions.

### click

```python
await tab.click(selector, *, button='left', click_count=1) → None
```

Mouse click via `Input.dispatchMouseEvent`. Auto-waits up to 2s. Produces `isTrusted=true` events.

### type_text

```python
await tab.type_text(selector, text, *, delay_ms=0, press_enter=False) → None
```

Focus element, select-all, type character-by-character. Auto-waits up to 2s.

### tap / swipe

```python
await tab.tap(selector_or_x, y=None) → None
await tab.swipe(x1, y1, x2, y2, *, steps=10, duration_ms=300) → None
```

Touch events for mobile Chrome via ADB.

### key

```python
await tab.key(key) → None
```

Dispatch a keyDown+keyUp pair for a named key (e.g. `"Enter"`, `"Escape"`).

### fetch

```python
await tab.fetch(url, *, method='GET', body=None, headers=None) → dict
```

In-page `fetch()` — inherits cookies, session, CORS origin. Returns `{"status": int, "ok": bool, "body": Any}`. Body is auto-parsed as JSON when content-type includes `json`.

### navigate / reload

```python
await tab.navigate(url) → None
await tab.reload() → None
```

Both wait for the `ready` signal after page load.

### tree

```python
await tab.tree() → list[str]
```

Compact accessibility tree as text lines. Crosses iframes.

### screenshot

```python
await tab.screenshot(*, full_page=False, path=None) → bytes | Path
```

### wait_for / wait_for_idle

```python
await tab.wait_for(selector, *, timeout=5.0) → None
await tab.wait_for_idle(*, timeout=5.0, quiet=0.5) → int  # settle ms
```

### pin / unpin / gates

```python
await tab.pin(reason='') → None
await tab.unpin() → None
await tab.confirm(prompt) → bool
await tab.choose(prompt, options) → str
await tab.ask(prompt) → str
```

### cdp

```python
await tab.cdp(method, **params) → dict
```

Raw CDP passthrough.

### cookies

```python
await tab.cookies() → list[dict]
```

All cookies for this tab via `Network.getCookies`.

## Sync query methods (DuckDB-backed)

### network

```python
tab.network(url=, method=, status=, type=, since=, include_assets=False) → Rows
```

Query captured requests. `url` uses LIKE matching (`*` → `%`). Assets excluded by default. Max 500 rows, newest-first.

### console

```python
tab.console(level=, source=, since=) → Rows
```

Query console messages. Max 200 rows.

### sse

```python
tab.sse(url=, event_name=, since=) → Rows
```

Query SSE (EventSource) messages. Each row: `request_id`, `event_name`, `event_id`, `data`, `timestamp`.

### lifecycle

```python
tab.lifecycle(name=, since=) → Rows
```

Query `Page.lifecycleEvent` entries: `DOMContentLoaded`, `load`, `networkIdle`, etc.

### request / body

```python
tab.request(request_id) → dict    # full HAR entry (headers, timing, postData)
tab.body(request_id) → dict       # response body {"body": str, "base64Encoded": bool}
row.body() → dict                 # shortcut on any network Row
```

### clear

```python
tab.clear() → None
```

## Multi-browser

`browser.connect(port)` adds a Chrome instance to the pool — call it multiple times for multi-browser setups. Target IDs include the port prefix (`42829:abc123` vs `43213:def456`), so tab-scoped tools route to the right Chrome automatically.

```python
await browser.connect(42829)
await browser.connect(43213)
await browser.watch("*localhost:5200*")   # watches across both
browser.tabs                              # tabs from all instances
```

Connected ports and watch patterns persist across kernel restarts. On boot, repld prompts on the terminal (`[Y/n]`, default yes) before reconnecting and re-watching — headless boot (`--no-display`) or non-tty stdin skips the restore entirely.

The [dashboard](/repld/docs/guides/dashboard/)'s Connections tab gives you the same connect/watch/disconnect controls from a browser instead of `exec`.

## Console error push

Console errors and uncaught exceptions from watched tabs push as `[console:error]` channel messages the moment they happen — no polling:

```
[console:error] 9222:af5ae1: TypeError: Cannot read property 'x' of null
```

Cross-tab duplicates within 2 seconds are collapsed into one follow-up message (`... (×14 tabs)`). Mute noisy patterns:

```python
browser.suppress("[vite] failed to connect")   # mute matching errors
browser.unsuppress("[vite] failed to connect") # un-mute
browser.suppressed                             # list active patterns
```

Suppress patterns persist across kernel restarts.

## Properties

| Property | Type | Description |
|----------|------|-------------|
| `tab.url` | `str` | Current URL (cached — use `tab.js("location.href")` for live) |
| `tab.title` | `str` | Page title (cached) |
| `tab.type` | `str` | `"page"`, `"iframe"`, `"service_worker"`, etc. |
| `tab.target_id` | `str` | Short ID in `{port}:{6-hex}` format |
| `tab.capture_bodies` | `bool` | Toggle Fetch body capture (True on get/open, False on watch) |
| `tab.label` | `str` | Human-readable identifier |

## Selectors

| Pattern | Type | Focus-safe |
|---------|------|-----------|
| `.class`, `#id`, `[attr]` | CSS | Yes |
| `[data-testid='name']` | CSS | Yes |
| `text=Submit` | Text match | No |
| `role=button[name="Save"]` | ARIA | No |
| `label=Username` | Label | No |
| `tag:has-text('OK')` | CSS + text | No |
