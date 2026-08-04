"""Dashboard markup: the control-panel page and the refusal page.

Split out of `dashboard.py`, which was 1321 lines of which 657 were this
string. Nothing here is Python — it is HTML, CSS and JS living in a Python
module so it ships in the wheel without a package-data entry, and keeping it
next to the HTTP server, the auth ladder and the JSON-RPC dispatch meant the
half of that file a reader had to skip was the larger half.

`PAGE` carries the API token, substituted at serve time for the
`__DASHBOARD_TOKEN__` placeholder — see `dashboard._handle_connection`, which
is also what refuses to serve it unauthenticated.
"""

UNAUTHORIZED_PAGE = """<!doctype html>
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


PAGE = """\
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
