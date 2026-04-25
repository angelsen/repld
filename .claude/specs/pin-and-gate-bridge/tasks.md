# Implementation Tasks: tab.pin() + browser gate bridge

**Status:** Complete
**Spec:** [requirements.md](./requirements.md) | [design.md](./design.md)

## Task Breakdown

### Task 1: CDPSession binding dispatch
**Completed**

**Acceptance:**
- [x] `_binding_handler` instance var added
- [x] `Runtime.bindingCalled` dispatched via `asyncio.create_task` (same pattern as `_fetch_handler`)

---

### Task 2: Pill JS constant + pin/unpin on Tab
**Completed**

**Acceptance:**
- [x] `tab.pin("reason")` injects pill + beforeunload via `Runtime.evaluate`
- [x] `tab.pin("new reason")` updates reason without re-injecting
- [x] `tab.unpin()` removes all injected DOM/CSS/handlers
- [x] `Runtime.addBinding` registered for `__repld_resolve`
- [x] `_handle_binding` parses payload and calls `resolve_gate()`
- [x] `_show_gate()` builds button config and calls `__repld_gate()` JS
- [x] Gate queue in JS: active gate on top, pending count, resolve pops next

---

### Task 3: Gate `tab=` parameter
**Completed**

**Acceptance:**
- [x] `confirm(prompt, tab=tab)` routes to pill when pinned
- [x] `choose(prompt, options, tab=tab)` routes to pill when pinned
- [x] `ask()` unchanged — no `tab=` parameter
- [x] No `tab=` (existing usage) → terminal only, backward compatible
- [x] Terminal and browser resolve same Future, first wins

---

### Task 4: Tab convenience methods
**Completed**

**Acceptance:**
- [x] `tab.confirm("prompt")` calls `gates.confirm(prompt, tab=self)`
- [x] `tab.choose("prompt", options)` calls `gates.choose(prompt, options, tab=self)`
- [x] `tab.ask("prompt")` calls `gates.ask(prompt)` (no tab routing)

---

### Task 5: Update X gist as first user
**Completed**

**Acceptance:**
- [x] `X.connect()` pins with reason
- [x] `post()` gates on confirm before posting
- [x] `delete()` gates on confirm before deleting

---

### Task 6: Docs + help updates
**Completed**

**Acceptance:**
- [x] Help topic shows pin + gate methods
- [x] Gist skill guide shows pin pattern in `connect()`

---

## Task Dependencies

```
Task 1 (cdp binding dispatch)
  └→ Task 2 (pill JS + pin/unpin)
       └→ Task 3 (gate tab= param)
            └→ Task 4 (tab convenience methods)
                 ├→ Task 5 (X gist)
                 └→ Task 6 (docs)
```
