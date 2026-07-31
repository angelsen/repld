"""The shared event vocabulary — how a cell, a channel push, or a browser
attach *looks*, wherever it is shown.

Two surfaces render the same events and are expected to agree: the live TUI
(`display.py`, straight off the event queue) and `repld log` (`log_cmd.py`,
replayed from the NDJSON event log). They necessarily differ in mechanism —
one holds mutable state (foreground task, per-cell byte cap, rich panels) and
writes incrementally, the other maps one JSON record to one line — so what is
shared here is only the formatting: pure functions, no state, no I/O.

They had already drifted three ways before this module existed (a differently
worded gate response, a dropped tab title, a colour left switched on), which
is the whole argument for keeping the strings in one place.

`display.py` still owns `VIEWER_MAX_BYTES`: that is a viewer policy, not a
format.
"""

import time

DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Task ids are uuid4().hex[:12]; 8 is enough to eyeball. Chrome target ids are
# `{port}:{6-hex}`, which is already short — 12 is a guard, not a squeeze.
# TASK_ID_CHARS is public because the TUI labels out-of-band output with the
# same abbreviation outside any of the formatters below.
TASK_ID_CHARS = 8
_TARGET_ID_CHARS = 12


def clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def short_task(task_id: str) -> str:
    return task_id[:TASK_ID_CHARS]


def cell_header_text(task_id: str, t: float) -> str:
    """The header without styling — for renderers that apply their own.

    `display.py`'s rich path needs the same text in rich's markup dialect,
    since console.print would emit this module's raw escapes literally.
    """
    return f"── cell {short_task(task_id)} · {clock(t)} ──"


def cell_header(task_id: str, t: float) -> str:
    return f"{DIM}{cell_header_text(task_id, t)}{RESET}"


def cell_source_block(source: str) -> str:
    """The cell's source, indented. Empty string when there is nothing to show.

    Plain-text form only — the TUI substitutes a rich `Syntax` render when
    rich is installed.
    """
    return "\n".join(f"  {line}" for line in source.rstrip().splitlines())


def cell_done_line(task_id: str, elapsed_ms: float, error: str | None) -> str:
    short = short_task(task_id)
    ms = f"{elapsed_ms:.0f}ms"
    if error:
        return f"{RED}✗{RESET} {DIM}{short} · err({error}) · {ms}{RESET}"
    return f"{GREEN}✓{RESET} {DIM}{short} · done · {ms}{RESET}"


def channel_block(content: str, meta: dict, *, t: float | None = None) -> str:
    """The bordered channel-push block.

    `t` is passed by the log replay, where a timestamp is the only way to place
    the push in time; the live TUI prints it as it happens and omits it.

    Only the border glyphs are coloured. Colouring the body too would tint
    whatever the push carries — usually a task's own output — a colour it never
    chose.
    """
    stamp = f" · {clock(t)} " if t is not None else " "
    lines = [f"{CYAN}┌─ channel{stamp}─{RESET}"]
    lines += [f"{CYAN}│{RESET} {line}" for line in content.rstrip().splitlines()]
    joined = "  ".join(f"{k}={v}" for k, v in meta.items())
    if joined:
        lines.append(f"{CYAN}│{RESET} {DIM}{joined}{RESET}")
    lines.append(f"{CYAN}└───────────{RESET}")
    return "\n".join(lines)


def gate_hint(kind: str, options: list[str] | None) -> str:
    """What a gate of *kind* accepts, unstyled — `[y/n]` or `[1=alpha, 2=beta]`.

    Empty for an `ask`, which takes free text. Shared with `repld gate`'s
    listing: that is a human deciding what to type, so it has to agree with the
    pane and with `repld log -f` about which numbers are on offer — the same
    reason every other event format lives in this module.
    """
    if kind == "confirm":
        return "[y/n]"
    if kind == "choose" and options:
        return "[" + ", ".join(f"{i + 1}={o}" for i, o in enumerate(options)) + "]"
    return ""


def prompt_open(
    kind: str, prompt: str, options: list[str] | None, gate_id: str = ""
) -> str:
    """The human-gate question. No trailing newline: the TUI leaves the cursor
    on this line waiting for stdin, so the `: ` is part of the string.

    The id is carried because `repld log -f` is where someone watching a
    headless kernel sees the gate open, and it is what `repld gate answer`
    takes. Harmless in the pane, where you just type the answer.
    """
    out = f"{BOLD}{CYAN}? {prompt}{RESET}"
    hint = gate_hint(kind, options)
    if hint:
        out += f" {DIM}{hint}{RESET}"
    if gate_id:
        out += f" {DIM}#{gate_id}{RESET}"
    return out + ": "


def prompt_response(value: object) -> str:
    return f"{DIM}↳ response recorded: {value}{RESET}"


def browser_attached(target: str, url: str, title: str = "") -> str:
    suffix = f" ({title})" if title else ""
    return f"{DIM}[browser] attached {target[:_TARGET_ID_CHARS]} {url}{suffix}{RESET}"


def browser_detached(target: str) -> str:
    return f"{DIM}[browser] detached {target[:_TARGET_ID_CHARS]}{RESET}"
