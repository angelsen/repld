# Feature: tab.pin() + browser gate bridge

## Overview

Gists that use browser tabs for authenticated API access need two things:
a visual indicator that repld owns the tab (preventing accidental close),
and a way to resolve human gates (confirm/ask/choose) in the browser where
the user's attention already is.

`tab.pin(reason)` injects a bottom-center pill + beforeunload guard.
`tab.confirm(prompt)` / `tab.ask(prompt)` / `tab.choose(prompt, options)`
are convenience methods that route gates to the pinned tab's pill UI.
The existing kernel terminal path remains a parallel resolution surface.

## What It Does

**Pin:**
- `tab.pin(reason)` injects a floating pill at bottom-center of the page via `Runtime.evaluate`. Adds a `beforeunload` handler to warn on accidental close.
- Pill shows a green dot + "repld" when connected. Clicking expands a panel showing status, hostname, and the gist's reason string.
- `tab.unpin()` removes the pill, beforeunload handler, and all injected DOM/CSS.
- Pinning is idempotent — calling `pin()` again updates the reason.
- Only one pin per tab. Pages and iframes only (skip workers).

**Gate bridge:**
- `tab.confirm(prompt)` / `tab.ask(prompt)` / `tab.choose(prompt, options)` call the existing `gates._gate()` with `tab=self`, which routes the gate to the pill UI.
- When a gate targets a pinned tab: pill switches to amber pulsing dot + "awaiting input", panel auto-expands showing the prompt and action buttons.
- User clicks a button in the pill → JS calls `Runtime.bindingCalled` → CDPSession dispatches → `resolve_gate(gate_id, value)`.
- Kernel terminal still shows the gate prompt simultaneously. First resolution (browser or terminal) wins — same Future.
- After resolution, pill returns to green "connected" state.

**Multiple gates:**
- Gates queue. Active gate shows on top with buttons. Pending count shown below ("2 more pending"). Resolve the top one, next slides up.

**Gate scoping:**
- Each gist calls `self._tab.confirm(...)` — gates route to that gist's tab.
- If `tab=` is not provided (existing call pattern), gates only show in the kernel terminal (backward compatible).

## Constraints

- Pill JS/CSS is a single `Runtime.evaluate` call — no external assets.
- `Runtime.addBinding` for the callback bridge (Chrome calls Python when JS invokes the bound function).
- No new dependencies. `resolve_gate()` is already thread-safe.
- The pill must work on any website without breaking page layout or functionality.

## Out of Scope

- Style variants (bar, corner, etc.) — one good default (pill) for now.
- Telegram or other remote gate surfaces — primitives support it but not built.
- Text input gates in the pill (`ask()`, `edit()`) — confirm and choose only for now. Text input adds keyboard/focus complexity. Terminal handles `ask()`. `edit()` is a future gate type. Primitives (gate queue, binding bridge, pill update API) are designed to support both when ready.
