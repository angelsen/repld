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
            "        return 'pong'\n\n"
            "    @property\n"
            "    def label(self) -> str:\n"
            '        """Display label."""\n'
            "        return self.name\n\n"
            "    @label.setter\n"
            "    def label(self, value: str) -> None:\n"
            "        self.name = value\n"
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

        # Cross-project registry resource is listed and readable
        assert_true(
            "repld://gists/_registry" in by_uri,
            "resources/list contains repld://gists/_registry",
        )
        resp = b.call("resources/read", {"uri": "repld://gists/_registry"})
        reg_text = resp["result"]["contents"][0]["text"]
        assert_true(
            "registry" in reg_text.lower(),
            f"registry resource read returns text (got {reg_text[:60]!r})",
        )
        print("  ✓ repld://gists/_registry listed + readable")

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
        assert_true(
            ".label -> str" in text and ".label(" not in text,
            f"property listed as attribute, no call parens (got {text!r})",
        )
        assert_eq(
            text.count(".label"),
            1,
            f"property listed once, setter not duplicated (got {text!r})",
        )
        print(f"  ✓ resources/read repld://gists/test_api:\n{text}")

        # Unknown gist → MCP error
        resp = b.call(
            "resources/read",
            {"uri": "repld://gists/nonexistent_xyz"},
        )
        assert_true("error" in resp, f"unknown gist returns error (got {resp!r})")
        print("  ✓ unknown gist → MCP error")

        # Malformed gist → MCP error pointing at the syntax error, like a linter
        broken_file = gists_dir / "test_broken.py"
        broken_file.write_text("def oops(:\n")
        try:
            resp = b.call(
                "resources/read",
                {"uri": "repld://gists/test_broken"},
            )
            assert_true("error" in resp, f"malformed gist returns error (got {resp!r})")
            msg = resp["error"]["message"]
            assert_true(
                "syntax error at line 1" in msg,
                f"error names the gist and line (got {msg!r})",
            )
            print(f"  ✓ malformed gist → MCP error: {msg!r}")
        finally:
            broken_file.unlink()

        # Doc resources return FULL text — not the 4KB spill preview
        # (resources are on-demand pulls; only >64KB falls back to spill).
        resp = b.call("resources/read", {"uri": "repld://docs/guide"})
        doc = resp["result"]["contents"][0]
        assert_true(
            len(doc["text"]) > 4096,
            f"docs/guide returned full text, not preview ({len(doc['text'])} bytes)",
        )
        assert_true(
            "[full output:" not in doc["text"],
            "docs/guide has no spill marker",
        )
        assert_eq(doc["mimeType"], "text/plain", "docs/guide mimeType")
        print(f"  ✓ repld://docs/guide read in full ({len(doc['text'])} bytes)")

        # scan_deps survives a dep whose dotted parent is missing —
        # find_spec("no_such_parent.sub") raises, _is_importable must swallow it
        # (this used to crash kernel boot).
        from repld import gist_deps

        dep_probe = gists_dir / "test_dep_probe.py"
        dep_probe.write_text('__repld_deps__ = ["no_such_parent_xyz.sub"]\n')
        try:
            missing = gist_deps.scan_deps(paths=[dep_probe])
            assert_true(
                any(d.requirement == "no_such_parent_xyz.sub" for d in missing),
                f"missing namespace-dotted dep reported, not raised (got {missing!r})",
            )
            print("  ✓ scan_deps handles missing namespace-dotted dep without raising")
        finally:
            dep_probe.unlink()

    finally:
        b.close()
