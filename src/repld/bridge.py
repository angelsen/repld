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
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from . import (
    __version__,
    bridge_tools,
    cli_args,
    core_schemas,
    ipc,
    paths,
    spawn,
    state,
)
from .core_schemas import (
    error as _error,
)
from .core_schemas import (
    notification as _notification,
)
from .core_schemas import (
    response as _response,
)

# 5s spawn window, polled at 100ms — long enough for a cold kernel with gists
# to bind its socket, short enough that a broken spawn fails a tool call
# instead of hanging the client.
WAIT_STEPS = 50
WAIT_STEP_SECONDS = 0.1

# Poll ticks between respawn attempts in `_reconnect`'s restart path. The
# connect probe is cheap and runs every tick; a respawn is not — on systemd it
# costs a `systemd-run` plus a `systemctl is-active` subprocess each time, so
# retrying every tick spent ~100 process spawns per restart and stretched the
# 5s window well past 5s of wall clock. It exists only to catch the moment the
# dying kernel drops its flock, which nothing is timing to 100ms.
_RESPAWN_EVERY = 5

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


def _ephemeral_socket_path() -> Path:
    """A private, single-use socket path for `repld bridge --ephemeral`.

    Deliberately outside PROJECTS_DIR: that tree is addressed by cwd
    (`paths.project_dir`) precisely so a second bridge for the same project
    attaches to the *same* kernel — the opposite of what --ephemeral
    promises. A per-invocation subdirectory under RUNTIME_DIR, keyed on this
    bridge's own pid plus a random suffix, guarantees no other bridge —
    ephemeral or not — ever resolves to the same path, and the whole
    directory is one `rmtree` for `_teardown_ephemeral` to remove on the way
    out. That matters because neither `state.sweep_dead_pid_files` nor
    `state.sweep_dead_project_dirs` would ever see it: the first only reaches
    RUNTIME_DIR's flat `{pid}-…` files, the second only PROJECTS_DIR.

    Two separate mkdir calls, not one `parents=True`: RUNTIME_DIR itself is
    already 0700 (`ensure_runtime_dir`), but `mkdir(parents=True, mode=...)`
    applies the mode only to the leaf it creates — the exact footgun
    `ensure_runtime_dir`'s own docstring warns about — so an `ephemeral/`
    parent brought into being implicitly would come out at umask default.
    Each call here has an already-existing direct parent, so its `mode` is
    the one that actually lands.
    """
    paths.ensure_runtime_dir()
    root = paths.RUNTIME_DIR / "ephemeral"
    root.mkdir(exist_ok=True, mode=0o700)
    d = root / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    d.mkdir(mode=0o700)
    return d / "kernel.sock"


def _teardown_ephemeral(sock_path: Path, pid: int | None) -> None:
    """Kill the one-off kernel `--ephemeral` spawned, then remove its directory.

    SIGTERM first, so `_shutdown`'s `@every`/`defer()` drain gets its usual
    2s budget — the same courtesy `restart_kernel` extends the kernel it
    replaces. SIGKILL only if it's still alive after `WAIT_STEPS` ticks: an
    ephemeral kernel outliving the bridge that owns it is exactly the leak
    this flag exists to prevent, so waiting indefinitely isn't an option the
    way it is for a kernel meant to keep running for weeks.
    """
    import shutil
    import signal

    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pid = None
        else:
            for _ in range(WAIT_STEPS):
                if not state.pid_alive(pid):
                    break
                time.sleep(WAIT_STEP_SECONDS)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    shutil.rmtree(sock_path.parent, ignore_errors=True)


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


def _static_mimetypes() -> dict[str, str]:
    """URI → mimeType, from the same declaration `resources/list` advertises.

    The kernel builds `protocol._RESOURCE_MIMETYPES` off `STATIC_RESOURCES` for
    exactly this; the bridge is the other author of a `resources/read` reply
    and had a bare "text/plain" literal instead.
    """
    return {r["uri"]: r["mimeType"] for r in core_schemas.STATIC_RESOURCES}


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
    def __init__(self, socket_path: Path, *, ephemeral: bool = False) -> None:
        self.socket_path = socket_path
        self.lock_path = paths.lock_for(socket_path)
        # ephemeral spawns eagerly in run() rather than lazily on first real
        # tool call, and tears the kernel down (_teardown_ephemeral) instead
        # of leaving it running when stdin closes. See run_bridge's --ephemeral.
        self._ephemeral = ephemeral
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
                _response(
                    rid,
                    {
                        # Negotiated per-client, never read from the cache: the
                        # spec's rule is echo-what-they-asked-for-if-supported,
                        # and a cached kernel answer was negotiated with *that*
                        # session's client, not this one. (The cache is
                        # version-gated to this repld build anyway, so the two
                        # sides can't disagree about what's supported.)
                        "protocolVersion": core_schemas.negotiate_version(
                            (msg.get("params") or {}).get("protocolVersion")
                        ),
                        "capabilities": core_schemas.CAPABILITIES,
                        "serverInfo": {
                            "name": "repld",
                            "version": cache["version"] if cache else __version__,
                        },
                        "instructions": cache["instructions"]
                        if cache
                        else _minimal_instructions(),
                    },
                )
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
            self._to_client(_response(rid, {"tools": tools}))
            return True

        if method == "resources/list":
            cache = self._load_cache()
            resources = (
                cache["resources"]
                if cache
                else core_schemas.wire(core_schemas.STATIC_RESOURCES)
            )
            self._to_client(_response(rid, {"resources": resources}))
            return True

        if method == "resources/templates/list":
            self._to_client(
                _response(rid, {"resourceTemplates": core_schemas.RESOURCE_TEMPLATES})
            )
            return True

        if method == "ping":
            # Answered here, not by a kernel — the bridge is the thing being
            # pinged. Must not fall through: anything the intercept declines
            # that isn't in _NEEDS_KERNEL is answered -32601.
            self._to_client(_response(rid, {}))
            return True

        if method == "resources/read":
            uri = (msg.get("params") or {}).get("uri", "")
            text = _static_docs().get(uri)
            if text is None:
                return False  # gist/browser resource — needs a live kernel
            # mimeType from the same declaration both sides advertise in
            # `resources/list`, not a literal. It was hardcoded "text/plain"
            # here while the kernel read `core_schemas` (`protocol.py`'s
            # `_RESOURCE_MIMETYPES`) — identical today, since all four docs are
            # plain text, and silently divergent the moment a doc isn't. The
            # cold path is the one nobody looks at, which is the whole argument
            # for `core_schemas` existing.
            self._to_client(
                _response(
                    rid,
                    {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": _static_mimetypes().get(uri, "text/plain"),
                                "text": text,
                            }
                        ]
                    },
                )
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
        # line that matters. It has to reach `spawn_headless` too — the prints
        # that explain *why* a spawn failed live there, and suppressing only
        # this function's line left them repeating unchecked.
        outcome = spawn.spawn_headless(self.socket_path, quiet=quiet)
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
        for step in range(WAIT_STEPS):
            time.sleep(WAIT_STEP_SECONDS)
            result = self._connect_excluding(exclude_pid)
            if not isinstance(result, str):
                self._attach(*result)
                return True
            if exclude_pid is not None and (step + 1) % _RESPAWN_EVERY == 0:
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
            self._to_client(_notification("notifications/tools/list_changed"))
            self._to_client(_notification("notifications/resources/list_changed"))
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
            self._to_client(_response(rid, result))

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
            self._to_kernel(_notification("notifications/initialized"))

    def _on_kernel_gone(self, gen: int | None = None) -> None:
        """Tear down the current kernel attachment and orphan its in-flight ids.

        `gen` makes the teardown *conditional*, and the condition is checked
        under the same lock that performs it. That matters for the one caller
        that has a generation: `_read_kernel` reaching EOF used to compare
        `gen != self._generation` under the lock, release it, and then call in
        here — which takes the lock again and unconditionally nulls `_sock`,
        with no idea whose socket it is closing. A `_reconnect` completing in
        that gap (main thread notices the dead pid first, `_attach` bumps the
        generation and installs a fresh socket) left the superseded reader
        tearing down the *replacement* and answering every id registered
        against it with -31001. Passing the generation in rather than moving
        the call inside the caller's `with` because `_state_lock` is a plain
        Lock, not an RLock.

        Callers with no generation to name (a send failure in `_to_kernel`, an
        explicit `restart_kernel`) pass None and always tear down: they are
        acting on whatever is current by definition.
        """
        with self._state_lock:
            if gen is not None and gen != self._generation:
                return
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
                _error(rid, KERNEL_GONE, "repld kernel restarted; request was lost")
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
        # a superseded reader is just finishing its own closed socket. The
        # check happens inside `_on_kernel_gone`, under the lock that does the
        # teardown, so a reconnect can't land between deciding and acting.
        self._on_kernel_gone(gen)

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
            self._to_client(_error(rid, -32603, f"repld bridge internal error: {exc}"))

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
                    self._to_client(_error(rid, -32601, f"method not found: {method}"))
                return

        if not self._ensure_kernel():
            if rid is not None:
                self._to_client(
                    _error(
                        rid,
                        KERNEL_GONE,
                        "repld kernel is not running and could not be started; "
                        "see stderr",
                    )
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
        #
        # --ephemeral is the one exception, on purpose: its whole point is a
        # live kernel for exactly this session, not a cache-backed discovery
        # answer that defers spawning to the first real tool call. Spawning
        # here also means every method — including `initialize` itself —
        # forwards to a real kernel instead of the static/cached fallback,
        # since _dispatch_client_line only takes that path while self._sock
        # is still None.
        if self._ephemeral:
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
        # --ephemeral inverts that: it dies with the bridge that spawned it.
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._ephemeral:
            _teardown_ephemeral(self.socket_path, self._kernel_pid)
        return 0


_BRIDGE_USAGE = (
    "repld bridge — stdio MCP proxy (spawned by Claude Code, not run by hand)\n"
    "\n"
    "  repld bridge [--socket PATH]\n"
    "  repld bridge --ephemeral\n"
    "\n"
    "  --ephemeral spawns a private, single-use kernel for this bridge alone,\n"
    "  eagerly rather than on first tool call, and kills it when stdin closes\n"
    "  instead of leaving it running. Mutually exclusive with --socket: its\n"
    "  whole point is a path nothing else could already be attached to.\n"
    "\n"
    "  Register with: claude mcp add repld -- repld bridge\n"
)


def run_bridge(argv: list[str]) -> int:
    # Validated like every other subcommand, which this one alone was not: it
    # threw the residue of `resolve_socket_path` away as `_`. That made `repld
    # bridge --help` start a proxy that blocks on stdin forever instead of
    # printing usage, and — the case that actually bites — a typo'd flag in an
    # MCP registration (`repld bridge --sockett /x`) silently ignored, on the
    # one command nobody ever sees a terminal for.
    if cli_args.wants_help(argv):
        print(_BRIDGE_USAGE)
        return 0
    if "--ephemeral" in argv:
        rest = [a for a in argv if a != "--ephemeral"]
        # No resolve_socket_path here: --ephemeral generates its own path and
        # ignores REPLD_SOCKET too, so a bare --socket left in argv falls
        # through to check_args as an unknown flag — which is exactly the
        # mutual-exclusivity refusal, for free rather than as a special case.
        bad = cli_args.check_args("repld bridge", rest, _BRIDGE_USAGE, positionals=0)
        if bad is not None:
            return bad
        return Bridge(_ephemeral_socket_path(), ephemeral=True).run()
    socket_path, rest = paths.resolve_socket_path(argv)
    bad = cli_args.check_args("repld bridge", rest, _BRIDGE_USAGE, positionals=0)
    if bad is not None:
        return bad
    return Bridge(socket_path).run()
