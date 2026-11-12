"""Phases 2–3: Core MCP plumbing — initialize, tools/list, sync exec, deferred exec, get_task."""

import json
import socket
import time
from pathlib import Path
from queue import Empty

from harness import REPO, Bridge, Kernel, assert_eq, assert_true, content_text

from repld import core_schemas
from repld.ipc import Session


def phase_3(kernel: Kernel) -> None:
    """initialize → tools/list → exec sync → exec nudge → get_task (poll to done)."""
    b = Bridge(kernel.cwd)
    try:
        resp = b.call("initialize", {"protocolVersion": "2024-11-05"})
        result = resp["result"]
        assert_eq(result["serverInfo"]["name"], "repld", "initialize.serverInfo.name")
        assert_true(
            "claude/channel" in result["capabilities"]["experimental"],
            "initialize.capabilities advertises claude/channel",
        )
        # The spec's negotiation rule: echo a supported requested version. A
        # server that always answers its own latest tells a 2024-11-05 client
        # to disconnect for no reason.
        assert_eq(
            result["protocolVersion"],
            "2024-11-05",
            "a supported requested version is echoed, not overridden",
        )
        # Drift guard, paired with the kernel-less assertion in phase 15: both
        # sides of the socket answer `initialize`, and a capability declared in
        # only one of them is silently absent from half the sessions.
        assert_eq(
            result["capabilities"],
            core_schemas.CAPABILITIES,
            "live kernel negotiates exactly core_schemas.CAPABILITIES",
        )
        print("  ✓ initialize")

        # The rest of negotiate_version is pure: unknown or absent versions get
        # our latest rather than an echo of a string nobody implements.
        assert_eq(
            core_schemas.negotiate_version("2099-01-01"),
            core_schemas.PROTOCOL_VERSION,
            "unknown requested version → our latest",
        )
        assert_eq(
            core_schemas.negotiate_version(None),
            core_schemas.PROTOCOL_VERSION,
            "absent requested version → our latest",
        )
        for v in core_schemas.SUPPORTED_VERSIONS:
            assert_eq(core_schemas.negotiate_version(v), v, f"{v} echoes")
        print("  ✓ version negotiation: echo supported, latest otherwise")

        resp = b.call("ping", {})
        assert_eq(resp["result"], {}, "ping answered with an empty result")
        print("  ✓ ping")

        b.send("notifications/initialized", {}, notif=True)
        print("  ✓ notifications/initialized sent")

        resp = b.call("tools/list")
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        core_tools = {"cancel", "exec", "get_task"}
        assert_true(
            core_tools.issubset(set(tool_names)),
            f"tools/list contains core tools (got {tool_names!r})",
        )
        print(f"  ✓ tools/list: {tool_names}")

        # Sync exec
        resp = b.exec("print('hi from exec')")
        content = content_text(resp)
        meta = resp["result"]["_meta"]
        assert_true("hi from exec" in content, f"sync exec output (got {content!r})")
        assert_eq(meta["done"], True, "sync exec meta.done")
        print(f"  ✓ sync exec: {content.strip()!r}")

        # Nudge exec
        resp = b.exec(
            "import time\nfor i in range(3):\n    time.sleep(0.3); print(f'step {i}')",
            timeout=0.2,
            call_timeout=3.0,
        )
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], False, "nudge exec meta.done")
        task_id = meta["task_id"]
        assert_true(task_id, "nudge has task_id")
        print(f"  ✓ nudged exec: task_id={task_id}")

        # Poll get_task until done
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            resp = b.call(
                "tools/call", {"name": "get_task", "arguments": {"task_id": task_id}}
            )
            snap = resp["result"]["_meta"]
            if snap.get("done"):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("get_task: task never completed within 5s")
        assert_true(
            "step 2" in snap["text"],
            f"get_task captured all output (got {snap['text']!r})",
        )
        # get_task declares an outputSchema, which commits every success to a
        # conforming structuredContent (2025-06-18); the text block mirrors it.
        assert_eq(
            resp["result"]["structuredContent"],
            snap,
            "get_task carries structuredContent matching its snapshot",
        )
        gt_schema = next(t for t in core_schemas.CORE_TOOLS if t["name"] == "get_task")[
            "outputSchema"
        ]
        missing = set(gt_schema["required"]) - set(snap)
        assert_eq(missing, set(), "snapshot carries every field outputSchema requires")
        assert_true(
            snap["push_state"] in gt_schema["properties"]["push_state"]["enum"],
            f"push_state is one of the schema's states (got {snap['push_state']!r})",
        )
        print(f"  ✓ get_task: done, output {snap['text'].strip()!r}")

        # Unknown task_id is an input problem the model can fix, so it is an
        # in-band error (SEP-1303), not a JSON-RPC one.
        resp = b.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": "bogus-tid"}}
        )
        text = content_text(resp)
        assert_true(
            resp.get("result", {}).get("isError") is True and "unknown task_id" in text,
            f"get_task rejects unknown task_id in-band (got {resp!r})",
        )
        assert_true(
            f"(kernel: {kernel.cwd}" in text,
            f"and names the kernel asked (got {text!r})",
        )
        print("  ✓ get_task: unknown task_id -> isError naming the kernel")

        _phase_3_input_validation(b)
        _phase_3_jsonrpc_edges(b)
        _phase_3_client_cancel(b)

        # An argument no schema declares is refused in-band (isError, so the model sees it), never dropped.
        for name, args in (
            ("get_task", {"task_id": "bogus-tid", "wait": 45}),
            ("exec", {"code": "RAN_WITH_EXTRA = 1", "label": "x"}),
            ("browser_tabs", {"bogus": 1}),
        ):
            resp = b.call("tools/call", {"name": name, "arguments": args})
            res = resp.get("result", {})
            assert_true(
                res.get("isError") is True and "unknown argument" in content_text(resp),
                f"{name} refuses an undeclared argument (got {resp!r})",
            )
        assert_true(
            "False" in content_text(b.exec("print('RAN_WITH_EXTRA' in dir())")),
            "refused exec never ran",
        )
        print(
            "  ✓ undeclared arguments → isError naming them; the refused exec never ran"
        )

        # Spill test — every cell with output now spills; large output
        # additionally trips the truncated-preview path.
        resp = b.exec("print('x' * 70000)", timeout=5.0, call_timeout=10.0)
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], True, "spill exec done")
        assert_eq(meta["spilled"], True, "spill exec meta.spilled")
        spill_path = meta["spill_path"]
        assert_true(
            spill_path and Path(spill_path).exists(), f"spill file exists: {spill_path}"
        )
        # Read directly from the file (no MCP read_spill tool — agents use Read).
        with open(spill_path) as f:
            head = f.read(100)
        assert_true(
            head.startswith("x" * 50), f"spill file starts with x's (got {head[:20]!r})"
        )
        print(f"  ✓ spill: {spill_path} ({len(head)} chars head, starts with x's)")

        # Multi-line str results print verbatim (no repr()-escaping of \n).
        resp = b.exec("'line one\\nline two'")
        content = content_text(resp)
        assert_true(
            "line one\nline two" in content and "\\n" not in content,
            f"multi-line str displayed verbatim (got {content!r})",
        )
        print(f"  ✓ multi-line str display: {content!r}")

        # no_display() suppresses the print but still returns/binds the value.
        resp = b.exec("no_display('quiet result')")
        content = content_text(resp)
        assert_true(
            "quiet result" not in content,
            f"no_display() suppressed output (got {content!r})",
        )
        resp = b.exec("_")
        content = content_text(resp)
        assert_true(
            "quiet result" in content,
            f"no_display() still bound to _ (got {content!r})",
        )
        print("  ✓ no_display(): suppressed on display, bound to _")

        # --- Session.close() explicitly closes rfile/wfile, not just the
        # raw socket -- otherwise their buffered close() is left to GC/
        # interpreter-shutdown finalization, which can surface an
        # uncatchable BrokenPipeError instead of being handled here ---
        sock, peer = socket.socketpair()
        session = Session(sock)
        try:
            assert_true(not session.rfile.closed, "rfile open before close()")
            session.close()
            assert_true(session.rfile.closed, "rfile closed by Session.close()")
            assert_true(session.wfile.closed, "wfile closed by Session.close()")
        finally:
            peer.close()
        print("  ✓ Session.close() explicitly closes rfile/wfile, not just the socket")
    finally:
        b.close()


def _phase_3_input_validation(b: Bridge) -> None:
    """A call that breaks its inputSchema is an isError the model can read, never a run or a -32603."""
    for name, args, needle in (
        ("exec", {}, "missing required"),
        ("exec", {"code": "RAN_BAD_TIMEOUT = 1", "timeout": "abc"}, "timeout"),
        ("exec", {"code": 5}, "code"),
        ("get_task", {}, "missing required"),
        ("cancel", {"task_id": 7}, "task_id"),
    ):
        resp = b.call("tools/call", {"name": name, "arguments": args})
        assert_true(
            resp.get("result", {}).get("isError") is True
            and needle in content_text(resp),
            f"{name} {args!r} refused in-band naming {needle!r} (got {resp!r})",
        )
    assert_true(
        "False" in content_text(b.exec("print('RAN_BAD_TIMEOUT' in dir())")),
        "the refused exec never ran",
    )
    resp = b.call("tools/call", {"name": "exec", "arguments": ["x"]})
    assert_eq(
        resp.get("error", {}).get("code"), -32602, "non-object arguments are -32602"
    )
    resp = b.call("tools/call", {"name": "no_such_tool", "arguments": {}})
    assert_eq(resp.get("error", {}).get("code"), -32602, "an unknown tool stays -32602")
    print(
        "  ✓ tools/call: required, type and shape checked against inputSchema, in-band"
    )


def _phase_3_jsonrpc_edges(b: Bridge) -> None:
    """Notifications get no reply; unparseable and malformed lines get -32700 / -32600."""
    assert b.proc.stdin is not None
    caps = b.call("initialize", {"protocolVersion": "2025-06-18"})["result"][
        "capabilities"
    ]
    assert_true(
        "claude/channel/permission" not in caps["experimental"],
        "no permission-relay capability is declared (nothing implements it)",
    )

    def raw(line: str) -> None:
        assert b.proc.stdin is not None
        b.proc.stdin.write(line + "\n")
        b.proc.stdin.flush()

    raw(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
    raw(json.dumps({"jsonrpc": "2.0", "method": "ping", "id": None}))
    resp = b.call("ping", {})
    assert_eq(resp["result"], {}, "a ping with an id is still answered")
    assert_true(
        b.inbox.empty(), "neither the id-less nor the null-id ping was answered first"
    )
    for line, code, label in (
        ("{nope", -32700, "unparseable JSON"),
        ('[{"jsonrpc":"2.0","method":"ping","id":9}]', -32600, "a batch array"),
        ('{"jsonrpc":"2.0","id":5}', -32600, "an id with no method"),
    ):
        raw(line)
        msg = b.inbox.get(timeout=3)
        assert_eq(msg.get("error", {}).get("code"), code, f"{label} -> {code}")
    assert_eq(b.call("ping", {})["result"], {}, "the bridge survives all of it")
    print(
        "  ✓ JSON-RPC edges: null-id ping silent, -32700 / -32600 for malformed lines"
    )


def _phase_3_client_cancel(b: Bridge) -> None:
    """`notifications/cancelled` stops the exec and suppresses its reply; unknown ids are ignored."""
    code = (
        "import asyncio\n"
        "try:\n"
        "    await asyncio.sleep(30)\n"
        "finally:\n"
        "    CANCEL_SEEN = True\n"
    )
    rid = b.send(
        "tools/call", {"name": "exec", "arguments": {"code": code, "timeout": 20}}
    )
    time.sleep(0.5)
    b.send("notifications/cancelled", {"requestId": 424242}, notif=True)
    b.send("notifications/cancelled", {"requestId": rid, "reason": "test"}, notif=True)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            msg = b.inbox.get(timeout=0.2)
        except Empty:
            continue
        assert_true(
            msg.get("id") != rid, f"a cancelled request is not answered (got {msg!r})"
        )
        b._notifs.append(msg)
    assert_true(
        "True" in content_text(b.exec("print(globals().get('CANCEL_SEEN'))")),
        "the cancelled cell was actually stopped",
    )
    assert_eq(b.call("ping", {})["result"], {}, "an unknown cancelled id is ignored")
    print("  ✓ notifications/cancelled: the cell stops, no reply, unknown ids ignored")


def phase_3_argv_and_registry(_kernel: Kernel) -> None:
    """Two guards that only fire on input nobody types on purpose.

    `repld bridge` was the one subcommand with no argv validation at all — it
    discarded `resolve_socket_path`'s residue into `_`. That is the command
    Claude Code spawns from an MCP registration, so a typo'd flag there is
    ignored on the one process with no terminal to notice it.

    `gists._read_registry` returned whatever JSON the registry file held,
    including a list or a string. Three callers then did `.items()` / `.values()`
    on it — `repld gist list`, `gist_links.link_targets`, and the
    `repld://gists/_registry` MCP resource — and a file nobody edits by hand
    took out all three. Its sibling `gist_links.read_links` has always guarded
    the same case on the same kind of file.
    """
    import json
    import subprocess
    import sys

    def _repld(*args: str) -> tuple[int, str]:
        p = subprocess.run(
            [sys.executable, "-m", "repld", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return p.returncode, p.stdout + p.stderr

    code, out = _repld("bridge", "--help")
    assert_eq(code, 0, "repld bridge --help exits 0")
    assert_true("repld bridge" in out, f"…and prints usage (got {out[:120]!r})")
    code, out = _repld("bridge", "--sockett", "/x")
    assert_eq(code, 2, "repld bridge rejects an unknown flag")
    assert_true("--sockett" in out, f"…naming it (got {out[:120]!r})")
    print("  ✓ repld bridge validates argv like every other subcommand")

    code, out = _repld("--socket", "/x/kernel.sock", "stop")
    assert_eq(code, 1, "a subcommand after a kernel flag is refused")
    assert_true(
        "repld stop --socket /x/kernel.sock" in out,
        f"…with the corrected order (got {out[:160]!r})",
    )
    print("  ✓ `repld --socket S stop` names the subcommand-first order")

    code, out = _repld("gist", "badverb", "--help")
    assert_eq(code, 0, "repld gist badverb --help asks for usage, not the verb")
    print("  ✓ `gist <unknown> --help` prints usage (wants_help scans every arg)")

    # Registry shape, checked directly — reaching it through the CLI would need
    # a real ~/.config/repld to clobber.
    from repld import gists

    original = gists._REGISTRY_PATH
    tmp = _kernel.cwd / "bogus-registry.json"
    try:
        for payload in ("[1, 2, 3]", '"a string"', "42", "not json at all"):
            tmp.write_text(payload)
            gists._REGISTRY_PATH = tmp
            gists._malformed_warned.clear()
            got = gists.registry()
            assert_eq(got, {}, f"registry root {payload[:12]!r} reads as empty")
            # The shape the three callers actually use.
            assert_eq(list(got.items()), [], "…and is safely .items()-able")
        tmp.write_text(json.dumps({"x": {"path": "/tmp/x.py"}}))
        gists._REGISTRY_PATH = tmp
        assert_eq(list(gists.registry()), ["x"], "a well-formed registry still reads")
    finally:
        gists._REGISTRY_PATH = original
        gists._malformed_warned.clear()
    print("  ✓ gist registry: wrong-shaped JSON reads as empty, not as a crash")


def phase_3_patch_targets(_kernel: Kernel) -> None:
    """Every `mod.attr = ...` in tests/ patches a name something resolves there.

    A monkeypatch is the one construct that fails *silently* when a module is
    split: the assignment always succeeds, so the test goes on passing while
    the real object stays in play. That is what happened when `Browser` moved
    out of `repld/browser/__init__.py` — phase 6 patched the re-export, the spy
    never saw a call, and the only reason it surfaced was that the real class
    then tried to open a socket.

    The rule is not "patch the module that defines it". `repld.gist_deps` does
    not read its own `scan_deps`, yet patching it works, because `gist_cmd` and
    `kernel` call `gist_deps.scan_deps(...)` — a lookup on the module object at
    call time. So a patch on `M.attr` reaches someone iff either M's own code
    reads a bare `attr`, or some module reads `<alias-of-M>.attr`. Anything
    else sets a name nobody ever resolves through.

    Discovered by sweep rather than listed, so a patch written later is covered
    without anyone remembering to add it here.
    """
    import ast
    import importlib

    src_trees = {
        f.resolve(): ast.parse(f.read_text())
        for f in sorted((REPO / "src" / "repld").rglob("*.py"))
    }

    def module_aliases(tree, modname: str) -> set[str]:
        """Local names in `tree` that refer to the module `modname`."""
        out, tail = set(), modname.split(".")[-1]
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == modname:
                        out.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == tail:
                        out.add(a.asname or a.name)
        return out

    def resolved_through(modname: str, attr: str) -> bool:
        own = Path(importlib.import_module(modname).__file__ or "").resolve()
        for n in ast.walk(src_trees[own]):
            if isinstance(n, ast.Name) and n.id == attr and isinstance(n.ctx, ast.Load):
                return True
        for tree in src_trees.values():
            aliases = module_aliases(tree, modname)
            if not aliases:
                continue
            for n in ast.walk(tree):
                if (
                    isinstance(n, ast.Attribute)
                    and n.attr == attr
                    and isinstance(n.value, ast.Name)
                    and n.value.id in aliases
                    and isinstance(n.ctx, ast.Load)
                ):
                    return True
        return False

    inert, checked = [], 0
    for f in sorted((REPO / "tests").rglob("*.py")):
        tree = ast.parse(f.read_text())
        # Local names bound to a repld module in this test file.
        mods: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.startswith("repld"):
                        mods[a.asname or a.name.split(".")[0]] = a.name
            elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("repld"):
                for a in n.names:
                    candidate = f"{n.module}.{a.name}"
                    try:
                        importlib.import_module(candidate)
                    except Exception:
                        continue  # a class or function, not a submodule
                    mods[a.asname or a.name] = candidate
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            for t in n.targets:
                if not (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)):
                    continue
                modname = mods.get(t.value.id)
                if modname is None:
                    continue  # an object attribute, not a module patch
                checked += 1
                if not resolved_through(modname, t.attr):
                    rel = f.relative_to(REPO)
                    inert.append(f"{rel}:{n.lineno} {modname}.{t.attr}")

    assert_eq(inert, [], "monkeypatches that nothing resolves through (inert)")
    assert_true(checked >= 8, f"the sweep found the patches (got {checked})")
    print(f"  ✓ {checked} module monkeypatch(es) all target a namespace in use")


def phase_3_task_spawns(_kernel: Kernel) -> None:
    """Every task the package spawns keeps its handle and carries a `repld-` name.

    Both rules are `bg.py`'s: asyncio holds a running task weakly, so a
    `create_task` whose result is dropped can vanish mid-flight, and
    `kernel._watch_block` cancels whatever holds the loop unless its name starts
    with `repld-`, so an unnamed internal task is a kill candidate. `bg.spawn`
    is the sanctioned spawn; a direct `create_task` is allowed where the handle
    is stored (`_FrontProbe`, the pill heartbeat) but must still be named.

    Swept rather than listed, like `phase_3_patch_targets`, so a spawn written
    later is covered without anyone remembering to add it here. A name must be
    a literal or an f-string whose literal prefix is `repld-`; a name computed
    elsewhere cannot be checked and is refused.
    """
    import ast

    SPAWNERS = {"create_task", "ensure_future", "spawn"}
    bad: list[str] = []
    checked = 0
    for f in sorted((REPO / "src" / "repld").rglob("*.py")):
        if f.name in ("bg.py", "injected_source.py"):
            continue
        tree = ast.parse(f.read_text())
        dropped = {
            id(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        }
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            attr = n.func.attr
            if attr not in SPAWNERS:
                continue
            is_bg = isinstance(n.func.value, ast.Name) and n.func.value.id == "bg"
            if attr == "spawn" and not is_bg:
                continue
            where = f"{f.relative_to(REPO)}:{n.lineno} {attr}"
            checked += 1
            name = next((k.value for k in n.keywords if k.arg == "name"), None)
            if isinstance(name, ast.JoinedStr) and name.values:
                name = name.values[0]
            if not (
                isinstance(name, ast.Constant)
                and isinstance(name.value, str)
                and name.value.startswith("repld-")
            ):
                bad.append(f"{where}: no literal `repld-` name")
            if not is_bg and id(n) in dropped:
                bad.append(f"{where}: handle dropped (use bg.spawn)")
    assert_true(not bad, "task spawns held and named:\n    " + "\n    ".join(bad))
    assert_true(checked >= 20, f"sweep saw only {checked} spawn sites")
    print(f"  ✓ {checked} task spawn(s) all held and `repld-` named")


def phase_3_own_group_signal(kernel: Kernel) -> None:
    """A cell cannot signal the kernel's own process group, with no project init.

    Signal 0 delivers nothing, so a missing guard fails the assertion instead
    of killing the kernel under test.
    """
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        for stmt in (
            "os.killpg(os.getpgrp(), 0)",
            "os.kill(0, 0)",
            "os.kill(-1, 0)",
            "os.kill(-os.getpgrp(), 0)",
        ):
            text = content_text(b.exec(f"import os\n{stmt}"))
            assert_true("OwnGroupSignal" in text, f"{stmt} refused (got {text!r})")
        text = content_text(b.exec("import os\nos.kill(os.getpid(), 0)\n'alive'"))
        assert_true("alive" in text, f"own pid still signalable (got {text!r})")
        print("  ✓ the kernel refuses signals at its own process group")
    finally:
        b.close()
