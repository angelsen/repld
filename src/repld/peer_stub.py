"""Typed shims for another project's gist: `repld gist stub`.

A stub is an ordinary local gist whose functions forward to a peer kernel, so
`import voice; await voice.transcribe(path)` works here without installing
voice's dependencies. It is generated, never edited: the peer's AST signatures
(`peer/list`) are the source of truth, and `--check` re-reads them to say which
functions have drifted.

Everything here is pure text in, text out except `fetch_surface`, so the
renderer can be exercised against a fake peer. Stdlib only; imports `gist_api`
for the AST helpers and nothing else from repld at module level.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from . import gist_api

STUB_DUNDER = "__repld_peer_stub__"

_HEADER = (
    "# source: peer {cwd} gist {gist}\n"
    "# generated: {date} by `repld gist stub`. Regenerate, do not edit;\n"
    "# `repld gist stub --check` reports drift from the peer.\n"
)


def function_hash(fn: dict) -> str:
    """A short digest of what a caller can observe of *fn*: its parameters,
    kinds, annotations, defaults and async-ness. Docs are left out, so a reworded
    docstring is not drift."""
    rows = [[p["name"], p["kind"], p["annotation"], p["default"]] for p in fn["params"]]
    body = json.dumps(
        [fn["name"], bool(fn["async"]), rows, fn["returns"]], separators=(",", ":")
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def select_functions(
    surface: dict, gist: str, names: list[str] | None
) -> tuple[dict, list[dict]]:
    """``(gist entry, functions)`` for *gist* in a `peer/list` body.

    Raises LookupError, naming what does exist, when the gist or a requested
    function is not there.
    """
    entries = {g["name"]: g for g in surface.get("gists", [])}
    if gist not in entries:
        have = ", ".join(sorted(entries)) or "none"
        raise LookupError(f"the peer has no gist {gist!r} (it has: {have})")
    entry = entries[gist]
    fns = {f["name"]: f for f in entry["functions"]}
    if not fns:
        raise LookupError(f"gist {gist!r} has no public functions to stub")
    if names is None:
        return entry, [fns[n] for n in fns]
    missing = [n for n in names if n not in fns]
    if missing:
        raise LookupError(
            f"gist {gist!r} has no function {', '.join(missing)} "
            f"(it has: {', '.join(fns)})"
        )
    return entry, [fns[n] for n in names]


def _param_text(p: dict) -> str:
    kind = p["kind"]
    name = p["name"]
    ann = f": {p['annotation']}" if p["annotation"] else ""
    if kind == "VAR_POSITIONAL":
        return f"*{name}{ann}"
    if kind == "VAR_KEYWORD":
        return f"**{name}{ann}"
    if not p["has_default"]:
        return f"{name}{ann}"
    # A default that is not a literal (a call, a name) cannot be copied; the
    # sentinel leaves the argument out of the forwarded call so the peer
    # applies its own.
    default = p["default"] if p["literal"] else "_UNSET"
    return f"{name}{ann} = {default}" if ann else f"{name}={default}"


def render_params(params: list[dict]) -> str:
    out: list[str] = []
    last_posonly = max(
        (i for i, p in enumerate(params) if p["kind"] == "POSITIONAL_ONLY"),
        default=-1,
    )
    has_varargs = any(p["kind"] == "VAR_POSITIONAL" for p in params)
    star_done = has_varargs
    for i, p in enumerate(params):
        if p["kind"] == "KEYWORD_ONLY" and not star_done:
            out.append("*")
            star_done = True
        out.append(_param_text(p))
        if i == last_posonly:
            out.append("/")
    return ", ".join(out)


def _render_function(gist: str, fn: dict) -> str:
    params = fn["params"]
    names = {k: [p["name"] for p in params if p["kind"] == k] for k in _KINDS}
    pairs = lambda ns: "{" + ", ".join(f"{n!r}: {n}" for n in ns) + "}"
    varargs = names["VAR_POSITIONAL"][0] if names["VAR_POSITIONAL"] else "()"
    varkw = names["VAR_KEYWORD"][0] if names["VAR_KEYWORD"] else "{}"
    ret = f" -> {fn['returns']}" if fn["returns"] else ""
    doc = fn["doc"] or f"Call {gist}.{fn['name']} on the peer."
    target = f"{gist}.{fn['name']}"
    return (
        f"async def {fn['name']}({render_params(params)}){ret}:\n"
        f"    {json.dumps(doc)}\n"
        f"    _a, _k = _STUB_ARGS(\n"
        f"        [{', '.join(names['POSITIONAL_ONLY'])}],\n"
        f"        {pairs(names['POSITIONAL_OR_KEYWORD'])},\n"
        f"        {pairs(names['KEYWORD_ONLY'])},\n"
        f"        {varargs},\n"
        f"        {varkw},\n"
        f"    )\n"
        f"    return await _PEER.call({target!r}, *_a, **_k)\n"
    )


_KINDS = (
    "POSITIONAL_ONLY",
    "POSITIONAL_OR_KEYWORD",
    "VAR_POSITIONAL",
    "KEYWORD_ONLY",
    "VAR_KEYWORD",
)


def render_stub(
    peer_cwd: str,
    gist: str,
    gist_doc: str,
    functions: list[dict],
    *,
    date: str,
    start: bool = False,
) -> str:
    """The stub module's source."""
    marker = {
        "peer": peer_cwd,
        "gist": gist,
        "functions": {f["name"]: function_hash(f) for f in functions},
    }
    peer_name = peer_cwd.rstrip("/").rsplit("/", 1)[-1]
    doc = f"{gist_doc or gist} (peer stub for {peer_name})."
    start_arg = ", start=True" if start else ""
    head = (
        f"{json.dumps(doc)}\n"
        f"{_HEADER.format(cwd=peer_cwd, gist=gist, date=date)}\n"
        "from __future__ import annotations\n\n"
        "from repld.peer import UNSET as _UNSET\n"
        "from repld.peer import peers as _peers\n"
        "from repld.peer import stub_args as _STUB_ARGS\n\n"
        f"{STUB_DUNDER} = {marker!r}\n\n"
        f"_PEER = _peers.get({peer_cwd!r}{start_arg})\n"
    )
    return head + "\n\n" + "\n\n".join(_render_function(gist, f) for f in functions)


def read_stub(path: Path) -> dict | None:
    """The `__repld_peer_stub__` marker of the gist at *path*, or None when the
    file is not a generated stub (or the marker is unreadable)."""
    tree = gist_api._parse(path)
    node = gist_api._dunder_value(tree, STUB_DUNDER) if tree else None
    if node is None:
        return None
    try:
        marker = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    ok = (
        isinstance(marker, dict)
        and isinstance(marker.get("peer"), str)
        and isinstance(marker.get("gist"), str)
        and isinstance(marker.get("functions"), dict)
    )
    return marker if ok else None


def drift(marker: dict, surface: dict) -> list[str]:
    """What changed since *marker* was generated, as human-readable items; an
    empty list means the stub still matches the peer."""
    entries = {g["name"]: g for g in surface.get("gists", [])}
    entry = entries.get(marker["gist"])
    if entry is None:
        return [f"gist {marker['gist']!r} is gone from the peer"]
    now = {f["name"]: function_hash(f) for f in entry["functions"]}
    out: list[str] = []
    for name, was in marker["functions"].items():
        if name not in now:
            out.append(f"{name}: removed")
        elif now[name] != was:
            out.append(f"{name}: signature changed")
    return out


def check(
    path: Path, fetch_surface: Callable[[str], dict | None]
) -> tuple[str, list[str]]:
    """``(status, details)`` for one stub: ``ok``, ``stale``, or ``unchecked``
    when its peer is not running. *fetch_surface* takes the peer's cwd and
    returns a `peer/list` body, or None if no kernel is up there."""
    marker = read_stub(path)
    if marker is None:
        return "unchecked", ["not a readable peer stub"]
    surface = fetch_surface(marker["peer"])
    if surface is None:
        return "unchecked", [f"no kernel is running for {marker['peer']}"]
    items = drift(marker, surface)
    return ("stale", items) if items else ("ok", [])
