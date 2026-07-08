"""Phases 2–3: Core MCP plumbing — initialize, tools/list, sync exec, deferred exec, get_task."""

import time
from pathlib import Path

from harness import Bridge, Kernel, assert_eq, assert_true


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
        print("  ✓ initialize")

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
        resp = b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "print('hi from exec')"}},
        )
        content = resp["result"]["content"][0]["text"]
        meta = resp["result"]["_meta"]
        assert_true("hi from exec" in content, f"sync exec output (got {content!r})")
        assert_eq(meta["done"], True, "sync exec meta.done")
        print(f"  ✓ sync exec: {content.strip()!r}")

        # Nudge exec
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time\nfor i in range(3):\n    time.sleep(0.3); print(f'step {i}')",
                    "timeout": 0.2,
                },
            },
            timeout=3.0,
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
        print(f"  ✓ get_task: done, output {snap['text'].strip()!r}")

        # Spill test — every cell with output now spills; large output
        # additionally trips the truncated-preview path.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "print('x' * 70000)", "timeout": 5.0},
            },
            timeout=10.0,
        )
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
        resp = b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "'line one\\nline two'"}},
        )
        content = resp["result"]["content"][0]["text"]
        assert_true(
            "line one\nline two" in content and "\\n" not in content,
            f"multi-line str displayed verbatim (got {content!r})",
        )
        print(f"  ✓ multi-line str display: {content!r}")

        # no_display() suppresses the print but still returns/binds the value.
        resp = b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "no_display('quiet result')"}},
        )
        content = resp["result"]["content"][0]["text"]
        assert_true(
            "quiet result" not in content,
            f"no_display() suppressed output (got {content!r})",
        )
        resp = b.call("tools/call", {"name": "exec", "arguments": {"code": "_"}})
        content = resp["result"]["content"][0]["text"]
        assert_true(
            "quiet result" in content,
            f"no_display() still bound to _ (got {content!r})",
        )
        print("  ✓ no_display(): suppressed on display, bound to _")
    finally:
        b.close()
