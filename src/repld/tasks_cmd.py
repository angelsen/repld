"""`repld tasks` — per-item listing of in-flight kernel tasks and active tickers,
plus `wait`/`cancel` on one by id.

`repld status` reduces the listing to a count; this is the detail behind it.
Listing reuses the same dashboard HTTP round trip `repld status` already makes
for its live counts — `lifecycle_cmd._live_state()` — rather than opening a
second IPC path. `wait`/`cancel` need the kernel itself (a `threading.Event`
to block on, `cancel_task` to call), so they go over the same Unix-socket
JSON-RPC path `repld gate` uses, via `exec_cmd._connect`/`_call`.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import cli_args, paths, state
from .exec_cmd import _call, _connect
from .lifecycle_cmd import _uptime
from .render import BOLD, DIM, GREEN, RED, RESET, YELLOW, short_task

_USAGE = """\
repld tasks — in-flight tasks and active tickers

  repld tasks [--json] [--socket PATH]
  repld tasks wait <task_id> [--json] [--socket PATH]
  repld tasks cancel <task_id> [--json] [--socket PATH]
"""

_LABEL = "repld tasks"


def _err(msg: str) -> None:
    print(f"{_LABEL}: {msg}", file=sys.stderr, flush=True)


def _report_error(err: dict) -> int:
    """Render a JSON-RPC error, naming version skew when that's what it is.

    Mirrors `gate_cmd._report_error` — `wait`/`cancel` are as new to an old
    kernel as `gates/list` once was, and get method-not-found the same way.
    """
    if err.get("code") == -32601:
        _err(
            "this kernel predates `repld tasks wait`/`cancel` — restart it to "
            "pick the command up (`repld restart`)"
        )
        return 1
    _err(err.get("message", "unknown error"))
    return 1


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


def _list(sock_path: Path, rest: list[str], as_json: bool) -> int:
    bad = cli_args.check_args(_LABEL, rest, _USAGE, positionals=0)
    if bad is not None:
        return bad

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


def _print_wait_result(snap: dict) -> None:
    task_id = snap.get("task_id", "?")
    label = snap.get("label")
    header = f"task {short_task(task_id)}"
    if label:
        header += f' "{label}"'
    exc = snap.get("exception")
    if exc:
        print(f"{header}: {RED}{exc}{RESET}", file=sys.stderr)
    else:
        result = snap.get("result")
        print(f"{header}: done" + (f" → {result}" if result is not None else ""))
    text = snap.get("text", "").rstrip()
    if text:
        print(text)
    if snap.get("truncated") and snap.get("spill_path"):
        print(f"[full output: {snap['spill_path']}]", file=sys.stderr)


def _wait(sock_path: Path, rest: list[str], as_json: bool) -> int:
    bad = cli_args.check_args(f"{_LABEL} wait", rest, _USAGE, positionals=1)
    if bad is not None:
        return bad
    positionals = [a for a in rest if not a.startswith("-")]
    if not positionals:
        print(f"{_LABEL}: wait needs a task_id\n")
        print(_USAGE)
        return 2
    task_id = positionals[0]

    conn = _connect(paths.lock_for(sock_path), label=_LABEL)
    if conn is None:
        return 1
    sock, rfile, wfile, _lock = conn
    try:
        resp = _call(
            rfile, wfile, "tasks/wait", {"task_id": task_id}, json_mode=as_json
        )
        if resp is None:
            _err("kernel disconnected")
            return 1
        if "error" in resp:
            return _report_error(resp["error"])
        snap = resp.get("result", {})
        if as_json:
            print(json.dumps(snap, indent=2))
        else:
            _print_wait_result(snap)
        return 1 if snap.get("exception") else 0
    except KeyboardInterrupt:
        _err("interrupted — the task keeps running")
        return 130
    finally:
        sock.close()


def _cancel(sock_path: Path, rest: list[str], as_json: bool) -> int:
    bad = cli_args.check_args(f"{_LABEL} cancel", rest, _USAGE, positionals=1)
    if bad is not None:
        return bad
    positionals = [a for a in rest if not a.startswith("-")]
    if not positionals:
        print(f"{_LABEL}: cancel needs a task_id\n")
        print(_USAGE)
        return 2
    task_id = positionals[0]

    conn = _connect(paths.lock_for(sock_path), label=_LABEL)
    if conn is None:
        return 1
    sock, rfile, wfile, _lock = conn
    try:
        resp = _call(rfile, wfile, "tasks/cancel", {"task_id": task_id})
        if resp is None:
            _err("kernel disconnected")
            return 1
        if "error" in resp:
            return _report_error(resp["error"])
        result = resp.get("result", {})
        accepted = bool(result.get("cancelled"))
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            status = "accepted" if accepted else "no-op (already done, or unknown id)"
            print(f"cancel {task_id}: {status}")
        return 0 if accepted else 1
    finally:
        sock.close()


def _pop_verb(rest: list[str]) -> tuple[str | None, list[str]]:
    """First non-flag positional, if it's a subverb — removed from *rest*.

    Task ids are single hex tokens, so unlike `gate answer`'s free-text value
    there's nothing here that needs protecting from flag parsing — the verb
    can be found and stripped up front, before `check_args` runs.
    """
    for i, a in enumerate(rest):
        if a.startswith("-"):
            continue
        if a in ("wait", "cancel"):
            return a, rest[:i] + rest[i + 1 :]
        break
    return None, rest


def run_tasks(argv: list[str]) -> int:
    if cli_args.wants_help(argv):
        print(_USAGE)
        return 0
    sock_path, rest = paths.resolve_socket_path(argv)
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]

    verb, rest = _pop_verb(rest)
    if verb == "wait":
        return _wait(sock_path, rest, as_json)
    if verb == "cancel":
        return _cancel(sock_path, rest, as_json)
    return _list(sock_path, rest, as_json)
