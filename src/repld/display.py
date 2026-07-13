"""Display consumer — main-thread event renderer.

`run_display(stop)` blocks on the main thread, popping events from the queue
and rendering them to sys.__stdout__. A companion stdin reader thread handles
human-gate input.

Falls back to plain ANSI coloring when `rich` is not installed.
"""

import queue
import sys
import threading
import time
from typing import TextIO, cast

from . import tasks
from .events import (
    BrowserTabAttached,
    BrowserTabDetached,
    CellDone,
    CellStart,
    ChannelPush,
    Event,
    HumanPromptOpen,
    HumanPromptResponse,
    StderrChunk,
    StdoutChunk,
    get_queue,
)
from .gates import resolve_gate

# ---------------------------------------------------------------------------
# Optional rich
# ---------------------------------------------------------------------------

try:
    from rich.console import Console as _RichConsole

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

# ---------------------------------------------------------------------------
# ANSI helpers (always available)
# ---------------------------------------------------------------------------

_DIM = "\033[2m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


# Pinned at import time. typeshed types these as Optional (None in GUI /
# no-console contexts); run_display() checks at entry, and --no-display
# (make_drainer) never touches them — so importing this module must stay
# safe without a terminal.
_STDOUT = cast(TextIO, sys.__stdout__)
_STDIN = cast(TextIO, sys.__stdin__)


def _out(text: str) -> None:
    _STDOUT.write(text)
    _STDOUT.flush()


# ---------------------------------------------------------------------------
# Renderer state
# ---------------------------------------------------------------------------

# task_id of the "foreground" cell (last started, not yet done).
# Output from other tasks is prefixed.
_foreground_task_id: str | None = None
# gate_id currently awaiting a stdin response (or None).
_awaiting_gate: str | None = None
_awaiting_gate_kind: str | None = None

# Per-cell viewer cap. Pure bytes — single source of truth, no chunk-boundary
# edge cases. ~4KB ≈ 50 short lines or ~20 wide ones. Full content is on disk
# via the spill file; Read/Grep on that path is the escape hatch.
# Independent of tasks.PREVIEW_MAX_BYTES (wire budget) — same value by
# coincidence, not by contract.
VIEWER_MAX_BYTES = 4 * 1024
# Per-task (bytes_written, last_char_was_newline).
_viewer_state: dict[str, tuple[int, bool]] = {}
_truncated_tasks: set[str] = set()

# rich Console (lazily created)
_console: "_RichConsole | None" = None


def _get_console() -> "_RichConsole":
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console(file=_STDOUT, highlight=False)
    return _console


# ---------------------------------------------------------------------------
# Individual event renderers
# ---------------------------------------------------------------------------


def _render_cell_start(ev: CellStart) -> None:
    global _foreground_task_id
    _foreground_task_id = ev.task_id
    short_id = ev.task_id[:8]
    ts = time.strftime("%H:%M:%S", time.localtime(ev.t))
    if _HAS_RICH:
        from rich.syntax import Syntax

        console = _get_console()
        header = f"[dim]── cell {short_id} · {ts} ──[/dim]"
        console.print(header, markup=True)
        src = ev.source.rstrip()
        if src:
            console.print(
                Syntax(
                    src,
                    "python",
                    theme="monokai",
                    line_numbers=False,
                    background_color="default",
                )
            )
    else:
        _out(f"{_DIM}── cell {short_id} · {ts} ──{_RESET}\n")
        for line in ev.source.rstrip().splitlines():
            _out(f"  {line}\n")


def _write_styled(
    text: str, task_id: str | None, *, is_stderr: bool, line_continues: bool = False
) -> None:
    """Write `text` to the terminal with the right prefix/color.

    `line_continues=True` means the previous emit for this task didn't end
    with a newline, so the first line of this chunk should NOT get a fresh
    prefix (it's continuing an existing line). Subsequent lines after a
    newline within this chunk still get the prefix.
    """
    if not text:
        return
    if task_id is None or task_id == _foreground_task_id:
        if is_stderr:
            _out(f"{_RED}{text}{_RESET}")
        else:
            _out(text)
        return
    short_id = task_id[:8]
    color = _RED if is_stderr else ""
    prefix = f"{_DIM}[{short_id}]{_RESET}{color} "
    parts = []
    for i, line in enumerate(text.splitlines(keepends=True)):
        if i == 0 and line_continues:
            parts.append(f"{color}{line}")
        else:
            parts.append(prefix + line)
    if is_stderr:
        parts.append(_RESET)
    _out("".join(parts))


def _emit_elision_notice(task_id: str, last_was_nl: bool) -> None:
    path = (tasks.get(task_id) or {}).get("spill_path") or f"task={task_id}"
    prefix = "" if last_was_nl else "\n"
    _out(
        f"{prefix}{_DIM}… cell {task_id[:8]} output elided "
        f"({VIEWER_MAX_BYTES // 1024}KB cap); full: {path}{_RESET}\n"
    )


def _render_chunk(text: str, task_id: str | None, *, is_stderr: bool) -> None:
    if not text:
        return
    if task_id is None:
        # Unattributed (startup banner, module-level) — no cap.
        _write_styled(text, None, is_stderr=is_stderr)
        return
    if task_id in _truncated_tasks:
        return
    cur_bytes, last_nl = _viewer_state.get(task_id, (0, True))
    remaining = VIEWER_MAX_BYTES - cur_bytes
    if remaining <= 0:
        # Already at cap before this chunk arrived (the previous chunk hit it
        # exactly). Mark + notice, drop this chunk.
        _truncated_tasks.add(task_id)
        _emit_elision_notice(task_id, last_nl)
        return
    if len(text) <= remaining:
        _write_styled(text, task_id, is_stderr=is_stderr, line_continues=not last_nl)
        _viewer_state[task_id] = (cur_bytes + len(text), text.endswith("\n"))
        return
    head = text[:remaining]
    if head:
        _write_styled(head, task_id, is_stderr=is_stderr, line_continues=not last_nl)
    _viewer_state[task_id] = (VIEWER_MAX_BYTES, head.endswith("\n"))
    _truncated_tasks.add(task_id)
    _emit_elision_notice(task_id, head.endswith("\n"))


def _render_stdout(ev: StdoutChunk) -> None:
    _render_chunk(ev.text, ev.task_id, is_stderr=False)


def _render_stderr(ev: StderrChunk) -> None:
    _render_chunk(ev.text, ev.task_id, is_stderr=True)


def _render_cell_done(ev: CellDone) -> None:
    global _foreground_task_id
    short_id = ev.task_id[:8]
    ms = f"{ev.elapsed_ms:.0f}ms"
    # If the last output didn't end with a newline, pad so the done marker
    # lands on its own line instead of gluing onto mid-line text.
    _, last_nl = _viewer_state.get(ev.task_id, (0, True))
    pad = "" if last_nl else "\n"
    if ev.error:
        marker = f"{_RED}✗{_RESET}"
        line = f"{marker} {_DIM}{short_id} · err({ev.error}) · {ms}{_RESET}"
    else:
        marker = f"{_GREEN}✓{_RESET}"
        line = f"{marker} {_DIM}{short_id} · done · {ms}{_RESET}"
    _out(pad + line + "\n")
    if ev.task_id == _foreground_task_id:
        _foreground_task_id = None
    _viewer_state.pop(ev.task_id, None)
    _truncated_tasks.discard(ev.task_id)


def _render_channel_push(ev: ChannelPush) -> None:
    if _HAS_RICH:
        from rich.panel import Panel
        from rich.text import Text

        console = _get_console()
        # Use Text() so `[repld]`-style prefixes in content don't get eaten
        # by rich markup parsing.
        body = Text(ev.content.rstrip())
        meta_line = "  ".join(f"{k}={v}" for k, v in ev.meta.items())
        if meta_line:
            body.append("\n")
            body.append(meta_line, style="dim")
        console.print(Panel(body, title="[cyan]channel[/cyan]", border_style="cyan"))
    else:
        _out(f"{_CYAN}┌─ channel ─\n")
        for line in ev.content.rstrip().splitlines():
            _out(f"│ {line}\n")
        meta_line = "  ".join(f"{k}={v}" for k, v in ev.meta.items())
        if meta_line:
            _out(f"│ {_DIM}{meta_line}{_RESET}\n")
        _out(f"{_CYAN}└───────────{_RESET}\n")


def _render_prompt_open(ev: HumanPromptOpen) -> None:
    global _awaiting_gate, _awaiting_gate_kind
    _awaiting_gate = ev.gate_id
    _awaiting_gate_kind = ev.kind
    _out(f"{_BOLD}{_CYAN}? {ev.prompt}{_RESET}")
    if ev.kind == "confirm":
        _out(f" {_DIM}[y/n]{_RESET}")
    elif ev.kind == "choose" and ev.options:
        opts = ", ".join(f"{i + 1}={o}" for i, o in enumerate(ev.options))
        _out(f" {_DIM}[{opts}]{_RESET}")
    _out(": ")


def _render_prompt_response(ev: HumanPromptResponse) -> None:
    global _awaiting_gate, _awaiting_gate_kind
    _awaiting_gate = None
    _awaiting_gate_kind = None
    _out(f"\n{_DIM}↳ response recorded: {ev.value}{_RESET}\n")


def _render_browser_attached(ev: BrowserTabAttached) -> None:
    short = ev.target[:12]
    title = f" ({ev.title})" if ev.title else ""
    _out(f"{_DIM}[browser] attached {short} {ev.url}{title}{_RESET}\n")


def _render_browser_detached(ev: BrowserTabDetached) -> None:
    short = ev.target[:12]
    _out(f"{_DIM}[browser] detached {short}{_RESET}\n")


def _render(ev: Event) -> None:
    if isinstance(ev, CellStart):
        _render_cell_start(ev)
    elif isinstance(ev, StdoutChunk):
        _render_stdout(ev)
    elif isinstance(ev, StderrChunk):
        _render_stderr(ev)
    elif isinstance(ev, CellDone):
        _render_cell_done(ev)
    elif isinstance(ev, ChannelPush):
        _render_channel_push(ev)
    elif isinstance(ev, HumanPromptOpen):
        _render_prompt_open(ev)
    elif isinstance(ev, HumanPromptResponse):
        _render_prompt_response(ev)
    elif isinstance(ev, BrowserTabAttached):
        _render_browser_attached(ev)
    elif isinstance(ev, BrowserTabDetached):
        _render_browser_detached(ev)


# ---------------------------------------------------------------------------
# Stdin reader
# ---------------------------------------------------------------------------


def _stdin_reader_loop(stop: threading.Event) -> None:
    """Read lines from stdin and route them to the active gate."""
    while not stop.is_set():
        try:
            line = _STDIN.readline()
        except (OSError, EOFError):
            break
        if not line:
            break
        line = line.rstrip("\n")
        gate_id = _awaiting_gate
        kind = _awaiting_gate_kind
        if gate_id is None:
            continue
        # Parse based on gate kind
        if kind == "confirm":
            if line.lower() in ("y", "yes", "1", "true"):
                resolve_gate(gate_id, True)
            elif line.lower() in ("n", "no", "0", "false"):
                resolve_gate(gate_id, False)
            else:
                _out(f"{_DIM}Type y or n: {_RESET}")
        elif kind == "choose":
            resolve_gate(gate_id, line.strip())
        else:
            # ask
            resolve_gate(gate_id, line)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_display(stop: threading.Event) -> None:
    """Main-thread display loop. Blocks until stop is set.

    Pops events from the event queue and renders them. Also drives a
    companion stdin-reader thread for human gates.
    """
    if sys.__stdout__ is None or sys.__stdin__ is None:
        raise RuntimeError(
            "display mode requires a terminal (stdio missing) — use --no-display"
        )
    q = get_queue()

    stdin_thread = threading.Thread(
        target=_stdin_reader_loop,
        args=(stop,),
        daemon=True,
        name="repld-stdin",
    )
    stdin_thread.start()

    while not stop.is_set():
        try:
            ev = q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _render(ev)
        except Exception as exc:
            # Never crash the display loop on a render error.
            _out(f"{_RED}[repld] display render error: {exc}{_RESET}\n")

    # Drain remaining events after stop.
    while True:
        try:
            ev = q.get_nowait()
            try:
                _render(ev)
            except Exception:
                pass
        except queue.Empty:
            break


def make_drainer(stop: threading.Event) -> threading.Thread:
    """In --no-display mode, drain the queue so memory doesn't grow.

    Returns a daemon thread (already started) that simply discards events
    until `stop` is set.
    """
    q = get_queue()

    def _drain():
        while not stop.is_set():
            try:
                q.get(timeout=0.5)
            except queue.Empty:
                continue
        # Final drain
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    t = threading.Thread(target=_drain, daemon=True, name="repld-drainer")
    t.start()
    return t
