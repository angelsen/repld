---
title: Browser guide
description: Attach to your logged-in Chrome, discover API surfaces, capture traffic.
---

repld's browser integration attaches to your real Chrome via CDP. No headless automation profile — you log in normally, and the agent sees your traffic.

## Prerequisites

Start Chrome with remote debugging:

```bash
google-chrome --remote-debugging-port=9222
```

Run the kernel via the `browser` subcommand — it re-execs under `uv run` with `duckdb`/`websockets` for this invocation, so browser tools work without adding anything to your project's dependencies:

```bash
repld browser
```

For a permanent global install instead:

```bash
uv tool install repld-tool[browser]
```

## Getting tabs

```python
tab = await browser.get("*example.com*")      # find by URL glob
tab = await browser.open("https://...")       # open new tab
await browser.watch("*pattern*")              # auto-attach matching tabs
```

`get()` returns a `Tab` object. The glob matches against the tab URL — `*` is a wildcard. If no tab matches, it raises `RuntimeError`.

```python
browser.tabs              # list of attached Tab objects
browser.pages()           # all Chrome targets (attached or not)
browser.detach()          # detach everything
```

## The observe pipeline

Every mutation — `click`, `type_text`, `navigate` — **settles** before returning, then reports what changed:

- **Accessibility tree** — the page's semantic structure
- **Network delta** — requests fired since the last observation
- **Console delta** — log messages and errors

This is what makes repld's browser different from Playwright: the agent sees exactly what its action changed, in one round-trip.

## Discovering APIs

The typical workflow: interact with a page, then inspect the traffic.

```python
tab = await browser.get("*dashboard.example.com*")
await tab.click("text=Export")

# what did that click do?
reqs = tab.network(url="*api*")
# → [<Request POST /api/exports → 201 (340ms, 1.2KB)>]

# inspect the request
entry = tab.request(reqs[0].request_id)
# → {request: {headers: {...}, postData: "..."}, response: {...}}

# get the response body
body = tab.body(reqs[0].request_id)
```

## In-page fetch

`tab.fetch()` runs a `fetch()` inside the browser — inheriting cookies, session, CORS origin:

```python
data = await tab.fetch("/api/accounts")
# → {"status": 200, "ok": True, "body": [...]}

await tab.fetch("/api/orders", method="POST", body={"status": "open"})
```

This is the bridge between browser-as-explorer and browser-as-API-client.

## Selectors

All interaction methods (`click`, `type_text`, `tap`, `wait_for`) share the same selector syntax:

| Pattern | Type | Notes |
|---------|------|-------|
| `.class`, `#id`, `[attr]` | CSS | Pure CDP, no JS eval, no focus steal |
| `[data-testid='name']` | CSS | Recommended for own code |
| `text=Submit` | Text | Visible text match |
| `role=button[name="Save"]` | ARIA | Role + accessible name |
| `label=Username` | Label | Input by associated label |

CSS selectors use `DOM.querySelector` — no JavaScript runs in the page. Custom selectors (`text=`, `role=`, `label=`) use `Runtime.evaluate`, which can trigger focus changes.

## Pin and gate

Guard a tab from accidental navigation:

```python
await tab.pin("admin session — don't close")
```

This injects a floating pill UI with a `beforeunload` guard. Gates route human decisions through the pill:

```python
ok = await tab.confirm("Delete all draft orders?")
choice = await tab.choose("Which environment?", ["staging", "production"])
```

## Console errors

Watched tabs push console errors and uncaught exceptions to the channel the instant they happen — no polling. Duplicate errors firing across tabs within 2 seconds collapse into one follow-up message. Mute a noisy pattern (a dev-server HMR warning, a third-party script) with `browser.suppress("substring")`; `browser.unsuppress(...)` un-mutes, `browser.suppressed` lists active patterns.

## Mobile viewport testing

CDP's `Emulation.setDeviceMetricsOverride` works for one-shot mobile screenshots, but reapplying a different override on the same tab can leave `document.documentElement.clientWidth` and `window.innerWidth` disagreeing — a state real browsers never produce. Prefer a fresh tab per distinct viewport size, and verify `clientWidth === innerWidth` before trusting the capture.

For definitive results, connect to a real device over ADB instead of emulating:

```bash
adb forward tcp:9333 localabstract:chrome_devtools_remote
```

```python
mobile = browser.connect(9333)
tab = mobile.tabs[0]
```

This sidesteps emulation entirely — touch events, viewport metrics, and screenshots all reflect the actual hardware.

## What's next

- [Browser reference](/repld/docs/reference/browser/) — full Tab API with every method and property
- [Gists guide](/repld/docs/guides/gists/) — turn browser patterns into reusable modules
