"""repld exec — human-facing CLI for the running kernel.

One-shot:     repld exec 'await browser.watch("*gmail*")'
Interactive:  repld exec

Connects to the kernel via its unix socket (.pyrepl.lock), sends JSON-RPC
tools/call requests, and renders results. State persists in the kernel
across invocations.
"""

from __future__ import annotations

import ast
import code
import json
import signal
import socket
import sys
from pathlib import Path
from typing import IO, Any

from . import __version__
from .ipc import connect_to_kernel, resolve_lock_path
from .tasks import spill_marker

HISTORY_DIR = Path.home() / ".repld"
HISTORY_PATH = HISTORY_DIR / "history"

_next_id = 0


def _err(msg: str) -> None:
    print(f"repld exec: {msg}", file=sys.stderr, flush=True)


def _send(
    wfile: IO[str], method: str, params: dict | None = None, *, notif: bool = False
) -> int | None:
    global _next_id
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notif:
        _next_id += 1
        msg["id"] = _next_id
    wfile.write(json.dumps(msg) + "\n")
    wfile.flush()
    return msg.get("id")


def _recv(rfile: IO[str]) -> dict | None:
    line = rfile.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def _handle_notification(data: dict, json_mode: bool) -> None:
    """Render a channel notification to stderr."""
    params = data.get("params", {})
    if json_mode:
        json.dump(params, sys.stderr)
        sys.stderr.write("\n")
    else:
        content = params.get("content", "")
        if content:
            sys.stderr.write(content + "\n")
    sys.stderr.flush()


def _call(
    rfile: IO[str],
    wfile: IO[str],
    method: str,
    params: dict | None = None,
    json_mode: bool = False,
) -> dict | None:
    """Send a request and wait for the matching response.

    Channel notifications received while waiting are rendered to stderr.
    Returns the response dict, or None on disconnect.
    """
    rid = _send(wfile, method, params)
    while True:
        data = _recv(rfile)
        if data is None:
            return None
        if "id" in data and data["id"] == rid:
            return data
        if "method" in data:
            _handle_notification(data, json_mode)


def _connect(lock_path: Path) -> tuple[socket.socket, IO[str], IO[str], dict] | None:
    """Read lockfile, connect to kernel, perform MCP handshake.

    Returns (sock, rfile, wfile, lock_info) or None on failure.
    """
    result = connect_to_kernel(lock_path)
    if isinstance(result, str):
        _err(result)
        return None
    sock, lock = result

    rfile = sock.makefile("r", encoding="utf-8")
    wfile = sock.makefile("w", encoding="utf-8")

    # MCP handshake
    resp = _call(
        rfile,
        wfile,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "repld-exec", "version": __version__},
        },
    )
    if resp is None:
        _err("kernel disconnected during handshake")
        sock.close()
        return None
    _send(wfile, "notifications/initialized", notif=True)

    return sock, rfile, wfile, lock


def _exec_and_print(rfile: IO[str], wfile: IO[str], src: str, json_mode: bool) -> int:
    """Execute code and print the result. Returns exit code."""
    resp = _call(
        rfile,
        wfile,
        "tools/call",
        {
            "name": "exec",
            "arguments": {"code": src, "timeout": 30},
        },
        json_mode=json_mode,
    )
    if resp is None:
        _err("kernel disconnected")
        return 1

    if "error" in resp:
        _err(resp["error"].get("message", "unknown error"))
        return 1

    result = resp.get("result", {})
    meta = result.get("_meta", {})
    is_error = result.get("isError", False)
    text = result["content"][0]["text"] if result.get("content") else ""

    if meta.get("done"):
        if json_mode:
            json.dump(result, sys.stdout)
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            if text:
                print(text, file=sys.stderr if is_error else sys.stdout)
        return 1 if is_error else 0

    # Deferred — show preview, wait for task_done channel notification
    task_id = meta.get("task_id", "")
    if text and not json_mode:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    return _wait_task(rfile, wfile, task_id, json_mode)


def _wait_task(rfile: IO[str], wfile: IO[str], task_id: str, json_mode: bool) -> int:
    """Wait for task_done channel notification, then fetch final output."""
    original_handler = signal.getsignal(signal.SIGINT)
    cancelled = False

    def _cancel_handler(sig: int, frame: Any) -> None:
        nonlocal cancelled
        if cancelled:
            # Second Ctrl-C: force exit
            sys.exit(130)
        cancelled = True
        _err("cancelling...")
        _send(
            wfile, "tools/call", {"name": "cancel", "arguments": {"task_id": task_id}}
        )

    signal.signal(signal.SIGINT, _cancel_handler)
    try:
        while True:
            data = _recv(rfile)
            if data is None:
                _err("kernel disconnected while waiting for task")
                return 1

            if "method" in data:
                params = data.get("params", {})
                meta = params.get("meta", {})
                if meta.get("kind") == "task_done" and meta.get("task_id") == task_id:
                    # Task completed — get final snapshot
                    resp = _call(
                        rfile,
                        wfile,
                        "tools/call",
                        {
                            "name": "get_task",
                            "arguments": {"task_id": task_id},
                        },
                        json_mode=json_mode,
                    )
                    if resp is None:
                        return 1
                    result = resp.get("result", {})
                    snap_text = (
                        result["content"][0]["text"] if result.get("content") else "{}"
                    )
                    snap = json.loads(snap_text)
                    if json_mode:
                        json.dump(snap, sys.stdout)
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                    else:
                        text = snap.get("text", "").rstrip()
                        if text:
                            print(text)
                        if snap.get("truncated"):
                            _err(spill_marker(snap.get("spill_path")))
                    return 1 if snap.get("exception") else 0
                else:
                    _handle_notification(data, json_mode)
    finally:
        signal.signal(signal.SIGINT, original_handler)


def _setup_readline() -> None:
    """Configure readline with persistent history."""
    try:
        import readline
    except ImportError:
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(str(HISTORY_PATH))
    except FileNotFoundError:
        pass
    import atexit

    atexit.register(readline.write_history_file, str(HISTORY_PATH))


class RemoteConsole(code.InteractiveConsole):
    """Interactive console that dispatches to the kernel over IPC."""

    def __init__(self, rfile: IO[str], wfile: IO[str], json_mode: bool = False) -> None:
        super().__init__()
        # Allow top-level await so `await browser.watch(...)` works —
        # the kernel compiles with the same flag.
        self.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT  # type: ignore[attr-defined]
        self.rfile = rfile
        self.wfile = wfile
        self.json_mode = json_mode

    def runsource(
        self, source: str, filename: str = "<input>", symbol: str = "single"
    ) -> bool:
        # Check completeness (handles multi-line blocks + top-level await)
        try:
            compiled = self.compile(source, filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            self.showsyntaxerror(filename)
            return False
        if compiled is None:
            return True  # incomplete — prompt for more

        source = source.rstrip()
        if not source:
            return False

        try:
            _exec_and_print(self.rfile, self.wfile, source, self.json_mode)
        except KeyboardInterrupt:
            self.write("\nKeyboardInterrupt\n")
        return False


def run_exec(argv: list[str]) -> int:
    """Entrypoint for `repld exec`."""
    args = list(argv)

    if "-h" in args or "--help" in args:
        print("usage: repld exec [--json] [--socket PATH] [CODE]")
        print()
        print("  CODE given:  one-shot (run, print, exit)")
        print("  no CODE:     interactive REPL")
        print()
        print("  --json         emit JSON to stdout (for scripting)")
        print("  --socket PATH  connect to a kernel at a non-default socket path")
        return 0

    lock_path, args = resolve_lock_path(args)

    json_mode = False
    if "--json" in args:
        json_mode = True
        args.remove("--json")

    # Support -- separator
    if args and args[0] == "--":
        args = args[1:]

    code_arg = " ".join(args) if args else None

    conn = _connect(lock_path)
    if conn is None:
        return 1
    sock, rfile, wfile, lock = conn

    try:
        if code_arg is not None:
            return _exec_and_print(rfile, wfile, code_arg, json_mode)
        else:
            _setup_readline()
            pid = lock.get("pid", "?")
            console = RemoteConsole(rfile, wfile, json_mode)
            console.interact(
                banner=f"repld (kernel pid={pid})\nbuiltins: browser, notify, defer, every, ask, confirm, choose",
                exitmsg="",
            )
            return 0
    except (BrokenPipeError, OSError) as e:
        _err(f"connection lost: {e}")
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass
