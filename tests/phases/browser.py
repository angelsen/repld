"""Phase 6: Tool registration, gist auto-reload, browser integration.

The browser checks run against the developer's own Chrome, so they are careful
to touch only tabs they created themselves. Every one carries `_MARKER` in its
URL, which is both how the watch pattern finds it and how `_close_marked_tabs`
cleans up — including on the failure path, so a broken run can't leave tabs
behind for the next one to trip over.
"""

import io
import json
import time
import urllib.request

from harness import Bridge, Kernel, assert_eq, assert_true

_CDP = "http://localhost:9222"

# Distinctive enough that a watch pattern built from it can't match anything of
# the developer's. A bare word on purpose: Chrome may hand the URL back
# percent-encoded (`%3Ctitle%3E…`), which would defeat matching on markup.
_MARKER = "repld-smoketest"
_TEST_URL = f"data:text/html,<title>{_MARKER}</title><p>{_MARKER}</p>"


def _chrome_ready(label: str) -> bool:
    """Whether the browser stack can run here at all. Prints why if not."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        print(f"  - {label}: websockets not installed (uv sync --extra browser), skip")
        return False
    try:
        with urllib.request.urlopen(f"{_CDP}/json/version", timeout=2) as r:
            r.read()
    except Exception:
        print(f"  - {label}: Chrome not available on port 9222, skipping")
        return False
    return True


def _close_marked_tabs() -> None:
    """Close every tab this phase opened. Best-effort; never raises.

    Matches on the URL rather than a target id: repld's short `9222:abc123`
    form keeps only 6 hex characters of Chrome's 32-char target id, so it
    cannot be expanded back into something `/json/close` accepts.
    """
    try:
        with urllib.request.urlopen(f"{_CDP}/json/list", timeout=2) as r:
            targets = json.load(r)
    except Exception:
        return
    for t in targets:
        if _MARKER not in t.get("url", ""):
            continue
        try:
            with urllib.request.urlopen(f"{_CDP}/json/close/{t['id']}", timeout=2) as r:
                r.read()
        except Exception:
            pass


def phase_6_png_resize(_kernel: Kernel) -> None:
    """Pure-function checks for browser/png.py — no kernel or Chrome needed."""
    from PIL import Image

    from repld.browser.png import _MAX_PX, _MAX_TOKENS, _model_dims, _resize_png

    for mode, kind in [("RGBA", "rgba"), ("P", "palette"), ("L", "grayscale")]:
        img = Image.new(mode, (40, 30), 0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        resized = _resize_png(buf.getvalue(), 10, 8)
        out = Image.open(io.BytesIO(resized))
        assert_eq(out.size, (10, 8), f"_resize_png resizes a {kind} PNG")
    print("  ✓ _resize_png: rgba/palette/grayscale PNGs all resize correctly")

    def _tok(px: int) -> int:
        return (px - 1) // 28 + 1

    for w, h in [(3000, 3000), (4000, 1000), (1000, 4000), (100, 100)]:
        tw, th = _model_dims(w, h)
        assert_true(tw <= _MAX_PX and th <= _MAX_PX, f"_model_dims({w},{h}) <= max px")
        assert_true(
            _tok(tw) * _tok(th) <= _MAX_TOKENS,
            f"_model_dims({w},{h}) within token budget",
        )
    assert_eq(
        _model_dims(100, 100), (100, 100), "_model_dims leaves small images alone"
    )
    print("  ✓ _model_dims: token-budget invariants hold across sizes")


def phase_6_har_redirects(_kernel: Kernel) -> None:
    """har_entries resolves a redirect chain hop-by-hop — no kernel or Chrome.

    Chrome fires Network.*ExtraInfo once per redirect hop under a single
    requestId. Aggregating those with MAX() GROUP BY picks lexicographically
    across hops, which reported a 302 -> 200 chain's *final* hop as 302 and
    pulled its headers from whichever hop's JSON sorted highest. Single-hop
    requests emit one event each and so never showed it — hence the chain here.
    """
    try:
        import duckdb
    except ImportError:
        print("  - har redirects: duckdb not installed (uv sync --extra browser), skip")
        return

    from repld.browser.har import _create_views

    def _entries(hops: list[tuple[int, str]]) -> list[tuple]:
        """Build a redirect chain and return (redirect_index, status, state)."""
        db = duckdb.connect(":memory:")
        db.execute(
            "CREATE TABLE events "
            "(event JSON, method VARCHAR, request_id VARCHAR, target VARCHAR)"
        )
        _create_views(db.execute)

        def ins(method: str, params: dict) -> None:
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                [
                    json.dumps({"method": method, "params": params}),
                    method,
                    params.get("requestId"),
                    "T",
                ],
            )

        for i, (status, cookie) in enumerate(hops):
            req: dict = {
                "requestId": "R",
                "wallTime": str(1000 + i),
                "timestamp": str(i + 1),
                "request": {"method": "GET", "url": f"https://x/{i}", "headers": {}},
                "type": "Document",
            }
            if i:
                req["redirectResponse"] = {"status": hops[i - 1][0]}
            ins("Network.requestWillBeSent", req)
            ins(
                "Network.requestWillBeSentExtraInfo",
                {"requestId": "R", "headers": {"Cookie": cookie}},
            )
            ins(
                "Network.responseReceivedExtraInfo",
                {"requestId": "R", "statusCode": status, "headers": {}},
            )
        final = hops[-1][0]
        ins(
            "Network.responseReceived",
            {
                "requestId": "R",
                "response": {
                    "status": final,
                    "statusText": "OK",
                    "headers": {},
                    "mimeType": "text/html",
                },
            },
        )
        ins(
            "Network.loadingFinished",
            {"requestId": "R", "timestamp": "9", "encodedDataLength": 10},
        )
        rows = db.execute(
            "SELECT redirect_index, status, state FROM har_summary "
            "ORDER BY redirect_index"
        ).fetchall()
        headers = db.execute(
            "SELECT request_headers FROM har_entries WHERE redirect_index = ? LIMIT 1",
            [len(hops) - 1],
        ).fetchone()
        return [rows, headers]

    # 302 -> 200: the final hop must report 200, not the chain's lexicographic max.
    rows, headers = _entries([(302, "a=1"), (200, "b=2")])
    assert_eq(
        [(r[0], r[1]) for r in rows],
        [(0, 302), (1, 200)],
        "redirect chain reports each hop's own status",
    )
    assert_eq(rows[-1][2], "complete", "final hop state is complete")
    assert_true(
        "b=2" in (headers[0] or ""),
        f"final hop carries its own request headers (got {headers[0]!r})",
    )

    # Final hop's cookie sorts *below* the first hop's — the case a MAX() over
    # header JSON silently got backwards.
    _rows, headers = _entries([(302, "z=1"), (200, "a=2")])
    assert_true(
        "a=2" in (headers[0] or ""),
        f"header hop resolution is positional, not lexicographic (got {headers[0]!r})",
    )

    # 307 -> 200, and a single-hop control that must be unaffected.
    rows, _headers = _entries([(307, "a=1"), (200, "b=2")])
    assert_eq(rows[-1][1], 200, "307 chain reports 200 on the final hop")
    rows, _headers = _entries([(200, "a=1")])
    assert_eq(
        [(r[0], r[1], r[2]) for r in rows],
        [(0, 200, "complete")],
        "single-hop request is unchanged",
    )
    print("  ✓ har_entries: redirect hops resolve status + headers per hop")


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


def phase_6_label_and_reattach(kernel: Kernel) -> None:
    """Label state survives Tab re-wrapping; ready-selector poll survives a
    document replacement mid-wait (regression: stale DOM.getDocument root).

    Requires Chrome with --remote-debugging-port=9222; skips gracefully.
    """
    if not _chrome_ready("phase 6 label/reattach"):
        return

    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        def _exec(code: str) -> str:
            resp = b.call(
                "tools/call",
                {"name": "exec", "arguments": {"code": code, "timeout": 20}},
                timeout=30.0,
            )
            return resp["result"]["content"][0]["text"]

        # Label set on one Tab wrapper must be visible on a fresh wrapper for
        # the same target (state lives on CDPSession, Tabs are ephemeral).
        out = _exec(
            "import asyncio\n"
            f"_t1 = await browser.open('data:text/html,<p>{_MARKER}-label</p>')\n"
            "_t1.label = 'phase6'\n"
            "await asyncio.sleep(0.5)\n"
            "_t2 = await browser.get(_t1.target_id)\n"
            "print('LABEL=' + repr(_t2.label))"
        )
        assert_true("LABEL='phase6'" in out, f"label survives re-get (got {out!r})")

        # Re-labelling via the fresh wrapper must replace the old bar, not
        # orphan it (the old wrapper lost _label_script_id before the fix).
        out = _exec(
            "_t2.label = 'phase6b'\n"
            "await asyncio.sleep(0.5)\n"
            "print('BARS=', await _t2.js("
            "\"document.querySelectorAll('#__repld_label_bar').length\"))"
        )
        assert_true("BARS= 1" in out, f"single label bar after re-label (got {out!r})")
        print("  ✓ label survives Tab re-wrap; re-label replaces, no orphaned bar")

        # Ready-selector poll: navigate (replacing the document) while the
        # poll is waiting for an element only the *next* document has. The
        # old DOM.getDocument root went stale here and never matched.
        out = _exec(
            f"_t3 = await browser.open('data:text/html,<p>{_MARKER}-one</p>')\n"
            "async def _nav():\n"
            "    await asyncio.sleep(0.5)\n"
            "    await _t3.cdp('Page.navigate',"
            f" url='data:text/html,<div id=\"late-el\">{_MARKER}-two</div>')\n"
            "_nt = asyncio.create_task(_nav())\n"
            "await _t3._await_ready_signal('#late-el', timeout=8)\n"
            "await _nt\n"
            "print('READY-OK')"
        )
        assert_true(
            "READY-OK" in out,
            f"ready-selector survives document replacement (got {out!r})",
        )
        print("  ✓ ready-selector poll survives mid-wait navigation")

        b.call("tools/call", {"name": "browser_detach", "arguments": {}}, timeout=5.0)
    finally:
        # In `finally`, not after the assertions: a failure above used to leave
        # both tabs open, and repeated failing runs pile them up.
        _close_marked_tabs()
        b.close()


def phase_6(kernel: Kernel) -> None:
    """Browser integration — requires Chrome with --remote-debugging-port=9222.

    Every assertion runs against a tab this phase opened itself. Watching `*`
    and asserting against whichever tab sorted first made the result depend on
    the developer's browser rather than on repld: the runtime scaled with their
    tab count, and a backgrounded or busy page failed `browser_js` on a 10s
    timeout while a fresh one passed.

    Skips gracefully if Chrome is not reachable.
    """
    if not _chrome_ready("phase 6"):
        return

    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # Verify browser tools are in the list — exact match, not a subset
        # check, so an added or removed tool actually fails this instead of
        # silently drifting out of sync with protocol.py's TOOLS.
        resp = b.call("tools/list")
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        browser_tools = {
            "browser_watch",
            "browser_detach",
            "browser_tabs",
            "browser_pages",
            "browser_js",
            "browser_network",
            "browser_request",
            "browser_body",
            "browser_click",
            "browser_type",
            "browser_console",
            "browser_screenshot",
            "browser_cdp",
            "browser_clear",
            "browser_controls",
            "browser_invoke",
            "browser_navigate",
            "browser_key",
            "browser_open",
            "browser_tree",
            "browser_fetch",
        }
        got_browser_tools = {n for n in tool_names if n.startswith("browser_")}
        assert_eq(
            got_browser_tools,
            browser_tools,
            f"tools/list contains exactly the {len(browser_tools)} browser tools",
        )
        print(f"  ✓ all {len(browser_tools)} browser tools in tools/list")

        def _tabs() -> list[str]:
            resp = b.call(
                "tools/call", {"name": "browser_tabs", "arguments": {}}, timeout=5.0
            )
            text = resp["result"]["content"][0]["text"]
            text = text.split("\n[full output:")[0].strip()
            if text == "(no attached tabs)":
                return []
            return [ln.strip() for ln in text.splitlines() if ln.strip()]

        # Register the pattern before the tab exists: this is the auto-attach
        # path for *future* matching tabs, which is what browser_open trips.
        resp = b.call(
            "tools/call",
            {"name": "browser_watch", "arguments": {"pattern": f"*{_MARKER}*"}},
            timeout=10.0,
        )
        result_text = resp["result"]["content"][0]["text"]
        assert_true(
            "attached" in result_text.lower(),
            f"browser_watch returned attach summary (got {result_text!r})",
        )
        print(f"  ✓ browser_watch: {result_text[:80]!r}")

        # Our own tab — Target.createTarget, so its identity is ours to assert.
        resp = b.call(
            "tools/call",
            {"name": "browser_open", "arguments": {"url": _TEST_URL}},
            timeout=20.0,
        )
        open_text = resp["result"]["content"][0]["text"]
        target_line = next(
            (ln for ln in open_text.splitlines() if ln.startswith("target: ")), None
        )
        assert_true(
            target_line is not None,
            f"browser_open reports the new tab's target (got {open_text[:120]!r})",
        )
        assert target_line is not None
        tab_target = target_line.split("target: ", 1)[1].strip()

        # Exactly one, and it is ours: deterministic because nothing else in
        # the browser can match the marker pattern.
        tab_lines = _tabs()
        assert_eq(len(tab_lines), 1, f"exactly our tab attached (got {tab_lines!r})")
        assert_true(
            tab_lines[0].startswith(tab_target),
            f"the attached tab is the one we opened (got {tab_lines[0]!r})",
        )
        print(f"  ✓ browser_open + auto-attach: 1 tab, target={tab_target!r}")

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

        # browser_js await semantics: multi-statement top-level await,
        # promise results auto-awaited (regression: replMode + awaitPromise)
        for code, expected, label in [
            ("await new Promise(r => setTimeout(r, 10)); 1 + 1", 2, "multi-stmt await"),
            ("(async () => 'iife')()", "iife", "async IIFE"),
            ("Promise.resolve(42)", 42, "bare promise"),
        ]:
            resp = b.call(
                "tools/call",
                {
                    "name": "browser_js",
                    "arguments": {"target": tab_target, "code": code},
                },
                timeout=10.0,
            )
            js_result = json.loads(resp["result"]["content"][0]["text"])
            assert_true(
                js_result.get("result") == expected,
                f"browser_js {label}: expected {expected!r}, got {js_result!r}",
            )
        print("  ✓ browser_js: top-level await, async IIFE, bare promise all resolve")

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

        assert_eq(_tabs(), [], "browser_tabs after detach is empty")
        print("  ✓ browser_tabs empty after detach")

        # The other half of watch: our tab is still open, so re-registering the
        # pattern must attach it *now* rather than waiting for a new one.
        resp = b.call(
            "tools/call",
            {"name": "browser_watch", "arguments": {"pattern": f"*{_MARKER}*"}},
            timeout=10.0,
        )
        again = _tabs()
        assert_eq(len(again), 1, f"watch re-attaches the open tab (got {again!r})")
        assert_true(
            again[0].startswith(tab_target),
            f"and it is the same tab (got {again[0]!r})",
        )
        print("  ✓ browser_watch attaches an already-open matching tab")

        b.call("tools/call", {"name": "browser_detach", "arguments": {}}, timeout=5.0)
    finally:
        _close_marked_tabs()
        b.close()
