"""`repld tasks` — per-item listing of in-flight kernel tasks and active tickers.

`repld status` reduces these to a count; this is the detail behind it. Reuses
the same dashboard HTTP round trip `repld status` already makes for its live
counts — `lifecycle_cmd._live_state()` — rather than opening a second IPC path.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import cli_args, paths, state
from .lifecycle_cmd import _uptime
from .render import BOLD, DIM, GREEN, RESET, YELLOW, short_task

_USAGE = """\
repld tasks — in-flight tasks and active tickers

  repld tasks [--json] [--socket PATH]
"""

_LABEL = "repld tasks"


def _fetch(lock: dict, hint_path: Path) -> dict | None:
    """Ask the dashboard for the per-task/per-ticker listing.

    Same round trip as `lifecycle_cmd._live_state()`, a different RPC method —
    kept off `"state"` so the dashboard page's frequent poll stays a count.

    Returns the raw JSON-RPC envelope, not just `result`: `POST /api` always
    answers HTTP 200 (`dashboard._handle_api`), so a kernel that predates this
    method comes back as `{"error": {...}}`, not a transport failure — the
    caller has to see that to tell "dashboard unreachable" from "kernel is
    just older than this command" apart, rather than reporting both alike.
    """
    port = lock.get("dashboard_port")
    if not port:
        return None
    token = state.dashboard_token(hint_path)
    if not token:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api",
        data=json.dumps({"method": "tasks"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Host": f"127.0.0.1:{port}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _print_tasks(data: dict) -> None:
    active = [t for t in data.get("tasks", []) if not t["done"]]
    if active:
        print(f"{BOLD}tasks ({len(active)} active){RESET}")
        for t in active:
            origin = t.get("origin_session")
            origin_str = origin[:8] if origin else "ambient"
            if t.get("origin_kind") == "bg":
                origin_str += " (bg)"
            label = f"  {t['label']}" if t.get("label") else ""
            err = f"  {YELLOW}err={t['exception']}{RESET}" if t.get("exception") else ""
            print(
                f"  {GREEN}{short_task(t['task_id'])}{RESET}{label}"
                f"  {DIM}age={_uptime(t.get('started_at'))}  from={origin_str}{RESET}{err}"
            )
    else:
        print(f"{DIM}no active tasks{RESET}")

    tickers = data.get("tickers", [])
    if tickers:
        print(f"\n{BOLD}tickers ({len(tickers)}){RESET}")
        for tk in tickers:
            tab = f"  tab={tk['tab']}" if tk.get("tab") else ""
            print(
                f"  {GREEN}{tk['label']}{RESET}  {DIM}every {tk['seconds']}s{tab}{RESET}"
            )


def run_tasks(argv: list[str]) -> int:
    if cli_args.wants_help(argv):
        print(_USAGE)
        return 0
    sock_path, rest = paths.resolve_socket_path(argv)
    bad = cli_args.check_args(_LABEL, rest, _USAGE, flags=("--json",), positionals=0)
    if bad is not None:
        return bad
    as_json = "--json" in rest

    lock_path = paths.lock_for(sock_path)
    lock = state.read_lock(lock_path)
    if not isinstance(lock, dict):
        print(
            f"{_LABEL}: {lock if isinstance(lock, str) else 'no kernel running'}",
            file=sys.stderr,
        )
        return 1

    body = _fetch(lock, paths.hint_for(sock_path))
    if body is None:
        print(f"{_LABEL}: could not reach the dashboard", file=sys.stderr)
        return 1
    if "error" in body:
        msg = body["error"].get("message", "unknown error")
        if "Unknown method" in msg:
            print(
                f"{_LABEL}: this kernel predates `repld tasks` — restart it to "
                "pick the command up (`repld restart`)",
                file=sys.stderr,
            )
        else:
            print(f"{_LABEL}: {msg}", file=sys.stderr)
        return 1
    data = body.get("result")
    if not isinstance(data, dict):
        print(f"{_LABEL}: unexpected dashboard response", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(data, indent=2))
    else:
        _print_tasks(data)
    return 0
