"""Compile + eval for user cells.

Supports top-level await, last-expression display (whether the cell is one
expression or has a trailing expression after statements), and returns the
last value so the kernel can bind it to `_` / `_N`.
"""

import ast
import asyncio
import gc
import inspect
import sys
import traceback
from typing import Any

# Tagged compile result. One of:
#   ("eval", code)                        — pure expression
#   ("exec_eval", head, tail, names)      — exec head, then eval+display tail
#   ("exec", code, names)                 — pure exec, no display
# `names` is the set of top-level simple-assignment targets (`x = ...`,
# `x: T = ...`, chained `x = y = ...`), which is what lets a `_NoDisplay` a
# callee assigned directly be unwrapped: assignment doesn't go through the
# display path where unwrapping normally happens.


class _NoDisplay:
    """Marker wrapper: suppress auto-display of an otherwise-displayable
    result while still returning the real value for programmatic use,
    direct assignment, and `_`/`_N` history binding. Constructed via
    `no_display()`, unwrapped in `run_cell()` — never meant to be
    instantiated or inspected elsewhere.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def no_display(value: Any) -> Any:
    """Wrap `value` so the cell-display hook won't print it — whether the
    call is a cell's bare last expression or the right-hand side of a
    top-level assignment (`x = await foo()`) — while still returning the
    real value for programmatic use and `_`/`_N` history binding. Injected
    into `__main__` and the `repld` module — call as `no_display(x)` or
    `repld.no_display(x)`.
    """
    return _NoDisplay(value)


def unwrap_display(result: Any) -> tuple[Any, bool]:
    """Unwrap a `no_display()` wrapper, if `result` is one. Returns (value, quiet).

    The one sanctioned way to inspect `_NoDisplay` from outside this module —
    `defer()`'s deferred tasks have the same suppress-print-but-not-recovery
    need as a cell's trailing expression, with no cell number to route
    through `run_cell` for.
    """
    if isinstance(result, _NoDisplay):
        return result.value, True
    return result, False


def print_display(result: Any) -> None:
    """Print `result` the way a cell's auto-displayed trailing expression does."""
    if isinstance(result, str) and "\n" in result:
        print(result)
    else:
        print(repr(result))


def _assign_target_names(stmts: list) -> set:
    """Collect top-level simple-assignment target names from a statement list.

    Covers `x = expr`, chained `x = y = expr`, and annotated `x: T = expr`.
    Deliberately does not cover tuple/list/starred unpacking, attribute or
    subscript targets, or walrus (`:=`) expressions nested inside larger
    expressions — those are rarer in single-cell REPL usage and the
    _NoDisplay unwrap isn't reachable there without a full-tree walk.
    """
    names = set()
    for stmt in stmts:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def compile_cell(src: str, task_id: str) -> tuple:
    flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    fname = f"<repld:{task_id}>"
    # Try eval-mode first — handles single-expression cells like `1 + 1`.
    try:
        code = compile(src, fname, "eval", flags=flags)
        return ("eval", code)
    except SyntaxError:
        pass
    # Multi-statement: parse as exec, see if the last node is an expression
    # we can split out for display.
    tree = ast.parse(src, filename=fname, mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        head_tree = ast.Module(body=tree.body[:-1], type_ignores=[])
        tail_tree = ast.Expression(body=tree.body[-1].value)
        head_code = compile(head_tree, fname, "exec", flags=flags)
        tail_code = compile(tail_tree, fname, "eval", flags=flags)
        return ("exec_eval", head_code, tail_code, _assign_target_names(head_tree.body))
    code = compile(tree, fname, "exec", flags=flags)
    return ("exec", code, _assign_target_names(tree.body))


_CO_COROUTINE = inspect.CO_COROUTINE


async def _eval(code, ns: dict) -> Any:
    """Eval a code object. Threads sync code to keep the event loop responsive.

    Cells compiled with PyCF_ALLOW_TOP_LEVEL_AWAIT that contain ``await``
    have the CO_COROUTINE flag set — those must run on the event loop.
    Pure-sync cells run in a thread via ``asyncio.to_thread`` so they
    don't block the loop (e.g. sync HTTP, time.sleep, heavy computation).
    """
    if code.co_flags & _CO_COROUTINE:
        return await eval(code, ns)
    raw = await asyncio.to_thread(eval, code, ns)
    if inspect.iscoroutine(raw):
        return await raw
    return raw


def _unwrap_assigned(ns: dict, names: set) -> None:
    """Unwrap `_NoDisplay` off any of `names` that got bound in `ns`.

    Assignment (`x = await foo()`) never goes through the display path —
    only a cell's bare last expression does — so without this, a
    no_display()-returning callee would leak the internal wrapper object
    to `x` instead of the real value.

    Runs once, after every statement preceding the cell's own trailing
    expression (if any) has executed — not incrementally after each
    assignment. A statement *within the same cell* that reads `x` before
    that point still sees the wrapper; reading `x` in a later cell, or in
    the current cell's own trailing expression, sees the real value. In
    practice this only matters for the rare "assign, use it mid-cell, then
    do something unrelated" shape — the common "assign then use" and
    "assign in one cell, use in the next" patterns are both covered.
    """
    for name in names:
        val = ns.get(name)
        if isinstance(val, _NoDisplay):
            ns[name] = val.value


async def run_cell(compiled: tuple, ns: dict, n: int) -> Any:
    """Execute a compiled cell. Returns the last-expression value (or None).

    On success, binds `_` and `_{n}` in `ns` to the returned result (when
    not None). Coroutines from PyCF_ALLOW_TOP_LEVEL_AWAIT are awaited.
    Sync cells run in a background thread to keep the event loop responsive.
    Exceptions are formatted to stderr and re-raised so the caller can
    record CellDone.error.
    """
    try:
        tag = compiled[0]
        result: Any = None
        if tag == "eval":
            _, code = compiled
            result = await _eval(code, ns)
        elif tag == "exec_eval":
            _, head_code, tail_code, names = compiled
            await _eval(head_code, ns)
            _unwrap_assigned(ns, names)
            result = await _eval(tail_code, ns)
        else:  # "exec"
            _, code, names = compiled
            await _eval(code, ns)
            _unwrap_assigned(ns, names)
        if tag in ("eval", "exec_eval") and result is not None:
            result, quiet = unwrap_display(result)
            if not quiet:
                print_display(result)
            # Shift history: _ → __, __ → ___. Matches IPython convention.
            ns["___"] = ns.get("__")
            ns["__"] = ns.get("_")
            ns["_"] = result
            ns[f"_{n}"] = result
        return result
    except asyncio.CancelledError:
        # Expected control flow via the cancel tool — don't traceback-spam.
        raise
    except BaseException as exc:
        sys.stderr.write(_format_user_traceback(exc))
        raise
    finally:
        # Gen-0 only. The point is to flush "coroutine was never awaited"
        # warnings into *this* cell's output, and an abandoned coroutine is by
        # definition young — the non-cyclic case never needed a collection at
        # all (refcount drops at cell exit and __del__ fires), and the cyclic
        # one is a traceback holding the frame that made it, allocated moments
        # ago. A full collect() walks the whole heap for the same result: 164ms
        # on an 800k-object heap, per cell, in a kernel whose entire premise is
        # holding large state for weeks. Gen-0 is ~0.005ms.
        gc.collect(0)


def _format_user_traceback(exc: BaseException) -> str:
    """Format a traceback with repld-internal frames trimmed off the top.

    Walks the tb until the first frame whose filename is `<repld:...>`
    (user cell code) and formats from there. Falls back to the full
    traceback if no user frame is present.
    """
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename.startswith("<repld:"):
            break
        tb = tb.tb_next
    if tb is None:
        formatted = traceback.format_exc()
    else:
        formatted = "".join(traceback.format_exception(type(exc), exc, tb))
    if isinstance(exc, NameError) and exc.name:
        from . import gists

        hint = gists.hint_for_name(exc.name)
        if hint:
            formatted += f"\nHint: {hint}\n"
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if "cannot be called from a running event loop" in msg:
            formatted += (
                "\nHint: repld already runs an event loop. "
                "Use 'await' directly:\n"
                "  result = await some_async_fn()\n"
            )
        elif "no current event loop" in msg or "no running event loop" in msg:
            formatted += (
                "\nHint: this cell ran in a background thread (no top-level "
                "'await' in the cell), so asyncio.gather()/create_task()/"
                "ensure_future() had no loop to attach to — they reach for "
                "one at construction, not just to await it. Build it inside "
                "an async def instead:\n"
                "  async def _run():\n"
                "      return await asyncio.gather(a(), b())\n"
                "  await _run()\n"
                "or, if deferring it, pass defer() a zero-arg callable so it "
                "builds on the kernel's loop rather than in this thread:\n"
                "  defer(lambda: asyncio.gather(a(), b()))\n"
            )
    return formatted
