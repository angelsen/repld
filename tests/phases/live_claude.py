"""Phase 20: human gates under a real `claude -p` and `claude --bg`.

Phase 17 drives the gate wiring with a hand-written client. This phase puts the
real Claude Code binary in front of it for the two kinds that have no modal: a
one-shot `-p` run is gone before anyone answers, and a `--bg` worker takes its
pushes through an inbox socket. Both cost a few cents on haiku, so the phase
runs only when named (`--phases 20`) and skips cleanly with no `claude` on PATH.

The project dir sits under the repo's gitignored `.live.local/` because
`claude --bg` refuses a workspace it has not been told to trust and inherits
that trust from the parent directory.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from harness import REPO, Kernel, assert_eq, assert_true

from phases.gates import _gate_cli, _pending
from phases.tasks_cli import _tasks_cli

LIVE_ROOT = REPO / ".live.local"
GATE_WAIT_S = 90.0
DEFER_AFTER_S = 1.5  # past the prompts' `timeout 1`
CLAUDE_FLAGS = (
    "--model",
    "haiku",
    "--allowedTools",
    "mcp__repld__exec",
    "--strict-mcp-config",
    "--setting-sources",
    "project",
    "--permission-mode",
    "dontAsk",
)


def phase_20_live_claude(kernel: Kernel) -> None:
    """Run both live cases against one throwaway project kernel of their own."""
    if shutil.which("claude") is None:
        print("  - no claude binary on PATH, phase 20 skipped")
        return
    LIVE_ROOT.mkdir(exist_ok=True)
    proj = Path(tempfile.mkdtemp(prefix="p20-", dir=LIVE_ROOT))
    live = Kernel(proj)
    try:
        _write_mcp_config(proj)
        _print_mode_leaves_gate_open(live)
        _bg_worker_gets_inbox_push(live)
    finally:
        live.stop()
        shutil.rmtree(proj, ignore_errors=True)


def _write_mcp_config(proj: Path) -> None:
    """Point `claude` at this checkout's bridge, named `repld` so the tool is `mcp__repld__exec`."""
    server = {
        "command": "uv",
        "args": ["run", "--project", str(REPO), "repld", "bridge"],
    }
    (proj / "mcp.json").write_text(json.dumps({"mcpServers": {"repld": server}}))


def _claude(
    live: Kernel, flags: tuple[str, ...], prompt: str
) -> subprocess.CompletedProcess:
    """Run `claude` in the project dir; the prompt follows `--` because `--allowedTools` is variadic."""
    env = os.environ.copy()
    # A suite run from inside a `--bg` worker would make this bridge kind='bg'.
    env.pop("CLAUDE_JOB_DIR", None)
    return subprocess.run(
        ["claude", *flags, *CLAUDE_FLAGS, "--mcp-config", "mcp.json", "--", prompt],
        cwd=str(live.cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _tasks(live: Kernel) -> list[dict]:
    proc = _tasks_cli(live, "--json")
    assert_eq(proc.returncode, 0, f"repld tasks --json exits 0 (stderr: {proc.stderr})")
    return json.loads(proc.stdout)["tasks"]


def _wait_for_gate(live: Kernel, prompt: str) -> dict:
    """The one pending gate with `prompt`, once the agent's cell has opened it."""
    deadline = time.monotonic() + GATE_WAIT_S
    while time.monotonic() < deadline:
        found = [g for g in _pending(live) if g["prompt"] == prompt]
        if found:
            return found[0]
        time.sleep(0.5)
    raise AssertionError(f"gate {prompt!r} never opened")


def _answer_and_finish(live: Kernel, gate: dict, task_id: str) -> dict:
    """Answer `gate` yes from the CLI and return task `task_id` once it is done."""
    (task,) = [t for t in _tasks(live) if t["task_id"] == task_id]
    # Answered inside the agent's `timeout 1`, the cell returns inline and owes no push.
    time.sleep(max(0.0, task["started_at"] + DEFER_AFTER_S - time.time()))
    ok = _gate_cli(live, "answer", gate["gate_id"], "y")
    assert_eq(ok.returncode, 0, f"CLI answer lands (stderr: {ok.stderr})")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        done = [t for t in _tasks(live) if t["task_id"] == task_id and t["done"]]
        if done:
            return done[0]
        time.sleep(0.2)
    raise AssertionError("the cell never finished after the CLI answer")


def _wait_push_settled(live: Kernel, task_id: str) -> dict:
    """Task `task_id` once its completion push has an outcome; the task is `done` slightly before."""
    deadline = time.monotonic() + 15.0
    task: dict = {}
    while time.monotonic() < deadline:
        (task,) = [t for t in _tasks(live) if t["task_id"] == task_id]
        if task["push_delivered"] is not None:
            return task
        time.sleep(0.2)
    raise AssertionError(f"the completion push never settled (last: {task})")


def _print_mode_leaves_gate_open(live: Kernel) -> None:
    """A `claude -p` run exits with its gate still pending, and the CLI can still answer it."""
    proc = _claude(
        live,
        ("-p", "--no-session-persistence"),
        "Call the repld exec tool once with code: print(await confirm('Live p?')) "
        "and timeout 1. It will defer. Then stop; do not poll or answer it.",
    )
    assert_eq(proc.returncode, 0, f"claude -p exits 0 (stderr: {proc.stderr[-300:]})")
    gate = _wait_for_gate(live, "Live p?")
    assert_eq(
        gate["schema"]["properties"]["answer"]["type"],
        "boolean",
        "gate carries its schema",
    )
    (task,) = _tasks(live)
    assert_eq(task["done"], False, "the cell outlives the claude -p process")
    print("  ✓ claude -p exits with its confirm gate still pending")

    task = _answer_and_finish(live, gate, task["task_id"])
    assert_eq(task["exception"], None, "the cell finished cleanly")
    assert_eq(task["origin_kind"], None, "a -p run is not a bg worker")
    assert_eq(
        task["push_state"], "queued", "the completion push waits for its gone session"
    )
    print("  ✓ the CLI answers it, and the push for the departed session is queued")


def _bg_worker_gets_inbox_push(live: Kernel) -> None:
    """A `claude --bg` worker is told of the gate by inbox push; the CLI answer reaches it."""
    for task in _tasks(live):
        assert_true(task["done"], "no cell left over from the -p case")
    proc = _claude(
        live,
        ("--bg",),
        "Call the repld exec tool once with code: print(await confirm('Live bg?')) "
        "and timeout 1. It will defer. Do not answer it. When the task_done push "
        "arrives, reply with exactly: DONE <printed value>.",
    )
    if "Workspace not trusted" in proc.stdout + proc.stderr:
        print("  - claude --bg refused the workspace as untrusted, case skipped")
        return
    started = re.search(r"backgrounded · (\w+)", proc.stdout)
    assert_true(
        started is not None, f"claude --bg printed a session id (got {proc.stdout!r})"
    )
    assert started is not None
    worker = started.group(1)
    try:
        gate = _wait_for_gate(live, "Live bg?")
        running = next(t for t in _tasks(live) if not t["done"])
        assert_eq(running["origin_kind"], "bg", "the cell is attributed to a bg worker")
        print("  ✓ claude --bg opens its gate as kind='bg'")

        done = _answer_and_finish(live, gate, running["task_id"])
        done = _wait_push_settled(live, done["task_id"])
        assert_eq(
            done["push_delivered"], True, "the completion push reached the worker"
        )
        assert_eq(
            done["push_state"], "written", "it went down the worker's inbox socket"
        )
        print(
            "  ✓ the CLI answer completes the cell and the worker's inbox gets the push"
        )
    finally:
        subprocess.run(
            ["claude", "stop", worker],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
