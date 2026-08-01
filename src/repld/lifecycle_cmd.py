"""`repld stop` / `restart` / `status` — lifecycle control for kernels nobody started.

Auto-spawn creates kernels the user never launched in a pane, so it has to ship
with the means to see and stop them. `status` lists live siblings from the
user-scoped session registry, so headless kernels in other projects are visible
rather than silently accumulating.
"""

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths, sessions, spawn, state
from .render import (
    DIM as _DIM,
    GREEN as _GREEN,
    RESET as _RESET,
    YELLOW as _YELLOW,
)

_STOP_USAGE = """\
repld stop — stop this project's kernel

  repld stop [--all] [--socket PATH]

  --all   stop every live repld kernel, not just this project's
"""

_RESTART_USAGE = """\
repld restart — stop this project's kernel and start a fresh headless one

  repld restart [--socket PATH]
"""

_STATUS_USAGE = """\
repld status — this project's kernel plus live siblings

  repld status [--json] [--socket PATH]
"""


def _uptime(started_at: float | None) -> str:
    if not started_at:
        return "?"
    secs = int(time.time() - started_at)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _wait_gone(pid: int, lock_path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not state.pid_alive(pid) and not lock_path.exists():
            return True
        time.sleep(0.1)
    return not state.pid_alive(pid)


def _stop_one(pid: int, lock_path: Path, label: str) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"{label}: pid {pid} was already gone")
        return True
    except PermissionError:
        print(f"{label}: not permitted to signal pid {pid}", file=sys.stderr)
        return False
    if _wait_gone(pid, lock_path):
        print(f"{label}: stopped (pid {pid})")
        return True
    print(f"{label}: pid {pid} did not exit within 10s", file=sys.stderr)
    return False


def run_stop(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(_STOP_USAGE)
        return 0
    sock_path, rest = paths.resolve_socket_path(argv)
    stop_all = "--all" in rest
    unknown = [a for a in rest if a != "--all"]
    if unknown:
        print(f"repld stop: unknown argument {unknown[0]!r}\n")
        print(_STOP_USAGE)
        return 2

    if stop_all:
        live = sessions.list_sessions()
        if not live:
            print("no repld kernels running")
            return 0
        ok = True
        for s in live:
            pid = int(s["pid"])
            label = Path(s.get("cwd", "?")).name or str(pid)
            # A session file with no socket_path can't be resolved to a
            # lockfile — Path("").with_suffix() raises. Skip it rather than
            # letting one malformed entry abort the whole sweep.
            sock = s.get("socket_path")
            if not sock:
                print(f"{label}: session file has no socket_path — skipped")
                ok = False
                continue
            ok = _stop_one(pid, paths.lock_for(Path(sock)), label) and ok
        return 0 if ok else 1

    lock_path = paths.lock_for(sock_path)
    lock = state.read_lock(lock_path)
    if isinstance(lock, str):
        print(f"nothing to stop: {lock}")
        return 0
    return 0 if _stop_one(int(lock["pid"]), lock_path, "repld") else 1


def _spawn_headless(sock_path: Path) -> int:
    # Only FAILED means nothing is coming. INCUMBENT is a racing boot that
    # already owns the unit — poll for it exactly as for our own spawn, rather
    # than reporting a failure while a healthy kernel comes up.
    if spawn.spawn_headless(sock_path) == spawn.FAILED:
        print("repld restart: could not start a kernel", file=sys.stderr)
        return 1
    # 10s, unlike the bridge's 5s: there's a human waiting at a terminal here,
    # not a tool call that would rather fail than hang.
    lock_path = paths.lock_for(sock_path)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if isinstance(state.read_lock(lock_path), dict):
            return 0
        time.sleep(0.1)
    print("repld restart: new kernel never came up", file=sys.stderr)
    return 1


def run_restart(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(_RESTART_USAGE)
        return 0
    sock_path, rest = paths.resolve_socket_path(argv)
    if rest:
        print(f"repld restart: unknown argument {rest[0]!r}\n")
        print(_RESTART_USAGE)
        return 2
    rc = run_stop(["--socket", str(sock_path)])
    if rc != 0:
        return rc
    rc = _spawn_headless(sock_path)
    if rc == 0:
        print("repld: fresh headless kernel started")
    return rc


def _live_state(lock: dict, hint_path: Path) -> dict | None:
    """Ask the dashboard for task/ticker counts.

    Reuses the existing `POST /api {"method": "state"}` with the token from the
    0600 hint file — no new IPC surface for a read-only status line.
    """
    port = lock.get("dashboard_port")
    if not port:
        return None
    token = state.dashboard_token(hint_path)
    if not token:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api",
        data=json.dumps({"method": "state"}).encode(),
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
    return body.get("result") if isinstance(body, dict) else None


def _print_python(lock: dict) -> None:
    """The kernel's interpreter, flagged when it can't import the project.

    Silent when the lockfile carries no `python` field, and for the common
    case where there's no project venv to disagree with.
    """
    running = lock.get("python")
    if not running:
        return
    from . import bind

    # ambient=False: this asks about the *kernel's* project, which may not be
    # the directory `repld status` was run from. Letting $VIRTUAL_ENV answer
    # would warn about the venv active in this shell — unrelated to that kernel.
    venv = bind.project_venv(Path(lock.get("cwd") or "."), ambient=False)
    want = bind.venv_python_version(venv) if venv is not None else None
    if want is None or f"{want[0]}.{want[1]}" == ".".join(running.split(".")[:2]):
        print(f"  python:    {running}")
        return
    print(
        f"  python:    {running}  {_YELLOW}⚠ project venv is "
        f"{want[0]}.{want[1]} — project packages are not importable; "
        f"`repld restart` to rebind{_RESET}"
    )


def run_status(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(_STATUS_USAGE)
        return 0
    sock_path, rest = paths.resolve_socket_path(argv)
    as_json = "--json" in rest
    unknown = [a for a in rest if a != "--json"]
    if unknown:
        print(f"repld status: unknown argument {unknown[0]!r}\n")
        print(_STATUS_USAGE)
        return 2

    lock_path = paths.lock_for(sock_path)
    lock = state.read_lock(lock_path)
    here: dict | None = None
    if isinstance(lock, dict):
        here = dict(lock)
        live_state = _live_state(lock, paths.hint_for(sock_path))
        if live_state:
            kernel = live_state.get("kernel", {})
            here["tasks_active"] = kernel.get("tasks_active")
            here["tickers"] = len(kernel.get("tickers") or [])

    mine = int(here["pid"]) if here else None
    siblings = [s for s in sessions.list_sessions() if int(s["pid"]) != mine]

    if as_json:
        print(json.dumps({"kernel": here, "siblings": siblings}, indent=2))
        return 0

    if here is None:
        print(lock if isinstance(lock, str) else "no kernel running for this project")
    else:
        print(
            f"{_GREEN}●{_RESET} kernel pid={here['pid']}  up {_uptime(here.get('started_at'))}"
        )
        print(f"  socket:    {here.get('socket_path')}")
        print(f"  cwd:       {here.get('cwd')}")
        _print_python(here)
        if here.get("dashboard_port"):
            # The port, not a URL — same reason the boot banner stopped
            # printing one: `GET /` carries the API token and so requires one,
            # and a bare http://127.0.0.1:<port>/ answers 401. `repld
            # dashboard` reads the token from the 0600 hint file and opens the
            # authenticated URL.
            print(
                f"  dashboard: port {here['dashboard_port']}"
                f"{_DIM} — open with `repld dashboard`{_RESET}"
            )
        if here.get("tasks_active") is not None:
            print(
                f"  active:    {here['tasks_active']} task(s), {here['tickers']} ticker(s)"
            )
        print("  log:       repld log -f")

    if siblings:
        print(f"\n{_DIM}live kernels elsewhere:{_RESET}")
        any_dash = False
        for s in sorted(siblings, key=lambda x: str(x.get("cwd", ""))):
            port = s.get("dashboard_port")
            dash = f"  dashboard port {port}" if port else ""
            any_dash = any_dash or bool(port)
            print(f"  pid={s['pid']:<7} {s.get('cwd', '?')}{_DIM}{dash}{_RESET}")
        if any_dash:
            # A sibling's dashboard needs *its* token, which lives in its own
            # project's 0600 hint file — so the way in is that project's own
            # `repld dashboard`, not a URL printed here.
            print(f"{_DIM}  open one with: cd <its dir> && repld dashboard{_RESET}")
    return 0
