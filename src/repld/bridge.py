"""Slim loader: stdio MCP ↔ unix-socket proxy that owns the kernel's existence.

Claude Code spawns one of these per session. It guarantees a kernel is running
for the cwd — spawning a headless one if there isn't — and then outlives every
kernel generation, so a kernel that dies mid-session is invisible to the client
beyond one error response.

It is deliberately *not* a dumb byte-pipe any more. Three things forced that:

  1. MCP client dispatchers are single-shot. Letting our own stdout hit EOF
     ends the client's session permanently — there is no re-handshake. So this
     process never closes stdout and never exits on kernel death.
  2. MCP initialization is per-connection and happens exactly once, so a
     respawned kernel starts uninitialized and would queue every channel push
     forever. We cache the client's `initialize` and replay it (plus
     `notifications/initialized`) to each fresh kernel.
  3. Requests in flight when a kernel dies are lost, and the client has no
     retry logic — so we synthesize an error reply per orphaned id.

Two rules that fall out of running arbitrary user code:

  - Probe before forwarding, never retry after failing. An `exec` that reached
    the kernel may already have run; sending it twice is not recoverable.
  - stdout carries nothing but MCP messages. All diagnostics go to stderr.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import ipc

# 5s spawn window, polled at 100ms — long enough for a cold kernel with gists
# to bind its socket, short enough that a broken spawn fails a tool call
# instead of hanging the client.
WAIT_STEPS = 50
WAIT_STEP_SECONDS = 0.1

# Outside JSON-RPC's reserved -32768..-32000 range, per the MCP spec's guidance
# for implementation-defined codes.
KERNEL_GONE = -31001

# String id, so it can never collide with a client's integer ids.
BRIDGE_INIT_ID = "repld-bridge-init"


def _err(msg: str) -> None:
    print(f"repld bridge: {msg}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.lock_path = ipc.lock_for(socket_path)
        self._sock: socket.socket | None = None
        self._kernel_pid: int | None = None
        self._generation = 0
        self._client_init: dict | None = None
        self._inflight: set[object] = set()
        self._state_lock = threading.Lock()
        self._stdout_lock = threading.Lock()
        self._spawned_once = False

    # -- client I/O ---------------------------------------------------------

    def _to_client(self, msg: dict) -> None:
        with self._stdout_lock:
            try:
                sys.stdout.write(json.dumps(msg) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _to_client_raw(self, line: str) -> None:
        with self._stdout_lock:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    # -- kernel liveness ----------------------------------------------------

    def _spawn_kernel(self) -> None:
        """Start a detached headless kernel for this cwd.

        Detached (`start_new_session`) so it survives this bridge — in-memory
        state is meant to outlive a Claude Code restart. If two bridges race
        here, the kernel's flock mutex settles it and the loser exits 0.
        """
        cmd = [sys.executable, "-m", "repld", "--no-display"]
        if self._explicit_socket:
            cmd += ["--socket", str(self.socket_path)]
        try:
            subprocess.Popen(
                cmd,
                cwd=os.getcwd(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            _err(f"could not spawn kernel: {e}")
            return
        self._spawned_once = True
        _err("no kernel running for this project — spawned a headless one")

    @property
    def _explicit_socket(self) -> bool:
        from . import paths

        return self.socket_path != paths.socket_path()

    def _ensure_kernel(self) -> bool:
        """Cheapest-first liveness ladder, run before every forward.

        Connected and the pid is alive → nothing to do. Otherwise reconnect,
        spawning a kernel if the lockfile is missing, stale, or unreachable.
        """
        if self._sock is not None:
            if self._kernel_pid is not None and ipc.pid_alive(self._kernel_pid):
                return True
            self._on_kernel_gone()
        return self._reconnect()

    def _reconnect(self) -> bool:
        result = ipc.connect_to_kernel(self.lock_path)
        if not isinstance(result, str):
            self._attach(*result)
            return True

        self._spawn_kernel()
        for _ in range(WAIT_STEPS):
            threading.Event().wait(WAIT_STEP_SECONDS)
            result = ipc.connect_to_kernel(self.lock_path)
            if not isinstance(result, str):
                self._attach(*result)
                return True
        _err(f"kernel unreachable after spawn: {result}")
        return False

    def _attach(self, sock: socket.socket, lock: dict) -> None:
        with self._state_lock:
            self._sock = sock
            self._kernel_pid = lock.get("pid")
            self._generation += 1
            gen = self._generation
        threading.Thread(
            target=self._read_kernel,
            args=(sock, gen),
            daemon=True,
            name="repld-bridge-kernel",
        ).start()
        if self._client_init is not None:
            self._replay_handshake()
            # The fresh kernel may expose a different tool set (gists edited,
            # browser extra now present). Declared listChanged makes this the
            # sanctioned way to invalidate the client's cache.
            self._to_client(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            )
            self._to_client(
                {"jsonrpc": "2.0", "method": "notifications/resources/list_changed"}
            )
            _err("reconnected to a fresh kernel; handshake replayed")

    def _replay_handshake(self) -> None:
        """Re-run the client's one-and-only initialize against a new kernel.

        Its response is swallowed by the reader (matched on BRIDGE_INIT_ID) —
        the client already completed its handshake with a previous generation
        and must not see a second one.
        """
        assert self._client_init is not None
        replay = dict(self._client_init)
        replay["id"] = BRIDGE_INIT_ID
        self._to_kernel(replay)
        self._to_kernel({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _on_kernel_gone(self) -> None:
        with self._state_lock:
            sock, self._sock = self._sock, None
            self._kernel_pid = None
            orphans, self._inflight = self._inflight, set()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for rid in orphans:
            self._to_client(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {
                        "code": KERNEL_GONE,
                        "message": "repld kernel restarted; request was lost",
                    },
                }
            )

    # -- kernel I/O ---------------------------------------------------------

    def _to_kernel(self, msg: dict) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            return True
        except OSError:
            self._on_kernel_gone()
            return False

    def _read_kernel(self, sock: socket.socket, gen: int) -> None:
        """Pump one kernel generation's replies to the client."""
        try:
            rfile = sock.makefile("r", encoding="utf-8")
            for line in rfile:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._to_client_raw(line)
                    continue
                rid = msg.get("id")
                if rid == BRIDGE_INIT_ID:
                    continue  # our replayed handshake — the client must not see it
                if rid is not None:
                    with self._state_lock:
                        self._inflight.discard(rid)
                self._to_client_raw(line)
        except (BrokenPipeError, OSError, ValueError):
            pass
        # EOF: the kernel died. Only the current generation may declare that —
        # a superseded reader is just finishing its own closed socket.
        with self._state_lock:
            current = gen == self._generation
        if current:
            self._on_kernel_gone()

    # -- main loop ----------------------------------------------------------

    def _handle_client_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            msg = None

        rid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            self._client_init = msg

        if not self._ensure_kernel():
            if rid is not None:
                self._to_client(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": KERNEL_GONE,
                            "message": "repld kernel is not running and could "
                            "not be started; see stderr",
                        },
                    }
                )
            return

        # Registered before the write, so a send that fails mid-flight is
        # answered by _on_kernel_gone rather than hanging the client.
        if rid is not None:
            with self._state_lock:
                self._inflight.add(rid)
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(line.encode("utf-8"))
        except OSError:
            self._on_kernel_gone()

    def _drain_inflight(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if not self._inflight or self._sock is None:
                    return
            time.sleep(0.05)

    def run(self) -> int:
        self._ensure_kernel()
        try:
            for line in sys.stdin:
                if not line.endswith("\n"):
                    line += "\n"
                if not line.strip():
                    continue
                self._handle_client_line(line)
        except (BrokenPipeError, OSError, ValueError):
            pass
        # Client-side EOF is the only thing that ends this process. Drain first:
        # a client that closes stdin right after its last request is still owed
        # those replies, and the reader thread writes them to stdout.
        self._drain_inflight()
        # The kernel keeps running — its state is meant to outlive the session.
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return 0


def run_bridge(argv: list[str]) -> int:
    socket_path, _ = ipc.resolve_socket_path(argv)
    return Bridge(socket_path).run()
