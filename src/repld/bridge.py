"""Slim loader: stdio MCP ↔ unix-socket proxy that owns the kernel's existence.

Claude Code spawns one of these per session. If a kernel for this project is
already running, it attaches to it immediately at no extra cost — that is the
common case (a second window, a kernel a prior session left up) and it works
exactly as it always has. Only when *no* kernel exists at all does it defer:
MCP discovery (`initialize`, `tools/list`, `resources/list`, static docs) is
answered from the previous kernel's cache (`kernel.cache`, written at boot —
see `kernel._write_cache` / `protocol.build_discovery_cache`) or a minimal
static fallback if none exists yet, so a session that never calls a repld tool
in a cold project never spawns a kernel at all (`_try_bridge_intercept`). The
first message that actually needs live state — a real `tools/call`, a
gist/browser `resources/read` — spawns one on demand. Once a kernel is
attached it outlives every generation, so a kernel that dies mid-session is
invisible to the client beyond one error response.

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
import socket
import sys
import threading
import time
from pathlib import Path

from . import __version__, bridge_tools, core_schemas, ipc, paths, spawn, state

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

# The only two methods worth spawning a kernel for. Everything else the client
# can send is either answered by `_try_bridge_intercept` from cache or is a
# method repld does not implement; `resources/read` is here because the
# intercept declines exactly the gist and browser URIs, which do need one.
_NEEDS_KERNEL = frozenset({"tools/call", "resources/read"})

_static_docs_cache: dict[str, str] | None = None


def _static_docs() -> dict[str, str]:
    """The doc resources — pure constants, lazily imported from help.py.

    Which URIs these are, and which `help.py` constant backs each, comes from
    `core_schemas.DOC_HELP_ATTRS` — the same map the kernel reads, so adding a
    doc is one edit rather than three. Resolved on first `resources/read` of
    one of these URIs rather than at module import time, so a bridge that never
    serves docs never pays to parse help.py.
    """
    global _static_docs_cache
    if _static_docs_cache is None:
        from . import help as _help

        _static_docs_cache = {
            uri: getattr(_help, attr)
            for uri, attr in core_schemas.DOC_HELP_ATTRS.items()
        }
    return _static_docs_cache


def _minimal_instructions() -> str:
    from .help import static_instructions

    return static_instructions()


def _err(msg: str) -> None:
    print(f"repld bridge: {msg}", file=sys.stderr, flush=True)


def _rid_of(line: str) -> object | None:
    """The `id` of a client line, or None if it has none or won't parse."""
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    return msg.get("id") if isinstance(msg, dict) else None


class Bridge:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.lock_path = paths.lock_for(socket_path)
        self._sock: socket.socket | None = None
        self._kernel_pid: int | None = None
        self._generation = 0
        self._client_init: dict | None = None
        self._client_initialized = False
        self._inflight: set[object] = set()
        self._state_lock = threading.Lock()
        self._stdout_lock = threading.Lock()
        self._cache: dict | None = None
        self._cache_loaded = False

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

    # -- lazy-kernel discovery -----------------------------------------------

    def _load_cache(self) -> dict | None:
        """The last kernel's computed instructions/tools/resources, if any.

        Read once and memoized — a fresh kernel spawn always supersedes it via
        `list_changed`, so there is no need to re-read mid-session. Discarded
        if it came from a different repld version (schemas may have changed)
        or the file is missing/corrupt.
        """
        if self._cache_loaded:
            return self._cache
        self._cache_loaded = True
        try:
            data = json.loads(paths.cache_for(self.socket_path).read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("version") == __version__:
            self._cache = data
        return self._cache

    def _try_bridge_intercept(self, msg: dict, rid) -> bool:
        """Answer MCP discovery methods without spawning a kernel.

        Only consulted while `self._sock is None` (see `_handle_client_line`).
        Falls back to a static minimal response when there's no cache yet —
        the very first time a bridge ever runs in this project. Returns True
        if a response was sent, False to fall through to the normal
        ensure-kernel-and-forward path (which is how gist tools, gist
        resources, and browser state — none of which are static — end up
        spawning a kernel on first real use).
        """
        method = msg.get("method")

        if method == "initialize":
            self._client_init = msg
            cache = self._load_cache()
            self._to_client(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        # What the last kernel actually advertised, when we
                        # have it. Same value in practice — _load_cache drops a
                        # cache from another repld version — but the recorded
                        # answer beats a re-derived one if that gate ever loosens.
                        "protocolVersion": cache["protocolVersion"]
                        if cache
                        else core_schemas.PROTOCOL_VERSION,
                        "capabilities": core_schemas.CAPABILITIES,
                        "serverInfo": {
                            "name": "repld",
                            "version": cache["version"] if cache else __version__,
                        },
                        "instructions": cache["instructions"]
                        if cache
                        else _minimal_instructions(),
                    },
                }
            )
            return True

        if method == "notifications/initialized":
            # No kernel yet to flush queued channel pushes at. Remembered so
            # _replay_handshake knows the client genuinely completed its
            # handshake before the first spawn — it must not fabricate this
            # notification for a kernel the client hasn't actually initialized.
            self._client_initialized = True
            return True

        if method == "tools/list":
            cache = self._load_cache()
            tools = (
                cache["tools"]
                if cache
                else core_schemas.CORE_TOOLS + bridge_tools.SCHEMAS
            )
            self._to_client({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
            return True

        if method == "resources/list":
            cache = self._load_cache()
            resources = (
                cache["resources"]
                if cache
                else core_schemas.wire(core_schemas.STATIC_RESOURCES)
            )
            self._to_client(
                {"jsonrpc": "2.0", "id": rid, "result": {"resources": resources}}
            )
            return True

        if method == "resources/templates/list":
            self._to_client(
                {"jsonrpc": "2.0", "id": rid, "result": {"resourceTemplates": []}}
            )
            return True

        if method == "resources/read":
            uri = (msg.get("params") or {}).get("uri", "")
            text = _static_docs().get(uri)
            if text is None:
                return False  # gist/browser resource — needs a live kernel
            self._to_client(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "contents": [
                            {"uri": uri, "mimeType": "text/plain", "text": text}
                        ]
                    },
                }
            )
            return True

        return False

    # -- kernel liveness ----------------------------------------------------

    def _try_attach_existing(self) -> bool:
        """Cheap, no-spawn attach: is a kernel for this project already up?

        Tried before falling back to cache-based discovery. The common case
        is not a cold project — it's a second Claude Code window, or a kernel
        a previous session already spawned — and that case deserves live,
        accurate `initialize`/`tools/list`/`resources/list` answers, not a
        possibly-stale cache. Only when this fails (no kernel at all) does
        discovery defer to the cache, and a real tool call eventually to
        `_ensure_kernel`'s actual spawn-and-wait.
        """
        result = ipc.connect_to_kernel(self.lock_path)
        if isinstance(result, str):
            return False
        self._attach(*result)
        return True

    def _spawn_kernel(self, *, quiet: bool = False) -> None:
        # Both non-failures are followed by the same poll in _reconnect; they
        # differ only in what happened, which is worth saying accurately —
        # claiming to have spawned a kernel that a racing boot actually started
        # would send anyone reading stderr after the wrong process.
        #
        # `quiet` is for the retry inside that poll: the outcome has already
        # been reported once, and repeating it every 500 ms would bury the
        # line that matters.
        outcome = spawn.spawn_headless(self.socket_path)
        if quiet:
            return
        if outcome == spawn.STARTED:
            _err("no kernel running for this project — spawned a headless one")
        elif outcome == spawn.INCUMBENT:
            _err("another boot won the race — waiting for its kernel")

    def _ensure_kernel(self) -> bool:
        """Cheapest-first liveness ladder, run before every forward.

        Connected and the pid is alive → nothing to do. Otherwise reconnect,
        spawning a kernel if the lockfile is missing, stale, or unreachable.
        """
        if self._sock is not None:
            if self._kernel_pid is not None and state.pid_alive(self._kernel_pid):
                return True
            self._on_kernel_gone()
        return self._reconnect()

    def _connect_excluding(
        self, exclude_pid: int | None
    ) -> tuple[socket.socket, dict] | str:
        """`ipc.connect_to_kernel`, refusing one pid. Returns a reason on failure.

        A kernel that has been SIGTERMed keeps both its pid and its bound
        socket for as long as `_shutdown` takes to drain `@every` and
        `defer()` finally blocks, so a plain connect during a restart hands
        back the very process we just killed.
        """
        result = ipc.connect_to_kernel(self.lock_path)
        if isinstance(result, str):
            return result
        sock, lock = result
        if exclude_pid is not None and lock.get("pid") == exclude_pid:
            try:
                sock.close()
            except OSError:
                pass
            return f"kernel pid {exclude_pid} is still shutting down"
        return sock, lock

    def _reconnect(self, *, exclude_pid: int | None = None) -> bool:
        """Attach to this project's kernel, spawning one if there is none.

        `exclude_pid` is set by `restart_kernel` and refuses the kernel being
        replaced. Refusing it has a consequence the plain path doesn't have:
        the first spawn is a no-op, because one kernel per project is enforced
        by an flock the dying kernel still holds, so the poll below has to keep
        asking. Once it lets go, nothing else is going to start one for us.
        """
        result = self._connect_excluding(exclude_pid)
        if not isinstance(result, str):
            self._attach(*result)
            return True

        self._spawn_kernel()
        for _ in range(WAIT_STEPS):
            time.sleep(WAIT_STEP_SECONDS)
            result = self._connect_excluding(exclude_pid)
            if not isinstance(result, str):
                self._attach(*result)
                return True
            if exclude_pid is not None:
                self._spawn_kernel(quiet=True)
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

    def restart_kernel(self) -> tuple[int | None, int | None]:
        """Stop this project's kernel and bring a fresh one up.

        Returns ``(old_pid, new_pid)``; ``new_pid`` is None if the replacement
        never became reachable.

        The wait between SIGTERM and respawn is load-bearing, not politeness:
        one kernel per project is enforced by an flock the dying kernel still
        holds, so a replacement spawned too early loses the race and exits 0 —
        leaving `_reconnect` polling for a kernel that deliberately gave up.

        Teardown goes through `_on_kernel_gone` so requests already in flight
        get their `-31001` reply instead of hanging, exactly as they do when a
        kernel dies on its own.

        The wait is a budget, not a guarantee — a kernel draining a slow
        `finally` can outlast it — so the reattach is guarded by `exclude_pid`
        rather than by the wait having succeeded. Without that guard a slow
        shutdown reports `pid N → N`: the same process, replied to as the
        replacement, with the handshake replayed onto it on its way out.
        """
        import os
        import signal

        old_pid = self._kernel_pid
        if old_pid is not None:
            try:
                os.kill(old_pid, signal.SIGTERM)
            except OSError:
                pass  # already gone; fall through to the reconnect ladder
            for _ in range(WAIT_STEPS):
                if not state.pid_alive(old_pid):
                    break
                time.sleep(WAIT_STEP_SECONDS)
        self._on_kernel_gone()
        if not self._reconnect(exclude_pid=old_pid):
            return old_pid, None
        return old_pid, self._kernel_pid

    # -- bridge-served tools -------------------------------------------------

    def _handle_bridge_tool(self, rid, name: str, args: dict) -> None:
        entry = bridge_tools.BRIDGE_TOOLS[name]
        try:
            result = entry["handler"](self, args)
        except Exception as e:  # a bridge tool must never take the session down
            _err(f"{name} failed: {type(e).__name__}: {e}")
            result = {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            }
        if rid is not None:
            self._to_client({"jsonrpc": "2.0", "id": rid, "result": result})

    def _replay_handshake(self) -> None:
        """Re-run the client's initialize (and initialized, if seen) on a new kernel.

        The initialize response is swallowed by the reader (matched on
        BRIDGE_INIT_ID) — the client already completed its handshake, either
        with a previous generation or via the discovery intercept, and must
        not see a second response. `notifications/initialized` is replayed
        only if `_client_initialized` — on the very first kernel spawn (lazy
        mode), a tool call could in principle arrive before the client's real
        `notifications/initialized` does; forging it early would let a
        channel push escape the kernel's pre-init queue ahead of schedule.
        """
        assert self._client_init is not None
        replay = dict(self._client_init)
        replay["id"] = BRIDGE_INIT_ID
        self._to_kernel(replay)
        if self._client_initialized:
            self._to_kernel({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _on_kernel_gone(self) -> None:
        with self._state_lock:
            sock, self._sock = self._sock, None
            self._kernel_pid = None
            orphans, self._inflight = self._inflight, set()
        if sock is not None:
            # shutdown() before close(): the reader thread holds a makefile,
            # which keeps its own reference to the fd, so close() alone leaves
            # it parked in recv() on a socket nobody will ever write to. Only
            # a shutdown forces the EOF that ends that generation's pump.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
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
        rfile = None
        try:
            rfile = sock.makefile("r", encoding="utf-8")
            for line in rfile:
                if not line.strip():
                    continue
                # Checked per line, not just at EOF: a superseded generation
                # can still have whole replies buffered in its makefile, and
                # forwarding them would interleave a dead kernel's answers
                # with the replacement's.
                with self._state_lock:
                    if gen != self._generation:
                        break
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
        finally:
            if rfile is not None:
                try:
                    rfile.close()
                except OSError:
                    pass
        # EOF: the kernel died. Only the current generation may declare that —
        # a superseded reader is just finishing its own closed socket.
        with self._state_lock:
            current = gen == self._generation
        if current:
            self._on_kernel_gone()

    # -- main loop ----------------------------------------------------------

    def _handle_client_line(self, line: str) -> None:
        """Answer our own bugs instead of dying of them.

        `run()`'s loop is the only reader of client stdin, so an exception
        escaping here ends that loop and closes our stdout — and an MCP
        client dispatcher is single-shot, so it can never re-handshake. The
        kernel end of this same socket has always answered its own bugs with
        -32603 (`ipc.py`); this is the matching guard on the side that has to
        survive. Narrow excepts stay where they are, in the paths that know
        what they're catching — this is only the backstop for the rest.
        """
        try:
            self._dispatch_client_line(line)
        except Exception as exc:
            _err(f"internal error handling client message: {exc!r}")
            rid = _rid_of(line)
            if rid is None:
                return  # a notification: nothing to answer.
            # Discard before replying: the id may have been registered on the
            # way in, and answering it here means nothing downstream will.
            # Left in place it would hang the client and then burn
            # _drain_inflight's whole budget at shutdown.
            with self._state_lock:
                self._inflight.discard(rid)
            self._to_client(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {
                        "code": -32603,
                        "message": f"repld bridge internal error: {exc}",
                    },
                }
            )

    def _dispatch_client_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            msg = None

        if not isinstance(msg, dict):
            # Dropped rather than forwarded. There is no id to answer, the
            # kernel can't act on it either, and the old fall-through reached
            # _ensure_kernel — so one truncated stdin line used to spawn a
            # kernel and then ship it the garbage.
            _err("ignoring unparseable line from client")
            return

        rid = msg.get("id")

        # Bridge-served tools are answered *before* the liveness ladder. They
        # exist for the cases where the kernel is dead or is about to be
        # replaced, so insisting on a live one first would be backwards — and
        # for a restart it would spawn a kernel only to kill it.
        method = msg.get("method")
        if method == "tools/call":
            params = msg.get("params") or {}
            if params.get("name") in bridge_tools.BRIDGE_TOOLS:
                self._handle_bridge_tool(
                    rid, params["name"], params.get("arguments") or {}
                )
                return

        # No kernel attached yet: attach for free if one is already running
        # (the common case), so discovery gets live data below exactly as it
        # always has. Only when that fails — no kernel at all for this
        # project — does discovery fall back to the last kernel's cache
        # instead of spawning one just to ask it. A gist tool call, a
        # gist/browser resource read, or anything else that actually needs
        # live state falls through and reaches _ensure_kernel below — that's
        # what triggers the lazy spawn.
        if self._sock is None:
            self._try_attach_existing()
        if self._sock is None:
            if self._try_bridge_intercept(msg, rid):
                return
            if method not in _NEEDS_KERNEL:
                # Everything the intercept declined that isn't one of those two
                # is a method no kernel of ours implements — a client-side
                # `ping`, a `prompts/list` probe. Spawning one to have it say
                # "method not found" is the whole cost of the lazy-spawn
                # design paid for an answer we already know.
                if rid is not None:
                    self._to_client(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": -32601,
                                "message": f"method not found: {method}",
                            },
                        }
                    )
                return

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

        # Cached *after* _ensure_kernel, never before: this same message is
        # about to be forwarded, and a kernel attached while it is already in
        # _client_init would have the handshake replayed onto it — firing
        # list_changed at a client that hasn't yet had its `initialize`
        # answered. Only a kernel attached from here on is a *fresh* one.
        if method == "initialize":
            self._client_init = msg
        # Reaching here (rather than the discovery intercept) means a kernel
        # was already attached, so this is a plain forward — record it the
        # same way, for whatever *next* reconnect's _replay_handshake needs.
        if method == "notifications/initialized":
            self._client_initialized = True

        # Registered before the write, so a send that fails mid-flight is
        # answered by _on_kernel_gone rather than hanging the client.
        if rid is not None:
            with self._state_lock:
                self._inflight.add(rid)
        sock = self._sock
        if sock is None:
            # The kernel died between _ensure_kernel() and here — the reader
            # thread got EOF and ran _on_kernel_gone() after we registered.
            # Returning would strand the id we just added: nothing will ever
            # answer it, so the client hangs and _drain_inflight burns its full
            # timeout at shutdown. Go through the same path a failed send does.
            self._on_kernel_gone()
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
        # No proactive spawn: MCP discovery is answered from cache (see
        # _try_bridge_intercept), and the first message that actually needs a
        # kernel — a real tools/call, a gist/browser resources/read — reaches
        # _ensure_kernel() in _handle_client_line and spawns one then.
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
    socket_path, _ = paths.resolve_socket_path(argv)
    return Bridge(socket_path).run()
