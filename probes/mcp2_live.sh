#!/usr/bin/env bash
# Probe: disposable Claude Code sessions against probes/mcp2_bridge.py and a scratch kernel.
#
# Everything lives under .live.local/mcp2 (gitignored) and an isolated tmux
# server; nothing here touches another kernel, pane or session. Each verb is one
# step, so a row in RUNLOG.md is a few of these typed in order.
#
#   mcp2_live.sh kernel            start the scratch kernel, print pid and socket
#   mcp2_live.sh cell CODE         run a cell in it from outside any session
#   mcp2_live.sh tui NAME [K=V..]  an interactive session in tmux window NAME
#   mcp2_live.sh print NAME PROMPT [K=V..]   claude -p, output to stdout
#   mcp2_live.sh bg NAME PROMPT [K=V..]      claude --bg
#   mcp2_live.sh say NAME TEXT     type TEXT and Enter into window NAME
#   mcp2_live.sh key NAME KEY..    raw tmux keys
#   mcp2_live.sh see NAME [N]      last N non-blank screen lines
#   mcp2_live.sh log NAME          the bridge's wire log, one short line per message
#   mcp2_live.sh down              stop the kernel and the tmux server
#
# K=V pairs become the bridge's environment (MCP2_REGISTER=1, MCP2_PAD=6000...).
# MCP2_CLAUDE_ARGS adds flags to a tui launch (--resume ID); MCP2_TOOLS names built-in tools (default none).
set -euo pipefail

WT=$(cd "$(dirname "$0")/.." && pwd)
ROOT=$WT/.live.local/mcp2
PROJ=$ROOT/proj
export TMUX_TMPDIR=$XDG_RUNTIME_DIR/mcp2-probe-tmux
unset TMUX
mkdir -p "$PROJ" "$TMUX_TMPDIR"

# The kernel gets a config and state home of its own: it writes a gist registry and a
# discovery cache there (RUNLOG, Session 1, "Left behind"). XDG_RUNTIME_DIR stays real,
# since the sessions' inbox sockets live under it.
export XDG_CONFIG_HOME=$ROOT/xdg-config XDG_STATE_HOME=$ROOT/xdg-state
repld() { (cd "$PROJ" && uv run --project "$WT" repld --project "$PROJ" "$@"); }
sock() { repld status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["kernel"]["socket_path"])'; }
clean() { env -i HOME="$HOME" USER="$USER" PATH=/usr/bin:/bin TERM=xterm-256color LANG=C.UTF-8 XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" "$@"; }

config() { # NAME K=V... -> path of an --mcp-config file
  local name=$1; shift
  python3 - "$ROOT/$name.mcp.json" "$WT/probes/mcp2_bridge.py" "$(cat "$ROOT/socket")" "$ROOT/$name.jsonl" "$@" <<'PY'
import json, sys
out, bridge, kernel, log, *pairs = sys.argv[1:]
env = {"MCP2_KERNEL_SOCKET": kernel, "MCP2_LOG": log, **dict(p.split("=", 1) for p in pairs)}
server = {"command": "/usr/bin/python3", "args": [bridge], "env": env}
json.dump({"mcpServers": {"repld2": server}}, open(out, "w"))
print(out)
PY
}

FLAGS=(--model haiku --strict-mcp-config --tools "${MCP2_TOOLS:-}" --allowedTools mcp__repld2)

case $1 in
kernel)
  repld start
  sock >"$ROOT/socket"
  repld status | head -3
  ;;
cell) repld exec "$2" ;;
tui)
  name=$2; shift 2
  cfg=$(config "$name" "$@")
  cmd="cd '$PROJ' && env -i HOME='$HOME' USER='$USER' PATH=/usr/bin:/bin TERM=xterm-256color LANG=C.UTF-8 XDG_RUNTIME_DIR='$XDG_RUNTIME_DIR' /usr/bin/claude ${FLAGS[*]@Q} --mcp-config '$cfg' -n 'mcp2-$name' ${MCP2_CLAUDE_ARGS:-}; echo CLAUDE-EXIT \$?; sleep 600"
  if tmux has-session -t probe 2>/dev/null; then
    tmux new-window -d -t probe -n "$name" "$cmd"
  else
    tmux new-session -d -s probe -n "$name" -x 140 -y 45 "$cmd"
  fi
  ;;
print)
  name=$2 prompt=$3; shift 3
  cfg=$(config "$name" "$@")
  (cd "$PROJ" && clean /usr/bin/claude "${FLAGS[@]}" --mcp-config "$cfg" -p "$prompt" </dev/null)
  ;;
bg)
  name=$2 prompt=$3; shift 3
  cfg=$(config "$name" "$@")
  # The prompt follows -n: after a variadic flag (--mcp-config, --allowedTools) it is read as that flag's value.
  (cd "$PROJ" && clean setsid -w /usr/bin/claude --bg "${FLAGS[@]}" --mcp-config "$cfg" -n "mcp2-$name" "$prompt" </dev/null)
  ;;
say) tmux send-keys -t "probe:$2" -l "$3"; sleep 0.4; tmux send-keys -t "probe:$2" Enter ;;
key) name=$2; shift 2; tmux send-keys -t "probe:$name" "$@" ;;
see) tmux capture-pane -p -J -t "probe:$2" | grep -v '^\s*$' | tail -n "${3:-25}" ;;
log)
  python3 - "$ROOT/$2.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    m = r["msg"]
    if isinstance(m, dict):
        p = m.get("params") or {}
        if isinstance(p, dict):
            p = {k: v for k, v in p.items() if k != "_meta"}
        brief = m.get("method") or ("result" in m and "result") or ("error" in m and "error") or ""
        body = json.dumps(p or m.get("result") or m.get("error") or (m if not brief else ""))[:230]
    else:
        brief, body = "", str(m)
    print(r["t"][11:23], r["pid"], r["dir"], m.get("id", "") if isinstance(m, dict) else "", brief, body)
PY
  ;;
down)
  tmux kill-server 2>/dev/null || true
  repld stop || true
  rm -rf "$XDG_RUNTIME_DIR/repld/projects/$(basename "$(dirname "$(cat "$ROOT/socket")")")"
  rmdir "$TMUX_TMPDIR/tmux-$(id -u)" "$TMUX_TMPDIR" 2>/dev/null || true
  ;;
*) sed -n 2,20p "$0" ;;
esac
