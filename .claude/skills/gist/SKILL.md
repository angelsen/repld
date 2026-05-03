---
name: gist
description: Reverse-engineer a web app's API and write a callable Python gist. Use when you're looking at an app in the browser and want to make it programmable — any app, any framework, any auth model.
argument-hint: <name> [target or URL pattern]
---

# Gist

Make a web app callable from Python.

Input: `$ARGUMENTS` — a gist name (e.g. `shopify_sd`) and optionally a target ID or URL pattern to attach to.

## Mindset

You're turning a GUI into an API. The browser is your auth layer — the user is already logged in. Your job is to find what the app does over the network, verify you can replay it from `tab.fetch()`, then wrap it in a Python module.

**Don't follow a recipe.** Every app is different. A Shopify embedded app uses Remix loaders inside an iframe. Instagram uses a GraphQL API behind a custom header. A SaaS dashboard might be plain REST. Adapt to what you find.

**The loop is always:**
1. See what's there (tree, network, JS globals)
2. Trigger an action, capture what fires
3. Replay it from Python
4. Wrap it in a gist
5. Test, iterate, done

## Tools

```
# Discovery
browser.get(target, timeout=)     → Tab  (glob or target ID; timeout= polls for match)
browser.watch(pattern)            → str  (watch all matching, auto-attach new)
browser.tabs                      → list[Tab]
tab.tree()                        → accessibility tree (what's on screen)
tab.js(code)                      → run JS in page context

# Capture
tab.network(url=, since=)         → captured requests (DuckDB query)
tab.request(rid)                  → full headers, auth, postData
tab.body(rid)                     → response content
tab.console(level=, since=)       → console logs + exceptions

# Interact
tab.click(selector)               → trusted click (auto-waits 2s)
tab.type_text(selector, text)     → clear + type (auto-waits)
tab.wait_for(selector, timeout=5) → wait for element to appear
tab.navigate(url)                 → page navigation
tab.reload()                      → reload page
tab.clear()                       → reset network/console capture

# Replay
tab.fetch(url, method=, body=, headers=) → {status, ok, body}
```

`tab.fetch()` runs in the page context — inherits cookies, session tokens, App Bridge JWTs, CSRF tokens, everything. If the browser can do it, `tab.fetch()` can do it.

The kernel is persistent — variables survive across cells. Build understanding incrementally.

## Phase 1: Attach and orient

Find the app. Understand what you're looking at.

```python
tab = await browser.get("*example.com*")
await tab.tree()
```

Key questions:
- What page/view am I on?
- Is this an iframe inside a shell? (Shopify admin, Salesforce, Google Workspace)
- What framework? Check `__remixManifest`, `__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__`

```python
await tab.js("!!window.__remixManifest")   # Remix?
await tab.js("!!window.__NEXT_DATA__")     # Next.js?
```

### Embedded apps (iframes)

Many apps run inside a host page: Shopify apps inside admin, Salesforce Lightning components, Google Workspace add-ons. You'll see two tabs — the host and the iframe.

```python
# The host page (for navigation)
admin = await browser.get("*admin.shopify.com*search-and-discovery*")

# The embedded app iframe (for API calls)
iframe = await browser.get("*search-and-discovery.shopifyapps*", timeout=10)
```

**Rules:**
- Never `navigate()` an iframe directly — it breaks the embedding and kills the session.
- Navigate the **host** page. The iframe reloads automatically.
- Use the **iframe** tab for `tab.fetch()` and `tab.js()` — that's where the app's auth context lives.
- After host navigation, use `browser.get(pattern, timeout=10)` to wait for the iframe to reload.

## Phase 2: Capture traffic

Clear network, trigger an action, see what fires.

```python
tab.clear()
await tab.click('text=Filters')
await tab.wait_for('role=heading[name="Filters"]')  # wait for page to render
tab.network(url="*api*")
```

Or capture everything since a point in time:

```python
mark = tab.network()[-1].id if tab.network() else 0  # bookmark
await tab.click('text=Save')
tab.network(since=mark)  # only new requests
```

Look for:
- The **data request** — largest JSON response, not tracking/analytics pixels
- The **auth pattern** — Bearer token? Cookie? Custom header? CSRF?
- The **API shape** — REST? GraphQL? Remix `?_data=`? Next.js `/_next/data/`?

Inspect the interesting request:
```python
tab.request("rid")  # headers, auth scheme, postData, initiator
tab.body("rid")     # response content
```

## Phase 3: Replay from Python

Can you call the same endpoint from `tab.fetch()`?

```python
r = await tab.fetch("/api/endpoint", headers={"x-requested-with": "XMLHttpRequest"})
r["status"], r["body"]
```

If it works, you have the pattern. If not:
- **Missing headers?** Copy them from `tab.request(rid)` — especially CSRF tokens, custom auth headers.
- **Different domain?** The iframe's fetch context might differ from the host's. Use the right tab.
- **Client-side state required?** Try `tab.js()` to call the app's own functions directly.

```python
# When fetch doesn't work — call the app's internal API layer
await tab.js("window.__app.api.getFilters()")
```

## Phase 4: Map the API surface

One endpoint working. Now map the rest.

- What routes/endpoints exist?
- Which are reads vs writes?
- What parameters do they take?

**Remix/React Router:**
```python
routes = await tab.js("Object.keys(window.__remixManifest.routes)")
# Routes with hasAction=True support mutations (POST)
# Data loads: GET /path?_data=routes/route-id
# Mutations: POST /path?_data=routes/route-id with FormData
```

**Next.js:**
```python
data = await tab.js("JSON.parse(document.getElementById('__NEXT_DATA__').textContent)")
# API routes: /api/...
# Data fetching: /_next/data/{buildId}/page.json
```

**GraphQL:**
```python
await tab.fetch("/graphql", method="POST", body={"query": "{ __schema { types { name } } }"})
# If introspection disabled, capture queries from network traffic
```

**REST:** Clear network, click through the UI systematically. Each view loads its data. Catalog the endpoints.

**Unknown:** Look for JS globals, service workers, WebSocket connections. Every app has a data layer — find it.

## Phase 5: Write the gist

Create `gists/{name}.py`. The recommended pattern:

```python
"""AppName — what it does."""

__repld_usage__ = "app = await AppName.connect()"


class AppName:
    """AppName internal API — feature X, feature Y, feature Z."""

    def __init__(self, tab) -> None:
        self._tab = tab

    @classmethod
    async def connect(cls) -> "AppName":
        """Find or open the app and return a ready instance."""
        from __main__ import browser

        try:
            tab = await browser.get("*app.example.com*")
        except RuntimeError:
            tab = await browser.open("https://app.example.com")
            await tab.wait_for("role=main", timeout=10)
        return cls(tab)

    async def list_things(self) -> list[dict]:
        """List all things. -> [{id, name, status, created_at}]"""
        return (await self._tab.fetch("/api/things"))["body"]

    async def create_thing(self, name: str) -> dict:
        """Create a new thing. -> {id, name, status}"""
        return (await self._tab.fetch(
            "/api/things", method="POST", body={"name": name}
        ))["body"]
```

### Conventions

- **Async by default.** All methods `async def`, use `await tab.fetch()`. Async gists yield to the event loop — browser stays responsive, multiple gists can interleave. Sync gists work (auto-threaded) but can't interleave.
- **`connect()` classmethod.** Finds or opens the app, handles iframe discovery, returns a ready instance. One-line usage: `app = await AppName.connect()`. Import kernel builtins (`browser`, `notify`, `defer`, `every`) inside `connect()`, not at module top level — top-level imports break auto-reload and introspection.
- **`__repld_usage__`** — one line shown in the MCP instructions listing. Show the happy path, not the constructor.
- **Module docstring** — first line auto-discovered by repld for the gist listing.
- **Type hints + one-line docstrings** on public methods — auto-introspected when the agent imports the gist. Document return shapes inline: `"""Search things. -> [{id, name, status}]"""` so the agent knows dict keys without guessing.

### Pinning tabs and routing gates to the browser

For gists that use browser tabs for authenticated API access, call `tab.pin()` in `connect()` to inject a floating status pill that prevents accidental tab close and serves as a gate surface.

```python
@classmethod
async def connect(cls) -> "AppName":
    """Find or open the app and return a ready instance."""
    try:
        tab = await browser.get("*app.example.com*")
    except RuntimeError:
        tab = await browser.open("https://app.example.com")
        await tab.wait_for("role=main", timeout=10)
    await tab.pin("AppName — repld integration")  # ← inject pill + beforeunload
    return cls(tab)
```

For write operations that need human confirmation, use `self._tab.confirm()` or `self._tab.choose()` — these route the gate to the pill UI (amber pulsing dot, prompt + buttons) while also showing in the terminal. First resolution wins.

```python
async def post(self, text: str) -> dict:
    """Post something — gated on confirm."""
    ok = await self._tab.confirm(f"Post: "{text[:60]}"?")
    if not ok:
        raise RuntimeError("Cancelled")
    return await self._do_post(text)
```

`tab.ask()` is terminal-only (no pill UI for free-text input). `tab.pin()` is idempotent — calling it again updates the reason string.

### Multi-tab gists (embedded apps)

When the app lives in an iframe, hold both tabs:

```python
class SD:
    def __init__(self, admin_tab, iframe_tab) -> None:
        self._admin = admin_tab    # navigate here
        self._tab = iframe_tab     # fetch from here

    @classmethod
    async def connect(cls) -> "SD":
        try:
            admin = await browser.get("*admin.shopify*search-and-discovery*")
        except RuntimeError:
            admin = await browser.open(
                "https://admin.shopify.com/store/myshop/apps/search-and-discovery"
            )
        iframe = await browser.get("*search-and-discovery.shopifyapps*", timeout=10)
        return cls(admin, iframe)

    async def _navigate_to(self, path: str) -> None:
        """Navigate via admin tab, wait for iframe to reload."""
        base = self._admin.url.split("/apps/")[0]
        await self._admin.navigate(f"{base}/apps/search-and-discovery/{path}")
        self._tab = await browser.get(
            "*search-and-discovery.shopifyapps*", timeout=10
        )
```

### Watch/recipe methods

For list endpoints with a natural diff (inbox, feed, orders, notifications), add a `watch_*` method that polls and notifies on new items:

```python
    async def watch_inbox(self, on_new=None):
        """Poll inbox, notify on unread. Wire with @every(30)."""
        if on_new is None:
            from __main__ import notify
            on_new = notify
        if not hasattr(self, "_seen"):
            self._seen = set()
        for item in await self.list_items():
            if item["id"] not in self._seen and item["is_new"]:
                self._seen.add(item["id"])
                on_new(f"{item['title']}", kind="new_item")
```

## Phase 6: Verify

```python
import {name}  # auto-prints full API (class, methods, signatures)
app = await {name}.ClassName.connect()
await app.list_things()
```

If it breaks, edit the file and re-import — auto-reload gives you the fresh version:
```python
import {name}  # reloaded, API printed again
```

Iterate until all methods return real data.

## Auth patterns

| Pattern | How to handle |
|---------|--------------|
| Cookie-based session | `tab.fetch()` inherits cookies automatically |
| Bearer JWT (App Bridge) | `tab.fetch()` inherits — App Bridge intercepts fetch |
| Bearer JWT (manual) | Extract via `tab.js("getToken()")`, pass in headers |
| CSRF token in header | Copy from `tab.request(rid)` — look for `X-CSRF-Token`, `X-XSRF-TOKEN` |
| CSRF token in cookie | `tab.fetch()` inherits — framework JS reads it automatically |
| API key in page source | Extract via `tab.js("window.API_KEY")`, store in gist |
| OAuth stored elsewhere | Use `httpx.AsyncClient` with stored token — no tab needed |

If auth doesn't require the browser (stored OAuth tokens, API keys), the gist can use `httpx.AsyncClient` directly — no tab needed. The browser is just one auth strategy.

## When you're stuck

- **No API calls visible:** Data might be server-rendered. Check `__NEXT_DATA__`, `__remixManifest`, `window.__INITIAL_STATE__`, or `view-source:` for embedded JSON.
- **Click does nothing:** Embedded apps show dialogs on the **parent** frame ("Unsaved changes", "Leave page?"). Check the parent target's tree for `role=dialog` and dismiss it there.
- **Iframe not found after navigation:** Use `browser.get(pattern, timeout=10)` — the iframe takes time to load. Don't `asyncio.sleep()`.
- **Navigate kills the iframe:** Never `tab.navigate()` an iframe. Navigate the **host** page instead — the iframe reloads inside it.
- **CORS blocks `tab.fetch()`:** Copy headers from the captured request. Some APIs check `Origin`, `Referer`, or custom headers.
- **Auth expires mid-session:** The browser refreshes tokens automatically. `tab.fetch()` always gets fresh auth. If it stops working, the user's login expired — they need to re-authenticate in the browser.
- **Wrong gist version loaded:** Local `./gists/` shadows `~/.repld/gists/`. Check which file is active if behavior doesn't match the source you're editing.
- **Binary/WebSocket protocol:** Try monkey-patching transports via `tab.js()`, or intercept at the network level.
- **Can't find the right selector:** Use `tab.tree()` for the accessibility tree. Look for `role=`, `text=`, `label=` patterns rather than brittle CSS selectors.

## Output

A working gist at `gists/{name}.py` that:
1. Has a module docstring and `__repld_usage__` (for discovery)
2. Has a `connect()` classmethod (for one-line instantiation)
3. Uses `async def` methods with type hints and docstrings (for introspection)
4. All methods return real data when called
5. Uses `tab.fetch()` for browser-auth or `httpx.AsyncClient` for stored-token auth
