---
name: gist
description: Reverse-engineer a web app's API and write a callable Python gist. Use when you're looking at an app in the browser and want to make it programmable — any app, any framework, any auth model.
argument-hint: <name> [target or URL pattern]
---

# Gist

Make a web app callable from Python.

Input: `$ARGUMENTS` — a gist name (e.g. `shopify_sd`) and optionally a target ID or URL pattern to attach to.

## Mindset

You're turning a GUI into an API. The browser is your auth layer — the user is already logged in. Your job is to find what the app does over the network, verify you can call it from `tab.fetch()` or `tab.js()`, then wrap it in a Python module the agent can use forever.

**Don't follow a recipe.** Every app is different. A Shopify embedded app uses Remix loaders. A Salesforce dashboard uses Lightning API. Microsoft Teams uses a mix of REST and binary WebSocket. An internal tool might be plain REST. Adapt to what you find.

**The loop is always:**
1. See what's there (tree, network, globals)
2. Trigger an action, capture what fires
3. Replay it from Python
4. Wrap it in a gist
5. Test, iterate, done

## Tools at your disposal

```
browser.attach(pattern)       → connect to tabs
tab.tree()                    → accessibility tree (what's on screen)
tab.network(url=...)          → captured requests
tab.request(rid)              → headers, auth, postData
tab.body(rid)                 → response content
tab.fetch(url, method=, ...)  → call API with page's session
tab.js(code)                  → run JS in page context
tab.click(selector)           → trigger UI action
tab.clear()                   → reset network capture
```

The kernel is persistent — variables survive across cells. Build up your understanding incrementally.

## Phase 1: Attach and orient

Attach to the app. Read the tree. Understand what you're looking at.

```python
await browser.attach("*example.com*")
tab = browser.find("9222:...")
tab.tree()
```

Key questions:
- What page/view am I on?
- Is this an iframe inside a shell (Shopify, Salesforce)?
- What framework is this? (Check `__remixManifest`, `__NEXT_DATA__`, `__NUXT__`, React DevTools)

**Embedded app iframes:** Never `browser_navigate` an iframe directly — it kills the session. Navigate via parent frame links or in-app clicks. Use the iframe target for `tab.fetch()` and `tab.js()`.

## Phase 2: Capture traffic

Clear network, trigger an action (click a nav link, submit a form), see what fires.

```python
tab.clear()
await tab.click('a[href*="settings"]')
import asyncio; await asyncio.sleep(1)
tab.network()
```

Look for:
- The data request (largest JSON response, not tracking/analytics)
- The auth pattern (Bearer token? Cookie? Custom header?)
- The API shape (REST? GraphQL? Remix `?_data=`? Next.js `/_next/data/`?)

Inspect the interesting request:
```python
tab.request("rid")  # headers, auth
tab.body("rid")     # response content
```

## Phase 3: Replay from Python

Can you call the same endpoint from `tab.fetch()`?

```python
r = await tab.fetch("/api/endpoint", headers={"x-requested-with": "XMLHttpRequest"})
r["status"], r["body"]
```

`tab.fetch()` runs in the page context — it inherits cookies, session tokens, App Bridge JWTs, everything. If the browser can do it, `tab.fetch()` can do it.

If `tab.fetch()` doesn't work (CORS, binary protocol, client-side state), try `tab.js()`:
```python
await tab.js("window.someGlobal.sendMessage('hello')")
```

## Phase 4: Map the API surface

Once you have one endpoint working, map the full surface:
- What routes/endpoints exist?
- Which ones are reads vs writes?
- What parameters do they take?
- What do they return?

Framework-specific discovery:

**Remix/React Router:**
```python
await tab.js("Object.keys(window.__remixManifest.routes)")
# Routes with hasAction=True support mutations (POST)
```

**Next.js:**
```python
await tab.js("JSON.parse(document.getElementById('__NEXT_DATA__').textContent)")
```

**GraphQL:**
```python
# Check for schema introspection
await tab.fetch("/graphql", method="POST", body={"query": "{ __schema { types { name } } }"})
```

**Unknown:** Clear network, interact with different parts of the UI, catalog what fires.

## Phase 5: Write the gist

Create `gists/{name}.py`. Convention:

- **Module docstring** — one line describing the app (auto-discovered by repld)
- **Class if stateful** (holds a tab/client) — methods are the API
- **Functions if stateless** — each function is a standalone call

```python
"""AppName — what it does."""

class AppName:
    """Wrapper for AppName's internal API.

    Usage:
        app = AppName(tab)
        app.list_things()
    """

    def __init__(self, tab) -> None:
        self._tab = tab

    async def list_things(self) -> dict:
        """List all things."""
        return (await self._tab.fetch("/api/things"))["body"]
```

Type hints on public methods. One-line docstrings. These get auto-introspected by `repld://gists/{name}` for agent discovery.

**Watch/recipe methods:** For list endpoints with a natural diff (inbox, feed, orders, notifications), add a `watch_*` method that polls and calls `on_new=notify` for new items. The gist author knows what "new" means (the key field, the version/read_state check) — encode that once so agents don't rediscover it.

```python
    async def watch_inbox(self, on_new=None):
        """Poll inbox, notify on unread. Use with @every(30)."""
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

The agent wires it with one line: `@every(30) async def _(): await app.watch_inbox()`

## Phase 6: Verify

```python
import {name}
app = {name}.ClassName(tab)
await app.list_things()
```

If it breaks, edit the file and re-import — auto-reload handles the rest:
```python
import {name}  # fresh version loaded
```

Iterate until all methods return real data.

## Auth patterns you'll encounter

| Pattern | How to handle |
|---------|--------------|
| Cookie-based session | `tab.fetch()` inherits cookies automatically |
| Bearer JWT (App Bridge) | `tab.fetch()` inherits — App Bridge intercepts fetch |
| Bearer JWT (manual) | Extract via `tab.js("getToken()")`, pass in headers |
| CSRF token | Extract via `tab.js("document.cookie")` or DOM |
| API key in page source | Extract via `tab.js()`, store in gist |
| OAuth stored elsewhere | Use `httpx` with stored token instead of browser |

If auth doesn't require the browser (you have stored OAuth tokens, API keys), the gist can use `httpx` directly — no tab needed. The browser is just one auth strategy.

## When you're stuck

- **No API calls visible:** Check SSR payloads (`__NEXT_DATA__`, `__remixManifest`, `window.__INITIAL_STATE__`). The data might be baked into the page.
- **Click does nothing:** Embedded apps show confirmation dialogs on the parent frame (e.g. "Unsaved changes"). Check the parent target's tree for `role=dialog` and dismiss it there.
- **Binary/WebSocket:** Try `/instrument` techniques — monkey-patch transports, trace handlers.
- **CORS blocks `tab.fetch()`:** The API might need specific headers. Copy them from the captured request.
- **Auth expires:** The browser refreshes tokens automatically. If your gist calls `tab.fetch()` it always gets fresh auth.
- **Can't find the right selector:** Use `tab.tree()` to see the accessibility tree, find landmarks.

## Output

A working gist at `gists/{name}.py` that:
1. Has a module docstring (for discovery)
2. Has typed methods with docstrings (for introspection)
3. All methods return real data when called
4. Uses `tab.fetch()` for browser-auth or `httpx` for stored-token auth
