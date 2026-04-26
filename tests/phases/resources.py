"""Phase 8: Gist resources — resources/list includes gists, resources/read returns API."""

from harness import Bridge, Kernel, assert_eq, assert_true


def phase_8_gist_resources(kernel: Kernel) -> None:
    """resources/list includes one entry per gist; resources/read repld://gists/{name} works."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

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

        # resources/list includes the gist as a concrete entry
        resp = b.call("resources/list")
        resources = resp["result"]["resources"]
        by_uri = {r["uri"]: r for r in resources}
        gist_entry = by_uri.get("repld://gists/test_api")
        assert_true(
            gist_entry is not None,
            f"resources/list contains repld://gists/test_api (got URIs: {list(by_uri)!r})",
        )
        assert gist_entry is not None
        assert_eq(gist_entry["name"], "test_api", "gist resource name")
        assert_eq(
            gist_entry["description"],
            "Test API gist for smoketest.",
            "gist resource description = first docstring line",
        )
        assert_eq(gist_entry["mimeType"], "text/plain", "gist resource mimeType")
        print(
            f"  ✓ resources/list includes gist: {gist_entry['name']!r} — {gist_entry['description']!r}"
        )

        # Browser resources still listed
        for required in (
            "repld://browser/tabs",
            "repld://browser/network",
            "repld://browser/console",
        ):
            assert_true(required in by_uri, f"resources/list contains {required}")
        print("  ✓ resources/list still includes browser resources")

        # resources/templates/list returns empty (template was removed)
        resp = b.call("resources/templates/list")
        templates = resp["result"]["resourceTemplates"]
        assert_eq(templates, [], "resources/templates/list returns empty list")
        print("  ✓ resources/templates/list: []")

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
