"""Compile + eval for user cells.

Supports top-level await, last-expression display (whether the cell is one
expression or has a trailing expression after statements), and returns the
last value so the kernel can bind it to `_` / `_N`.
"""

from __future__ import annotations

import ast
import inspect
import sys
import traceback
from typing import Any

# Tagged compile result. One of:
#   ("eval", code)                 — pure expression
#   ("exec_eval", head, tail)      — exec head, then eval+display tail
#   ("exec", code)                 — pure exec, no display
CompiledCell = tuple


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


async def _maybe_await(result: Any) -> Any:
    if inspect.iscoroutine(result):
        return await result
    return result


async def run_cell(compiled: CompiledCell, ns: dict, n: int) -> Any:
    """Execute a compiled cell. Returns the last-expression value (or None).

    On success, binds `_` and `_{n}` in `ns` to the returned result (when
    not None). Coroutines from PyCF_ALLOW_TOP_LEVEL_AWAIT are awaited.
    Exceptions are formatted to stderr and re-raised so the caller can
    record CellDone.error.
    """
    try:
        tag = compiled[0]
        result: Any = None
        if tag == "eval":
            _, code = compiled
            result = await _maybe_await(eval(code, ns))  # noqa: S307
        elif tag == "exec_eval":
            _, head_code, tail_code = compiled
            await _maybe_await(eval(head_code, ns))  # noqa: S307
            result = await _maybe_await(eval(tail_code, ns))  # noqa: S307
        else:  # "exec"
            _, code = compiled
            await _maybe_await(eval(code, ns))  # noqa: S307
        if tag in ("eval", "exec_eval") and result is not None:
            print(repr(result))
            ns["_"] = result
            ns[f"_{n}"] = result
        return result
    except BaseException:
        sys.stderr.write(traceback.format_exc())
        raise
