"""Phase 9: Gist-registered MCP tools — discovery, dispatch, auto-reload, error handling."""

import json
import time

from harness import Bridge, Kernel, assert_eq, assert_true


def phase_9_gist_tools(kernel: Kernel) -> None:
    """Gist-registered MCP tools: discovery, dispatch, auto-reload, error handling."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # Write a gist with a typed _tool_* function — schema auto-inferred,
        # no __repld_tools__ needed.
        gists_dir = kernel.cwd / "gists"
        gists_dir.mkdir(exist_ok=True)
        gist_file = gists_dir / "smoke_tools.py"
        gist_file.write_text(
            '"""Smoketest gist with tools."""\n\n'
            "async def _tool_smoke_greet(name: str) -> dict:\n"
            '    """Return a greeting."""\n'
            '    return {"greeting": f"hello {name}"}\n'
        )

        # tools/list should include the gist tool with an inferred schema
        resp = b.call("tools/list")
        tools_by_name = {t["name"]: t for t in resp["result"]["tools"]}
        assert_true(
            "smoke_greet" in tools_by_name,
            f"gist tool in tools/list (got {list(tools_by_name)!r})",
        )
        schema = tools_by_name["smoke_greet"]
        assert_eq(schema["description"], "Return a greeting.", "inferred description")
        assert_eq(
            schema["inputSchema"]["properties"]["name"]["type"],
            "string",
            "inferred param type",
        )
        assert_eq(schema["inputSchema"]["required"], ["name"], "inferred required")
        print("  ✓ gist tool 'smoke_greet' in tools/list with inferred schema")

        # Annotated[T, "..."] is the only way to describe a *parameter* — the
        # docstring's first line is spent on the tool description.
        annotated_file = gists_dir / "smoke_annotated.py"
        annotated_file.write_text(
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
        resp = b.call("tools/list")
        props = {t["name"]: t for t in resp["result"]["tools"]}["smoke_annotated"][
            "inputSchema"
        ]["properties"]
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

        resp = b.call(
            "tools/call",
            {
                "name": "smoke_annotated",
                "arguments": {"query": "q", "fra": "2026-01-01"},
            },
        )
        assert_eq(
            resp["result"]["content"][0]["text"],
            "q|2026-01-01|7|1",
            "annotated tool dispatches on real kwargs",
        )
        annotated_file.unlink()
        print("  ✓ Annotated param descriptions inferred, dispatched, and scoped")

        # Call the gist tool — new-style dispatch (handler(**args))
        resp = b.call(
            "tools/call",
            {"name": "smoke_greet", "arguments": {"name": "world"}},
        )
        content = resp["result"]["content"][0]["text"]
        result = json.loads(content)
        assert_eq(result["greeting"], "hello world", "gist tool response")
        # Verify no spill metadata — gist tools bypass spill pipeline
        assert_true(
            "_meta" not in resp["result"],
            f"gist tool has no _meta (got {list(resp['result'].keys())})",
        )
        print(f"  ✓ gist tool call: {content!r} (no spill)")

        # Auto-reload: edit the handler, re-call → fresh result
        time.sleep(0.01)  # ensure mtime changes
        gist_file.write_text(
            '"""Smoketest gist with tools — v2."""\n\n'
            "async def _tool_smoke_greet(name: str) -> dict:\n"
            '    """Return a greeting v2."""\n'
            '    return {"greeting": f"hey {name}!"}\n'
        )

        resp = b.call(
            "tools/call",
            {"name": "smoke_greet", "arguments": {"name": "world"}},
        )
        content = resp["result"]["content"][0]["text"]
        result = json.loads(content)
        assert_eq(result["greeting"], "hey world!", "gist tool auto-reload")
        print(f"  ✓ gist tool auto-reload: {content!r}")

        # Legacy path: __repld_tools__ + _tool_*(args: dict) still dispatches
        # (old-style handler receives the raw args dict).
        legacy_file = gists_dir / "smoke_legacy_tools.py"
        legacy_file.write_text(
            '"""Smoketest gist — legacy tool registration."""\n\n'
            "__repld_tools__ = [\n"
            "    {\n"
            '        "name": "smoke_legacy_greet",\n'
            '        "description": "Return a greeting (legacy)",\n'
            '        "inputSchema": {\n'
            '            "type": "object",\n'
            '            "properties": {"name": {"type": "string"}},\n'
            '            "required": ["name"],\n'
            "        },\n"
            "    },\n"
            "]\n\n\n"
            "async def _tool_smoke_legacy_greet(args: dict) -> str:\n"
            "    import json\n"
            '    return json.dumps({"greeting": f"legacy hello {args[\'name\']}"})\n'
        )

        resp = b.call("tools/list")
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        assert_true(
            "smoke_legacy_greet" in tool_names,
            f"legacy gist tool in tools/list (got {tool_names!r})",
        )
        resp = b.call(
            "tools/call",
            {"name": "smoke_legacy_greet", "arguments": {"name": "world"}},
        )
        content = resp["result"]["content"][0]["text"]
        result = json.loads(content)
        assert_eq(result["greeting"], "legacy hello world", "legacy gist tool response")
        print(f"  ✓ legacy gist tool call (old-style dispatch): {content!r}")

        # Error case: handler that raises
        time.sleep(0.01)
        gist_file.write_text(
            '"""Smoketest gist — error case."""\n\n'
            "async def _tool_smoke_greet(name: str) -> dict:\n"
            '    """Raise intentionally."""\n'
            '    raise ValueError("intentional boom")\n'
        )

        resp = b.call(
            "tools/call",
            {"name": "smoke_greet", "arguments": {"name": "world"}},
        )
        assert_true(
            "error" in resp,
            f"handler exception → MCP error (got {resp!r})",
        )
        assert_true(
            "intentional boom" in resp["error"]["message"],
            f"error message contains exception text (got {resp['error']['message']!r})",
        )
        print(f"  ✓ gist tool error: {resp['error']['message']!r}")

        # Unknown tool → error
        resp = b.call(
            "tools/call",
            {"name": "totally_nonexistent_tool", "arguments": {}},
        )
        assert_true(
            "error" in resp,
            f"unknown tool → MCP error (got {resp!r})",
        )
        print("  ✓ unknown tool → MCP error")

        # Missing tool name → fast error (no gist scan)
        resp = b.call(
            "tools/call",
            {"arguments": {}},
        )
        assert_true(
            "error" in resp,
            f"missing tool name → MCP error (got {resp!r})",
        )
        assert_true(
            "missing tool name" in resp["error"]["message"],
            f"error says 'missing tool name' (got {resp['error']['message']!r})",
        )
        print("  ✓ missing tool name → MCP error")

    finally:
        b.close()
