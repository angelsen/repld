"""Phase 9: Gist-registered MCP tools — discovery, dispatch, auto-reload, error handling."""

import base64
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness import Bridge, Kernel, assert_eq, assert_true, content_text

from repld import paths


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
        _oversized_result_spills(b, gists_dir)
        _image_result(b, gists_dir)
        _bad_tool_calls(b)
        _tool_input_validation(b, gists_dir)
        _reserved_tool_names(b, gists_dir)
        _list_changed(b, gists_dir)
        _imported_on_loop(b, gists_dir)
        _sync_handler_defers(b, gists_dir)
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
        # Small results carry no spill _meta — only the exec/get_task paths
        # expose spill bookkeeping via _meta; a gist tool's spill marker (when
        # one fires) lives inline in the text block instead, see
        # _oversized_result_spills.
        assert_true(
            "_meta" not in resp["result"],
            f"gist tool has no _meta (got {list(resp['result'].keys())})",
        )
        print(f"  ✓ gist tool call: {text!r} (structured, no _meta)")


def _annotated_params(b: Bridge, gists_dir: Path) -> None:
    """Annotated[T, "..."] is the only way to describe a *parameter* — the
    docstring's first line is spent on the tool description."""
    source = (
        '"""Smoketest gist with annotated params."""\n\n'
        "from typing import Annotated, Literal\n\n"
        "import repld\n\n"
        "@repld.tool(title='Smoke annotated', read_only=True)\n"
        "async def _tool_smoke_annotated(\n"
        "    query: Annotated[str, \"Search term (e.g. 'strom')\"],\n"
        '    fra: Annotated[str | None, "From date YYYY-MM-DD"] = None,\n'
        "    plain: int = 7,\n"
        "    tagged: Annotated[int, 42] = 1,\n"
        '    way: Annotated[Literal["below", "above"], "Which way"] = "below",\n'
        "    level: Literal[1, 2] | None = None,\n"
        "    mixed: Literal['a', 1] = 'a',\n"
        "    tags: list[str] | None = None,\n"
        ") -> str:\n"
        '    """Annotated tool."""\n'
        '    return f"{query}|{fra}|{plain}|{tagged}"\n'
    )
    with _gist(gists_dir, "smoke_annotated", source):
        schema = _tool(b, "smoke_annotated")
        props = schema["inputSchema"]["properties"]
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
        # Literal lists its values as an enum, through Annotated and `| None` too;
        # mixed-type values have no single JSON type, so neither type nor enum.
        assert_eq(
            (props["way"]["type"], props["way"]["enum"]),
            ("string", ["below", "above"]),
            "Literal enum",
        )
        assert_eq(
            props["way"]["description"], "Which way", "Literal keeps its description"
        )
        assert_eq(
            (props["level"]["type"], props["level"]["enum"]),
            ("integer", [1, 2]),
            "Optional Literal enum",
        )
        assert_true("enum" not in props["mixed"], "a mixed Literal gets no enum")
        assert_eq(
            props["tags"],
            {"type": "array", "items": {"type": "string"}, "default": None},
            "list[str] carries items",
        )
        # The decorator reaches tools/list as both the spec's title and
        # annotations.title, which is the one Claude Code displays.
        assert_eq(
            (schema.get("title"), schema.get("annotations")),
            ("Smoke annotated", {"title": "Smoke annotated", "readOnlyHint": True}),
            "@repld.tool title + hints",
        )

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
        assert_true(
            "error" not in resp, f"handler exception is not a protocol error: {resp!r}"
        )
        res = resp["result"]
        assert_true(
            res.get("isError") is True,
            f"handler exception → isError result (got {res!r})",
        )
        text = res["content"][0]["text"]
        assert_true(
            "intentional boom" in text,
            f"result text carries the exception (got {text!r})",
        )
        # Bad arguments are execution errors too (spec 2025-11-25, SEP-1303).
        resp = _call(b, "smoke_greet", {"nope": 1})
        assert_true(
            resp.get("result", {}).get("isError") is True,
            f"bad arguments → isError result (got {resp!r})",
        )
        print(f"  ✓ gist tool error: {text!r}")


def _oversized_result_spills(b: Bridge, gists_dir: Path) -> None:
    """A gist tool's text result is arbitrary code output, same as exec — it
    goes through the spill pipeline too, so a large return doesn't dump
    straight into the response."""
    source = (
        '"""Smoketest gist — oversized result."""\n\n'
        "async def _tool_smoke_big(size: int) -> str:\n"
        '    """Return a string of the given size."""\n'
        '    return "x" * size\n'
    )
    with _gist(gists_dir, "smoke_tools", source):
        resp = _call(b, "smoke_big", {"size": 10000})
        text = content_text(resp)
        m = re.search(r"\[full output: (.+)\]", text)
        assert_true(m is not None, f"spilled result carries a marker (got {text!r})")
        assert m is not None
        spill_path = m.group(1)
        assert_true(Path(spill_path).exists(), f"spill file exists: {spill_path}")
        with open(spill_path) as f:
            full = f.read()
        assert_eq(len(full), 10000, "spill file holds the untruncated result")
        print(f"  ✓ oversized gist tool result spilled to {spill_path}")


def _image_result(b: Bridge, gists_dir: Path) -> None:
    """A handler returning repld.Image, alone or in a list with str/dict, gets
    one content block per item; the image is base64 in an `image` block, never
    spilled, and a plain list without an image stays one JSON text block."""
    source = (
        '"""Smoketest gist — image results."""\n\n'
        "import repld\n\n"
        "PNG = bytes.fromhex('89504e470d0a1a0a') + b'smoke'\n\n"
        "async def _tool_smoke_image() -> str:\n"
        '    """Return an image alone."""\n'
        "    return repld.Image(PNG, 'image/png')\n\n"
        "async def _tool_smoke_mixed() -> str:\n"
        '    """Return text, a dict and an image."""\n'
        "    return ['shot', {'changed': 'none'}, repld.Image(PNG * 4000, 'image/jpeg')]\n\n"
        "async def _tool_smoke_plain_list() -> str:\n"
        '    """Return a plain list."""\n'
        "    return [1, 2, 3]\n"
    )
    png = bytes.fromhex("89504e470d0a1a0a") + b"smoke"
    with _gist(gists_dir, "smoke_images", source):
        alone = _call(b, "smoke_image", {})["result"]["content"]
        assert_eq(
            alone,
            [
                {
                    "type": "image",
                    "data": base64.b64encode(png).decode(),
                    "mimeType": "image/png",
                }
            ],
            "an Image alone is one image block",
        )
        mixed = _call(b, "smoke_mixed", {})["result"]
        kinds = [c["type"] for c in mixed["content"]]
        assert_eq(kinds, ["text", "text", "image"], "one block per item, in order")
        assert_eq(
            mixed["structuredContent"],
            {"changed": "none"},
            "the dict item is the structuredContent",
        )
        image = mixed["content"][2]
        assert_eq(
            base64.b64decode(image["data"]),
            png * 4000,
            "a large image is inlined whole, never spilled",
        )
        assert_eq(image["mimeType"], "image/jpeg", "the mime type is the handler's")
        plain = _call(b, "smoke_plain_list", {})["result"]["content"]
        assert_eq(
            [c["type"] for c in plain],
            ["text"],
            "a list without an image stays one text block",
        )
        print(
            "  ✓ gist tool images: alone, mixed with text/dict, never spilled; plain list unchanged"
        )


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


def _tool_input_validation(b: Bridge, gists_dir: Path) -> None:
    """A gist tool's arguments are checked against its inferred schema before it runs."""
    source = (
        '"""Smoketest gist with a typed tool."""\n\n'
        "def _tool_smoke_add(a: int, b: int = 1) -> dict:\n"
        '    """Add two numbers."""\n'
        '    return {"sum": a + b}\n'
    )
    with _gist(gists_dir, "smoke_validate", source):
        assert_eq(
            _tool(b, "smoke_add")["inputSchema"]["required"], ["a"], "a is required"
        )
        ok = _call(b, "smoke_add", {"a": 2})
        assert_eq(ok["result"]["structuredContent"], {"sum": 3}, "a valid call runs")
        for args, needle in (
            ({"a": "x"}, "must be integer"),
            ({}, "missing required"),
            ({"a": 1, "z": 2}, "unknown argument"),
        ):
            resp = _call(b, "smoke_add", args)
            assert_true(
                resp["result"].get("isError") is True and needle in content_text(resp),
                f"smoke_add {args!r} refused in-band with {needle!r} (got {resp!r})",
            )
    print(
        "  ✓ gist tool: wrong type, missing and unknown arguments refused before the handler"
    )


def _reserved_tool_names(b: Bridge, gists_dir: Path) -> None:
    """A `_tool_*` named like a built-in is skipped, so tools/list never duplicates a name."""
    source = (
        '"""Smoketest gist shadowing built-ins."""\n\n'
        "def _tool_exec(x: str) -> str:\n"
        '    """Shadow exec."""\n'
        "    return x\n\n"
        "def _tool_browser_zzz(x: str) -> str:\n"
        '    """Shadow a browser tool."""\n'
        "    return x\n\n"
        "def _tool_smoke_fine(x: str) -> str:\n"
        '    """A tool that is fine."""\n'
        "    return x\n"
    )
    with _gist(gists_dir, "smoke_reserved", source):
        names = _tool_names(b)
        assert_eq(names.count("exec"), 1, "exec is listed once, not shadowed")
        assert_true("browser_zzz" not in names, "browser_* names are reserved")
        assert_true("smoke_fine" in names, "the gist's other tool still lists")
        from repld import gist_lint

        findings = gist_lint.lint_file(gists_dir / "smoke_reserved.py")
        assert_eq(
            sorted(f.rule for f in findings if f.rule == "tool-name"),
            ["tool-name", "tool-name"],
            "gist lint flags both reserved names",
        )
    print("  ✓ gist tool named like a built-in is skipped, and lint says so")


def _list_changed(b: Bridge, gists_dir: Path) -> None:
    """A gist gaining or losing a `_tool_*` pushes `tools/list_changed` — both are
    advertised with `listChanged`, and nothing else tells a client to re-list."""
    method = "notifications/tools/list_changed"
    # Outlast one poll so an earlier scenario's change has already fired.
    time.sleep(3.0)
    while True:
        try:
            b.wait_notification(method, timeout=0.2)
        except TimeoutError:
            break
    source = (
        '"""Smoketest gist: list_changed."""\n\n'
        "def _tool_smoke_changed() -> str:\n"
        '    """Appears mid-session."""\n'
        '    return "x"\n'
    )
    cache = paths.cache_for(paths.socket_path(gists_dir.parent))
    with _gist(gists_dir, "smoke_changed", source):
        b.wait_notification(method, timeout=6.0)
        assert_true("smoke_changed" in _tool_names(b), "tool listed after the push")
        assert_true(
            _cache_lists(cache, "smoke_changed", present=True),
            "kernel.cache gains the tool the push announced",
        )
    b.wait_notification(method, timeout=6.0)
    assert_true("smoke_changed" not in _tool_names(b), "tool gone after removal")
    assert_true(
        _cache_lists(cache, "smoke_changed", present=False),
        "kernel.cache drops the removed tool",
    )
    print(
        "  ✓ tools/list_changed on gist tool add and remove, kernel.cache kept in step"
    )


def _imported_on_loop(b: Bridge, gists_dir: Path) -> None:
    """A tool gist whose module level needs a running loop is listed, by `tools/list`
    and by the poller's `kernel.cache` rewrite alike: both import on the loop."""
    source = (
        '"""Smoketest gist: module-level loop use."""\n\n'
        "import asyncio\n\n"
        "_LOOP = asyncio.get_running_loop()\n\n\n"
        "def _tool_smoke_onloop() -> str:\n"
        '    """Imported with a running loop."""\n'
        '    return "x"\n'
    )
    cache = paths.cache_for(paths.socket_path(gists_dir.parent))
    with _gist(gists_dir, "smoke_onloop", source):
        names = _tool_names(b)
        assert_true(
            "smoke_onloop" in names, f"tool of a loop-bound gist listed (got {names!r})"
        )
        assert_true(
            _cache_lists(cache, "smoke_onloop", present=True),
            "kernel.cache lists the loop-bound gist's tool",
        )
        assert_eq(
            content_text(_call(b, "smoke_onloop", {})),
            "x",
            "tools/call on a loop-bound gist",
        )
        _write(gists_dir / "smoke_onloop.py", source.replace('"x"', '"edited"'))
        assert_eq(
            content_text(_call(b, "smoke_onloop", {})),
            "edited",
            "tools/call re-imports a changed loop-bound gist on the loop",
        )
    assert_true(
        _cache_lists(cache, "smoke_onloop", present=False),
        "kernel.cache drops it once the gist is gone",
    )
    print("  ✓ gist tools imported on the loop: module-level get_running_loop() works")


def _sync_handler_defers(b: Bridge, gists_dir: Path) -> None:
    """A sync handler runs off the loop, so `repld.defer()` is how it schedules work."""
    source = (
        '"""Smoketest gist: sync handler."""\n\n'
        "import asyncio\n\n"
        "import repld\n\n\n"
        "async def _job() -> None:\n"
        "    await asyncio.sleep(0.1)\n\n\n"
        "def _tool_smoke_syncdefer() -> str:\n"
        '    """Defer a job from a sync handler."""\n'
        '    return repld.defer(_job(), "syncdefer")\n'
    )
    with _gist(gists_dir, "smoke_syncdefer", source):
        tid = content_text(_call(b, "smoke_syncdefer", {})).strip()
        b.wait_notification(
            "notifications/claude/channel",
            kind="task_done",
            timeout=10,
            where=lambda n: n["params"]["meta"].get("task_id") == tid,
        )
    print("  ✓ a sync tool handler schedules work with defer()")


def _cache_lists(
    cache: Path, tool: str, *, present: bool, timeout: float = 5.0
) -> bool:
    """Whether `kernel.cache` comes to list (or not list) *tool* within *timeout*.

    The poller writes the cache right after it broadcasts, so a reader that
    just saw the push may beat the write.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = {t["name"] for t in json.loads(cache.read_text())["tools"]}
        if (tool in names) == present:
            return True
        time.sleep(0.2)
    return False
