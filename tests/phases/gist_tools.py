"""Phase 9: Gist-registered MCP tools — discovery, dispatch, auto-reload, error handling."""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness import Bridge, Kernel, assert_eq, assert_true, content_text


def phase_9_gist_tools(kernel: Kernel) -> None:
    """Gist-registered MCP tools: discovery, dispatch, auto-reload, error handling."""
    gists_dir = kernel.cwd / "gists"
    gists_dir.mkdir(exist_ok=True)
    b = Bridge(kernel.cwd)
    try:
        b.handshake()
        _typed_tool_discovery_and_dispatch(b, gists_dir)
        _annotated_params(b, gists_dir)
        _postponed_annotations(b, gists_dir)
        _auto_reload(b, gists_dir)
        _legacy_declaration_inert(b, gists_dir)
        _single_dict_param(b, gists_dir)
        _handler_error(b, gists_dir)
        _bad_tool_calls(b)
    finally:
        b.close()


@contextmanager
def _gist(gists_dir: Path, name: str, source: str) -> Iterator[Path]:
    """Write `gists_dir/<name>.py` for the block; removed after, so nothing
    this phase registers is still advertised to the phases after it."""
    path = gists_dir / f"{name}.py"
    _write(path, source)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _write(path: Path, source: str) -> None:
    time.sleep(0.01)  # reload is mtime-keyed; a rewrite within the tick is invisible
    path.write_text(source)


def _tool(b: Bridge, name: str) -> dict:
    return {t["name"]: t for t in b.call("tools/list")["result"]["tools"]}[name]


def _tool_names(b: Bridge) -> list[str]:
    return [t["name"] for t in b.call("tools/list")["result"]["tools"]]


def _call(b: Bridge, name: str, arguments: dict) -> dict:
    return b.call("tools/call", {"name": name, "arguments": arguments})


_GREET = (
    '"""Smoketest gist with tools."""\n\n'
    "async def _tool_smoke_greet(name: str) -> dict:\n"
    '    """Return a greeting."""\n'
    '    return {"greeting": f"hello {name}"}\n'
)


def _typed_tool_discovery_and_dispatch(b: Bridge, gists_dir: Path) -> None:
    """A typed _tool_* function — schema auto-inferred, no __repld_tools__ needed."""
    with _gist(gists_dir, "smoke_tools", _GREET):
        names = _tool_names(b)
        assert_true("smoke_greet" in names, f"gist tool in tools/list (got {names!r})")
        schema = _tool(b, "smoke_greet")
        assert_eq(schema["description"], "Return a greeting.", "inferred description")
        assert_eq(
            schema["inputSchema"]["properties"]["name"]["type"],
            "string",
            "inferred param type",
        )
        assert_eq(schema["inputSchema"]["required"], ["name"], "inferred required")
        print("  ✓ gist tool 'smoke_greet' in tools/list with inferred schema")

        # New-style dispatch (handler(**args)).
        resp = _call(b, "smoke_greet", {"name": "world"})
        text = content_text(resp)
        result = json.loads(text)
        assert_eq(result["greeting"], "hello world", "gist tool response")
        # A dict return rides as structuredContent alongside the text block —
        # same data, so a client on either protocol revision reads the same
        # answer.
        assert_eq(
            resp["result"]["structuredContent"],
            result,
            "dict return carried as structuredContent too",
        )
        # No spill metadata — gist tools bypass the spill pipeline.
        assert_true(
            "_meta" not in resp["result"],
            f"gist tool has no _meta (got {list(resp['result'].keys())})",
        )
        print(f"  ✓ gist tool call: {text!r} (no spill, structured)")


def _annotated_params(b: Bridge, gists_dir: Path) -> None:
    """Annotated[T, "..."] is the only way to describe a *parameter* — the
    docstring's first line is spent on the tool description."""
    source = (
        '"""Smoketest gist with annotated params."""\n\n'
        "from typing import Annotated\n\n"
        "async def _tool_smoke_annotated(\n"
        "    query: Annotated[str, \"Search term (e.g. 'strom')\"],\n"
        '    fra: Annotated[str | None, "From date YYYY-MM-DD"] = None,\n'
        "    plain: int = 7,\n"
        "    tagged: Annotated[int, 42] = 1,\n"
        ") -> str:\n"
        '    """Annotated tool."""\n'
        '    return f"{query}|{fra}|{plain}|{tagged}"\n'
    )
    with _gist(gists_dir, "smoke_annotated", source):
        props = _tool(b, "smoke_annotated")["inputSchema"]["properties"]
        assert_eq(
            props["query"]["description"],
            "Search term (e.g. 'strom')",
            "Annotated description reaches the schema",
        )
        # Composes with `| None`: description kept, type still unwrapped.
        assert_eq(props["fra"]["description"], "From date YYYY-MM-DD", "optional desc")
        assert_eq(props["fra"]["type"], "string", "optional type still unwrapped")
        assert_eq(props["fra"]["default"], None, "optional default still advertised")
        # A bare annotation is unchanged, and non-str metadata is ignored
        # rather than rejected — it may belong to some other consumer.
        assert_true("description" not in props["plain"], "un-annotated param has none")
        assert_eq(props["plain"]["type"], "integer", "un-annotated type unaffected")
        assert_true("description" not in props["tagged"], "non-str metadata ignored")
        assert_eq(props["tagged"]["type"], "integer", "non-str metadata keeps type")

        resp = _call(b, "smoke_annotated", {"query": "q", "fra": "2026-01-01"})
        assert_eq(
            content_text(resp),
            "q|2026-01-01|7|1",
            "annotated tool dispatches on real kwargs",
        )
    print("  ✓ Annotated param descriptions inferred, dispatched, and scoped")


def _postponed_annotations(b: Bridge, gists_dir: Path) -> None:
    """Same again under `from __future__ import annotations`, where every
    annotation reaches inspect.signature as a *string*. Without resolving
    them, `int` stops mapping to "integer" and the Annotated wrapper is
    invisible — the schema silently degrades to all-strings, no descriptions.
    Real gists use this import, so it is not hypothetical."""
    source = (
        '"""Smoketest gist with postponed annotations."""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Annotated\n\n"
        "async def _tool_smoke_pep563(\n"
        '    name: Annotated[str, "Who to greet"],\n'
        '    count: Annotated[int, "How many times"] = 2,\n'
        "    plain: float = 1.5,\n"
        ") -> str:\n"
        '    """Postponed-annotation tool."""\n'
        '    return f"{name}|{count}|{plain}"\n'
    )
    with _gist(gists_dir, "smoke_pep563", source):
        props = _tool(b, "smoke_pep563")["inputSchema"]["properties"]
        assert_eq(props["name"]["description"], "Who to greet", "PEP 563 description")
        assert_eq(props["count"]["description"], "How many times", "PEP 563 + default")
        assert_eq(props["count"]["type"], "integer", "PEP 563 int stays an integer")
        assert_eq(props["plain"]["type"], "number", "PEP 563 float stays a number")
        resp = _call(b, "smoke_pep563", {"name": "x", "count": 3})
        assert_eq(content_text(resp), "x|3|1.5", "PEP 563 tool dispatches")
    print("  ✓ postponed annotations resolved — types and descriptions survive")


def _auto_reload(b: Bridge, gists_dir: Path) -> None:
    """Edit the handler, re-call → fresh result."""
    with _gist(gists_dir, "smoke_tools", _GREET) as path:
        first = json.loads(content_text(_call(b, "smoke_greet", {"name": "world"})))
        assert_eq(first["greeting"], "hello world", "v1 loaded")
        _write(
            path,
            '"""Smoketest gist with tools — v2."""\n\n'
            "async def _tool_smoke_greet(name: str) -> dict:\n"
            '    """Return a greeting v2."""\n'
            '    return {"greeting": f"hey {name}!"}\n',
        )
        text = content_text(_call(b, "smoke_greet", {"name": "world"}))
        assert_eq(json.loads(text)["greeting"], "hey world!", "gist tool auto-reload")
        print(f"  ✓ gist tool auto-reload: {text!r}")


def _legacy_declaration_inert(b: Bridge, gists_dir: Path) -> None:
    """__repld_tools__ was removed in 0.2. A declaration is now inert: only the
    _tool_* functions in the file are exposed, and the names the list invented
    never appear."""
    source = (
        '"""Smoketest gist — a stale __repld_tools__ declaration."""\n\n'
        "__repld_tools__ = [\n"
        "    {\n"
        '        "name": "smoke_declared_only",\n'
        '        "description": "Never had a handler.",\n'
        '        "inputSchema": {"type": "object", "properties": {}},\n'
        "    },\n"
        "]\n\n\n"
        "async def _tool_smoke_still_typed(name: str) -> str:\n"
        '    """Typed sibling of a stale declaration."""\n'
        '    return f"typed {name}"\n'
    )
    with _gist(gists_dir, "smoke_legacy_tools", source):
        names = _tool_names(b)
        assert_true(
            "smoke_declared_only" not in names,
            f"__repld_tools__ name is ignored (got {names!r})",
        )
        # And it no longer suppresses the file's typed functions, which is
        # what the old precedence rule did when both conventions appeared
        # together.
        assert_true(
            "smoke_still_typed" in names,
            f"typed tool alongside a stale list still registers (got {names!r})",
        )
        resp = _call(b, "smoke_still_typed", {"name": "world"})
        assert_eq(content_text(resp), "typed world", "typed sibling runs")
    print("  ✓ __repld_tools__ inert: name ignored, typed sibling unaffected")


def _single_dict_param(b: Bridge, gists_dir: Path) -> None:
    """A single dict parameter is an ordinary tool taking one object — it used
    to be indistinguishable from the legacy handler shape and was skipped."""
    source = (
        '"""Smoketest gist — single dict parameter."""\n\n'
        "async def _tool_smoke_payload(payload: dict) -> str:\n"
        '    """Take one object argument."""\n'
        "    return str(sorted(payload.items()))\n"
    )
    with _gist(gists_dir, "smoke_dict_param", source):
        names = _tool_names(b)
        assert_true(
            "smoke_payload" in names,
            f"single-dict-param tool now registers (got {names!r})",
        )
        assert_eq(
            _tool(b, "smoke_payload")["inputSchema"]["properties"]["payload"]["type"],
            "object",
            "dict param maps to object",
        )
        resp = _call(b, "smoke_payload", {"payload": {"b": 2, "a": 1}})
        assert_eq(
            content_text(resp),
            "[('a', 1), ('b', 2)]",
            "single-dict-param tool dispatches",
        )
    print("  ✓ single dict param is a real tool now, not a legacy handler")


def _handler_error(b: Bridge, gists_dir: Path) -> None:
    source = (
        '"""Smoketest gist — error case."""\n\n'
        "async def _tool_smoke_greet(name: str) -> dict:\n"
        '    """Raise intentionally."""\n'
        '    raise ValueError("intentional boom")\n'
    )
    with _gist(gists_dir, "smoke_tools", source):
        resp = _call(b, "smoke_greet", {"name": "world"})
        assert_true("error" in resp, f"handler exception → MCP error (got {resp!r})")
        assert_true(
            "intentional boom" in resp["error"]["message"],
            f"error message contains exception text (got {resp['error']['message']!r})",
        )
        print(f"  ✓ gist tool error: {resp['error']['message']!r}")


def _bad_tool_calls(b: Bridge) -> None:
    resp = _call(b, "totally_nonexistent_tool", {})
    assert_true("error" in resp, f"unknown tool → MCP error (got {resp!r})")
    print("  ✓ unknown tool → MCP error")

    # Missing tool name → fast error (no gist scan).
    resp = b.call("tools/call", {"arguments": {}})
    assert_true("error" in resp, f"missing tool name → MCP error (got {resp!r})")
    assert_true(
        "missing tool name" in resp["error"]["message"],
        f"error says 'missing tool name' (got {resp['error']['message']!r})",
    )
    print("  ✓ missing tool name → MCP error")
