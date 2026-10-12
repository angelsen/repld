"""Phase 19: kernel-to-kernel calls (`peers`, `peer/*`).

A is the shared smoketest kernel, driven through a Bridge that carries a Claude
session id. B is a second real kernel with a `svc` gist and a slow bootstrap; C,
C2 and D are throwaways for dying mid-call and for starting a cold peer. Every
lookup is by path or by a tempdir basename, because `XDG_RUNTIME_DIR` is shared
with the developer's own kernels and the sessions registry shows all of them.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

from harness import (
    REPO,
    Bridge,
    Kernel,
    assert_eq,
    assert_true,
    content_text,
    lock_path_for,
    runtime_dir_for,
)

_SESSION = "sess-peer"

_SVC = textwrap.dedent('''
    """Peer test service."""
    import asyncio
    import time
    from pathlib import Path

    import repld

    STATE = {}

    def add(a, b=1):
        return a + b

    async def slow(s):
        await asyncio.sleep(s)
        return "ok"

    async def ping():
        return "pong"

    def boom():
        raise ValueError("bad wav")

    def obj():
        return object()

    def big(n):
        return "x" * n

    def mark(path):
        with open(path, "a") as f:
            f.write("x\\n")
        return Path(path).read_text().count("x")

    def whoami():
        return repld.peer_caller()

    def sessions():
        return repld.claude_sessions()

    def shout():
        repld.notify("hello from the peer test")
        return True

    def block_sync(s):
        time.sleep(s)
        return "slept"

    async def wait_forever():
        try:
            await asyncio.sleep(60)
        finally:
            STATE["cancelled"] = True

    def cancelled():
        return STATE.get("cancelled", False)

    def reset():
        STATE.clear()

    async def call_back(cwd):
        return await repld.peers.get(cwd).call("os.getcwd")

    async def nested(cwd):
        return await repld.peers.get(cwd).call("svc.whoami")

    CONST = 3
''')

_INIT = textwrap.dedent("""
    import time
    time.sleep(2)
    def main_ready():
        return True
""")

_CSVC = textwrap.dedent('''
    """Service that records a run, then outlasts its kernel."""
    import asyncio
    import time

    def count_then_sleep(path, s):
        with open(path, "a") as f:
            f.write("x\\n")
        time.sleep(s)

    async def count_then_wait(path):
        with open(path, "a") as f:
            f.write("x\\n")
        await asyncio.sleep(60)

    async def count_then_hog(path):
        # Holds the loop, so a shutdown cannot drain this task before it stops.
        with open(path, "a") as f:
            f.write("x\\n")
        time.sleep(30)
''')


def _project(
    root: Path, name: str, gists: dict[str, str], init: str | None = None
) -> Path:
    path = root / name
    (path / "gists").mkdir(parents=True)
    for fname, src in gists.items():
        (path / "gists" / fname).write_text(src)
    if init is not None:
        (path / "repld_init.py").write_text(init)
    return path


def _run(a: Bridge, code: str, *, timeout: float = 30.0, wrap: bool = True) -> str:
    """One cell on A, finished inline and not raised; returns its output.

    Wrapped in a coroutine so scratch names stay local: A is the shared smoketest
    kernel, and only `pp_`-prefixed globals (declared with `global`) may outlive
    a cell.
    """
    if wrap:
        code = (
            "async def pp_cell():\n"
            + textwrap.indent(code, "    ")
            + "\nawait pp_cell()\n"
        )
    resp = a.exec(code, timeout=timeout, call_timeout=timeout + 10)
    text = content_text(resp)
    assert_true(resp["result"]["_meta"]["done"], f"cell finished inline: {text[-400:]}")
    assert_true(not resp["result"].get("isError"), f"cell raised: {text[-900:]}")
    return text


def _last_json(text: str):
    return json.loads(text.strip().splitlines()[-1])


def _stop(proc_owner: Kernel | None, path: Path | None) -> None:
    if proc_owner is not None:
        proc_owner.stop()
    if path is not None:
        shutil.rmtree(runtime_dir_for(path), ignore_errors=True)
        shutil.rmtree(path, ignore_errors=True)


def _surface(a: Bridge, b_path: Path) -> None:
    prelude = f"import asyncio, json, os, time\npp_b = {str(b_path)!r}\npp_peer = peers.get(pp_b)\n"
    _run(a, prelude, wrap=False)
    # B's bootstrap sleeps 2s after the socket binds; the first call has to
    # wait for it rather than see a bare __main__.
    out = _run(a, "print(json.dumps(await pp_peer.call('main_ready')))")
    assert_eq(_last_json(out), True, "first call waits for the callee's bootstrap")

    out = _run(a, f"print(json.dumps(peers.get({b_path.name!r}) is pp_peer))")
    assert_eq(
        _last_json(out),
        True,
        "a live kernel's directory name resolves to the same handle",
    )
    out = _run(
        a,
        "try:\n    peers.get('no-such-kernel-xyz')\nexcept peers.PeerNotFound as e:\n"
        "    print(json.dumps(['no live kernel' in str(e)]))",
    )
    assert_eq(_last_json(out), [True], "an unknown name is PeerNotFound")
    out = _run(
        a, "print(json.dumps([p['name'] for p in peers.list() if p['cwd'] == pp_b]))"
    )
    assert_eq(_last_json(out), [b_path.name], "peers.list() shows the live kernel")

    out = _run(
        a,
        "info = await pp_peer.info()\nprint(json.dumps([info['peer_protocol'], info['kernel']['cwd'] == pp_b, [g['name'] for g in info['gists']], [f['name'] for g in info['gists'] if g['name'] == 'svc' for f in g['functions']][:3], [m['name'] for m in info['main']]]))",
    )
    proto, cwd_ok, gist_names, first_fns, main_names = _last_json(out)
    assert_eq((proto, cwd_ok), (1, True), "peer/list identifies the kernel")
    assert_true("svc" in gist_names, "peer/list names the gist")
    assert_eq(
        first_fns, ["add", "slow", "ping"], "peer/list carries function names in order"
    )
    assert_true("main_ready" in main_names, "peer/list shows __main__ callables")
    out = _run(a, "print(json.dumps(await pp_peer.inspect('svc.add')))")
    ins = _last_json(out)
    assert_eq(
        (ins["signature"], ins["kind"], ins["async"]),
        ("(a, b=1)", "function", False),
        "peer/inspect",
    )
    print("  ✓ peers: bootstrap wait, name/path handles, list/info/inspect")


def _values_and_errors(a: Bridge, tmp: Path) -> None:
    marks = tmp / "marks.txt"
    out = _run(
        a,
        "r = [await pp_peer.call('svc.add', 2, b=3), await pp_peer.call('svc.slow', 0), "
        "await pp_peer.call('os.path.join', 'a', 'b'), await pp_peer.call('math.sqrt', 4)]\n"
        "print(json.dumps(r))",
    )
    assert_eq(
        _last_json(out),
        [5, "ok", "a/b", 2.0],
        "sync, async, stdlib and builtin targets",
    )

    out = _run(
        a,
        "try:\n    await pp_peer.call('svc.boom')\nexcept peers.PeerCallError as e:\n"
        "    print(json.dumps([e.remote_type, e.remote_message, 'svc.py' in e.remote_traceback, 'bad wav' in str(e)]))",
    )
    assert_eq(
        _last_json(out),
        ["ValueError", "bad wav", True, True],
        "a raised error is PeerCallError with the remote traceback",
    )

    # Each refusal below must leave the counter untouched: never ran.
    code = (
        "res = []\n"
        "for tgt, args in [('svc.nope', []), ('nosuchmodxyz.f', []), ('svc.CONST', []), "
        f"('svc.mark', [{str(marks)!r}, 1, 2]), ('svc.mark', [])]:\n"
        "    try:\n        await pp_peer.call(tgt, *args)\n"
        "    except peers.PeerRefused as e:\n        res.append(e.reason)\n"
        "print(json.dumps(res))"
    )
    assert_eq(
        _last_json(_run(a, code)),
        ["unresolved", "unresolved", "unresolved", "bad_args", "bad_args"],
        "refusals say they never ran",
    )
    assert_true(not marks.exists(), "a refused call did not run")
    out = _run(a, f"print(json.dumps(await pp_peer.call('svc.mark', {str(marks)!r})))")
    assert_eq(_last_json(out), 1, "the same target runs once when called properly")

    out = _run(
        a,
        "res = []\n"
        "for tgt, args in [('svc.obj', []), ('svc.big', [9_000_000])]:\n"
        "    try:\n        await pp_peer.call(tgt, *args)\n"
        "    except peers.PeerCallError as e:\n        res.append([e.remote_type, 'file' in e.remote_message])\n"
        "try:\n    await pp_peer.call('svc.add', object())\nexcept TypeError as e:\n    res.append('client-side')\n"
        "print(json.dumps(res))",
    )
    assert_eq(
        _last_json(out),
        [["PayloadError", True], ["PayloadError", True], "client-side"],
        "unserialisable and oversized values fail loudly, bad arguments never leave",
    )
    print("  ✓ peers: values, typed errors, never-ran refusals, payload limits")


def _identity(a: Bridge, b_path: Path) -> None:
    out = _run(a, "print(json.dumps(await pp_peer.call('svc.whoami')))")
    caller = _last_json(out)
    assert_eq(
        caller["claude_session_id"], _SESSION, "the Claude session id travels per call"
    )
    assert_true(
        caller["pid_verified"] is True, "the kernel pid is checked against SO_PEERCRED"
    )
    assert_eq(
        caller["peer_pid"], caller["kernel"]["pid"], "peer pid equals the claimed pid"
    )
    assert_eq(len(caller["chain"]), 1, "a direct call has a one-hop chain")

    events = lock_path_for(b_path).with_suffix(".events")
    deadline = time.monotonic() + 5
    recs: list[dict] = []
    while time.monotonic() < deadline and not recs:
        recs = [
            r
            for line in events.read_text().splitlines()
            if (r := _safe_json(line))
            and r.get("type") == "CellStart"
            and (r.get("caller") or {}).get("claude_session_id") == _SESSION
        ]
        time.sleep(0.1)
    assert_true(bool(recs), "B's event log records the caller on CellStart")
    assert_true(
        "peer call from" in recs[-1]["source"], "the CellStart source names the caller"
    )

    b = Bridge(b_path)
    try:
        b.handshake()
        text = content_text(
            b.exec(
                "from repld import tasks\nprint([i for i, t in tasks.items() if t.get('label') == 'peer:svc.whoami'][-1])"
            )
        )
        tid = text.strip().splitlines()[-1]
        snap = b.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": tid}}
        )["result"]["structuredContent"]
        assert_eq(
            snap["caller"]["claude_session_id"], _SESSION, "get_task carries the caller"
        )
        assert_eq(snap["label"], "peer:svc.whoami", "get_task label names the target")
    finally:
        b.close()
    cli = subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "tasks", "--json"],
        cwd=str(b_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    rows = [
        t
        for t in json.loads(cli.stdout)["tasks"]
        if t.get("label") == "peer:svc.whoami"
    ]
    assert_true(
        bool(rows) and rows[-1]["caller"]["claude_session_id"] == _SESSION,
        f"repld tasks --json shows the caller ({cli.stdout[:200]})",
    )

    out = _run(
        a,
        "print(json.dumps([await pp_peer.call('svc.sessions'), await pp_peer.call('svc.shout'), await pp_peer.call('svc.add', 1)]))",
    )
    assert_eq(
        _last_json(out),
        [[], True, 2],
        "a peer connection is not a Claude session, and a broadcast does not disturb a call",
    )
    print("  ✓ peers: caller identity on the call, event log, get_task, tasks --json")


def _safe_json(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _async_behaviour(a: Bridge, b_path: Path) -> None:
    out = _run(
        a,
        "t = time.time()\nr = await asyncio.gather(*[pp_peer.call('svc.slow', 0.5) for _ in range(5)])\nprint(json.dumps([r, time.time() - t]))",
    )
    results, elapsed = _last_json(out)
    assert_eq(results, ["ok"] * 5, "gathered calls all return")
    assert_true(elapsed < 1.5, f"gather ran concurrently ({elapsed:.2f}s for 5 x 0.5s)")

    out = _run(
        a,
        "blocker = asyncio.ensure_future(pp_peer.call('svc.block_sync', 1.5))\n"
        "await asyncio.sleep(0.2)\nt = time.time()\nawait pp_peer.call('svc.ping')\nlat = time.time() - t\n"
        "await blocker\nprint(json.dumps(lat))",
    )
    assert_true(
        _last_json(out) < 0.6, "a blocking sync callee does not stall the callee's loop"
    )

    # A call outliving exec's inline budget defers; the value arrives with the push.
    resp = a.exec("await pp_peer.call('svc.slow', 2.5)", timeout=1, call_timeout=10)
    meta = resp["result"]["_meta"]
    assert_eq(meta["done"], False, "a long peer call defers the caller's cell")
    notif = a.wait_notification(
        "notifications/claude/channel",
        kind="task_done",
        where=lambda m: m["params"]["meta"].get("task_id") == meta["task_id"],
        timeout=15.0,
    )
    assert_true(
        "ok" in notif["params"]["content"], "the completion push carries the value"
    )

    _run(a, "await pp_peer.call('svc.reset')")
    out = _run(
        a,
        "try:\n    async with asyncio.timeout(0.5):\n        await pp_peer.call('svc.wait_forever')\n"
        "except TimeoutError:\n    pass\nawait asyncio.sleep(1.5)\nprint(json.dumps(await pp_peer.call('svc.cancelled')))",
    )
    assert_eq(_last_json(out), True, "asyncio.timeout cancels the target on the callee")

    out = _run(
        a,
        "await pp_peer.call('svc.reset')\nprint(defer(pp_peer.call('svc.wait_forever')))",
    )
    tid = out.strip().splitlines()[-1]
    time.sleep(
        0.5
    )  # the target must be running on the callee before a cancel means anything
    resp = a.call("tools/call", {"name": "cancel", "arguments": {"task_id": tid}})
    assert_true(
        "accepted" in content_text(resp), f"cancel accepted ({content_text(resp)})"
    )
    time.sleep(1.5)
    out = _run(a, "print(json.dumps(await pp_peer.call('svc.cancelled')))")
    assert_eq(
        _last_json(out), True, "MCP cancel of the awaiting task cancels the target"
    )
    print(
        "  ✓ peers: gather concurrency, free loop, deferred value push, cancellation reaches the callee"
    )


def _cycles(a: Bridge, kernel: Kernel) -> None:
    a_cwd = str(kernel.cwd)
    out = _run(
        a,
        f"try:\n    await pp_peer.call('svc.call_back', {a_cwd!r})\nexcept peers.PeerCallError as e:\n"
        "    print(json.dumps([e.remote_type, 'cycle' in e.remote_message]))",
    )
    assert_eq(
        _last_json(out),
        ["PeerRefused", True],
        "a call that comes back to its origin is refused",
    )
    out = _run(
        a,
        f"try:\n    await peers.get({a_cwd!r}).call('os.getcwd')\nexcept peers.PeerRefused as e:\n"
        "    print(json.dumps(e.reason))",
    )
    assert_eq(_last_json(out), "self", "a kernel cannot call itself")
    print("  ✓ peers: cycle and self-call refused")


def _nested_identity(a: Bridge, b_path: Path, c_path: Path) -> None:
    out = _run(
        a,
        f"print(json.dumps(await pp_peer.call('svc.nested', {str(c_path)!r})))",
    )
    caller = _last_json(out)
    assert_eq(
        caller["claude_session_id"],
        _SESSION,
        "the originating Claude session survives a nested hop",
    )
    assert_eq(len(caller["chain"]), 2, "the chain lists both kernels on the path")
    assert_eq(
        caller["chain"][0]["claude_session_id"],
        _SESSION,
        "the first hop is the original caller",
    )
    print("  ✓ peers: a nested call keeps the originating session and the whole chain")


def _death(a: Bridge, tmp: Path) -> None:
    # (signal, target, what the caller sees). A graceful stop drains the task
    # and answers it as cancelled; only a callee that cannot drain (a held loop)
    # or dies outright leaves the caller with an unknown outcome.
    cases = (
        (signal.SIGKILL, "count_then_sleep", ["disconnected", False]),
        (signal.SIGTERM, "count_then_hog", ["disconnected", True]),
        (signal.SIGTERM, "count_then_wait", ["call_error", "CancelledError"]),
    )
    for sig, target, expected in cases:
        label = f"{sig.name} {target}"
        path = _project(tmp, f"die-{target}", {"csvc.py": _CSVC})
        counter = tmp / f"counter-{target}.txt"
        k = Kernel(path)
        try:
            args = (
                f"{str(counter)!r}, 30"
                if target == "count_then_sleep"
                else f"{str(counter)!r}"
            )
            _run(
                a,
                "global pp_c, pp_t\n"
                f"pp_c = peers.get({str(path)!r})\n"
                f"pp_t = asyncio.ensure_future(pp_c.call('csvc.{target}', {args}))",
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not counter.exists():
                time.sleep(0.1)
            assert_true(counter.exists(), f"{label}: the call started on the callee")
            # The lockfile's pid is the kernel itself; `k.proc` may be the `uv run`
            # wrapper, which SIGKILL would leave orphaning the real process.
            os.kill(int(json.loads(lock_path_for(path).read_text())["pid"]), sig)
            out = _run(
                a,
                "global pp_t\ntry:\n    await asyncio.wait_for(pp_t, 20)\n"
                "except peers.PeerDisconnected as e:\n    print(json.dumps(['disconnected', e.clean]))\n"
                "except peers.PeerCallError as e:\n    print(json.dumps(['call_error', e.remote_type]))",
                timeout=30,
            )
            assert_eq(_last_json(out), expected, f"{label}: what the caller sees")
            assert_eq(
                counter.read_text().count("x"),
                1,
                f"{label}: the target ran exactly once, nothing was retried",
            )
        finally:
            try:
                k.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                k.proc.kill()
            _stop(None, path)
    print(
        "  ✓ peers: death mid-call is unknown (clean only after a goodbye), a graceful stop answers as cancelled, never retried"
    )


def _cold_peer(a: Bridge, tmp: Path) -> None:
    cold = tmp / "cold"
    (cold / "gists").mkdir(parents=True)
    out = _run(
        a,
        f"try:\n    await peers.get({str(cold)!r}).call('os.getcwd')\nexcept peers.PeerUnavailable as e:\n"
        "    print(json.dumps(['repld --project' in str(e), 'start=True' in str(e)]))",
    )
    assert_eq(
        _last_json(out),
        [True, True],
        "a stopped peer is PeerUnavailable naming the way to start it",
    )

    out = _run(
        a,
        "from repld.kernel import DOTENV_KEYS\nos.environ['PEER_SECRET'] = '1'\nDOTENV_KEYS.add('PEER_SECRET')\n"
        f"pd = peers.get({str(cold)!r}, start=True)\n"
        "print(json.dumps([await pd.call('os.getcwd'), await pd.call('os.environ.get', 'PEER_SECRET')]))\n"
        "os.environ.pop('PEER_SECRET', None)\nDOTENV_KEYS.discard('PEER_SECRET')",
        timeout=90,
    )
    try:
        cwd, secret = _last_json(out)
        assert_eq(
            os.path.realpath(cwd),
            os.path.realpath(str(cold)),
            "start=True spawned the target's own kernel",
        )
        assert_eq(
            secret, None, "the caller's .env keys did not reach the spawned kernel"
        )
    finally:
        lock = json.loads(lock_path_for(cold).read_text())
        os.kill(int(lock["pid"]), signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and lock_path_for(cold).exists():
            time.sleep(0.1)
        shutil.rmtree(runtime_dir_for(cold), ignore_errors=True)
    print(
        "  ✓ peers: cold peer is an error unless start=True, which spawns it with a cleaned environment"
    )


def phase_19_peers(kernel: Kernel) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="repld-peers-"))
    b_path = _project(tmp, "svc-peer", {"svc.py": _SVC}, _INIT)
    c_path = _project(tmp, "svc-peer-c", {"svc.py": _SVC})
    kb: Kernel | None = None
    kc: Kernel | None = None
    a = Bridge(kernel.cwd, env={"CLAUDE_CODE_SESSION_ID": _SESSION})
    try:
        a.handshake()
        kb = Kernel(b_path)
        kc = Kernel(c_path)
        _surface(a, b_path)
        _values_and_errors(a, tmp)
        _identity(a, b_path)
        _async_behaviour(a, b_path)
        _cycles(a, kernel)
        _nested_identity(a, b_path, c_path)
        _death(a, tmp)
        _cold_peer(a, tmp)
    finally:
        try:
            a.exec(
                "for n in [n for n in list(globals()) if n.startswith('pp_')]:\n    globals().pop(n, None)"
            )
        except Exception:
            pass
        a.close()
        _stop(kb, b_path)
        _stop(kc, c_path)
        shutil.rmtree(tmp, ignore_errors=True)
