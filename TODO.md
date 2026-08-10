# TODO

## Site

- [ ] OG social images — `astro-og-canvas` per-page cards (`OGImageRoute` at
  `src/pages/open-graph/[...path].ts`) + Starlight `routeMiddleware` injection (docs/cards
  lack `og:image`). Terminal-noir bg + repld logo. Meta-tag plumbing already ships with a
  placeholder default in `SEO.astro`.

## Gist deps tooling

- [ ] **The pre-versioning shared deps dir is orphaned, and only the changelog says so.**
  `_deps_dir()` is `~/.local/share/repld/deps/py3.X` now; the flat
  `~/.local/share/repld/deps/` it replaced keeps everything ever installed there and nothing
  reads it. The `[Unreleased]` note does tell users it's unused and deletable by hand, which
  is why this is small — but nothing at runtime mentions it, so the only symptom is a re-
  install prompt for deps that look like they're already there. Options: migrate the old
  `.repld-manifest.txt` forward on the first versioned install, or warn once when the flat
  dir is non-empty. Anything that cleans it must skip `py3.*` — the versioned dirs are
  children of it, not siblings. Cleared on this machine 2026-07-31 (518 MB, 259 entries;
  the two `-e` installs there were `mym-shopify` and `partbridge`, both of which will
  re-prompt on their next boot with a terminal).
- [ ] `repld doctor`-style check for shared-gist-deps binary-ABI mismatches
  (the dir is `gist_deps._deps_dir()` now — `~/.local/share/repld/deps/py3.X`, keyed by
  interpreter version, which is itself the fix for most of this class) — came up
  organically in session 018 debugging a `_cffi_backend` `ModuleNotFoundError` three causes
  deep (tool-venv deps installed against the wrong Python ABI after a missing `--python`
  pin, since fixed in `b6b2758e`). A static scan of installed `.so` files' `cpython-NNN`
  tags against the actually-running interpreter's version would catch this class of bug
  before it manifests as a confusing runtime import error for a compiled extension.

## Browser

- [x] **No public `Tab.close()`.** Added: `async def close(self) -> None` wraps
  `Target.closeTarget({"targetId": self._chrome_target_id})`. Session cleanup follows from
  the resulting `Target.targetDestroyed` event (already handled in `session.py`), so no
  extra bookkeeping was needed. Documented in `BROWSER_GUIDE`, `_TOPICS["browser"]`, and the
  site's browser reference. New `phase_6_tab_close` asserts against Chrome's own
  `/json/list` rather than repld's session bookkeeping, since `detach()` alone leaves the
  target open and a check against `browser.tabs` couldn't tell the two apart.
- [x] **`selector.py` has no shadow-DOM fallback.** Fixed: `_deep_qsa()` (new, `selector.py`)
  is a shadow-piercing `querySelectorAll` — a named JS function expression that recurses
  through `el.shadowRoot` — swapped in for the plain `document.querySelectorAll` inside
  all four custom-selector builders (`text=`, `role=`, `label=`, `:has-text`); plain CSS
  selectors are untouched, still resolving via `DOM.querySelector`'s CDP fast path.
  `label=`'s `for=` lookup also switched from `document.getElementById` to
  `lbl.getRootNode().getElementById`, so a label/control pair inside the same shadow root
  resolves too. New `phase_6_shadow_dom_selectors` nests the existing light-DOM selector
  fixture two shadow roots deep. Verified live against the actual repro
  (2026-08-10's `chrome://extensions`, `text=Reload` on a `<cr-icon-button aria-label
  ="Reload">` with no visible text) via `mcp__repld__exec` and `browser_click` end to end.
- [ ] **`browser.get()`'s attached-session fast path can read a stale `target_info` cache.**
  `_get_by_glob` (`browser.py:293`) matches currently-attached sessions against
  `cdp.target_info.get("url", "")` before falling through to the polling loop. That field
  updates via the async `Target.targetInfoChanged` CDP event, which can lag a
  `tab.navigate(url)` call by a couple hundred ms behind what `location.href` already
  reports in-page — reproduced twice live (2026-08-10, same extension-dev gist session):
  a tab that had just navigated via `tab.navigate()` and was confirmed loaded
  (`await tab.js("location.href")` matched) still failed an immediate `browser.get(glob,
  timeout=0)` for its own URL; a ~0.3s sleep after the navigate made it reliably findable.
  `_get_by_id`/`_attach_racing` may have the same exposure — not checked. Deferred rather
  than fixed blind: it's core event-ordering logic and, per the "Testing gaps" item below,
  `browser/`'s reconnect/reattach paths are only exercised incidentally, so a fix here has
  nothing to catch a regression. Candidate fix: on a `tab.navigate()` (or after any call
  that changes a target's URL), await the actual `Target.targetInfoChanged` event for that
  target instead of returning as soon as the page-level load signal is satisfied — makes
  the cache change synchronous with the observable navigation instead of racing it.
- [ ] `Tab._reattach()` auto-remap across a genuine target swap — currently (session 019) a
  destroyed-and-replaced target (cross-origin/site-isolation process swap) surfaces a clear error
  pointing at `browser_tabs` rather than silently recovering. Considered and deferred:
  re-resolving the same `Tab` handle to a freshly-created target matching its watched glob
  pattern, transparently. Bigger semantic shift than it sounds — changes what a `Tab` *means*
  (pinned to one immutable CDP target → "logically the same tab" across swaps), with more places
  to get subtly wrong (pin state, event bindings, DuckDB event history keyed by the old target
  id). Revisit if a real cross-origin swap actually bites in practice, not preemptively.

## Testing gaps

- [ ] **`browser/` has no direct coverage.** Phase 6 exercises the tools end to end, but
  `har.py`'s SQL, `capture.py`'s Fetch interception, and `cdp.py`'s reconnect/reattach paths
  are only touched incidentally. Both browser bugs found on 2026-07-31 were in that area,
  and both surfaced from *using* the code rather than from three separate reading passes that
  had all explicitly scoped it out.
- [ ] Phase 6 still needs a live Chrome on `:9222`. It is deterministic now — it opens its own
  `data:` tab, asserts against that alone, and cleans up in `finally` — but it skips silently
  without a browser, so CI would report green having run none of it.
- [ ] `cdp._async_prune` runs its `DELETE` on the loop. Measured at 4.5 ms for 5k of 50k rows,
  which is why it was left alone; the docstring's claim that running as a task "avoids
  blocking recv" is still wrong in kind (a task on the same loop defers *when* it blocks, not
  *whether*). Revisit only if the cap or the row count changes.

## Deferred by design

- [ ] **Dashboard gate surface.** `gates/list` and `gates/resolve` are kernel JSON-RPC methods
  and `gates.open_gates()` returns a JSON-ready shape specifically so a second surface is
  cheap — but only `repld gate` calls them. The dashboard already has the token, the RPC
  dispatch table, and a UI; wiring a pending-gates card is maybe 40 lines. Deliberately not
  MCP tools: an agent able to answer its own `confirm()` defeats the primitive.
- [ ] **Per-parameter descriptions beyond a bare string.** `Annotated[T, "..."]` carries a
  description and nothing else. Enum/min/max would need a dict metadata item; no demand yet,
  and `repld` staying dependency-free rules out borrowing pydantic's `Field`.

## Screenshot / vision

- [ ] Chunked screenshots — tile full-page and ultrawide captures into overlapping viewport-sized chunks (each ≤1440x900 token budget) instead of scaling down to unreadable sizes. Heuristic: chunk when either dimension would shrink below ~600px. Agent gets an array of images.
- [ ] Auto-viewport in `tab.screenshot()` — temporarily set `Emulation.setDeviceMetricsOverride`
  with `deviceScaleFactor: 1` and model-optimal dims before capture, then restore. Avoids
  client-side downscale entirely; text rendered at target resolution. Tradeoff: responsive
  breakpoints may trigger on viewport resize. **Must verify `document.documentElement.clientWidth
  === window.innerWidth` after applying the override, before capturing** — confirmed on real
  hardware that reapplying the override on an already-emulated tab can leave the two
  disagreeing (a self-contradicting state a real browser never produces on a fresh load), which
  silently corrupts the capture. Fall back to a fresh tab/reload if mismatched rather than
  proceeding on bad viewport data (session 011).

## Features (from session 002 backlog)

- [ ] Safari/iOS support — WebKit Inspector over usbmuxd (gist, not core)
- [ ] `py-align` as PyPI package — currently `~/.local/bin/` vendored script
- [ ] Vite plugin — auto-inject `data-testid` in dev mode (SvelteKit + Astro)

## OpenCode channel support

Researched while comparing Claude Code's Channels feature to other coding agents. `push_channel()`
(`kernel.py`) is Claude-Code-specific by construction — it declares `claude/channel` +
`claude/channel/permission` in the MCP `initialize` capabilities and broadcasts
`notifications/claude/channel` over the MCP connection. That's a dead end for OpenCode: its MCP
client (`packages/opencode/src/mcp/index.ts`) only registers handlers for the two standard MCP
notification types (`LoggingMessageNotification`, `ToolListChangedNotification`) — any custom
notification method, including ours, is silently dropped.

- [ ] Add a second delivery path in `push_channel()` for OpenCode targets, over HTTP instead of
  the MCP connection — the two mechanisms are unrelated:
  - Requires the OpenCode instance be launched network-reachable: plain `opencode` (not just
    `opencode serve`) accepts `--port`/`--hostname` (`withNetworkOptions` in `cli/network.ts`);
    without those flags it only talks to itself over an in-process fake URL. Auth is opt-in via
    `OPENCODE_SERVER_PASSWORD`/`_USERNAME` (HTTP Basic) — fine to skip for localhost-only.
  - Two calls needed, not one: `POST /tui/append-prompt` (`{text, workspace}` — routes by
    `directory`/`workspace` query params, no session ID needed) just inserts text into the visible
    input buffer; it does **not** execute. `POST /tui/submit-prompt` (no payload — submits
    whatever's currently in the box) is the separate call that actually runs it. Mirrors
    typing + Enter as two HTTP calls.
  - **Gotcha**: `append-prompt`'s handler does `input.insertText(...)` — inserts at cursor,
    does not replace. No `GET`-style endpoint exists to read the current buffer first. So a push
    landing while the user has an unsent draft typed will splice into it rather than queue
    cleanly or get rejected. Only defensive option is an unconditional `clear-prompt` before
    `append-prompt`, which is safe against garbling but silently destroys any in-progress human
    draft with no way to detect one first.
  - Source refs (OpenCode repo, `github.com/anomalyco/opencode`): `packages/opencode/src/mcp/index.ts`
    (notification handlers), `packages/opencode/src/server/routes/instance/httpapi/groups/tui.ts`
    + `handlers/tui.ts` (route defs), `packages/tui/src/component/prompt/index.tsx:237`
    (client-side append handler), `packages/sdk/js/src/v2/server.ts` (`createOpencodeServer`
    defaults to `127.0.0.1:4096`).

**Alternative to the HTTP workaround above: upstream native support.** OpenCode's MCP client is
built directly on the official `@modelcontextprotocol/sdk` (`Client`/`Protocol` classes, not a
custom implementation), and that SDK's `Protocol` base class already exposes
`fallbackNotificationHandler?: (notification: Notification) => Promise<void>` — a catch-all for
any notification method without a registered schema handler (confirmed in the SDK's own
`shared/protocol.d.ts:265`). OpenCode just doesn't use it. This would be a small, additive PR
rather than an architectural one — the exact pattern already exists twice in the same function.

- [ ] Sketch a PR against `packages/opencode/src/mcp/index.ts`'s `watch(s, name, client, bridge,
  timeout)` function (where the two existing `setNotificationHandler` calls live), adding:
  ```ts
  // alongside the existing setNotificationHandler(LoggingMessageNotificationSchema, ...) call
  client.fallbackNotificationHandler = async (notification) => {
    if (notification.method !== "notifications/claude/channel") return
    // gate on the server having declared the capability, mirroring the existing
    // `if (!client.getServerCapabilities()?.tools) return` pattern below — a server that
    // never declared claude/channel shouldn't be able to inject via this method name
    if (!client.getServerCapabilities()?.experimental?.["claude/channel"]) return
    const { content } = notification.params as { content: string; meta?: Record<string, unknown> }
    await bridge.promise(
      events
        .publish(TuiEvent.PromptAppend, { text: content, workspace: directory })
        .pipe(
          Effect.andThen(events.publish(TuiEvent.CommandExecute, { command: "prompt.submit" })),
          Effect.ignore,
        ),
    )
  }
  ```
  No HTTP round-trip needed at all — `events` (`EventV2Bridge.Service`) and `TuiEvent` are
  already imported in this file and used for the identical push pattern (`TuiEvent.ToastShow`,
  e.g. the "MCP Authentication Required" toasts a few lines above `watch()`). `directory` (the
  workspace this MCP connection belongs to) is already threaded through the enclosing scope via
  `InstanceState.directory` — may need promoting from closure-capture to an explicit `watch()`
  parameter depending on exact call-site scoping, the one part not fully nailed down.
  - **Open design question, not just implementation**: whether to literally adopt Claude Code's
    `claude/channel` capability name + `notifications/claude/channel` method (so repld's
    `push_channel()` works against OpenCode with **zero changes on repld's side**, and any other
    tool built for Claude Code Channels gets OpenCode support for free) vs. inventing an
    OpenCode-native `opencode/channel` equivalent. Adopting Claude's naming is a de facto
    cross-tool standard bet; inventing a new one avoids depending on another vendor's unstable
    (research-preview) contract. Worth raising as the actual PR discussion point, not deciding
    unilaterally in the diff.
  - If this lands upstream, repld's HTTP-bridge item above becomes unnecessary — `push_channel()`
    already speaks the protocol this PR would make OpenCode listen for.

## Infra

- [ ] CI + lint pass — `ruff` and `basedpyright` are trivial to wire; the smoketest is the
  question, since phases 6 and 16 need a live Chrome and a usable systemd user manager
  respectively and both skip silently without them. A CI run that reports green having
  skipped them is worse than no CI.
- [ ] GitHub Actions build pipeline for site — add when docs generation from `help.py` lands
- [ ] `scripts/gen-reference.py` — import `_TOPICS` + `GUIDE` from `help.py`, emit Starlight markdown at build time
