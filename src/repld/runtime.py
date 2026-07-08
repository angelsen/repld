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
#   ("eval", code)             — pure expression
#   ("exec_eval", head, tail)  — exec head, then eval+display tail
#   ("exec", code)             — pure exec, no display
CompiledCell = tuple


class _NoDisplay:
    """Marker wrapper: suppress auto-display of an otherwise-displayable
    result while still returning the real value for programmatic use and
    `_`/`_N` history binding. Constructed via `no_display()`, unwrapped in
    `run_cell()` — never meant to be instantiated or inspected elsewhere.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def no_display(value: Any) -> Any:
    """Wrap `value` so the cell-display hook won't print it when this call
    is a cell's bare last expression, while still returning it (and binding
    `_`/`_N`) for programmatic composition. Injected into `__main__` and
    the `repld` module — call as `no_display(x)` or `repld.no_display(x)`.
    """
    return _NoDisplay(value)


def compile_cell(src: str, task_id: str) -> CompiledCell:
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
        return ("exec_eval", head_code, tail_code)
    code = compile(tree, fname, "exec", flags=flags)
    return ("exec", code)


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


async def run_cell(compiled: CompiledCell, ns: dict, n: int) -> Any:
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
            _, head_code, tail_code = compiled
            await _eval(head_code, ns)
            result = await _eval(tail_code, ns)
        else:  # "exec"
            _, code = compiled
            await _eval(code, ns)
        if tag in ("eval", "exec_eval") and result is not None:
            quiet = isinstance(result, _NoDisplay)
            if quiet:
                result = result.value
            if not quiet and result is not None:
                if isinstance(result, str) and "\n" in result:
                    print(result)
                else:
                    print(repr(result))
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
        gc.collect()  # flush unawaited-coroutine warnings to this cell's output


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
                "\nHint: this cell ran in a background thread "
                "(no 'await' detected). Use defer() to schedule async work:\n"
                "  defer(some_coroutine())\n"
            )
    return formatted
