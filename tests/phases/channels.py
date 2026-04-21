"""Phase 4: Channel notifications — task_done push, notify() from user code, pre-gate queuing."""

import time
from queue import Empty

from harness import Bridge, Kernel, assert_eq, assert_true


def phase_4(kernel: Kernel) -> None:
    """Nudged exec → channel notification arrives with kind=task_done.
    notify() from user code → channel notification with custom meta."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)
        print("  ✓ initialize + notifications/initialized")

        # Nudge-and-wait-for-channel
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time; time.sleep(0.5); print('slow done')",
                    "timeout": 0.1,
                },
            },
            timeout=3.0,
        )
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], False, "nudge meta.done")
        task_id = meta["task_id"]
        print(f"  ✓ nudged: task_id={task_id}")

        notif = b.wait_notification("notifications/claude/channel", timeout=5.0)
        params = notif["params"]
        nmeta = params["meta"]
        assert_eq(nmeta["kind"], "task_done", "channel meta.kind")
        assert_eq(nmeta["task_id"], task_id, "channel meta.task_id matches")
        assert_eq(nmeta["error"], "0", "channel meta.error=0 for success")
        assert_true(
            "slow done" in params["content"],
            f"channel content contains delta (got {params['content']!r})",
        )
        print(f"  ✓ channel task_done: {params['content'][:60]!r}...")

        # notify() from user code
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "notify('ping', kind='user', color='blue')"},
            },
            timeout=3.0,
        )
        assert_eq(resp["result"]["_meta"]["done"], True, "notify exec done sync")

        notif = b.wait_notification("notifications/claude/channel", timeout=3.0)
        params = notif["params"]
        assert_eq(params["content"], "ping", "notify content")
        assert_eq(params["meta"]["kind"], "user", "notify meta.kind")
        assert_eq(params["meta"]["color"], "blue", "notify meta.color")
        print(f"  ✓ notify(): content={params['content']!r} meta={params['meta']}")

        # Error in nudged exec → error="1" + traceback content
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time\ntime.sleep(0.3)\nraise RuntimeError('boom')",
                    "timeout": 0.1,
                },
            },
            timeout=3.0,
        )
        err_task_id = resp["result"]["_meta"]["task_id"]
        notif = b.wait_notification("notifications/claude/channel", timeout=5.0)
        nmeta = notif["params"]["meta"]
        assert_eq(nmeta["task_id"], err_task_id, "error channel task_id")
        assert_eq(nmeta["error"], "1", "channel error=1 on exception")
        print(
            f"  ✓ error case: error=1, content mentions RuntimeError={('RuntimeError' in notif['params']['content'])}"
        )
    finally:
        b.close()


def phase_4b_pregate(kernel: Kernel) -> None:
    """A channel push produced between initialize and notifications/initialized
    should be queued and arrive once the client completes the handshake."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        # Do NOT send notifications/initialized yet.
        # Trigger a channel push while the session is pre-init.
        # Use a nudged exec so the push is guaranteed.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time; time.sleep(0.3); notify('queued push')",
                    "timeout": 0.1,
                },
            },
            timeout=3.0,
        )
        task_id = resp["result"]["_meta"]["task_id"]
        # Wait for the task to finish. Push should now be queued, not delivered.
        time.sleep(0.6)
        # Confirm nothing arrived yet.
        try:
            msg = b.inbox.get(timeout=0.3)
            raise AssertionError(f"channel push arrived before init: {msg}")
        except Empty:
            pass
        # Now send initialized. Queued pushes should flush.
        b.send("notifications/initialized", {}, notif=True)
        # Expect two pushes: one from notify(), one from task_done.
        seen_contents = set()
        for _ in range(2):
            notif = b.wait_notification("notifications/claude/channel", timeout=3.0)
            seen_contents.add(notif["params"]["content"][:40])
        assert_true(
            any("queued push" in c for c in seen_contents),
            f"notify('queued push') delivered after init (got {seen_contents})",
        )
        assert_true(
            any(task_id in c for c in seen_contents),
            f"task_done for {task_id} delivered after init (got {seen_contents})",
        )
        print("  ✓ pre-init channel push queued & flushed on initialized")
    finally:
        b.close()
