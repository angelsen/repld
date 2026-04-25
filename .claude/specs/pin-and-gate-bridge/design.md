# Design: tab.pin() + browser gate bridge

## Architecture Overview

Three layers, each self-contained:

```
tab.pin(reason)  →  JS pill injected via Runtime.evaluate
tab.confirm()    →  gates._gate(tab=self)  →  pill shows buttons
button click     →  Runtime.bindingCalled  →  resolve_gate()
```

The pill is a JS/CSS blob stored as a Python constant. The gate bridge
uses Chrome's `Runtime.addBinding` to call back from JS to Python — the
same CDP event dispatch pattern as Fetch capture.

## Component Changes

### `src/repld/browser/tab.py` — Pin API + gate convenience methods

Add to Tab class:

```python
_PIN_JS: str = "..."  # module-level constant, the pill JS/CSS blob

class Tab:
    def __init__(self, ...):
        ...
        self._pinned: bool = False

    async def pin(self, reason: str = "") -> None:
        """Inject pill + beforeunload. Idempotent."""
        if not self._pinned:
            await self._setup_binding()
            await self.js(_PIN_JS)
            self._pinned = True
        if reason:
            await self.js(f"__repld_update({{reason: {json.dumps(reason)}}})")

    async def unpin(self) -> None:
        """Remove pill + beforeunload."""
        if self._pinned:
            await self.js("__repld_remove && __repld_remove()")
            self._pinned = False

    async def _setup_binding(self) -> None:
        """Register __repld_resolve binding for gate callbacks."""
        await self._session.execute(
            "Runtime.addBinding", {"name": "__repld_resolve"}
        )
        self._session._binding_handler = _handle_binding

    async def confirm(self, prompt: str, **kw) -> bool:
        """Gate routed to this tab's pill."""
        from ..gates import confirm
        return await confirm(prompt, tab=self, **kw)

    async def choose(self, prompt: str, options: list[str], **kw) -> str:
        """Gate routed to this tab's pill."""
        from ..gates import choose
        return await choose(prompt, options, tab=self, **kw)

    async def ask(self, prompt: str, **kw) -> str:
        """Gate routed to terminal (no pill UI for text input)."""
        from ..gates import ask
        return await ask(prompt, **kw)

    async def _show_gate(self, gate_id: str, kind: str, prompt: str, options) -> None:
        """Present a gate in this tab's pin UI. Tab owns the rendering."""
        buttons = []
        if kind == "confirm":
            buttons = [
                {"label": "No", "value": False, "style": ""},
                {"label": "Yes", "value": True, "style": "primary"},
            ]
        elif kind == "choose" and options:
            buttons = [{"label": opt, "value": opt, "style": ""} for opt in options]
        else:
            return  # ask() not supported in pill — terminal only
        await self.js(
            f"__repld_gate({json.dumps(gate_id)}, {json.dumps(prompt)}, {json.dumps(buttons)})"
        )
```

`_handle_binding` is a module-level async function:

```python
async def _handle_binding(session, params: dict) -> None:
    """Handle __repld_resolve callback from pill UI."""
    payload = json.loads(params.get("payload", "{}"))
    gate_id = payload.get("gate_id")
    value = payload.get("value")
    if gate_id:
        from ..gates import resolve_gate
        resolve_gate(gate_id, value)
```

### `src/repld/browser/cdp.py` — Binding dispatch

Add `_binding_handler` to CDPSession, dispatch in `_handle_event`:

```python
class CDPSession:
    def __init__(self, ...):
        ...
        self._binding_handler: Any | None = None

    def _handle_event(self, data):
        ...
        if method == "Runtime.bindingCalled":
            if self._binding_handler is not None:
                asyncio.create_task(
                    self._binding_handler(self, params),
                    name=f"repld-binding-{params.get('name', '?')}",
                )
```

Follows the exact `_fetch_handler` pattern.

### `src/repld/gates.py` — Add `tab=` parameter

```python
async def confirm(prompt, *, tab=None, default=None, timeout=None) -> bool:
    value = await _gate("confirm", prompt, None, default, timeout, tab=tab)
    return bool(value)

async def choose(prompt, options, *, tab=None, default=None, timeout=None) -> str:
    return await _gate("choose", prompt, options, default, timeout, tab=tab)

async def _gate(kind, prompt, options, default, timeout, tab=None):
    ...
    # After emitting HumanPromptOpen, route to tab if pinned
    if tab is not None and getattr(tab, '_pinned', False):
        asyncio.create_task(tab._show_gate(gate_id, kind, prompt, options))
    ...
```

### Pill JS — `_PIN_JS` constant in `tab.py`

The JS blob from our v4 prototype, refined:

- Bottom-center pill, 320px wide, dark glassmorphism
- Green dot (connected) / amber pulsing dot (awaiting input)
- Click to expand panel: status, hostname, reason, gate prompt + buttons
- `__repld_update(opts)` — update status/reason from Python
- `__repld_gate(gate_id, prompt, buttons)` — show gate with action buttons, queue if one is active
- `__repld_remove()` — full cleanup
- Button click calls `window.__repld_resolve(JSON.stringify({gate_id, value}))` which triggers `Runtime.bindingCalled`
- Gate queue: active gate on top, pending count shown, resolve pops next

## Data Flow

```
Gist calls:  await self._tab.confirm("Post tweet?")
                    │
                    ▼
            gates._gate("confirm", prompt, tab=self._tab)
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
   push_channel()      tab._show_gate()
   (MCP + terminal)    (tab.js → pill shows buttons)
          │                   │
          ▼                   ▼
   Terminal shows:      Pill switches to amber,
   "? Post tweet? [y/n]"  shows "Post tweet?" + [No] [Yes]
          │                   │
          ▼                   ▼
   stdin → resolve_gate()   button click → __repld_resolve()
                              → Runtime.bindingCalled
                              → _handle_binding()
                              → resolve_gate()
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
   First one wins. Future.set_result(value).
   Second is a no-op (fut.done() check).
                    │
                    ▼
   Pill returns to green. Cell resumes with bool.
```

## Method Signatures

```python
# tab.py
async def pin(self, reason: str = "") -> None
async def unpin(self) -> None
async def confirm(self, prompt: str, **kw) -> bool
async def choose(self, prompt: str, options: list[str], **kw) -> str
async def ask(self, prompt: str, **kw) -> str

# gates.py (changed signatures)
async def confirm(prompt, *, tab=None, default=None, timeout=None) -> bool
async def choose(prompt, options, *, tab=None, default=None, timeout=None) -> str
# ask() unchanged — no tab= parameter

# cdp.py (new instance var)
self._binding_handler: Any | None = None

# tab.py module-level
_PIN_JS: str = "..."  # pill injection blob
async def _handle_binding(session, params) -> None  # binding callback
```
