"""Dashboard: browser control panel + kernel status served over HTTP.

Pure-stdlib async HTTP server on an ephemeral port.  Two routes:
  GET /      → inline HTML page (`dashboard_html.PAGE`)
  POST /api  → JSON-RPC commands (state, browser.connect, browser.watch, etc.)

The markup lives in `dashboard_html.py`. This module is the server: routing,
the auth ladder, and the JSON-RPC dispatch.
"""

import asyncio
import json
import os
import secrets
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import __main__

from . import tasks
from .channel import push_kind
from .dashboard_html import PAGE, UNAUTHORIZED_PAGE
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
        from .browser.cdp import _no_capture_patterns, _suppress_patterns

        if _suppress_patterns:
            hint["suppress"] = sorted(_suppress_patterns)
        else:
            hint.pop("suppress", None)

        if _no_capture_patterns:
            hint["no_capture"] = sorted(_no_capture_patterns)
        else:
            hint.pop("no_capture", None)
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
                    UNAUTHORIZED_PAGE.encode("utf-8"),
                    "text/html; charset=utf-8",
                    origin=origin,
                )
                return
            html = PAGE.replace("__DASHBOARD_TOKEN__", _token)
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
        TimeoutError,
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
