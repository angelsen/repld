"""Callee side of kernel-to-kernel calls: resolve a dotted name, vet a call.

Everything here runs on a connection's reader thread and none of it touches the
asyncio loop, so imports and signature binding never stall the kernel. The
caller side is `peer.py`; the `peer/*` dispatch is `protocol.Dispatcher`.

Reach is deliberately that of `exec`: any dotted name in `__main__` or any
importable module. A peer call opens no door a same-uid process did not already
have through the kernel socket.
"""

from __future__ import annotations

import builtins
import importlib
import inspect
import os
import sys
import traceback
import types
from collections.abc import Callable

from . import gist_api, gists, ipc, sessions
from .core_schemas import (
    PEER_ERR_REFUSED,
    PEER_ERR_UNRESOLVED,
    PEER_MAX_DEPTH,
    PEER_PROTOCOL,
)

_INVALID_PARAMS = -32602
_MAIN_ENTRY_LIMIT = 500


class TargetError(Exception):
    """A call that never ran: carries the JSON-RPC code and optional data."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


class PayloadError(Exception):
    """A peer call's return value that cannot travel as JSON (unserialisable or
    over the size limit). Raised inside the task, so it reports as a ran-and-
    raised `result.error`, not a refusal."""


def target_of(params: dict) -> str:
    """`params["target"]`, which must be a string; anything else never runs."""
    target = params.get("target")
    if not isinstance(target, str):
        raise TargetError(_INVALID_PARAMS, "target must be a dotted-name string")
    return target


def parse_target(target: object) -> list[str]:
    """`"gist.fn"` -> ["gist", "fn"]; every segment must be an identifier."""
    if not isinstance(target, str) or not target:
        raise ValueError("target must be a non-empty dotted name")
    parts = target.split(".")
    for part in parts:
        if not part.isidentifier():
            raise ValueError(f"not a dotted name: {target!r}")
    return parts


def _unresolved(message: str, exc: BaseException | None = None) -> TargetError:
    data = {"traceback": traceback.format_exc()[-4000:]} if exc is not None else None
    return TargetError(PEER_ERR_UNRESOLVED, message, data)


def resolve_target(target: str) -> Callable:
    """The callable behind *target*.

    The first segment is a `__main__` name if one exists, else an import through
    `builtins.__import__` — `gists._GistImportHook` wraps that, which is what
    reloads an edited gist, and `importlib.import_module` would bypass it.
    Later segments are `getattr`, falling back to a submodule import when the
    object is a module.
    """
    try:
        parts = parse_target(target)
    except ValueError as exc:
        raise TargetError(_INVALID_PARAMS, str(exc)) from exc
    main_ns = sys.modules["__main__"].__dict__
    first = parts[0]
    if first in main_ns:
        obj: object = main_ns[first]
    else:
        try:
            obj = builtins.__import__(first)
        except Exception as exc:
            raise _unresolved(f"cannot resolve {first!r}: {exc!r}", exc) from exc
    walked = first
    for seg in parts[1:]:
        try:
            obj = getattr(obj, seg)
        except AttributeError:
            if not isinstance(obj, types.ModuleType):
                raise _unresolved(f"{walked!r} has no attribute {seg!r}") from None
            try:
                obj = importlib.import_module(f"{obj.__name__}.{seg}")
            except Exception as exc:
                raise _unresolved(
                    f"module {walked!r} has no attribute or submodule {seg!r}", exc
                ) from exc
        except Exception as exc:
            raise _unresolved(f"reading {walked}.{seg} raised {exc!r}", exc) from exc
        walked = f"{walked}.{seg}"
    if not callable(obj):
        raise _unresolved(f"not callable: {walked} is a {type(obj).__name__}")
    return obj


def check_chain(caller: dict | None, own_socket: str) -> str | None:
    """ "cycle" if this kernel is already in the call chain, "depth" if the chain
    is too long to extend, else None. A refused call never runs."""
    caller = caller or {}
    chain = [e for e in (caller.get("chain") or []) if isinstance(e, dict)]
    sockets = {str(e.get("socket")) for e in chain}
    own = (caller.get("kernel") or {}).get("socket")
    if own is not None:
        sockets.add(str(own))
    if own_socket in sockets:
        return "cycle"
    if len(chain) >= PEER_MAX_DEPTH:
        return "depth"
    return None


def refusal(reason: str, caller: dict | None) -> TargetError:
    chain = (caller or {}).get("chain") or []
    if reason == "cycle":
        msg = "cycle: this kernel is already in the call chain"
    else:
        msg = f"depth: call chain already {len(chain)} kernels long (limit {PEER_MAX_DEPTH})"
    return TargetError(PEER_ERR_REFUSED, msg, {"reason": reason, "chain": chain})


def bind_check(func: Callable, args: list, kwargs: dict) -> None:
    """Fail a call whose arguments can't bind before it is scheduled, so a typo
    reads as never-ran. Objects with no introspectable signature skip the check."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return
    try:
        sig.bind(*args, **kwargs)
    except TypeError as exc:
        raise TargetError(_INVALID_PARAMS, f"bad arguments: {exc}") from exc


def _kernel_info() -> dict:
    started_at = None
    for entry in sessions.list_sessions():
        if int(entry["pid"]) == os.getpid():
            started_at = entry.get("started_at")
    venv = sys.prefix if sys.prefix != sys.base_prefix else None
    return {
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "socket": str(ipc.socket_path()),
        "started_at": started_at,
        "python": sys.version.split()[0],
        "venv": venv,
    }


def _describe_main(name: str, obj: Callable) -> dict:
    try:
        signature = str(inspect.signature(obj))
    except (ValueError, TypeError):
        signature = "(...)"
    return {
        "name": name,
        "signature": signature,
        "async": inspect.iscoroutinefunction(obj),
        "doc": gist_api._first_line(inspect.getdoc(obj)),
    }


def describe_surface() -> dict:
    """The `peer/list` body: kernel identity, public gists with their AST
    signatures, and callables defined in `__main__`."""
    gist_rows = [
        {
            "name": p.stem,
            "doc": gist_api._extract_doc(p),
            "path": str(p),
            "functions": gist_api.functions_for_path(p),
        }
        for p in gists._iter_gist_files()
    ]
    main_ns = sys.modules["__main__"].__dict__
    main_rows: list[dict] = []
    # list() of a plain dict's items is one atomic snapshot under the GIL; a
    # cell on the loop may be assigning names while this reader thread walks.
    for name, val in list(main_ns.items()):
        if name.startswith("_") or isinstance(val, type) or not callable(val):
            continue
        if getattr(val, "__module__", None) != "__main__":
            continue
        main_rows.append(_describe_main(name, val))
        if len(main_rows) >= _MAIN_ENTRY_LIMIT:
            break
    return {
        "peer_protocol": PEER_PROTOCOL,
        "kernel": _kernel_info(),
        "gists": gist_rows,
        "main": main_rows,
    }


def inspect_target(target: str) -> dict:
    """The `peer/inspect` body: what the resolved live object looks like."""
    obj = resolve_target(target)
    try:
        signature: str | None = str(inspect.signature(obj))
    except (ValueError, TypeError):
        signature = None
    try:
        file = inspect.getsourcefile(obj)  # type: ignore[arg-type]
    except (TypeError, OSError):
        file = None
    if inspect.isclass(obj):
        kind = "class"
    elif inspect.isbuiltin(obj):
        kind = "builtin"
    elif inspect.isfunction(obj) or inspect.ismethod(obj):
        kind = "function"
    else:
        kind = "callable"
    return {
        "target": target,
        "kind": kind,
        "signature": signature,
        "async": inspect.iscoroutinefunction(obj),
        "doc": gist_api._first_line(inspect.getdoc(obj)),
        "module": getattr(obj, "__module__", None),
        "file": file,
    }
