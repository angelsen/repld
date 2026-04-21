# Implementation Tasks: Mutations Return Observations + Gist Layer

**Status:** In Progress
**Spec:** [requirements.md](./requirements.md) | [design.md](./design.md)

## Task Breakdown

### Task 1: observe.py — tree builder + iframe composition
**Completed**

**Acceptance:**
- [x] `build_tree(tab)` returns compact text lines from a11y tree
- [x] `compose_tree(tab, session)` inlines iframe children, returns `(lines, child_tabs)`
- [x] Iframe matching: DOM `<iframe src>` → attached tab by URL, skips dead tabs
- [x] Role filtering: SKIP_ROLES, INTERESTING_ROLES, LEAF_ROLES sets
- [x] Node names truncated at 55 chars, props shown for checked/disabled/expanded/selected/pressed

---

### Task 2: observe.py — settle + observation bundle
**Completed**

**Acceptance:**
- [x] `pre_observe(tab, session)` returns `PreObservation` with iframe children, HAR max IDs, console counts
- [x] `post_observe(tab, session, pre, timeout, quiet, extra_header=)` settles then returns formatted text
- [x] Settle polls DuckDB for inflight requests across target + iframe children
- [x] Network delta uses HAR entry IDs (`WHERE id > snapshot`), tagged per target
- [x] Assets collapsed into summary line (`+ N assets (XKB)`)
- [x] Console delta tagged per target
- [x] Output format matches spec: url, tree, network, console sections

---

### Task 3: tab.py — new methods (tree, fetch, navigate, wait)
**Completed**

**Acceptance:**
- [x] `tab.tree()` returns `list[str]` via `observe.build_tree`
- [x] `tab.fetch(url, method=, body=, headers=)` builds JS fetch, runs via `tab.js()`, returns `{status, ok, body}`
- [x] `tab.navigate(url)` calls `Page.navigate` CDP command
- [x] `tab.wait(selector, timeout=, interval=)` polls querySelector, raises TimeoutError

---

### Task 4: Browser.open() + protocol wiring
**Completed**

**Acceptance:**
- [x] `Browser.open(url)` creates tab, attaches, returns Tab
- [x] `browser_open` new tool, returns observation text with `target:` header
- [x] `browser_click` returns observation text (not `{"result": "ok"}`)
- [x] `browser_type` returns observation text with debounce
- [x] `browser_navigate` new tool, returns observation text
- [x] `browser_key` new tool, returns observation text
- [x] `browser_tree` new tool, returns composed tree text
- [x] `browser_fetch` new tool, returns JSON response
- [x] `_browser_tool` detects string vs dict result for spill pipeline
- [x] TOOLS list has schemas for all 5 new tools

---

### Task 5: gists.py — auto-reloading import finder
**Completed**

**Acceptance:**
- [x] `~/.repld/gists/` and `./gists/` on `sys.path` at kernel startup
- [x] Directories created if they don't exist
- [x] `import gists.x` works for `.py` files in either directory
- [x] Re-importing after file edit loads the fresh version (mtime check via builtins.__import__ hook)
- [x] No new dependencies (stdlib `importlib` only)

**Notes:** Auto-reload implemented via `_GistImportHook` wrapping `builtins.__import__` (checks mtime before the standard cached-module path) plus `_GistFinder` on `sys.meta_path` for initial discovery.

---

### Task 6: help.py — update INSTRUCTIONS and topics
**Completed**

---

### Task 7: smoketest — extend with observation + gist tests
**Completed**

**Acceptance:**
- [x] Verify new tools (browser_navigate, browser_key, browser_open, browser_tree, browser_fetch) appear in tool list
- [x] Gist auto-reload: write file to gists dir → import → modify → re-import → verify fresh module
- [x] Also updated old phase_6 Chrome test to handle spilled browser_tabs/browser_network responses

---

## Task Dependencies

```
Task 1 (tree builder)     Task 5 (gists)
  ↓                          ↓
Task 2 (settle + observe) + Task 3 (tab methods)
  ↓
Task 4 (protocol + browser.open)
  ↓
Task 6 (help docs)
  ↓
Task 7 (smoketest)
```

## Parallel Tracks

- **Task 1 + Task 3 + Task 5** are all independent — can run in parallel
- **Task 6 + Task 7** are independent of each other, both depend on Task 4 + Task 5
