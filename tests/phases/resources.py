"""Phase 8: Gist resource templates — resources/templates/list + resources/read."""

from harness import Bridge, Kernel, assert_true


def phase_8_gist_resources(kernel: Kernel) -> None:
    """resources/templates/list + resources/read repld://gists/{name}."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # resources/templates/list
        resp = b.call("resources/templates/list")
        templates = resp["result"]["resourceTemplates"]
        uri_templates = [t["uriTemplate"] for t in templates]
        assert_true(
            "repld://gists/{name}" in uri_templates,
            f"resources/templates/list contains repld://gists/{{name}} (got {uri_templates!r})",
        )
        print(f"  ✓ resources/templates/list: {uri_templates}")

        # Write a gist with a class so introspect() has something to parse
        gists_dir = kernel.cwd / "gists"
        gists_dir.mkdir(exist_ok=True)
        gist_file = gists_dir / "test_api.py"
        gist_file.write_text(
            '"""Test API gist for smoketest."""\n\n'
            "class Widget:\n"
            '    """A simple widget."""\n\n'
            "    def __init__(self, name: str) -> None:\n"
            '        """Init."""\n'
            "        self.name = name\n\n"
            "    def ping(self) -> str:\n"
            '        """Return pong."""\n'
            "        return 'pong'\n"
        )

        # resources/read for the gist
        resp = b.call(
            "resources/read",
            {"uri": "repld://gists/test_api"},
        )
        contents = resp["result"]["contents"]
        assert_true(
            len(contents) == 1,
            f"resources/read returns 1 content item (got {len(contents)})",
        )
        text = contents[0]["text"]
        assert_true(
            "Widget" in text, f"introspect output contains class name (got {text!r})"
        )
        assert_true("ping" in text, f"introspect output contains method (got {text!r})")
        assert_true(contents[0]["mimeType"] == "text/plain", "mimeType is text/plain")
        print(f"  ✓ resources/read repld://gists/test_api:\n{text}")

        # Unknown gist → MCP error
        resp = b.call(
            "resources/read",
            {"uri": "repld://gists/nonexistent_xyz"},
        )
        assert_true("error" in resp, f"unknown gist returns error (got {resp!r})")
        print("  ✓ unknown gist → MCP error")

    finally:
        b.close()
