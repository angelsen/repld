"""Pin/gate/label UI — JS/CSS blobs and the CDP binding handler behind
Tab.pin(), Tab._set_label(), and the human-gate pill.
"""

import json

__all__ = [
    "_PIN_JS",
    "_LABEL_JS",
    "_next_label_color",
    "_handle_binding",
]

# ---------------------------------------------------------------------------
# Pill JS/CSS blob — injected via Runtime.evaluate on tab.pin()
# ---------------------------------------------------------------------------
_PIN_JS = r"""
(function() {
  if (window.__repld_pill) {
    // Already injected — idempotent, just ensure update function is live
    return;
  }

  // ---- CSS ----
  var style = document.createElement('style');
  style.id = '__repld_style';
  style.textContent = `
    #__repld_pill {
      position: fixed;
      bottom: 18px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 2147483647;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 13px;
      pointer-events: auto;
    }
    #__repld_pill * { box-sizing: border-box; }
    #__repld_pill_bar {
      display: flex;
      align-items: center;
      gap: 7px;
      background: rgba(20,20,28,0.92);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 999px;
      padding: 5px 14px 5px 10px;
      cursor: pointer;
      user-select: none;
      box-shadow: 0 4px 24px rgba(0,0,0,0.45);
      transition: background 0.15s;
    }
    #__repld_pill_bar:hover { background: rgba(30,30,42,0.97); }
    #__repld_dot {
      width: 9px; height: 9px;
      border-radius: 50%;
      background: #22c55e;
      flex-shrink: 0;
      transition: background 0.2s;
    }
    #__repld_dot.amber {
      background: #f59e0b;
      animation: __repld_pulse 1s ease-in-out infinite;
    }
    @keyframes __repld_pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
    }
    #__repld_label {
      color: rgba(255,255,255,0.88);
      white-space: nowrap;
    }
    #__repld_panel {
      display: none;
      margin-top: 6px;
      background: rgba(20,20,28,0.96);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 14px;
      padding: 14px 16px;
      min-width: 280px;
      max-width: 380px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.6);
      color: rgba(255,255,255,0.80);
    }
    #__repld_panel.open { display: block; }
    .__repld_row {
      display: flex;
      gap: 8px;
      margin-bottom: 6px;
      font-size: 12px;
    }
    .__repld_row_label {
      color: rgba(255,255,255,0.42);
      min-width: 60px;
      flex-shrink: 0;
    }
    .__repld_row_value { color: rgba(255,255,255,0.82); word-break: break-all; }
    #__repld_gate_area {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid rgba(255,255,255,0.08);
    }
    #__repld_gate_prompt {
      font-size: 13px;
      color: rgba(255,255,255,0.90);
      margin-bottom: 10px;
      line-height: 1.4;
    }
    #__repld_gate_buttons {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .__repld_btn {
      padding: 5px 14px;
      border-radius: 7px;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.08);
      color: rgba(255,255,255,0.90);
      cursor: pointer;
      font-size: 12px;
      transition: background 0.12s;
    }
    .__repld_btn:hover { background: rgba(255,255,255,0.16); }
    .__repld_btn.primary {
      background: #3b82f6;
      border-color: #3b82f6;
      color: #fff;
    }
    .__repld_btn.primary:hover { background: #2563eb; }
    #__repld_pending_count {
      font-size: 11px;
      color: rgba(255,255,255,0.38);
      margin-top: 8px;
    }
  `;
  document.head.appendChild(style);

  // ---- DOM (createElement only — no innerHTML, Trusted Types safe) ----
  function _el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function(k) {
      if (k === 'text') e.textContent = attrs[k];
      else if (k === 'style') e.style.cssText = attrs[k];
      else e[k] = attrs[k];
    });
    if (children) children.forEach(function(c) { e.appendChild(c); });
    return e;
  }

  var pill = _el('div', {id: '__repld_pill'}, [
    _el('div', {id: '__repld_pill_bar'}, [
      _el('div', {id: '__repld_dot'}),
      _el('span', {id: '__repld_label', text: 'repld'})
    ]),
    _el('div', {id: '__repld_panel'}, [
      _el('div', {className: '__repld_row'}, [
        _el('span', {className: '__repld_row_label', text: 'status'}),
        _el('span', {className: '__repld_row_value', id: '__repld_status', text: 'connected'})
      ]),
      _el('div', {className: '__repld_row'}, [
        _el('span', {className: '__repld_row_label', text: 'host'}),
        _el('span', {className: '__repld_row_value', id: '__repld_host'})
      ]),
      _el('div', {className: '__repld_row', id: '__repld_reason_row', style: 'display:none'}, [
        _el('span', {className: '__repld_row_label', text: 'reason'}),
        _el('span', {className: '__repld_row_value', id: '__repld_reason'})
      ]),
      _el('div', {id: '__repld_gate_area', style: 'display:none'}, [
        _el('div', {id: '__repld_gate_prompt'}),
        _el('div', {id: '__repld_gate_buttons'}),
        _el('div', {id: '__repld_pending_count'})
      ])
    ])
  ]);
  document.body.appendChild(pill);

  // Set host
  document.getElementById('__repld_host').textContent = location.hostname;

  // Toggle panel on pill bar click
  var pillBar = document.getElementById('__repld_pill_bar');
  var panel = document.getElementById('__repld_panel');
  pillBar.addEventListener('click', function() {
    panel.classList.toggle('open');
  });

  // ---- Gate queue ----
  var _gate_queue = [];
  var _active_gate = null;

  function _render_gate() {
    var area = document.getElementById('__repld_gate_area');
    var promptEl = document.getElementById('__repld_gate_prompt');
    var buttonsEl = document.getElementById('__repld_gate_buttons');
    var pendingEl = document.getElementById('__repld_pending_count');
    var dot = document.getElementById('__repld_dot');
    var label = document.getElementById('__repld_label');
    var statusEl = document.getElementById('__repld_status');

    if (!_active_gate) {
      area.style.display = 'none';
      dot.className = '';
      label.textContent = 'repld';
      statusEl.textContent = 'connected';
      pendingEl.textContent = '';
      return;
    }

    area.style.display = 'block';
    panel.classList.add('open');
    dot.className = 'amber';
    label.textContent = 'repld';
    statusEl.textContent = 'awaiting input';
    promptEl.textContent = _active_gate.prompt;

    // Build buttons
    while (buttonsEl.firstChild) buttonsEl.removeChild(buttonsEl.firstChild);
    _active_gate.buttons.forEach(function(btn) {
      var el = document.createElement('button');
      el.className = '__repld_btn' + (btn.style === 'primary' ? ' primary' : '');
      el.textContent = btn.label;
      el.addEventListener('click', function() {
        var gid = _active_gate.gate_id;
        var val = btn.value;
        _active_gate = null;
        if (_gate_queue.length > 0) {
          _active_gate = _gate_queue.shift();
        }
        _render_gate();
        window.__repld_resolve(JSON.stringify({gate_id: gid, value: val}));
      });
      buttonsEl.appendChild(el);
    });

    var remaining = _gate_queue.length;
    pendingEl.textContent = remaining > 0 ? remaining + ' more pending' : '';
  }

  // ---- Public API ----
  window.__repld_pill = true;

  window.__repld_update = function(opts) {
    if (opts.reason !== undefined) {
      var reasonRow = document.getElementById('__repld_reason_row');
      var reasonEl = document.getElementById('__repld_reason');
      if (opts.reason) {
        reasonEl.textContent = opts.reason;
        reasonRow.style.display = 'flex';
      } else {
        reasonRow.style.display = 'none';
      }
    }
  };

  window.__repld_gate = function(gate_id, prompt, buttons) {
    var entry = {gate_id: gate_id, prompt: prompt, buttons: buttons};
    if (!_active_gate) {
      _active_gate = entry;
    } else {
      _gate_queue.push(entry);
    }
    _render_gate();
  };

  window.__repld_remove = function() {
    if (window.__repld_hb_timer) clearInterval(window.__repld_hb_timer);
    window.removeEventListener('beforeunload', window.__repld_beforeunload);
    var el = document.getElementById('__repld_pill');
    if (el) el.remove();
    var st = document.getElementById('__repld_style');
    if (st) st.remove();
    window.__repld_pill = false;
    window.__repld_hb = undefined;
    window.__repld_hb_timer = undefined;
    window.__repld_beforeunload = undefined;
    window.__repld_update = undefined;
    window.__repld_gate = undefined;
    window.__repld_remove = undefined;
  };

  // ---- Heartbeat (liveness) ----
  window.__repld_hb = Date.now();
  window.__repld_hb_timer = setInterval(function() {
    if (Date.now() - window.__repld_hb > 15000) {
      if (window.__repld_remove) window.__repld_remove();
    }
  }, 5000);

  // ---- beforeunload guard ----
  window.__repld_beforeunload = function(e) {
    e.preventDefault();
    e.returnValue = 'repld is using this tab. Leave anyway?';
    return e.returnValue;
  };
  window.addEventListener('beforeunload', window.__repld_beforeunload);
})();
"""


# ---------------------------------------------------------------------------
# Label JS — injected via Page.addScriptToEvaluateOnNewDocument
# ---------------------------------------------------------------------------
_LABEL_JS = r"""
(function() {
  if (document.getElementById('__repld_label_bar')) return;
  var el = document.createElement('div');
  el.id = '__repld_label_bar';
  el.textContent = %TEXT%;
  el.style.cssText = 'position:fixed;top:0;left:0;right:0;height:24px;'
    + 'background:%COLOR%;color:#fff;font:bold 12px system-ui;'
    + 'display:flex;align-items:center;justify-content:center;'
    + 'z-index:2147483647;pointer-events:none;';
  document.body.style.paddingTop = '24px';
  document.body.appendChild(el);
})();
"""

_LABEL_PALETTE = ["#ef4444", "#3b82f6", "#22c55e", "#a855f7", "#f59e0b", "#06b6d4"]
_label_color_index = 0


def _next_label_color() -> str:
    global _label_color_index
    color = _LABEL_PALETTE[_label_color_index % len(_LABEL_PALETTE)]
    _label_color_index += 1
    return color


async def _handle_binding(session, params: dict) -> None:
    """Handle __repld_resolve callback from pill UI."""
    payload_str = params.get("payload", "{}")
    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, TypeError):
        return
    gate_id = payload.get("gate_id")
    value = payload.get("value")
    if gate_id:
        from ..gates import resolve_gate

        resolve_gate(gate_id, value)
