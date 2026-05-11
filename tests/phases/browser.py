"""Phase 6: Tool registration, gist auto-reload, browser integration."""

import json
import time

from harness import Bridge, Kernel, assert_eq, assert_true


def phase_6_tools_and_gists(kernel: Kernel) -> None:
    """Verify new tool registrations and gist auto-reload machinery."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # Verify new tools appear in tool list
        resp = b.call("tools/list")
        tool_names = set(t["name"] for t in resp["result"]["tools"])
        new_tools = {
            "browser_navigate",
            "browser_key",
            "browser_open",
            "browser_tree",
            "browser_fetch",
        }
        assert_true(
            new_tools.issubset(tool_names),
            f"new browser tools in tools/list (missing: {new_tools - tool_names})",
        )
        print(f"  ✓ new tools registered: {sorted(new_tools)}")

        # Gist auto-reload test
        # Write a gist module to the project-local gists/ dir
        gists_dir = kernel.cwd / "gists"
        gists_dir.mkdir(exist_ok=True)
        gist_file = gists_dir / "smoke_gist.py"
        gist_file.write_text("VALUE = 1\n")

        # Import it via exec
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "import smoke_gist; print(smoke_gist.VALUE)"},
            },
            timeout=5.0,
        )
        content = resp["result"]["content"][0]["text"]
        assert_true(
            "1" in content,
            f"initial gist import VALUE=1 (got {content!r})",
        )
        print("  ✓ gist imported, VALUE=1")

        # Edit the file
        time.sleep(0.01)  # ensure mtime changes
        gist_file.write_text("VALUE = 42\n")

        # Re-import — auto-reload should detect mtime change
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "import smoke_gist; print(smoke_gist.VALUE)"},
            },
            timeout=5.0,
        )
        content = resp["result"]["content"][0]["text"]
        assert_true(
            "42" in content,
            f"gist auto-reload VALUE=42 after edit (got {content!r})",
        )
        print("  ✓ gist auto-reload: VALUE=42 after edit")
    finally:
        b.close()


def phase_6(kernel: Kernel) -> None:
    """Browser integration — requires Chrome with --remote-debugging-port=9222.

    Skips gracefully if Chrome is not reachable.
    """
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("  - phase 6: websockets not installed (uv sync --extra browser), skipping")
        return

    import urllib.request as _urlreq

    try:
        with _urlreq.urlopen("http://localhost:9222/json/version", timeout=2) as r:
            r.read()
    except Exception:
        print("  - phase 6: Chrome not available on port 9222, skipping")
        return

    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # Verify browser tools are in the list
        resp = b.call("tools/list")
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        browser_tools = {
            "browser_watch",
            "browser_detach",
            "browser_tabs",
            "browser_pages",
            "browser_js",
            "browser_network",
            "browser_body",
            "browser_click",
            "browser_type",
            "browser_console",
            "browser_screenshot",
            "browser_cdp",
            "browser_clear",
            "browser_navigate",
            "browser_key",
            "browser_open",
            "browser_tree",
            "browser_fetch",
        }
        assert_true(
            browser_tools.issubset(set(tool_names)),
            f"tools/list contains all 18 browser tools (got {tool_names!r})",
        )
        print("  ✓ all 18 browser tools in tools/list")

        # Attach any open tab
        resp = b.call(
            "tools/call",
            {"name": "browser_watch", "arguments": {"pattern": "*"}},
            timeout=10.0,
        )
        result_text = resp["result"]["content"][0]["text"]
        assert_true(
            "result" in result_text,
            f"browser_watch returned result (got {result_text!r})",
        )
        print(f"  ✓ browser_watch: {result_text[:80]!r}")

        # List attached tabs (plain text, nested format)
        resp = b.call(
            "tools/call", {"name": "browser_tabs", "arguments": {}}, timeout=5.0
        )
        tabs_text = resp["result"]["content"][0]["text"]
        tabs_text = tabs_text.split("\n[full output:")[0].strip()
        if not tabs_text or tabs_text == "(no attached tabs)":
            print(
                "  - browser_tabs: no tabs attached (Chrome may have no open tabs), skipping js/network"
            )
            return
        tab_lines = [line for line in tabs_text.splitlines() if line.strip()]
        # First non-indented line: "9222:abc123  page  https://..."
        first_line = tab_lines[0].strip()
        tab_target = first_line.split()[0]  # e.g. "9222:abc123"
        tab_url = first_line.split()[-1]  # last token is URL
        print(
            f"  ✓ browser_tabs: {len(tab_lines)} tab(s), first target={tab_target!r} url={tab_url!r}"
        )

        # browser_js: evaluate 1+1
        resp = b.call(
            "tools/call",
            {
                "name": "browser_js",
                "arguments": {"target": tab_target, "code": "1+1"},
            },
            timeout=10.0,
        )
        js_text = resp["result"]["content"][0]["text"]
        js_result = json.loads(js_text)
        assert_true(
            js_result.get("result") == 2,
            f"browser_js 1+1 == 2 (got {js_result!r})",
        )
        print(f"  ✓ browser_js: 1+1 = {js_result['result']!r}")

        # browser_network: returns a list (may be empty)
        resp = b.call(
            "tools/call",
            {
                "name": "browser_network",
                "arguments": {"target": tab_target},
            },
            timeout=5.0,
        )
        net_text = resp["result"]["content"][0]["text"]
        # May be spilled — just verify it contains list-like content
        net_text_raw = net_text.split("\n[full output:")[0].strip()
        try:
            net_rows = json.loads(net_text_raw)
            assert_true(
                isinstance(net_rows, list),
                f"browser_network returns list (got {net_text_raw[:80]!r})",
            )
            print(f"  ✓ browser_network: {len(net_rows)} row(s)")
        except json.JSONDecodeError:
            # Large response was spilled — that's fine, just verify it starts with [
            assert_true(
                net_text_raw.startswith("["),
                f"browser_network starts with [ (got {net_text_raw[:80]!r})",
            )
            print("  ✓ browser_network: (large response, spilled)")

        # browser_detach all
        resp = b.call(
            "tools/call",
            {"name": "browser_detach", "arguments": {}},
            timeout=5.0,
        )
        detach_text = resp["result"]["content"][0]["text"]
        print(f"  ✓ browser_detach: {detach_text[:80]!r}")

        # Verify tabs now empty
        resp = b.call(
            "tools/call", {"name": "browser_tabs", "arguments": {}}, timeout=5.0
        )
        tabs_after_text = (
            resp["result"]["content"][0]["text"].split("\n[full output:")[0].strip()
        )
        assert_eq(
            tabs_after_text, "(no attached tabs)", "browser_tabs after detach is empty"
        )
        print("  ✓ browser_tabs empty after detach")
    finally:
        b.close()
