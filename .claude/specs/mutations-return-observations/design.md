# Design: Mutations Return Observations + Gist Layer

## Architecture Overview

Two additions:

1. **`src/repld/browser/observe.py`** — tree builder, settle loop, iframe discovery, observation bundle assembly. Tab gets thin convenience methods. Protocol dispatch changes mutation handlers to return observation text instead of `{"result": "ok"}`.

2. **`src/repld/gists.py`** — auto-reloading import finder. Kernel startup adds gist directories to `sys.path` and installs the finder on `sys.meta_path`.

```
protocol.py  _browser_dispatch("browser_click", ...)
    ↓
tab.click(selector)                    # existing — performs the action
    ↓
observe.settle_and_observe(tab, session)  # NEW — wait + build bundle
    ↓
protocol._spill_response(rid, text)    # existing — preview + spill
```

## Exploration Findings

- `_browser_dispatch` (protocol.py:486-570) handles 14 browser tools via if/elif chain. Click returns `{"result": "ok"}`, type returns `{"result": "ok"}`. All results go through `_browser_tool` → JSON serialize → `_spill_response`.
- `Tab.click()` (tab.py:330-369) dispatches mouse events via CDP `Input.dispatchMouseEvent`. Returns None.
- `Tab.type_text()` (tab.py:371-406) dispatches key events via CDP `Input.dispatchKeyEvent`. Returns None.
- `Tab.network()` (tab.py:421-457) queries `har_summary` view with dynamic WHERE. Returns `Rows` (list subclass with repr).
- DuckDB `har_entries` view has `id` (monotonic), `state` column (`complete`/`pending`/`loading`/`failed`), `method`, per-target tracking via `target` column.
- `_spill_response` (protocol.py:454-466) calls `_spill_text()` → head+tail preview → `[full output: path]`. Already handles arbitrary text.
- `BrowserSession._sessions` maps `sessionId` → `CDPSession`. Each CDPSession has its own DuckDB instance.

## Component Changes

### New: `src/repld/browser/observe.py`

All observation logic lives here. ~200 lines.

```python
# --- Tree ---

SKIP_ROLES: set[str]     # StaticText, InlineTextBox, generic, none, ...
INTERESTING_ROLES: set[str]  # button, link, heading, textbox, table, ...
LEAF_ROLES: set[str]     # button, link, textbox, checkbox, ... (don't recurse)

async def build_tree(tab: Tab, max_depth: int = 6) -> list[str]:
    """Compact accessibility tree from CDP Accessibility.getFullAXTree.
    Returns list of indented text lines."""

async def compose_tree(
    tab: Tab, session: BrowserSession, max_depth: int = 8
) -> tuple[list[str], list[Tab]]:
    """Build tree with iframe children inlined.
    Returns (lines, iframe_child_tabs).
    
    Discovery: tab.js() to get <iframe src/title> from DOM,
    match src URLs to session._sessions by netloc+hmac,
    verify liveness via tab.js("document.body.innerText.length"),
    inline child tree under Iframe node with '→ {target_id}' annotation.
    """

# --- Settle ---

async def settle(
    tabs: list[Tab],
    timeout: float = 5.0,
    quiet: float = 0.5,
) -> int:
    """Wait for network idle across all tabs.
    Polls DuckDB: state != 'complete' AND method != 'WS'.
    Returns settle time in ms."""

# --- Observation ---

@dataclass
class NetworkEntry:
    target: str   # e.g. "9222:d942d2"
    method: str
    status: int
    path: str     # URL path + truncated query
    time_ms: str
    size: str
    is_asset: bool

@dataclass  
class Observation:
    url: str
    settle_ms: int
    tree: list[str]
    network: list[NetworkEntry]
    console: list[str]   # "target  level: text"

def snapshot_har_ids(tabs: list[Tab]) -> dict[str, int]:
    """Record MAX(id) from har_entries for each tab's DuckDB."""

def network_delta(
    tabs: list[Tab], pre_ids: dict[str, int]
) -> list[NetworkEntry]:
    """Query each tab's DuckDB for entries with id > snapshot.
    Tag each entry with target_id. Classify is_asset."""

def console_delta(
    tabs: list[Tab], pre_counts: dict[str, int]
) -> list[str]:
    """Query each tab's console_entries for new entries."""

def format_observation(obs: Observation) -> str:
    """Render observation as plain text.
    
    API requests shown individually with target tag.
    Assets collapsed: '  + N assets (XKB)'.
    Console entries with target + level prefix.
    """

async def settle_and_observe(
    tab: Tab, session: BrowserSession,
    timeout: float = 5.0, quiet: float = 0.5,
) -> str:
    """Full pipeline: compose_tree → discover iframe children →
    snapshot HAR IDs → settle across all tabs → build observation → format.
    Returns formatted text string ready for _spill_response."""
```

### Modified: `src/repld/browser/tab.py`

Add 4 new methods to Tab class:

```python
async def tree(self) -> list[str]:
    """Compact accessibility tree as text lines.
    Standalone read — no settle, no observation bundle."""
    from .observe import build_tree
    return await build_tree(self)

async def fetch(
    self,
    url: str,
    *,
    method: str = "GET",
    body: dict | str | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    """In-page JS fetch with Python-ergonomic args.
    Returns {status: int, ok: bool, body: Any}.
    Body auto-parsed as JSON when content-type is json."""
    # Build JS fetch() call string from args
    # Run via self.js(code, await_promise=True)

async def navigate(self, url: str) -> None:
    """Page.navigate. Caller handles settle separately."""
    await self._session.execute("Page.navigate", {"url": url})

async def wait(
    self, selector: str, *, timeout: float = 10.0, interval: float = 0.25
) -> bool:
    """Poll until querySelector(selector) returns non-null.
    Raises TimeoutError if not found within timeout."""
```

### Modified: `src/repld/protocol.py`

**TOOLS list** — add 4 new tool schemas:

```python
{
    "name": "browser_navigate",
    "description": "Navigate a tab to a URL. Returns accessibility tree + network/console activity.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "url": {"type": "string"},
        },
        "required": ["target", "url"],
    },
},
{
    "name": "browser_key",
    "description": "Send a key press (Enter, Escape, Tab, etc). Returns tree + network/console delta.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "key": {"type": "string", "description": "Key name: Enter, Escape, Tab, ArrowDown, etc."},
        },
        "required": ["target", "key"],
    },
},
{
    "name": "browser_tree",
    "description": "Get the page's accessibility tree as compact text. Crosses iframe boundaries for attached child targets.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
        },
        "required": ["target"],
    },
},
{
    "name": "browser_fetch",
    "description": "Execute a fetch() in the page's context (inherits cookies/session). Returns {status, body}.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "url": {"type": "string"},
            "method": {"type": "string", "default": "GET"},
            "body": {"description": "Request body (dict for JSON, string for raw)"},
            "headers": {"type": "object", "description": "Additional headers"},
        },
        "required": ["target", "url"],
    },
},
```

**`_browser_dispatch` changes:**

```python
# NEW — mutations now return observation text (not JSON)
if name == "browser_click":
    tab = browser.find(args["target"])
    self._run_async(tab.click(args["selector"]))
    return self._run_async(
        settle_and_observe(tab, browser._session, timeout=5.0)
    )

if name == "browser_type":
    tab = browser.find(args["target"])
    self._run_async(tab.type_text(args["selector"], args["text"],
        press_enter=bool(args.get("press_enter", False))))
    import asyncio
    self._run_async(asyncio.sleep(0.3))  # debounce
    return self._run_async(
        settle_and_observe(tab, browser._session, timeout=5.0)
    )

if name == "browser_navigate":
    tab = browser.find(args["target"])
    self._run_async(tab.navigate(args["url"]))
    return self._run_async(
        settle_and_observe(tab, browser._session, timeout=8.0)
    )

if name == "browser_key":
    tab = browser.find(args["target"])
    key = args["key"]
    self._run_async(tab.cdp("Input.dispatchKeyEvent",
        type="keyDown", key=key, code=key))
    self._run_async(tab.cdp("Input.dispatchKeyEvent",
        type="keyUp", key=key, code=key))
    return self._run_async(
        settle_and_observe(tab, browser._session, timeout=5.0)
    )

# NEW — non-mutations
if name == "browser_tree":
    tab = browser.find(args["target"])
    lines, _ = self._run_async(compose_tree(tab, browser._session))
    return "\n".join(lines)

if name == "browser_fetch":
    tab = browser.find(args["target"])
    return self._run_async(tab.fetch(
        args["url"], method=args.get("method", "GET"),
        body=args.get("body"), headers=args.get("headers")))
```

**`_browser_tool` change:** Mutation handlers now return a string (observation text) instead of a dict. Detect this:

```python
def _browser_tool(self, rid, name: str, args: dict) -> dict:
    try:
        result = self._browser_dispatch(name, args)
        if isinstance(result, str):
            # Observation text — pass directly to spill pipeline
            return self._spill_response(rid, result, label=name)
        text = json.dumps(result, default=str, indent=2)
        return self._spill_response(rid, text, label=name)
    except Exception as exc:
        return _error(rid, -32000, f"{name}: {exc}")
```

### Modified: `src/repld/help.py`

Update INSTRUCTIONS to mention that click/type/navigate return page state. Add `tree` and `fetch` to the tool list in the MCP instructions string.

## Data Flow

```
Agent calls browser_click(target, selector)
    ↓
protocol._browser_dispatch:
  1. tab.click(selector)              ← perform the action
  2. settle_and_observe(tab, session) ← build observation
    ↓
observe.settle_and_observe:
  a. compose_tree(tab, session)       ← tree + discover iframe children
  b. snapshot_har_ids(all_tabs)       ← record pre-action HAR max IDs  
     (note: snapshot taken AFTER compose_tree since tree fetch is fast
      and the action already happened — we want to capture network
      that fired from the click, which may already be done)
  c. settle(all_tabs, timeout, quiet) ← wait for network idle
  d. network_delta(all_tabs, pre_ids) ← query new HAR entries
  e. console_delta(all_tabs, pre_cnt) ← query new console entries
  f. format_observation(...)          ← render as text
    ↓
protocol._spill_response(rid, text)   ← preview + spill if needed
    ↓
MCP response to agent
```

**Correction on snapshot timing:** The HAR snapshot must happen BEFORE the action, not after. The dispatch flow needs to be:

```python
if name == "browser_click":
    tab = browser.find(args["target"])
    # Snapshot BEFORE action
    pre = self._run_async(pre_observe(tab, browser._session))
    # Perform action
    self._run_async(tab.click(args["selector"]))
    # Settle and observe
    return self._run_async(
        post_observe(tab, browser._session, pre, timeout=5.0)
    )
```

Where `pre_observe` returns the composed tree + iframe children + HAR snapshots + console counts, and `post_observe` does the settle + delta computation + formatting.

## Method Signatures

### observe.py

```python
@dataclass
class PreObservation:
    """State captured before the mutation."""
    iframe_children: list[Tab]
    har_snapshots: dict[str, int]    # tab_key → MAX(id)
    console_counts: dict[str, int]   # tab_key → COUNT(*)

async def pre_observe(tab: Tab, session: BrowserSession) -> PreObservation:
    """Capture state before a mutation. Fast — no blocking."""

async def post_observe(
    tab: Tab,
    session: BrowserSession,
    pre: PreObservation,
    *,
    timeout: float = 5.0,
    quiet: float = 0.5,
    extra_header: str | None = None,
) -> str:
    """Settle, build tree, compute deltas, format. Returns observation text.
    extra_header is prepended (e.g. 'target: 9222:f52dfc' for browser_open)."""
```

### New: `src/repld/browser/__init__.py` — `Browser.open()`

```python
async def open(self, url: str) -> Tab:
    """Create a new tab and attach to it.
    Target.createTarget → attach → return Tab."""
    result = await self._session.execute("Target.createTarget", {"url": url})
    tid = result["targetId"]
    await self._session.attach(tid)
    return self.find(make_target(self._session.port, tid))
```

Protocol dispatch for `browser_open`:

```python
if name == "browser_open":
    tab = self._run_async(browser.open(args["url"]))
    pre = self._run_async(pre_observe(tab, browser._session))
    # No action needed — createTarget already navigated
    return self._run_async(
        post_observe(tab, browser._session, pre, timeout=8.0,
                     extra_header=f"target: {tab.target_id}")
    )
```

### New: `src/repld/gists.py`

Auto-reloading import finder for gist directories. ~30 lines, stdlib only.

```python
"""Auto-reloading import finder for ~/.repld/gists/ and ./gists/."""

import importlib
import importlib.abc
import importlib.machinery
import sys
from pathlib import Path


class _GistFinder(importlib.abc.MetaPathFinder):
    """Finder that checks gist directories and tracks mtimes for auto-reload."""

    def __init__(self, dirs: list[Path]) -> None:
        self._dirs = dirs
        self._mtimes: dict[str, float] = {}  # module name → last mtime

    def find_spec(self, fullname, path, target=None):
        # Only handle gists.* or top-level modules in gist dirs
        parts = fullname.split(".")
        for d in self._dirs:
            candidate = d / "/".join(parts)
            # Check package (dir with __init__.py) or module (.py)
            for p in [candidate / "__init__.py", candidate.with_suffix(".py")]:
                if p.is_file():
                    mtime = p.stat().st_mtime
                    prev = self._mtimes.get(fullname)
                    if prev is not None and mtime > prev:
                        # File changed — evict cached module so it reloads
                        sys.modules.pop(fullname, None)
                    self._mtimes[fullname] = mtime
                    # Delegate to default loader
                    return importlib.util.spec_from_file_location(
                        fullname, p,
                        submodule_search_locations=(
                            [str(candidate)] if p.name == "__init__.py" else None
                        ),
                    )
        return None


def install(dirs: list[Path]) -> None:
    """Add gist directories to sys.path and install the auto-reload finder."""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)
    sys.meta_path.insert(0, _GistFinder(dirs))
```

### Modified: `src/repld/kernel.py`

After `install_tee()` (line 438), before helper injection (line 440):

```python
    # 2b. Set up gist directories on sys.path with auto-reload.
    from . import gists
    gists.install([
        Path.home() / ".repld" / "gists",
        Path.cwd() / "gists",
    ])
```
