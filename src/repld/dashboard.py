"""Dashboard: browser control panel + kernel status served over HTTP.

Pure-stdlib async HTTP server on an ephemeral port.  Two routes:
  GET /        → inline HTML page
  POST /api    → JSON-RPC commands (state, browser.connect, browser.watch, etc.)
"""

import __main__
import asyncio
import json
import os
import secrets
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from . import tasks
from .channel import push_kind
from .state import atomic_write_json

_start_time: float = 0.0
_socket_path: str = ""
_server: asyncio.Server | None = None
_hint_path: Path | None = None
_token: str = ""

# Ceiling on a `POST /api` body. Every real request is a small JSON-RPC
# envelope — the largest is a `browser.connect` params dict — so this is three
# orders of magnitude of headroom. It exists because `Content-Length` feeds
# `readexactly()` directly: without a bound, one header claiming 10 GB has the
# kernel allocate toward it, and the 5 s read timeout is no defence when the
# sender is on loopback. Holding the token is not licence to OOM the process
# the token exists to protect.
_MAX_BODY_BYTES = 1 << 20

# Ceiling on the request's header block. The same argument as `_MAX_BODY_BYTES`
# — an unbounded read on loopback is an OOM the token was supposed to prevent —
# except this one is worse, because the header loop runs *before* the Host
# check and before either auth path, so it was the one thing an unauthenticated
# caller could make the kernel do. `StreamReader.readline` caps each line at
# its own 64 KiB limit, but nothing capped the *count*, and the per-line 5 s
# timeout resets every line: measured, one connection fed 200 000 headers into
# the dict without the server answering or ever stopping. A real request sends
# a dozen; a generous browser with a long cookie jar sends a few dozen.
_MAX_HEADER_LINES = 100


def _bound_port() -> int | None:
    """The dashboard's listening port, or None before/without a bound server."""
    if _server and _server.sockets:
        return _server.sockets[0].getsockname()[1]
    return None


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------


def _collect_state() -> dict:
    active = sum(1 for _tid, t in tasks.items() if not t["done_event"].is_set())
    from .kernel import every_snapshot

    tickers = [{"label": h.label, "seconds": h.seconds} for h in every_snapshot()]
    state: dict[str, Any] = {
        "kernel": {
            "pid": os.getpid(),
            "uptime_s": int(time.monotonic() - _start_time),
            "socket": _socket_path,
            "tasks_active": active,
            "tickers": tickers,
        },
        "browser": None,
    }

    browser = getattr(__main__, "browser", None)
    if browser is None:
        return state

    pool = browser.peek()
    if pool is None:
        state["browser"] = {
            "connected": False,
            "ports": [],
            "patterns": [],
            "tabs": [],
        }
        return state

    state["browser"] = pool.snapshot()
    return state


def _resolve_tab(browser, target_id: str):
    """Find an attached Tab by its raw Chrome targetId."""
    pool = browser.peek()
    if pool is None:
        raise RuntimeError("not connected")
    return pool.resolve_tab(target_id)


def save_hint() -> None:
    """Persist dashboard port + API token + browser state to the hint file.

    Written once as soon as the server binds — `repld status` reads the token
    from here to ask for task/ticker counts, and a restart reads the port back
    to reclaim it — and again whenever browser state changes.

    Merges rather than replaces, and only rewrites the browser keys when a pool
    actually exists. Every browser-initiated call has one (they're BrowserPool
    methods); the startup call does not, and blanking `chrome_ports` /
    `patterns` there would discard the previous kernel's restorable session
    before `_restore_browser_state` has read it — silently, on a headless
    kernel, which never prompts to restore.
    """
    if _hint_path is None:
        return
    hint: dict[str, Any] = {}
    try:
        loaded = json.loads(_hint_path.read_text())
        if isinstance(loaded, dict):
            hint = loaded
    except (OSError, json.JSONDecodeError):
        pass

    hint["dashboard_port"] = _bound_port() or 0
    hint["token"] = _token

    browser = getattr(__main__, "browser", None)
    pool = browser.peek() if browser else None
    if pool is not None:
        hint["chrome_ports"] = pool.connected_ports
        hint["patterns"] = pool.patterns
        from .browser.cdp import _suppress_patterns

        if _suppress_patterns:
            hint["suppress"] = sorted(_suppress_patterns)
        else:
            hint.pop("suppress", None)
    try:
        atomic_write_json(_hint_path, hint, chmod=0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------


async def _rpc_browser_disconnect(browser, params: dict) -> Any:
    from .browser_dispatch import route_detach

    result = await route_detach(browser, params.get("target"), params.get("port"))
    if result is None:
        result = await browser.disconnect()
    push_kind(f"[dashboard] {result}", "browser_disconnect")
    return {"result": result}


async def _rpc_browser_connect(browser, params: dict) -> Any:
    port = params.get("port", 9222)
    b = await browser.connect(port)
    targets = await b.pages()
    page_lines = [
        f"  {port}:{t.get('targetId', '')[:6].lower()}  {t.get('url', '')}"
        for t in targets
        if t.get("type") == "page"
    ]
    summary = (
        f"[dashboard] connected to Chrome on port {port} — {len(page_lines)} page(s)"
    )
    if page_lines:
        summary += "\n" + "\n".join(page_lines[:10])
    push_kind(summary, "browser_connect", port=str(port))
    return {"connected": True, "port": port}


async def _rpc_browser_targets(browser, params: dict) -> Any:
    pool = browser.peek()
    if pool is None or not pool.connected_ports:
        raise RuntimeError("Not connected to Chrome")
    targets = await pool.pages()
    return [
        {
            "targetId": t.get("targetId", ""),
            "type": t.get("type", ""),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
        }
        for t in targets
        if t.get("type") not in ("service_worker", "shared_worker", "worker")
    ]


async def _rpc_browser_watch(browser, params: dict) -> Any:
    pattern = params.get("pattern", "")
    if not pattern:
        raise RuntimeError("pattern is required")
    result = await browser.watch(pattern)
    tab_lines = [
        f"  {t.target_id}  {t.url}" for t in browser.tabs if fnmatch(t.url, pattern)
    ]
    summary = f"[dashboard] watch '{pattern}': {result}"
    if tab_lines:
        summary += "\n" + "\n".join(tab_lines[:10])
    push_kind(summary, "browser_watch", pattern=pattern)
    return {"result": result}


async def _rpc_browser_unwatch(browser, params: dict) -> Any:
    pattern = params.get("pattern", "")
    if not pattern:
        raise RuntimeError("pattern is required")
    result = await browser.detach(pattern)
    push_kind(
        f"[dashboard] unwatch '{pattern}': {result}",
        "browser_unwatch",
        pattern=pattern,
    )
    return {"result": result}


async def _rpc_browser_console(browser, params: dict) -> Any:
    tab = _resolve_tab(browser, params.get("target_id", ""))
    # Off the loop: console_entries/har_summary re-evaluate a CTE chain that
    # observe.py measured at 39-56 ms per tab, and this server shares the
    # kernel's loop — so a Refresh click would stall every cell, ticker and
    # observation for as long as the scan takes. query_dicts opens a per-call
    # cursor precisely so another thread can read while the loop writes.
    rows = await asyncio.to_thread(tab.console)
    return [
        {
            "level": r.level,
            "source": r.source,
            "text": r.text[:500],
            "timestamp": r.timestamp,
        }
        for r in rows[:50]
    ]


async def _rpc_browser_network(browser, params: dict) -> Any:
    tab = _resolve_tab(browser, params.get("target_id", ""))
    rows = await asyncio.to_thread(
        tab.network
    )  # off the loop — see _rpc_browser_console
    return [
        {
            "method": r.method,
            "status": r.status,
            "url": r.url,
            "type": r.type,
            "size": r.size,
            "time_ms": r.time_ms,
        }
        for r in rows[:50]
    ]


# Browser RPCs: handler(browser, params) — dispatch injects the validated
# browser object. "state" and "sessions" are handled inline (no browser).
_BROWSER_RPCS = {
    "browser.disconnect": _rpc_browser_disconnect,
    "browser.connect": _rpc_browser_connect,
    "browser.targets": _rpc_browser_targets,
    "browser.watch": _rpc_browser_watch,
    "browser.unwatch": _rpc_browser_unwatch,
    "browser.console": _rpc_browser_console,
    "browser.network": _rpc_browser_network,
}


def _sessions_with_tokens() -> list[dict]:
    """Live sessions, each carrying its own dashboard token.

    The sidebar links to sibling projects' dashboards, and those now refuse an
    unauthenticated `GET /` like this one does — so the links need each peer's
    token. Read here rather than in `sessions.register`: a token belongs to a
    dashboard, not to the session registry, and `repld status` / `stop --all`
    read that registry with no business carrying credentials around.

    Every hint file is 0600 and owned by this uid, so this reveals nothing to
    the caller it couldn't already read — but only *because* the caller is
    already holding this kernel's token, which is the point of the gate.
    """
    from . import paths, sessions, state

    out = []
    for info in sessions.list_sessions():
        entry = dict(info)
        socket_path = entry.get("socket_path")
        if entry.get("pid") == os.getpid():
            entry["dashboard_token"] = _token
        elif isinstance(socket_path, str) and socket_path:
            entry["dashboard_token"] = state.dashboard_token(
                paths.hint_for(Path(socket_path))
            )
        else:
            entry["dashboard_token"] = ""
        out.append(entry)
    return out


async def _rpc_dispatch(method: str, params: dict) -> Any:
    if method == "state":
        return _collect_state()

    if method == "sessions":
        return _sessions_with_tokens()

    handler = _BROWSER_RPCS.get(method)
    if handler is None:
        raise RuntimeError(f"Unknown method: {method}")
    browser = getattr(__main__, "browser", None)
    if browser is None:
        raise RuntimeError("repld[browser] not installed")
    return await handler(browser, params)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _cors_header(origin: str | None) -> str:
    """Echo Access-Control-Allow-Origin only for this dashboard's own origin."""
    port = _bound_port()
    if not origin or port is None:
        return ""
    if origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return f"Access-Control-Allow-Origin: {origin}\r\n"
    return ""


def _cookie_name() -> str:
    """Per-port cookie name.

    Cookies ignore the port — every dashboard on 127.0.0.1 shares one cookie
    jar — so a fixed name would have each project's kernel overwrite the last
    one's cookie and log you out of every sibling tab. Nothing leaks either
    way (a cookie carrying project A's token simply fails B's compare_digest),
    but the suffix keeps each dashboard reading only its own.
    """
    return f"repld_token_{_bound_port()}"


def _parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep:
            out[key] = value
    return out


def _token_ok(candidate: str | None) -> bool:
    return bool(candidate) and secrets.compare_digest(candidate or "", _token)


def _page_auth(query: str, headers: dict[str, str]) -> tuple[bool, bool]:
    """`(authorized, came_from_query)` for a `GET /`.

    `GET /` is authenticated because it *carries* the token — the page needs
    it inlined to call `POST /api`. Serving it to anyone who asks made the
    token gate nothing at all: any process on the box could read the page,
    grep the token out, and then enumerate every project cwd via `sessions`
    or attach to the user's Chrome. Loopback is not a uid boundary, and every
    other file holding this data is written 0600.

    Two accepted sources. `?token=` is what `repld dashboard` opens with,
    reading it from the 0600 hint file; the cookie is what makes a refresh
    work after the page strips that query out of the address bar. The cookie
    is never accepted for `POST /api` — that stays Bearer-only, so a request
    a browser was tricked into making cannot carry credentials by itself.
    """
    from urllib.parse import parse_qs

    supplied = parse_qs(query).get("token", [None])[0]
    if _token_ok(supplied):
        return True, True
    cookies = _parse_cookies(headers.get("cookie", ""))
    return _token_ok(cookies.get(_cookie_name())), False


_UNAUTHORIZED_PAGE = """<!doctype html>
<meta charset="utf-8"><title>repld dashboard</title>
<body style="font:14px/1.6 ui-monospace,monospace;padding:2rem;max-width:40rem">
<h1 style="font-size:1.1rem">repld dashboard — not authorized</h1>
<p>This page carries an API token, so it is not served without one.</p>
<p>Open it from the project directory with:</p>
<pre style="background:#eee;padding:.6rem">repld dashboard</pre>
<p>That reads the token from the kernel's private state file and opens an
authenticated URL. If you had this page open across a kernel restart, the
token changed — reopen it the same way.</p>
</body>"""


def _host_allowed(host: str | None) -> bool:
    """Reject DNS rebinding: the Host header must name this loopback server.

    A rebound page (evil.com resolving to 127.0.0.1) is same-origin in the
    browser's eyes — no Origin header, so CORS can't stop it from reading
    GET / (and the embedded token). Its requests carry Host: evil.com:<port>.
    """
    port = _bound_port()
    if not host or port is None:
        return False
    # IPv4 loopback only — _start() binds "127.0.0.1", never "::1".
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


async def _send_response(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    content_type: str = "application/json",
    origin: str | None = None,
    extra_headers: str = "",
) -> None:
    reason = {
        200: "OK",
        401: "Unauthorized",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        413: "Payload Too Large",
        431: "Request Header Fields Too Large",
        500: "Internal Server Error",
    }.get(status, "OK")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        # `_handle_connection` serves exactly one request and then closes in
        # its `finally`, but HTTP/1.1 is keep-alive by default — so without
        # this the client pools a socket the server has already FIN'd and the
        # next request races the close. Chrome's socket-reuse retry hides it;
        # saying what we actually do is cheaper than relying on that.
        "Connection: close\r\n"
        # The page embeds a token and briefly has one in its URL: never cache
        # it to disk, and never hand the URL to whatever it links out to (the
        # sidebar links to sibling dashboards).
        "Cache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\n"
        f"{_cors_header(origin)}"
        f"{extra_headers}"
        "\r\n"
    )
    writer.write(header.encode() + body)
    await writer.drain()


async def _handle_api(body: bytes) -> bytes:
    from .protocol import _error, _response

    try:
        req = json.loads(body)
    except json.JSONDecodeError:
        return json.dumps(_error(None, -32700, "Parse error")).encode()

    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    try:
        result = await _rpc_dispatch(method, params)
        return json.dumps(_response(req_id, result), separators=(",", ":")).encode()
    except Exception as exc:
        return json.dumps(_error(req_id, -32000, str(exc))).encode()


async def _handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    headers: dict[str, str] = {}
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not request_line:
            return

        parts = request_line.decode("utf-8", errors="replace").strip().split()
        if len(parts) < 2:
            return
        method_http, target = parts[0], parts[1]
        path, _, query = target.partition("?")

        for _ in range(_MAX_HEADER_LINES):
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            key, sep, value = decoded.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        else:
            # Ran the bound without reaching the blank line. Answered rather
            # than parsed: whatever is on the other end is not a browser.
            await _send_response(writer, 431, b'{"error":"too many headers"}')
            return

        content_length = int(headers.get("content-length", "0") or "0")
        origin = headers.get("origin")

        if not _host_allowed(headers.get("host")):
            await _send_response(writer, 403, b'{"error":"forbidden host"}')
            return

        if method_http == "OPTIONS":
            cors = (
                "HTTP/1.1 204 No Content\r\n"
                f"{_cors_header(origin)}"
                "Access-Control-Allow-Methods: POST, GET, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            writer.write(cors.encode())
            await writer.drain()
            return

        if method_http == "GET" and path == "/":
            authorized, from_query = _page_auth(query, headers)
            if not authorized:
                await _send_response(
                    writer,
                    401,
                    _UNAUTHORIZED_PAGE.encode("utf-8"),
                    "text/html; charset=utf-8",
                    origin=origin,
                )
                return
            html = _HTML.replace("__DASHBOARD_TOKEN__", _token)
            extra = ""
            if from_query:
                # So a refresh works after the page drops ?token= from the
                # address bar. HttpOnly because the page is handed the token
                # inline and has no reason to read it back out of here;
                # SameSite=Strict so it never rides along on a request some
                # other site initiated.
                extra = (
                    f"Set-Cookie: {_cookie_name()}={_token}; Path=/; "
                    "HttpOnly; SameSite=Strict\r\n"
                )
            await _send_response(
                writer,
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                origin=origin,
                extra_headers=extra,
            )
            return

        if method_http == "POST" and path == "/api":
            auth = headers.get("authorization", "")
            if not secrets.compare_digest(auth, f"Bearer {_token}"):
                await _send_response(
                    writer, 401, b'{"error":"unauthorized"}', origin=origin
                )
                return
            # Checked after the Bearer compare, so an unauthenticated caller
            # learns nothing about the limit — and before the read, which is
            # the whole point: `readexactly` allocates toward whatever the
            # header claimed.
            if content_length > _MAX_BODY_BYTES:
                await _send_response(
                    writer,
                    413,
                    b'{"error":"request body too large"}',
                    origin=origin,
                )
                return
            body = (
                await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)
                if content_length
                else b"{}"
            )
            result = await _handle_api(body)
            await _send_response(writer, 200, result, origin=origin)
            return

        await _send_response(writer, 404, b'{"error":"not found"}', origin=origin)

    except (
        asyncio.TimeoutError,
        asyncio.IncompleteReadError,
        ConnectionResetError,
        BrokenPipeError,
    ):
        pass
    except Exception:
        try:
            await _send_response(
                writer, 500, b'{"error":"internal"}', origin=headers.get("origin")
            )
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


async def _start(
    socket_path: str,
    start_time: float,
    preferred_port: int,
) -> int:
    global _start_time, _socket_path, _server, _token
    _start_time = start_time
    _socket_path = socket_path
    _token = secrets.token_urlsafe(32)

    port = preferred_port
    try:
        _server = await asyncio.start_server(_handle_connection, "127.0.0.1", port)
    except OSError:
        _server = await asyncio.start_server(_handle_connection, "127.0.0.1", 0)
    port = _server.sockets[0].getsockname()[1]
    return port


def start_dashboard(
    loop: asyncio.AbstractEventLoop,
    socket_path: str,
    start_time: float,
    preferred_port: int = 0,
    hint_path: Path | None = None,
) -> int:
    """Start the dashboard HTTP server. Returns the bound port."""
    global _hint_path
    _hint_path = hint_path
    future = asyncio.run_coroutine_threadsafe(
        _start(socket_path, start_time, preferred_port), loop
    )
    port = future.result(timeout=5.0)
    # The port and token exist now; publish them before anything can ask.
    # Not left to the browser code that also writes this file: a kernel that
    # never touches the browser would then leave `repld status` unable to
    # authenticate, and lose its dashboard port on every restart.
    save_hint()
    return port


def stop_dashboard() -> None:
    global _server
    if _server is not None:
        _server.close()
        _server = None


# ---------------------------------------------------------------------------
# Inline HTML
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>repld</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0e0e10; --surface: #16161a; --border: #27272a;
  --text: #e4e4e7; --dim: #71717a; --accent: #3ce882;
  --green: #4ade80; --red: #f87171; --amber: #fbbf24;
  --mono: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; overflow: hidden; }
body { display: flex; flex-direction: row; }

/* --- sidebar --- */
.sidebar { flex-shrink: 0; width: 220px; height: 100%; display: flex; flex-direction: column; background: var(--surface); border-right: 1px solid var(--border); overflow-y: auto; }
.sidebar-section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); padding: 12px 16px 4px; }
#session-list { list-style: none; }
#session-list li { display: flex; align-items: center; gap: 8px; padding: 6px 16px; font-family: var(--mono); font-size: 12px; }
#session-list li.current { background: var(--bg); }
#session-list a { color: var(--text); text-decoration: none; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#session-list a:hover { color: var(--accent); }
#session-list .session-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#session-list .session-uptime { color: var(--dim); font-size: 10px; }
#session-list .empty { padding: 6px 16px; }

/* --- main --- */
.main { flex: 1; min-width: 0; height: 100%; display: flex; flex-direction: column; max-width: 960px; margin: 0 auto; border-left: 1px solid var(--border); border-right: 1px solid var(--border); }

/* --- header --- */
.header { flex-shrink: 0; display: flex; align-items: center; gap: 12px; padding: 10px 20px; border-bottom: 1px solid var(--border); background: var(--surface); }
.header .logo { font-family: var(--mono); font-size: 16px; font-weight: 600; letter-spacing: -0.5px; color: var(--text); text-decoration: none; }
.header .logo:hover { color: var(--accent); }
.header .logo .cursor { display: inline-block; width: 2px; height: 14px; background: var(--green); margin-left: 1px; vertical-align: middle; animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }
.header .meta { font-family: var(--mono); font-size: 11px; color: var(--dim); }
.header .spacer { flex: 1; }
.header-links { display: flex; gap: 16px; font-family: var(--mono); font-size: 11px; margin-right: 12px; }
.header-links a { color: var(--dim); text-decoration: none; }
.header-links a:hover { color: var(--text); }
.header .kernel-info { display: flex; gap: 12px; font-family: var(--mono); font-size: 11px; color: var(--dim); }

/* --- tab bar --- */
.tab-bar { flex-shrink: 0; display: flex; gap: 0; border-bottom: 1px solid var(--border); background: var(--surface); padding: 0 16px; }
.tab-bar button { background: none; border: none; border-bottom: 2px solid transparent; color: var(--dim); font-family: var(--mono); font-size: 12px; padding: 8px 16px; cursor: pointer; transition: color 0.15s, border-color 0.15s; }
.tab-bar button:hover { color: var(--text); }
.tab-bar button.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-bar .badge { display: inline-block; background: var(--border); color: var(--dim); font-size: 10px; padding: 1px 5px; border-radius: 0; margin-left: 4px; vertical-align: middle; }

/* --- content --- */
.content { flex: 1; overflow-y: auto; padding: 16px 20px; }
.tab-pane { display: none; }
.tab-pane.active { display: block; }

/* --- footer --- */
.footer { flex-shrink: 0; padding: 6px 20px; border-top: 1px solid var(--border); background: var(--surface); font-family: var(--mono); font-size: 11px; color: var(--dim); display: flex; gap: 16px; }

/* --- shared --- */
.status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.status.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
.status.off { background: var(--red); }

.connect-row { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }

input[type=number] { -moz-appearance: textfield; appearance: textfield; }
input[type=number]::-webkit-inner-spin-button,
input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
input[type=number], input[type=text] { background: var(--surface); border: 1px solid var(--border); color: var(--text); font-family: var(--mono); font-size: 12px; padding: 5px 10px; border-radius: 0; height: 28px; }
input:focus { outline: none; border-color: var(--accent); }
input[type=number] { width: 72px; }

button { background: var(--surface); border: 1px solid var(--border); color: var(--text); font-family: var(--mono); font-size: 11px; padding: 5px 12px; border-radius: 0; cursor: pointer; transition: border-color 0.15s; height: 28px; }
button:hover { border-color: var(--accent); }
button:active { background: var(--border); }
button.sm { padding: 2px 8px; font-size: 10px; height: auto; }
button.danger { color: var(--red); }

.section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); margin: 16px 0 6px; }
.section-label:first-child { margin-top: 0; }

.pattern-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.pattern-row input { flex: 1; }
.pattern-list { list-style: none; margin-bottom: 8px; }
.pattern-list li { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 12px; padding: 3px 0; }
.pattern-list li .glob { color: var(--accent); }
.pattern-list li .count { color: var(--dim); font-size: 11px; }

tr.conn-port td { font-weight: 600; cursor: pointer; }
tr.conn-port:hover td { background: var(--surface); }
tr.conn-target td { padding-left: 24px; }
tr.conn-target.collapsed { display: none; }

table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
th { text-align: left; color: var(--dim); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; background: var(--bg); }
td { padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tr:hover td { background: var(--surface); }
td.url { max-width: 500px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td.type { color: var(--dim); width: 55px; }
td.actions { width: 70px; text-align: right; white-space: nowrap; }
td.method { width: 50px; font-weight: 600; }
td.status-code { width: 40px; }
td.size { width: 60px; color: var(--dim); text-align: right; }
td.time { width: 50px; color: var(--dim); text-align: right; }
td.level { width: 55px; }
td.level.error { color: var(--red); }
td.level.warning { color: var(--amber); }
td.level.log { color: var(--dim); }
td.console-text { white-space: pre-wrap; word-break: break-all; max-width: 600px; }

.empty { color: var(--dim); font-style: italic; font-size: 12px; padding: 12px 0; }

.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.toolbar select { background: var(--surface); border: 1px solid var(--border); color: var(--text); font-family: var(--mono); font-size: 11px; padding: 4px 8px; border-radius: 0; }
.toolbar select:focus { outline: none; border-color: var(--accent); }

.toast { position: fixed; bottom: 16px; right: 16px; background: var(--surface); border: 1px solid var(--border); color: var(--text); font-family: var(--mono); font-size: 12px; padding: 8px 14px; border-radius: 0; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 100; }
.toast.show { opacity: 1; }
</style>
</head>
<body>

<aside class="sidebar">
  <div class="sidebar-section-label" style="padding-top:14px">sessions</div>
  <ul id="session-list"><li class="empty">loading&hellip;</li></ul>
</aside>

<div class="main">

<div class="header">
  <a href="https://angelsen.github.io/repld/" class="logo">repld<span class="cursor"></span></a>
  <span class="meta" id="hdr-pid"></span>
  <span class="meta" id="hdr-uptime"></span>
  <span class="spacer"></span>
  <div class="header-links">
    <a href="https://angelsen.github.io/repld/docs/">docs</a>
    <a href="https://github.com/angelsen/repld">github</a>
  </div>
  <div class="kernel-info">
    <span id="ki-tasks"></span>
    <span id="ki-tickers"></span>
  </div>
</div>

<div class="tab-bar" id="tab-bar">
  <button class="active" data-tab="browser">Browser</button>
  <button data-tab="connections">Connections</button>
  <button data-tab="targets">Targets</button>
  <button data-tab="console">Console</button>
  <button data-tab="network">Network</button>
</div>

<div class="content">
  <!-- BROWSER TAB -->
  <div class="tab-pane active" id="pane-browser">
    <div id="browser-unavailable" class="empty" hidden>repld[browser] not installed</div>
    <div id="browser-panel" hidden>
      <div class="section-label">connections</div>
      <div class="connect-row">
        <input type="number" id="chrome-port" value="9222" min="1" max="65535">
        <button id="btn-connect">Connect</button>
      </div>
      <ul class="pattern-list" id="ports-list"></ul>

      <div id="watch-section" hidden>
        <div class="section-label">watch patterns</div>
        <div class="pattern-row">
          <input type="text" id="watch-input" placeholder="*example.com*">
          <button id="btn-watch">Watch</button>
        </div>
        <table id="pattern-table" hidden>
          <thead><tr><th>pattern</th><th class="size">tabs</th><th class="actions"></th></tr></thead>
          <tbody id="pattern-body"></tbody>
        </table>

        <div class="section-label">attached tabs</div>
        <table id="tabs-table">
          <thead><tr><th class="type">type</th><th>url</th><th>title</th></tr></thead>
          <tbody id="tabs-body"></tbody>
        </table>
        <div class="empty" id="tabs-empty">no attached tabs</div>
      </div>
    </div>
  </div>

  <!-- CONNECTIONS TAB -->
  <div class="tab-pane" id="pane-connections">
    <div id="connections-unavailable" class="empty" hidden>repld[browser] not installed</div>
    <div id="connections-panel" hidden>
      <table id="connections-table" hidden>
        <thead><tr><th>port</th><th>tabs</th><th class="actions"></th></tr></thead>
        <tbody id="connections-body"></tbody>
      </table>
      <div class="empty" id="connections-empty">no browser connections</div>
    </div>
  </div>

  <!-- TARGETS TAB -->
  <div class="tab-pane" id="pane-targets">
    <div class="toolbar">
      <button id="btn-refresh-targets">Refresh</button>
    </div>
    <table id="targets-table" hidden>
      <thead><tr><th class="type">type</th><th>url</th><th class="actions"></th></tr></thead>
      <tbody id="targets-body"></tbody>
    </table>
    <div class="empty" id="targets-empty">not connected</div>
  </div>

  <!-- CONSOLE TAB -->
  <div class="tab-pane" id="pane-console">
    <div class="toolbar">
      <select id="console-tab-select"><option value="">select tab...</option></select>
      <button id="btn-refresh-console">Refresh</button>
    </div>
    <table id="console-table" hidden>
      <thead><tr><th class="level">level</th><th>message</th><th class="time">time</th></tr></thead>
      <tbody id="console-body"></tbody>
    </table>
    <div class="empty" id="console-empty">select a tab and click refresh</div>
  </div>

  <!-- NETWORK TAB -->
  <div class="tab-pane" id="pane-network">
    <div class="toolbar">
      <select id="network-tab-select"><option value="">select tab...</option></select>
      <button id="btn-refresh-network">Refresh</button>
    </div>
    <table id="network-table" hidden>
      <thead><tr><th class="method">method</th><th class="status-code">status</th><th>url</th><th class="size">size</th><th class="time">ms</th></tr></thead>
      <tbody id="network-body"></tbody>
    </table>
    <div class="empty" id="network-empty">select a tab and click refresh</div>
  </div>
</div>

<div class="footer">
  <span id="ft-socket"></span>
  <span id="ft-status"></span>
</div>

</div><!-- /.main -->

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let state = null;
let targets = null;
let activeTab = 'browser';

// --- tabs ---
function switchTab(name) {
  activeTab = name;
  location.hash = name;
  $$('#tab-bar button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + name));
}
$$('#tab-bar button').forEach(btn => { btn.onclick = () => switchTab(btn.dataset.tab); });
window.addEventListener('hashchange', () => { if (location.hash) switchTab(location.hash.slice(1)); });
if (location.hash) switchTab(location.hash.slice(1));

// --- RPC ---
const TOKEN = '__DASHBOARD_TOKEN__';

// Drop ?token= out of the address bar now that the page holds it. The server
// set a cookie for this port on the way in, so a refresh or a back-button
// still authenticates without it. Keeps the token out of the URL bar, the
// history entry, and anything the user copies out of either.
if (location.search) {
  try { history.replaceState(null, '', location.pathname + location.hash); } catch (e) {}
}
async function rpc(method, params = {}) {
  const res = await fetch('/api', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN },
    body: JSON.stringify({ jsonrpc: '2.0', method, params, id: Date.now() }),
  });
  const data = await res.json();
  if (data.error) { toast(data.error.message, true); throw new Error(data.error.message); }
  return data.result;
}

async function refreshState() {
  state = await rpc('state');
  render();
}

async function reload() {
  await refreshState();
  if (state?.browser?.connected) await refreshTargets();
}

function render() {
  if (!state) return;
  const k = state.kernel;

  $('#hdr-pid').textContent = 'pid ' + k.pid;
  $('#hdr-uptime').textContent = formatUptime(k.uptime_s);
  $('#ki-tasks').textContent = k.tasks_active ? k.tasks_active + ' task' + (k.tasks_active > 1 ? 's' : '') : '';
  $('#ki-tickers').textContent = k.tickers.length ? k.tickers.map(t => t.label).join(', ') : '';
  $('#ft-socket').textContent = k.socket;

  const b = state.browser;
  if (!b) {
    $('#browser-unavailable').hidden = false;
    $('#browser-panel').hidden = true;
    $('#connections-unavailable').hidden = false;
    $('#connections-panel').hidden = true;
    $('#ft-status').textContent = 'no browser';
    return;
  }
  $('#browser-unavailable').hidden = true;
  $('#browser-panel').hidden = false;
  $('#connections-unavailable').hidden = true;
  $('#connections-panel').hidden = false;

  $('#watch-section').hidden = !b.connected;
  const nPorts = (b.ports || []).length;
  const nTabs = b.tabs.length;
  $('#ft-status').textContent = b.connected
    ? nPorts + ' chrome' + (nPorts > 1 ? 's' : '') + ', ' + nTabs + ' tab' + (nTabs !== 1 ? 's' : '')
    : 'disconnected';

  // connected ports list
  const portsList = $('#ports-list');
  portsList.innerHTML = '';
  for (const p of (b.ports || [])) {
    const tabsOnPort = b.tabs.filter(t => t.port === p).length;
    const li = document.createElement('li');
    li.innerHTML = '<span class="status on"></span><span class="glob">:' + p + '</span> <span class="count">' + tabsOnPort + ' tab' + (tabsOnPort !== 1 ? 's' : '') + '</span>';
    portsList.appendChild(li);
  }

  // patterns
  const ptBody = $('#pattern-body');
  ptBody.innerHTML = '';
  $('#pattern-table').hidden = b.patterns.length === 0;
  for (const { pattern, count } of b.patterns) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + esc(pattern) + '</td>'
      + '<td class="size">' + count + '</td>'
      + '<td class="actions"></td>';
    const btn = document.createElement('button');
    btn.className = 'sm danger';
    btn.textContent = '\\u00d7';
    btn.onclick = async () => { await rpc('browser.unwatch', { pattern }); await reload(); };
    tr.querySelector('.actions').appendChild(btn);
    ptBody.appendChild(tr);
  }

  // attached tabs
  const tbody = $('#tabs-body');
  tbody.innerHTML = '';
  $('#tabs-empty').hidden = b.tabs.length > 0;
  for (const t of b.tabs) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="type">' + esc(t.type) + '</td>'
      + '<td class="url" title="' + esc(t.url) + '">' + esc(t.url) + '</td>'
      + '<td>' + esc(t.title || '') + '</td>';
    tbody.appendChild(tr);
  }

  // update tab selects for console/network
  updateTabSelects(b.tabs);

  // browser connections panel
  renderConnections(b);

  // auto-fetch targets on first connect
  if (b.connected && !targets) refreshTargets();
  if (targets) renderTargets();
}

// --- connections panel ---
function renderConnections(b) {
  const body = $('#connections-body');
  const ports = b.ports || [];
  $('#connections-empty').hidden = ports.length > 0;
  $('#connections-table').hidden = ports.length === 0;
  body.innerHTML = '';
  for (const p of ports) {
    const tabsOnPort = b.tabs.filter(t => t.port === p);
    const portRow = document.createElement('tr');
    portRow.className = 'conn-port';
    const countText = tabsOnPort.length + ' tab' + (tabsOnPort.length !== 1 ? 's' : '');
    portRow.innerHTML = '<td>:' + p + '</td><td class="type">' + countText + '</td><td class="actions"></td>';
    const disconnectBtn = document.createElement('button');
    disconnectBtn.className = 'sm danger';
    disconnectBtn.textContent = 'Disconnect';
    disconnectBtn.onclick = async (e) => {
      e.stopPropagation();
      await rpc('browser.disconnect', { port: p });
      toast('Disconnected port ' + p);
      await reload();
    };
    portRow.querySelector('.actions').appendChild(disconnectBtn);
    body.appendChild(portRow);

    for (const t of tabsOnPort) {
      const tr = document.createElement('tr');
      tr.className = 'conn-target collapsed';
      tr.innerHTML = '<td class="type">' + esc(t.type) + '</td>'
        + '<td class="url" title="' + esc(t.url) + '">' + esc(t.title || t.url) + '</td>'
        + '<td class="actions"></td>';
      const detachBtn = document.createElement('button');
      detachBtn.className = 'sm danger';
      detachBtn.textContent = 'Detach';
      detachBtn.onclick = async (e) => {
        e.stopPropagation();
        await rpc('browser.disconnect', { target: t.id });
        toast('Detached ' + t.id);
        await reload();
      };
      tr.querySelector('.actions').appendChild(detachBtn);
      body.appendChild(tr);
    }
    portRow.onclick = () => {
      let next = portRow.nextElementSibling;
      while (next && next.classList.contains('conn-target')) {
        next.classList.toggle('collapsed');
        next = next.nextElementSibling;
      }
    };
  }
}

function updateTabSelects(tabs) {
  for (const sel of [$('#console-tab-select'), $('#network-tab-select')]) {
    const cur = sel.value;
    const opts = '<option value="">select tab...</option>' +
      tabs.map(t => '<option value="' + esc(t.target_id) + '"' + (t.target_id === cur ? ' selected' : '') + '>' + esc(truncUrl(t.url, 60)) + '</option>').join('');
    sel.innerHTML = opts;
  }
}

// --- targets ---
async function refreshTargets() {
  try {
    targets = await rpc('browser.targets');
    renderTargets();
  } catch (e) { /* toast shown */ }
}

function renderTargets() {
  if (!targets || !state?.browser) return;
  const tbody = $('#targets-body');
  tbody.innerHTML = '';
  $('#targets-table').hidden = targets.length === 0;
  $('#targets-empty').hidden = targets.length > 0;
  if (!targets.length) { $('#targets-empty').textContent = 'no targets'; }

  const attachedIds = new Set((state.browser.tabs || []).map(t => t.target_id));

  for (const t of targets) {
    const attached = attachedIds.has(t.targetId);
    const tr = document.createElement('tr');
    const origin = urlOrigin(t.url);
    tr.innerHTML = '<td class="type">' + esc(t.type) + '</td>'
      + '<td class="url" title="' + esc(t.url) + '">' + esc(t.url) + '</td>'
      + '<td class="actions">'
      + (attached
          ? '<span style="color:var(--green);font-size:10px">attached</span>'
          : (origin ? '<button class="sm" data-origin="' + esc(origin) + '">watch</button>' : ''))
      + '</td>';
    tbody.appendChild(tr);
  }
  // bind quick-watch buttons
  tbody.querySelectorAll('button[data-origin]').forEach(btn => {
    btn.onclick = async () => {
      const pattern = '*' + btn.dataset.origin + '*';
      try {
        const r = await rpc('browser.watch', { pattern });
        toast(r.result);
        await reload();
      } catch (e) { /* toast shown */ }
    };
  });
}

$('#btn-refresh-targets').onclick = refreshTargets;

// --- console ---
$('#btn-refresh-console').onclick = async () => {
  const tid = $('#console-tab-select').value;
  if (!tid) { toast('Select a tab first', true); return; }
  try {
    const rows = await rpc('browser.console', { target_id: tid });
    const tbody = $('#console-body');
    tbody.innerHTML = '';
    $('#console-table').hidden = rows.length === 0;
    $('#console-empty').hidden = rows.length > 0;
    if (!rows.length) $('#console-empty').textContent = 'no console messages';
    for (const r of rows) {
      const tr = document.createElement('tr');
      const lvl = r.level || 'log';
      tr.innerHTML = '<td class="level ' + esc(lvl) + '">' + esc(lvl) + '</td>'
        + '<td class="console-text">' + esc(r.text || '') + '</td>'
        + '<td class="time">' + formatTs(r.timestamp) + '</td>';
      tbody.appendChild(tr);
    }
  } catch (e) { /* toast shown */ }
};

// --- network ---
$('#btn-refresh-network').onclick = async () => {
  const tid = $('#network-tab-select').value;
  if (!tid) { toast('Select a tab first', true); return; }
  try {
    const rows = await rpc('browser.network', { target_id: tid });
    const tbody = $('#network-body');
    tbody.innerHTML = '';
    $('#network-table').hidden = rows.length === 0;
    $('#network-empty').hidden = rows.length > 0;
    if (!rows.length) $('#network-empty').textContent = 'no network requests';
    for (const r of rows) {
      const tr = document.createElement('tr');
      const sc = r.status;
      const scColor = sc >= 400 ? 'var(--red)' : sc >= 300 ? 'var(--amber)' : 'var(--green)';
      tr.innerHTML = '<td class="method">' + esc(r.method) + '</td>'
        + '<td class="status-code" style="color:' + scColor + '">' + sc + '</td>'
        + '<td class="url" title="' + esc(r.url) + '">' + esc(r.url) + '</td>'
        + '<td class="size">' + formatSize(r.size) + '</td>'
        + '<td class="time">' + (r.time_ms != null ? r.time_ms + '' : '') + '</td>';
      tbody.appendChild(tr);
    }
  } catch (e) { /* toast shown */ }
};

// --- actions ---
$('#btn-connect').onclick = async () => {
  const port = parseInt($('#chrome-port').value) || 9222;
  try {
    await rpc('browser.connect', { port });
    toast('Connected to port ' + port);
    await reload();
  } catch (e) { /* toast shown */ }
};

$('#btn-watch').onclick = async () => {
  const input = $('#watch-input');
  const pattern = input.value.trim();
  if (!pattern) return;
  try {
    const r = await rpc('browser.watch', { pattern });
    toast(r.result);
    input.value = '';
    await reload();
  } catch (e) { /* toast shown */ }
};

$('#watch-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-watch').click(); });
$('#chrome-port').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-connect').click(); });

// --- sidebar: sessions ---
async function refreshSessions() {
  try {
    const list = await rpc('sessions');
    renderSessions(list);
  } catch (e) { /* toast shown */ }
}

function renderSessions(list) {
  const ul = $('#session-list');
  const currentPid = state?.kernel?.pid;
  ul.innerHTML = '';
  if (!list.length) {
    ul.innerHTML = '<li class="empty">no sessions found</li>';
    return;
  }
  list.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  for (const s of list) {
    const li = document.createElement('li');
    const isCurrent = s.pid === currentPid;
    if (isCurrent) li.classList.add('current');
    const name = (s.cwd || '').split('/').filter(Boolean).pop() || s.cwd || ('pid ' + s.pid);
    const uptime = formatUptime(Date.now() / 1000 - (s.started_at || 0));
    const dot = '<span class="status on"></span>';
    // A sibling dashboard refuses an unauthenticated GET / exactly as this
    // one does, so its link has to carry that kernel's own token — supplied
    // by the sessions RPC, which reads each peer's 0600 hint file. Without a
    // token there is nothing to link to but the 401 page, so render it plain.
    if (isCurrent || !s.dashboard_port || !s.dashboard_token) {
      li.innerHTML = dot + '<span class="session-name" title="' + esc(s.cwd || '') + '">' + esc(name) + '</span>'
        + '<span class="session-uptime">' + uptime + '</span>';
    } else {
      const href = 'http://127.0.0.1:' + s.dashboard_port + '/?token=' + encodeURIComponent(s.dashboard_token);
      li.innerHTML = dot + '<a href="' + esc(href) + '" rel="noreferrer" title="' + esc(s.cwd || '') + '">' + esc(name) + '</a>'
        + '<span class="session-uptime">' + uptime + '</span>';
    }
    ul.appendChild(li);
  }
}

refreshSessions();
setInterval(refreshSessions, 10000);

// --- initial load ---
// Polled on the same cadence as the sidebar. Fetching it once left uptime,
// active tasks, tickers and the tab table frozen at page-load values for as
// long as the panel stayed open, while the sidebar kept ticking beside them —
// so the stale half read as live, and anything started from exec (a ticker, a
// browser.watch) never showed up at all.
refreshState();
setInterval(refreshState, 10000);

// --- util ---
function formatUptime(s) {
  s = Math.floor(s);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}
function formatSize(b) {
  if (!b) return '';
  if (b < 1024) return b + 'B';
  if (b < 1048576) return (b / 1024).toFixed(1) + 'K';
  return (b / 1048576).toFixed(1) + 'M';
}
function formatTs(ts) {
  if (!ts) return '';
  try { const d = new Date(parseFloat(ts) * 1000); return d.toLocaleTimeString(undefined, {hour12: false}); } catch { return ''; }
}
// Safe in attribute position as well as in text, which the textContent →
// innerHTML round-trip alone is not: that serialization escapes & < > (and
// nbsp) but never quotes, because a text node has no quotes to escape. Every
// use below is `title="' + esc(x) + '"`, `href=`, `data-origin=`, `value=` —
// so a single " in a tab URL, a page title or a project path closed the
// attribute and let the rest of the string inject markup into the page that
// carries the API token. Quotes explicitly, then.
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function truncUrl(url, n) { return url.length > n ? url.slice(0, n - 1) + '\\u2026' : url; }
function urlOrigin(url) {
  try { const u = new URL(url); return u.host; } catch { return ''; }
}
let toastTimer = null;
function toast(msg, isError) {
  const el = $('#toast');
  el.textContent = msg;
  el.style.borderColor = isError ? 'var(--red)' : 'var(--accent)';
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}
</script>
</body>
</html>
"""
