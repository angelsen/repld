# Feature: Mutations Return Observations + Gist Layer

## Overview

Two related features that complete the browser-driven development workflow:

1. **Mutations return observations** — Browser mutation tools (navigate, click, type, key, open) block until the page settles, then return a complete observation bundle: composed accessibility tree + network delta + console delta. One round trip gives the agent full understanding of what happened.

2. **Gist layer** — `~/.repld/gists/` and `./gists/` on `sys.path` at kernel startup, with an auto-reloading import finder so the agent can iteratively write/edit/test Python modules that wrap discovered APIs.

## What It Does

### Observation bundle

Every mutation tool returns the same text shape:

```
url: https://example.com/page

tree (48 nodes):
  main
    heading 'Filtre'
    Iframe 'Search & Discovery' → 9222:d942d2
      main
        heading 'Synonymer'
        link 'Opprett synonymgruppe'

network (3 requests):
  9222:27cccd  GET  200 /api/operations/ba471dda...  879ms 5.0KB
  9222:d942d2  GET  200 /filters?_data=routes/...   562ms 1.2KB
  + 21 assets (5KB)

console (1 message):
  9222:d942d2  warn: polaris deprecation...
```

### Composed tree

The accessibility tree crosses iframe boundaries. When the target tab contains `<iframe>` elements that match attached CDP targets, their trees are inlined:

- Parent tree shows `Iframe 'Name' → {target_id}` with child content nested below
- The `→ target_id` annotation tells the agent which target to use for interactions inside the iframe
- Matching: scan DOM for `<iframe src=...>` → match src URL to attached tabs → pick live tabs (non-empty body)
- One level of iframe nesting is sufficient

### Network and console deltas

- Deltas are scoped to the target tab **plus its iframe children** (not all attached tabs)
- Iframe children are discovered the same way as for tree composition: DOM `<iframe src>` matched to attached tabs
- Each line is tagged with the target it came from (e.g. `9222:d942d2  GET 200 ...`)
- Assets are collapsed into a summary line (`+ N assets (XKB)`) but API calls shown individually
- Tracking uses HAR entry IDs (not row count) for accuracy: snapshot `MAX(id)` before action, query `WHERE id > snapshot` after

### Settle heuristic

Mutations block until the page settles:

- Poll DuckDB for inflight requests: `state != 'complete' AND method != 'WS'` across target + iframe children
- Settled when no inflight requests for `quiet` duration (default 500ms)
- Max timeout as safety valve (default 5s for clicks, 8s for navigation)
- `type` adds debounce: wait 300ms after last keystroke before starting settle check
- `navigate` waits for `Page.loadEventFired` before starting the quiet check

### Mutation tools

| Tool | Action | Settle |
|---|---|---|
| `browser_navigate(target, url)` | `Page.navigate` | load event + network idle |
| `browser_click(target, selector)` | JS click | network idle |
| `browser_type(target, selector, text)` | CDP keystroke input | debounce + network idle |
| `browser_key(target, key)` | single key (Enter, Escape, Tab) | network idle |
| `browser_open(url)` | `Target.createTarget` + auto-attach | load event + network idle |

All return the observation bundle as a single text content item, using the existing spill pipeline when the tree exceeds preview budget. `browser_open` additionally includes `target: {id}` in the header so the agent knows the new tab's target ID.

### Non-mutation tools (unchanged or new)

| Tool | Returns |
|---|---|
| `browser_tree(target)` | Composed tree only (standalone read) |
| `browser_fetch(target, url, method=, body=, headers=)` | `{status, body}` — in-page JS fetch with Python-ergonomic args |
| `browser_screenshot(target)` | PNG (existing, unchanged) |
| `browser_network(target, ...)` | HAR query (existing, unchanged) |
| `browser_request(target, ...)` | Request detail (existing, unchanged) |

### Gist layer

Reusable Python modules that the agent writes to wrap discovered APIs. Two directories on `sys.path`:

- `~/.repld/gists/` — global, available in every project's kernel
- `./gists/` — project-local, versioned with the repo

Both added to `sys.path` at kernel startup (before any user code runs, before `--init`).

**Auto-reload finder:** A custom `sys.meta_path` finder/loader that tracks file mtimes. When `import gists.x` is called and the file has changed since last import, the module is reloaded transparently. This enables the iterate loop:

1. Agent writes `gists/shopify_sd.py`
2. Agent runs `import gists.shopify_sd` → module loaded
3. Agent tests, finds a bug, edits the file
4. Agent runs `import gists.shopify_sd` → auto-reload detects mtime change → fresh module

No `importlib.reload()` needed. The agent just re-imports.

### Spill integration

Follows the existing `_spill_response` pattern in protocol.py:

- Observation text assembled as a single string
- If within preview budget (4KB): return inline
- If exceeds: head+tail preview + `[full output: /path/to/spill.out]`
- Spill files at `$XDG_RUNTIME_DIR/repld/{pid}-{label}-{uuid}.out`

## Constraints

- All new code is stdlib-only (no new dependencies). The tree builder uses CDP `Accessibility.getFullAXTree` which is already available via the websockets dependency in `repld[browser]`.
- Observation bundle is plain text, not JSON. The agent reads it directly in context.
- Existing browser tools (`browser_js`, `browser_screenshot`, `browser_cdp`, `browser_network`, `browser_request`, `browser_body`, `browser_console`, `browser_attach`, `browser_detach`, `browser_tabs`, `browser_pages`, `browser_clear`) keep their current signatures and return shapes.

## Out of Scope

- `tab.auth()` — magic header extraction doesn't fit the substrate philosophy. Agent inspects headers via `tab.request()` directly.
- Tree diffing (return only what changed) — future optimization
- TOON format for results — plain text is sufficient for the data sizes involved
- Gist scaffolding/templating — the agent writes plain `.py` files, no generator
