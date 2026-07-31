"""Display consumer — main-thread event renderer.

`run_display(stop)` blocks on the main thread, popping events from the queue
and rendering them to sys.__stdout__. A companion stdin reader thread handles
human-gate input.

Falls back to plain ANSI coloring when `rich` is not installed.
"""

import queue
import sys
import threading
from typing import TextIO, cast

from . import render, tasks
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
from .gates import parse_response, resolve_gate

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

# Shared with `repld log` — the two surfaces render the same events and are
# meant to look identical. Formats live in render.py; only the couple of
# strings unique to a live terminal are built here.
_DIM = render.DIM
_RED = render.RED
_RESET = render.RESET


# Pinned at import time. typeshed types these as Optional (None in GUI /
# no-console contexts); run_display() checks at entry, and a --no-display
# kernel never imports this module at all — so importing it must stay safe
# without a terminal.
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
# Gates still awaiting an answer, oldest first: gate_id → (kind, options).
# A dict rather than a single slot because `gates` has always supported any
# number at once, and since `repld gate` an answer can arrive out-of-band for
# any of them — the pane has to drop the one that was answered, not whichever
# it happened to be showing. Insertion-ordered, so stdin routes to the newest
# open gate (the one whose prompt is on screen) and falls back to the previous
# one when that is answered elsewhere.
_open_gates: "dict[str, tuple[str, list[str] | None]]" = {}

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
    if _HAS_RICH:
        from rich.syntax import Syntax

        console = _get_console()
        # Same header text as render.cell_header, wrapped in rich's markup
        # dialect instead of ANSI — console.print would emit raw escapes
        # literally. The text itself must keep coming from render.py.
        header = f"[dim]{render.cell_header_text(ev.task_id, ev.t)}[/dim]"
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
        _out(render.cell_header(ev.task_id, ev.t) + "\n")
        body = render.cell_source_block(ev.source)
        if body:
            _out(body + "\n")


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
    short_id = render.short_task(task_id)
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
        f"{prefix}{_DIM}… cell {render.short_task(task_id)} output elided "
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
    # If the last output didn't end with a newline, pad so the done marker
    # lands on its own line instead of gluing onto mid-line text.
    _, last_nl = _viewer_state.get(ev.task_id, (0, True))
    pad = "" if last_nl else "\n"
    line = render.cell_done_line(ev.task_id, ev.elapsed_ms, ev.error)
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
        _out(render.channel_block(ev.content, ev.meta) + "\n")


def _render_prompt_open(ev: HumanPromptOpen) -> None:
    _open_gates[ev.gate_id] = (ev.kind, ev.options)
    # No newline — the cursor stays on this line for the stdin reader.
    _out(render.prompt_open(ev.kind, ev.prompt, ev.options, ev.gate_id))


def _render_prompt_response(ev: HumanPromptResponse) -> None:
    # Drop the gate that was actually answered — the answer may have come from
    # `repld gate` or a browser pill for an *older* gate, in which case the one
    # on screen is still open and must stay answerable from stdin.
    _open_gates.pop(ev.gate_id, None)
    # Leading newline closes the line the prompt left the cursor on.
    _out("\n" + render.prompt_response(ev.value) + "\n")
    # Something else is still waiting: re-show it, since its prompt has now
    # scrolled above the response line and the cursor is no longer parked on it.
    if _open_gates:
        gate_id, (kind, options) = next(reversed(_open_gates.items()))
        _out(render.prompt_open(kind, "still waiting", options, gate_id))


def _render_browser_attached(ev: BrowserTabAttached) -> None:
    _out(render.browser_attached(ev.target, ev.url, ev.title) + "\n")


def _render_browser_detached(ev: BrowserTabDetached) -> None:
    _out(render.browser_detached(ev.target) + "\n")


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
    """Read lines from stdin and route them to the active gate.

    Coercion lives in `gates.parse_response`, shared with `repld gate answer`
    so a pane and the CLI can't disagree about what "y" or "2" means. On a
    rejected line we re-prompt and leave the gate open — the human is right
    here and can try again.
    """
    while not stop.is_set():
        try:
            line = _STDIN.readline()
        except (OSError, EOFError):
            break
        if not line:
            break
        line = line.rstrip("\n")
        # Newest open gate — the one whose prompt the cursor is parked on.
        if not _open_gates:
            continue
        gate_id, (kind, options) = next(reversed(_open_gates.items()))
        try:
            value = parse_response(kind, line, options)
        except ValueError as exc:
            _out(f"{_DIM}{exc}: {_RESET}")
            continue
        resolve_gate(gate_id, value)


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
