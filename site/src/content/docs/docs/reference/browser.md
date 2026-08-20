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
await browser.pages()                              # all Chrome targets
browser.patterns                                   # active watch patterns (property)
await browser.detach("*pattern*")                  # detach by pattern
await browser.detach()                             # detach everything
browser.clear(target=)                             # clear captured data

await browser.connect(9223)                        # add another Chrome instance
await browser.connect(profile="/path/to/profile")  # port from DevToolsActivePort
await browser.disconnect()                         # unpin tabs, close all WebSockets
await browser.disconnect(port=9222)                # unpin + close one Chrome instance
```

### ready= parameter

Stores a selector or a JS expression on the Tab. Used by `get()`, `open()`, `navigate()`, `reload()`, and session recovery after HMR. It's the one parameter that accepts either, so the shape decides:

- **Selector** → resolved and polled every 100ms. Anything starting with `.`, `#`, `[`, `data-`, `text=`, `role=` or `label=`, anything containing `:has-text(`, and any bare name (`main`, `my-app`) — the same set `click()` and `wait_for()` accept.
- **JS expression** → `Runtime.evaluate`, must return truthy. Anything else, which in practice means it has a dot or a call in it (`window.ready`, `app.isLoaded()`).
- **Default** (no `ready=`): waits for `document.readyState === 'complete'`.

The bare-name case is the one worth knowing: `ready="main"` and `ready="my-app"` are ordinary CSS, and are treated as such.

## Async methods

### js

```python
await tab.js(expr, *, await_promise=True, user_gesture=True) → Any
```

Evaluate JavaScript with REPL semantics. Top-level `await` works. Promise results are awaited by default. `let`/`const` can be redeclared across calls. Raises `BrowserJSError` on exceptions.

### click

```python
await tab.click(selector, *, button='left', click_count=1) → Receipt
```

Mouse click via `Input.dispatchMouseEvent`. Produces `isTrusted=true` events. Auto-waits up to 2s for the element, then for it to be visible, enabled and stable. Resolution is **strict**: a selector matching more than one element (with no single visible winner) raises with a candidate digest instead of guessing. The returned `Receipt` names what the click actually hit — `clicked: <button id="save">Save</button> — #save (412,133)`. When an unrelated element intercepts the point, a plain left-click switches to `element.click()` on the resolved target (a coordinate dispatch there can start a drag on a pan layer; the DOM click reaches the target the way assistive tech does — `isTrusted=false`, and the receipt says which path was taken). Modified clicks (right/double) keep the coordinate dispatch plus a warning.

### type_text

```python
await tab.type_text(selector, text, *, delay_ms=0, press_enter=False) → Receipt
```

Focus element, select-all, type character-by-character, then **verify the value landed**. If the keystrokes changed nothing — the signature of a framework-controlled input reverting them — it falls back to the native prototype value setter plus bubbled `input`/`change` events and re-verifies. The `Receipt` says which path the text took. Auto-waits + strict resolution like `click`.

### select_option

```python
await tab.select_option(selector, option) → Receipt
```

Select an option in a dropdown. Native `<select>`: finds the option by label (else value) and sets it via the prototype setter + `input`/`change` events. Custom widgets (react-select-style): clicks the field, waits for a `role=option` matching the name (exact, then substring), clicks it. A miss lists the visible options' accessible names.

### tap / swipe / scroll

```python
await tab.tap(selector_or_x, y=None) → Receipt
await tab.swipe(x1, y1, x2, y2, *, steps=10, duration_ms=300) → None
await tab.scroll(selector, dy=0, dx=0, *, steps=10, duration_ms=300) → None
```

Touch events for mobile Chrome via ADB. `scroll()` is sugar over `swipe()` —
resolves `selector` to its center and swipes the opposite direction
(scrollBy semantics: positive `dy` scrolls down, positive `dx` scrolls right).

### key

```python
await tab.key(key) → None
```

Dispatch a keyDown+keyUp pair for a named key (e.g. `"Enter"`, `"Escape"`,
`"Space"`). `"Enter"` and `"Space"` carry the produced character so Chromium's
native button/form activation fires — without it, the DOM keydown/keyup still
dispatch but a focused button silently doesn't click.

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

### close

```python
await tab.close() → None
```

Closes this tab (`Target.closeTarget`). Session cleanup follows from the resulting `Target.targetDestroyed` event, same as a user closing it.

### tree

```python
await tab.tree(mode="aria") → list[str]
```

Accessibility snapshot as text lines. Crosses iframes. The default `aria` mode is Playwright's LLM-oriented snapshot with `[ref=eN]` handles — each ref is usable as an `aria-ref=eN` selector in `click`/`type_text` until the next snapshot, navigation, or reattach. `mode="ax"` returns the raw CDP accessibility tree (pierces same-process iframes, no refs).

### screenshot

```python
await tab.screenshot(*, full_page=False, path=None) → dict
```

Always writes a PNG — to `path` if given, otherwise a 0600 file under `$XDG_RUNTIME_DIR/repld/`. Returns `{path, source: {width, height}, model: {width, height}, scale, bytes}`. The image is resized to the vision API's token grid; when `scale < 1`, multiply coordinates by `1/scale` to map back to page pixels.

### set_viewport

```python
await tab.set_viewport(width, height) → None
```

Emulate a fixed viewport at `deviceScaleFactor: 1` (`Emulation.setDeviceMetricsOverride`), so screenshot coordinates are page pixels with no multiplier math. `browser_open`'s `viewport="1440x900"` parameter calls this on open. Use a fresh tab per distinct size — re-overriding an already-overridden tab can leave `clientWidth` and `innerWidth` disagreeing.

### wait_for / wait_for_idle

```python
await tab.wait_for(selector, *, timeout=5.0) → None
await tab.wait_for_idle(*, timeout=5.0, quiet=0.5) → int  # settle ms
```

### pin / unpin / gates

```python
await tab.pin(reason='', guard_unload=True) → None  # guard_unload=False for live-reload dev servers
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

### controls / invoke

```python
await tab.controls() → dict | None
await tab.invoke(control, action, args=None) → dict
```

`controls()` calls the page's `window.controls.describeAll()`, returning the schema for every registered control — or `None` if the page exposes no `window.controls`. `invoke()` runs one action and returns `{returned, stateBefore, stateAfter, duration}`. See the [controls guide](/repld/docs/guides/controls/) for the protocol a page implements.

## Sync query methods (DuckDB-backed)

All four take `since=`, and on all four it is **epoch seconds** — pass `time.time()`. The three underlying CDP clocks (wall-time seconds, `Runtime.Timestamp` milliseconds, `Network.MonotonicTime` from an arbitrary origin) are converted for you.

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

| Property             | Type   | Description                                                   |
| -------------------- | ------ | ------------------------------------------------------------- |
| `tab.url`            | `str`  | Current URL (cached — use `tab.js("location.href")` for live) |
| `tab.title`          | `str`  | Page title (cached)                                           |
| `tab.type`           | `str`  | `"page"`, `"iframe"`, `"service_worker"`, etc.                |
| `tab.target_id`      | `str`  | Short ID in `{port}:{6-hex}` format                           |
| `tab.capture_bodies` | `bool` | Toggle Fetch body capture (True on get/open, False on watch)  |
| `tab.label`          | `str`  | Human-readable identifier                                     |

## Selectors

| Pattern                    | Type                            |
| -------------------------- | ------------------------------- |
| `.class`, `#id`, `[attr]`  | CSS                             |
| `[data-testid='name']`     | CSS                             |
| `text=Submit`              | Exact text match                |
| `role=button[name="Save"]` | ARIA role + accessible name     |
| `label=Username`           | Input by label                  |
| `placeholder=Search`       | Input by placeholder            |
| `testid=x`                 | `data-testid` shorthand         |
| `tag:has-text('OK')`       | CSS + text filter               |
| `aria-ref=e12`             | Ref from `tab.tree()` snapshot  |
| `getByRole('button', { name: 'OK' })` | Playwright locator call — strict-error suggestions paste as-is (also `getByTestId`/`getByText`/`getByLabel`/`getByPlaceholder`/`locator`) |

Every form resolves through a vendored build of Playwright's `InjectedScript` engine, evaluated once per document in an isolated world, and pierces open shadow roots. `role=` computes real implicit ARIA roles and accessible names (the W3C accname algorithm), so labels, `alt` text and `aria-labelledby` all resolve; hidden elements are excluded from `role=` matches.

Resolution is strict: zero matches auto-waits then errors; one match is used, visible or not (a lone off-screen control behind a styled proxy is still the target); multiple matches are filtered by visibility, and unless exactly one visible element remains the call raises with a candidate digest — a preview and generated selector for each — so a wrong-element click is impossible rather than diagnosable. Input methods then wait for the element to be visible, enabled and stable before dispatching.

`aria-ref=` refs come from the last `tab.tree()` / `browser_tree` snapshot and die on the next snapshot, navigation, or reattach; a dead ref errors immediately with a fresh-snapshot hint rather than polling.
