"""Dashboard: browser control panel + kernel status served over HTTP.

Pure-stdlib async HTTP server on an ephemeral port.  Two routes:
  GET /        → inline HTML page
  POST /api    → JSON-RPC commands (state, browser.connect, browser.watch, etc.)
"""

import __main__
import asyncio
import json
import os
import time
from typing import Any

from . import tasks

_start_time: float = 0.0
_socket_path: str = ""
_server: asyncio.Server | None = None


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------


def _collect_state() -> dict:
    active = sum(1 for t in tasks._tasks.values() if not t["done_event"].is_set())
    from .kernel import _every_registry

    tickers = [{"label": h.label, "seconds": h.seconds} for h in _every_registry]
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

    pool = getattr(browser, "_real", browser)
    if pool is None:
        state["browser"] = {
            "available": True,
            "connected": False,
            "ports": [],
            "patterns": [],
            "tabs": [],
        }
        return state

    connected = getattr(pool, "_connected", False)
    ports = getattr(pool, "ports", [])
    patterns = getattr(pool, "patterns", []) if connected else []
    tab_list = []
    if connected:
        try:
            browsers = getattr(pool, "_browsers", {})
            for port, b in browsers.items():
                if not b._connected:
                    continue
                for cdp in b._session._sessions.values():
                    info = cdp.target_info
                    tab_list.append(
                        {
                            "id": f"{port}:{info.get('targetId', '?')[:6]}",
                            "target_id": info.get("targetId", ""),
                            "port": port,
                            "type": info.get("type", ""),
                            "url": info.get("url", ""),
                            "title": info.get("title", ""),
                        }
                    )
        except Exception:
            pass

    state["browser"] = {
        "available": True,
        "connected": connected,
        "ports": ports,
        "patterns": patterns,
        "tabs": tab_list,
    }
    return state


def _resolve_tab(target_id: str):
    """Find an attached Tab by its raw Chrome targetId."""
    browser = getattr(__main__, "browser", None)
    if browser is None:
        raise RuntimeError("no browser")
    pool = getattr(browser, "_real", browser)
    if pool is None or not getattr(pool, "_connected", False):
        raise RuntimeError("not connected")

    browsers = getattr(pool, "_browsers", {})
    for port, b in browsers.items():
        if not b._connected:
            continue
        from .browser.tab import Tab

        for cdp in b._session._sessions.values():
            if cdp.target_info.get("targetId") == target_id:
                return Tab(cdp, target_id, port)
    raise RuntimeError(f"tab not attached: {target_id}")


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------


async def _rpc_dispatch(method: str, params: dict) -> Any:
    if method == "state":
        return _collect_state()

    from .kernel import push_channel

    browser = getattr(__main__, "browser", None)
    if browser is None:
        raise RuntimeError("repld[browser] not installed")

    if method == "browser.connect":
        port = params.get("port", 9222)
        pool = getattr(browser, "_real", None)
        if pool is None:
            from .browser import BrowserPool

            pool = BrowserPool()
            browser._real = pool
        await pool.connect(port)
        push_channel(
            f"[dashboard] connected to Chrome on port {port}",
            {"kind": "browser_connect", "port": str(port)},
        )
        return {"connected": True, "port": port}

    if method == "browser.targets":
        real = getattr(browser, "_real", browser)
        if real is None or not getattr(real, "_connected", False):
            raise RuntimeError("Not connected to Chrome")
        targets = await real.pages()
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

    if method == "browser.watch":
        pattern = params.get("pattern", "")
        if not pattern:
            raise RuntimeError("pattern is required")
        result = await browser.watch(pattern)
        push_channel(
            f"[dashboard] watch '{pattern}': {result}",
            {"kind": "browser_watch", "pattern": pattern},
        )
        return {"result": result}

    if method == "browser.unwatch":
        pattern = params.get("pattern", "")
        if not pattern:
            raise RuntimeError("pattern is required")
        result = await browser.detach(pattern)
        push_channel(
            f"[dashboard] unwatch '{pattern}': {result}",
            {"kind": "browser_unwatch", "pattern": pattern},
        )
        return {"result": result}

    if method == "browser.console":
        target_id = params.get("target_id", "")
        tab = _resolve_tab(target_id)
        rows = tab.console()
        return [
            {
                "level": r.level,
                "source": r.source,
                "text": r.text[:500],
                "timestamp": r.timestamp,
            }
            for r in rows[:50]
        ]

    if method == "browser.network":
        target_id = params.get("target_id", "")
        tab = _resolve_tab(target_id)
        rows = tab.network()
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

    raise RuntimeError(f"Unknown method: {method}")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


async def _send_response(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    content_type: str = "application/json",
    extra_headers: str = "",
) -> None:
    reason = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        500: "Internal Server Error",
    }.get(status, "OK")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        f"{extra_headers}"
        "\r\n"
    )
    writer.write(header.encode() + body)
    await writer.drain()


async def _handle_api(body: bytes) -> bytes:
    try:
        req = json.loads(body)
    except json.JSONDecodeError:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
        ).encode()

    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    try:
        result = await _rpc_dispatch(method, params)
        return json.dumps(
            {"jsonrpc": "2.0", "result": result, "id": req_id}, separators=(",", ":")
        ).encode()
    except Exception as exc:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": str(exc)},
                "id": req_id,
            }
        ).encode()


async def _handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not request_line:
            return

        parts = request_line.decode("utf-8", errors="replace").strip().split()
        if len(parts) < 2:
            return
        method_http, path = parts[0], parts[1]

        content_length = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line in (b"\r\n", b"\n", b""):
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":")[1].strip())

        if method_http == "OPTIONS":
            cors = (
                "HTTP/1.1 204 No Content\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: POST, GET, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type\r\n"
                "\r\n"
            )
            writer.write(cors.encode())
            await writer.drain()
            return

        if method_http == "GET" and path == "/":
            await _send_response(
                writer, 200, _HTML.encode("utf-8"), "text/html; charset=utf-8"
            )
            return

        if method_http == "POST" and path == "/api":
            body = (
                await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)
                if content_length
                else b"{}"
            )
            result = await _handle_api(body)
            await _send_response(writer, 200, result)
            return

        await _send_response(writer, 404, b'{"error":"not found"}')

    except (
        asyncio.TimeoutError,
        asyncio.IncompleteReadError,
        ConnectionResetError,
        BrokenPipeError,
    ):
        pass
    except Exception:
        try:
            await _send_response(writer, 500, b'{"error":"internal"}')
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
    loop: asyncio.AbstractEventLoop,
    socket_path: str,
    start_time: float,
    preferred_port: int,
) -> int:
    global _start_time, _socket_path, _server
    _start_time = start_time
    _socket_path = socket_path

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
) -> int:
    """Start the dashboard HTTP server. Returns the bound port."""
    future = asyncio.run_coroutine_threadsafe(
        _start(loop, socket_path, start_time, preferred_port), loop
    )
    return future.result(timeout=5.0)


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
  --text: #e4e4e7; --dim: #71717a; --accent: #22d3ee;
  --green: #4ade80; --red: #f87171; --amber: #fbbf24;
  --mono: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; overflow: hidden; }
body { display: flex; flex-direction: column; max-width: 960px; margin: 0 auto; border-left: 1px solid var(--border); border-right: 1px solid var(--border); }

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
button.sm { padding: 2px 8px; font-size: 10px; }
button.danger { color: var(--red); }

.section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); margin: 16px 0 6px; }
.section-label:first-child { margin-top: 0; }

.pattern-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.pattern-row input { flex: 1; }
.pattern-list { list-style: none; margin-bottom: 8px; }
.pattern-list li { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 12px; padding: 3px 0; }
.pattern-list li .glob { color: var(--accent); }
.pattern-list li .count { color: var(--dim); font-size: 11px; }

table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
th { text-align: left; color: var(--dim); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; background: var(--bg); }
td { padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
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
async function rpc(method, params = {}) {
  const res = await fetch('/api', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
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
    $('#ft-status').textContent = 'no browser';
    return;
  }
  $('#browser-unavailable').hidden = true;
  $('#browser-panel').hidden = false;

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
  for (const p of b.patterns) {
    const count = b.tabs.filter(t => matchGlob(t.url, p)).length;
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + esc(p) + '</td>'
      + '<td class="size">' + count + '</td>'
      + '<td class="actions"></td>';
    const btn = document.createElement('button');
    btn.className = 'sm danger';
    btn.textContent = '\\u00d7';
    btn.onclick = async () => { await rpc('browser.unwatch', { pattern: p }); await reload(); };
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

  // auto-fetch targets on first connect
  if (b.connected && !targets) refreshTargets();
  if (targets) renderTargets();
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
  const filtered = targets;
  const tbody = $('#targets-body');
  tbody.innerHTML = '';
  $('#targets-table').hidden = filtered.length === 0;
  $('#targets-empty').hidden = filtered.length > 0;
  if (!filtered.length) { $('#targets-empty').textContent = 'no targets'; }

  const attachedIds = new Set((state.browser.tabs || []).map(t => t.target_id));

  for (const t of filtered) {
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

// --- initial load ---
refreshState();

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
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
function truncUrl(url, n) { return url.length > n ? url.slice(0, n - 1) + '\\u2026' : url; }
function urlOrigin(url) {
  try { const u = new URL(url); return u.host; } catch { return ''; }
}
function matchGlob(str, pattern) {
  const re = new RegExp('^' + pattern.replace(/[.+^${}()|[\\]\\\\]/g, '\\\\$&').replace(/\\*/g, '.*').replace(/\\?/g, '.') + '$');
  return re.test(str);
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
