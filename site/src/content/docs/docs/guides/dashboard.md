---
title: Dashboard guide
description: The kernel's built-in web control panel — live sessions, browser connections, console and network queries.
---

Every kernel runs a small HTTP control panel alongside the socket — no setup, no separate process. It's a window into what the kernel and its attached browsers are doing right now.

## Opening the dashboard

```bash
repld dashboard          # resolve the port, read the token, open the browser
repld dashboard --print  # print the authenticated URL instead
```

That is the intended route, because the page itself is authenticated — see [Security](#security). `repld dashboard` reads the API token out of the project's 0600 hint file and opens the URL with it attached; a bare `http://127.0.0.1:<port>/` answers 401. The port alone is printed by `repld status` and recorded in the kernel's lockfile (`dashboard_port` field) if you need to script against it. It's ephemeral by default, and stable across restarts when possible.

## Layout

- **Sidebar** — every live repld session on the machine, with a link to its own dashboard.
- **Header** — PID, uptime, active task count, running ticker labels.
- **Tabs** — Browser, Connections, Targets, Console, Network (browser-related tabs need `repld[browser]`; see the [browser guide](/repld/docs/guides/browser/)).
- **Footer** — socket path and a one-line connection summary (e.g. "2 chromes, 5 tabs").

## Sidebar: live sessions

Every kernel registers itself at boot and deregisters on shutdown — this is what populates the sidebar. There's no central server; each kernel just writes a small JSON file recording its PID, working directory, socket path, and dashboard port to `$XDG_RUNTIME_DIR/repld/sessions/`. Any dashboard can read that directory to enumerate its siblings.

The sidebar polls every 10 seconds, prunes entries whose PID is no longer alive, and shows each session's project directory name and uptime. The session you're currently viewing is highlighted; every other live session is a clickable link straight to its own dashboard — useful when you're juggling repld across several projects and want to check on one without switching terminals.

## Browser tab

Connect to a Chrome instance (`--remote-debugging-port`), add or remove watch patterns, and see currently attached tabs — the dashboard equivalent of `browser.connect()` / `browser.watch()` / `browser.tabs` from Python.

## Connections tab

One row per connected Chrome instance, expandable to its individual attached targets. **Disconnect** closes a whole Chrome instance (unpinning tabs first); **Detach** removes a single tab. Mirrors `browser.disconnect(port=)` and `browser.detach(pattern)`.

## Targets tab

Every CDP target (tabs, iframes, service workers — excluding pure workers) across all connected Chrome instances, whether or not repld has attached to it. Already-attached targets show an "attached" badge; unattached ones get a quick "watch" button.

## Console / Network tabs

Pick a tab from the dropdown to see its most recent captured console messages or network requests (50-row preview, same DuckDB-backed store `tab.console()`/`tab.network()` query from Python). Useful for a quick look without dropping into `repld exec`.

## Security

The dashboard binds to `127.0.0.1` only — but loopback is not a user boundary. Anything running as any user on the box can reach the port, and the panel enumerates every project cwd on the machine while its browser controls reach your logged-in Chrome. So both endpoints are gated by a random per-boot token, written to the project's 0600 hint file:

- **`GET /`** accepts `?token=` (what `repld dashboard` opens with) or a `repld_token_<port>` cookie. The cookie is set on the way in, so a refresh survives the page dropping the query from the address bar. It's named per-port because cookies ignore the port and sibling dashboards would otherwise clobber each other's.
- **`POST /api`** is **Bearer-only**. A cookie rides along on requests you didn't initiate, so it must never be sufficient by itself.

The page needs the token inlined to call the API, which is exactly why serving it unauthenticated would hand the credential to every process on the machine.

Requests are also checked against a loopback `Host` header allowlist (`127.0.0.1:<port>` / `localhost:<port>`), which blocks DNS-rebinding attacks where an external domain resolves to your machine and tries to ride the same-origin policy into the API. The sidebar's links to sibling dashboards carry each peer's own token, read from that project's hint file.

## What's next

- [Browser guide](/repld/docs/guides/browser/) — the Python-side API the dashboard's Browser/Connections tabs mirror
- [Getting started](/repld/docs/guides/getting-started/) — install and start the kernel
