"""Phase 6: Tool registration, gist auto-reload, browser integration.

The browser checks run against the developer's own Chrome, so they are careful
to touch only tabs they created themselves. Every one carries `_MARKER` in its
URL, which is both how the watch pattern finds it and how `_close_marked_tabs`
cleans up — including on the failure path, so a broken run can't leave tabs
behind for the next one to trip over.
"""

import asyncio
import io
import json
import re
import time
import urllib.request

from harness import Bridge, Kernel, assert_eq, assert_true

_CDP = "http://localhost:9222"

# Distinctive enough that a watch pattern built from it can't match anything of
# the developer's. A bare word on purpose: Chrome may hand the URL back
# percent-encoded (`%3Ctitle%3E…`), which would defeat matching on markup.
_MARKER = "repld-smoketest"
_TEST_URL = f"data:text/html,<title>{_MARKER}</title><p>{_MARKER}</p>"

# Selector-ranking fixture. Attribute values are deliberately unquoted (valid
# HTML5 while they contain no spaces) so the markup survives being nested three
# quoting levels deep — Python source → exec cell → JS string literal.
# The hidden member of each pair comes first in document order.
_SEL_HTML = (
    "<button id=hb style=display:none>Save</button>"
    "<button id=vb>Save</button>"
    "<label id=l1 for=i1 style=display:none>Name</label><input id=i1>"
    "<label id=l2 for=i2>Name</label><input id=i2>"
)
_SEL_CASES = [
    ("text=Save", "vb"),
    ('role=button[name="Save"]', "vb"),
    ("button:has-text('Save')", "vb"),
    ("label=Name", "i2"),
]


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


def phase_6_connect_race(_kernel: Kernel) -> None:
    """Concurrent connects collapse to one, and the fetch lock can't deadlock.

    No kernel or Chrome — both are pure asyncio shapes. Two parallel `browser_*`
    tool calls on a cold pool is the ordinary way to reach the first, and the
    second is a hazard the fix for the third introduced: `enable_fetch` holding
    a lock across `Fetch.enable` can be re-entered by
    `BrowserSession._reattach_core` when that command trips a reconnect.
    """
    from repld.browser import Browser, BrowserPool
    from repld.browser.cdp import CDPSession

    class _FakeWS:
        async def close(self) -> None: ...

    async def _connect_once() -> int:
        b = Browser(port=9999)
        calls: list[int] = []

        async def fake(*, discover: bool = True) -> None:
            calls.append(1)
            await asyncio.sleep(0.01)  # the real connect awaits a WS handshake
            b._session._ws = _FakeWS()

        b._session.connect = fake
        await asyncio.gather(*[b._ensure_connected() for _ in range(4)])
        return len(calls)

    assert_eq(
        asyncio.run(_connect_once()), 1, "4 concurrent _ensure_connected → 1 socket"
    )

    async def _pool_once() -> tuple[int, int]:
        # Patched on `repld.browser.pool`, the module `BrowserPool.connect`
        # actually resolves `Browser` through — not on the package `__init__`,
        # which re-exports it. Patching the re-export left the real class in
        # play and the spy never saw a call, which showed up as a genuine
        # connect attempt to port 9999.
        import repld.browser.pool as mod

        pool = BrowserPool()
        made: list[object] = []
        real = mod.Browser

        class _Spy(real):
            def __init__(self, port=None):
                super().__init__(port)
                made.append(self)

            async def _ensure_connected(self) -> None:
                await asyncio.sleep(0.01)
                self._connected = True

        mod.Browser = _Spy
        try:
            await asyncio.gather(*[pool.connect(9999) for _ in range(4)])
        finally:
            mod.Browser = real
        return len(made), len(pool._browsers)

    built, registered = asyncio.run(_pool_once())
    assert_eq(built, 1, "4 concurrent BrowserPool.connect → 1 Browser built")
    assert_eq(registered, 1, "and exactly one registered (no orphan holding a socket)")

    class _S(CDPSession):
        """CDPSession stand-in with no socket — real enable/disable_fetch.

        `on_enable` fires once, from inside `Fetch.enable`, so a test can
        reproduce a reconnect landing in the middle of an in-flight enable.
        """

        def __init__(self, on_enable=None) -> None:
            self._fetch_lock = asyncio.Lock()
            self._fetch_enabled = False
            self.capture_bodies = False
            self._fetch_handler = None
            self.chrome_target_id = "t"
            self._on_enable = on_enable

        async def execute(
            self, method: str, params: dict | None = None, timeout: float = 30
        ) -> dict:
            await asyncio.sleep(0.005)
            if method == "Fetch.enable" and self._on_enable is not None:
                hook, self._on_enable = self._on_enable, None  # one shot
                await hook(self)
            return {}

        async def send_nowait(self, method: str, params: dict | None = None) -> None:
            return None

    async def _interleave() -> tuple[bool, bool]:
        s = _S()
        # What `tab.capture_bodies = True; tab.capture_bodies = False` schedules.
        await asyncio.gather(s.enable_fetch(), s.disable_fetch())
        return s._fetch_enabled, s.capture_bodies

    enabled, capturing = asyncio.run(_interleave())
    assert_eq(
        enabled, capturing, "interleaved enable/disable leave the two flags agreeing"
    )

    async def _reentrant() -> bool:
        async def reconnect(s: "_S") -> None:
            # Precisely what _reattach_core does when this command's own socket
            # error triggers a reconnect — on this same task, lock still held.
            s._fetch_enabled = False
            await s._enable_fetch_core()

        s = _S(on_enable=reconnect)
        try:
            await asyncio.wait_for(s.enable_fetch(), timeout=2)
            return True
        except asyncio.TimeoutError:
            return False

    assert_true(
        asyncio.run(_reentrant()),
        "a reconnect re-entering an in-flight enable_fetch does not deadlock",
    )
    print("  ✓ connect races collapse to one socket; fetch lock is re-entry safe")


def phase_6_reattach_binding(_kernel: Kernel) -> None:
    """A reattached session re-registers the pill's gate binding.

    No Chrome — `_reattach_core`'s CDP traffic is all it needs to be judged on.

    Bindings are scoped to the session that added them: Chrome drops
    `window.__repld_resolve` from the page the moment that session detaches.
    A reattach caused by *navigation* recovers on its own, because the pill goes
    with the document and `Tab._heartbeat_loop` sees 'reload' and re-injects —
    but a WebSocket `_reconnect` reattaches every session while leaving the
    pages alone, so the heartbeat sees 'ok' and never re-injects. The pill then
    looks live and its buttons call an undefined function, having already
    cleared the gate from their own queue: a human gate that cannot be answered
    from the surface that is showing it, on a kernel where the pill may be the
    only surface there is.

    Both directions matter. The control is the second half — an unpinned tab
    must not be handed a binding it never asked for, since `_binding_handler`
    is what `_handle_event` dispatches on.
    """
    from repld.browser.session import BrowserSession

    class _Cdp:
        """CDPSession stand-in: only what _reattach_core touches."""

        def __init__(self, *, pinned: bool, label: str | None = None) -> None:
            self._session_id = "old-sid"
            self.chrome_target_id = "t1"
            self._inflight = {"req-1": 0.0}
            self._fetch_enabled = False
            self._binding_handler = (lambda *_a: None) if pinned else None
            self._label_text = label
            self._label_color = "#3b82f6"
            self._label_script_id = "dead-identifier"
            self.sent: list[str] = []

        async def _enable_domains(self) -> None:
            self.sent.append("_enable_domains")

        async def execute(self, method: str, params: dict | None = None) -> dict:
            self.sent.append(method)
            return {}

    async def _reattach(*, pinned: bool, label: str | None = None) -> "_Cdp":
        session = BrowserSession(port=9999)
        cdp = _Cdp(pinned=pinned, label=label)
        session._sessions["old-sid"] = cdp  # type: ignore[assignment]

        async def fake_execute(method, params=None, session_id=None, timeout=30):
            return {"sessionId": "new-sid"}

        session.execute = fake_execute  # type: ignore[assignment]
        await session._reattach_core(cdp)  # type: ignore[arg-type]
        return cdp

    pinned = asyncio.run(_reattach(pinned=True))
    assert_true(
        "Runtime.addBinding" in pinned.sent,
        "a reattached pinned session re-registers its gate binding",
    )
    assert_true(
        pinned.sent.index("_enable_domains") < pinned.sent.index("Runtime.addBinding"),
        "and does it after Runtime is re-enabled, not before",
    )
    plain = asyncio.run(_reattach(pinned=False))
    assert_eq(
        [m for m in plain.sent if m == "Runtime.addBinding"],
        [],
        "an unpinned session gets no binding it never had",
    )
    print("  ✓ reattach re-registers the pill's gate binding (and only when pinned)")

    # The label's addScriptToEvaluateOnNewDocument registration is scoped to the
    # session exactly as the binding is, and was the one piece of per-session
    # state `_reattach_core` didn't restore — the restore lived only in
    # `Tab._reattach`, the navigation/HMR path. It stayed hidden because the
    # failure is *delayed*: the bar is live DOM, so it survives on the current
    # document and silently never comes back on the next navigation, which is
    # the entire reason it is an on-new-document registration.
    labelled = asyncio.run(_reattach(pinned=False, label="Skantz Tools"))
    assert_true(
        "Page.addScriptToEvaluateOnNewDocument" in labelled.sent,
        f"a reattached labelled session re-registers its label bar "
        f"(sent {labelled.sent!r})",
    )
    unlabelled = asyncio.run(_reattach(pinned=False))
    assert_eq(
        [m for m in unlabelled.sent if m == "Page.addScriptToEvaluateOnNewDocument"],
        [],
        "an unlabelled session gets no label script it never had",
    )
    print("  ✓ reattach re-registers the label bar too (and only when labelled)")


def phase_6_input_visibility_guard(_kernel: Kernel) -> None:
    """click/tap/type_text/key/swipe raise a backgrounded tab before dispatching.

    No Chrome needed — and a live-Chrome version of this couldn't be made
    deterministic anyway, since it depends on real window-manager occlusion.
    The bug this guards: Chrome silently drops Input.dispatch*Event for a
    tab whose renderer is backgrounded (document.visibilityState ===
    "hidden") — the CDP command still acks, no exception, no handler on the
    page ever fires. DOM reads (getContentQuads, querySelector) work fine on
    a hidden tab, which is why it read as "click did nothing" rather than an
    error, and why the multi-window watch workflow this kernel exists for —
    N-1 tabs hidden at any time — hit it immediately. `_ensure_front` is the
    guard; this asserts every input-dispatching entry point actually calls
    it before dispatching, and that a visible tab is never needlessly raised.
    """
    from repld.browser.tab import Tab

    class _FakeSession:
        """CDPSession stand-in: records every command, answers Runtime.evaluate
        with a fixed visibility so _ensure_front's branch is controllable."""

        def __init__(self, visibility: str) -> None:
            self.visibility = visibility
            self.sent: list[str] = []

        async def execute(
            self, method: str, params: dict | None = None, timeout: float = 30
        ) -> dict:
            self.sent.append(method)
            if method == "Runtime.evaluate":
                return {"result": {"value": self.visibility}}
            return {}

    async def _center(*_a, **_kw) -> tuple[float, float]:
        return (1.0, 2.0)

    async def _node(*_a, **_kw) -> tuple[int, str]:
        return (0, "null")

    def _make_tab(visibility: str) -> tuple[Tab, "_FakeSession"]:
        session = _FakeSession(visibility)
        tab = Tab(session, "abcdef0123456789", port=9222)  # type: ignore[arg-type]
        # Bypass selector resolution (DOM domain) — not what this test judges.
        tab._element_center = _center  # type: ignore[method-assign]
        tab._wait_for_node = _node  # type: ignore[method-assign]
        return tab, session

    async def _run(name: str, visibility: str):
        tab, session = _make_tab(visibility)
        calls = {
            "click": lambda: tab.click("#x"),
            "tap": lambda: tab.tap("#x"),
            "type_text": lambda: tab.type_text("#x", "hi"),
            "key": lambda: tab.key("Enter"),
            "swipe": lambda: tab.swipe(0, 0, 10, 10, steps=1),
        }
        await calls[name]()
        return session.sent

    for name in ("click", "tap", "type_text", "key", "swipe"):
        sent = asyncio.run(_run(name, "hidden"))
        assert_true(
            "Page.bringToFront" in sent,
            f"{name}() raises a hidden tab before dispatching input (sent {sent!r})",
        )
        raise_idx = sent.index("Page.bringToFront")
        first_dispatch = next(
            (i for i, m in enumerate(sent) if m.startswith("Input.dispatch")), None
        )
        assert_true(
            first_dispatch is None or raise_idx < first_dispatch,
            f"{name}() raises before dispatching input, not after (sent {sent!r})",
        )
    print("  ✓ click/tap/type_text/key/swipe: raise a hidden tab before input")

    visible_sent = asyncio.run(_run("click", "visible"))
    assert_true(
        "Page.bringToFront" not in visible_sent,
        f"a visible tab is never raised (sent {visible_sent!r})",
    )
    print("  ✓ a visible tab isn't raised — the guard checks, doesn't always act")


def phase_6_offloop_writes(_kernel: Kernel) -> None:
    """Loop-owned state is reached *through* the loop by callers not on it.

    Two things sat on the same mistake, and neither needs Chrome — a loop on a
    thread and a CDPSession over a stub transport are enough.

    `bg.spawn(..., loop=)` exists precisely for the sync callers that are off
    the loop (`Tab`'s `capture_bodies` / `label` setters, which any pure-sync
    exec cell reaches through `asyncio.to_thread`) and used
    `loop.create_task`. From a foreign thread that appends the task's first
    step to the ready queue without the `_write_to_self()` that
    `call_soon_threadsafe` does, so an idle loop is never woken and the
    coroutine simply does not run. It passed unnoticed only because the
    watchdog probes the loop once a second — hence the *idle* loop here, with
    nothing else scheduled to wake it.

    `CDPSession.clear_events` wrote `_event_count` / `_next_prune_check` /
    `_inflight` from whatever thread called it, racing `store_event`'s
    `+=` on the loop. Asserted by blocking the loop: the reset must still be
    pending, which is the observable difference from writing them in place.
    """
    import threading

    from repld import bg
    from repld.browser.cdp import PRUNE_CHECK_INTERVAL, CDPSession

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    try:
        # Let the loop settle into its selector with no timers pending. Any
        # wakeup at all would mask the bug this asserts.
        time.sleep(0.2)

        ran = threading.Event()
        seen_name: list[str] = []

        async def _work() -> None:
            task = asyncio.current_task()
            seen_name.append(task.get_name() if task else "?")
            ran.set()

        handle = bg.spawn(_work(), name="repld-offloop-probe", loop=loop)
        assert_true(ran.wait(2.0), "bg.spawn(loop=) runs the coroutine on an idle loop")
        assert_eq(handle, None, "the off-loop spawn returns no handle to this thread")
        # An unnamed loop task is what kernel._pick_victim treats as fair game
        # when the watchdog escalates, so the name has to survive the hop.
        assert_eq(seen_name, ["repld-offloop-probe"], "the task keeps its repld- name")
        print("  ✓ bg.spawn(loop=) wakes an idle loop and keeps the task's name")

        session = CDPSession(
            send=None,
            session_id="s1",
            target_info={"targetId": "t" * 32, "type": "page"},
            port=9222,
            loop=loop,
        )
        try:
            for i in range(3):
                session.store_event({"method": "X", "params": {}}, "X", str(i))
            session._inflight["r1"] = time.monotonic()

            gate = threading.Event()
            loop.call_soon_threadsafe(gate.wait)  # hold the loop
            try:
                session.clear_events()
                assert_eq(
                    session.query("SELECT COUNT(*) FROM events")[0][0],
                    0,
                    "the delete does not wait on the loop (per-call cursor)",
                )
                assert_eq(
                    session._event_count,
                    3,
                    "the counter reset is queued on the loop, not written here",
                )
            finally:
                gate.set()

            deadline = time.monotonic() + 2.0
            while session._event_count != 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert_eq(session._event_count, 0, "counters reset once the loop runs")
            assert_eq(
                session._next_prune_check,
                PRUNE_CHECK_INTERVAL,
                "prune threshold reset with it",
            )
            assert_eq(session._inflight, {}, "_inflight cleared with it")

            # On the loop, it stays synchronous — browser_dispatch's
            # _run_sync_on_loop path must not become fire-and-forget.
            session.store_event({"method": "X", "params": {}}, "X", "9")

            async def _on_loop_clear() -> int:
                session.clear_events()
                return session._event_count

            assert_eq(
                asyncio.run_coroutine_threadsafe(_on_loop_clear(), loop).result(2),
                0,
                "an on-loop caller sees the reset immediately",
            )
            print("  ✓ clear_events resets loop-owned counters on the loop")
        finally:
            session.cleanup()
    finally:
        loop.call_soon_threadsafe(loop.stop)


def phase_6_capture_filter(_kernel: Kernel) -> None:
    """`_should_capture_body` skips what the HAR view calls an asset.

    Capture registers on `*` because Fetch.enable has no resource-type filter,
    so this predicate is the only thing standing between a page load and a full
    fetch+fulfill round trip per image, font, stylesheet and script. It has to
    agree with har.py's `is_asset` derivation: capture and query disagreeing
    about what an asset is means bodies stored for rows `tab.network()` hides
    by default, which is the expensive half of the mistake.
    """
    from repld.browser.capture import _should_capture_body

    def _params(status: int = 200, rtype: str = "XHR", ctype: str = "") -> dict:
        p: dict = {"responseStatusCode": status, "resourceType": rtype}
        if ctype:
            p["responseHeaders"] = [{"name": "Content-Type", "value": ctype}]
        return p

    for rtype in ("Image", "Font", "Stylesheet", "Media", "Script"):
        assert_true(
            not _should_capture_body(_params(rtype=rtype)),
            f"asset resourceType {rtype} is not captured",
        )
    # Chrome labels plenty of asset traffic XHR/Other; Content-Type is the
    # backstop, and mirrors the same mime markers har.py matches on.
    for ctype in ("image/png", "font/woff2", "text/css", "video/mp4", "audio/mpeg"):
        assert_true(
            not _should_capture_body(_params(ctype=ctype)),
            f"asset Content-Type {ctype} is not captured",
        )
    assert_true(
        not _should_capture_body(_params(status=302)),
        "redirects are not captured (getResponseBody errors on them)",
    )
    assert_true(
        not _should_capture_body(_params(ctype="text/event-stream")),
        "SSE is not captured (getResponseBody never returns)",
    )
    # The traffic the capture store exists for still goes through.
    for rtype, ctype in [
        ("XHR", "application/json"),
        ("Fetch", "application/json"),
        ("Document", "text/html"),
        ("Other", ""),
    ]:
        assert_true(
            _should_capture_body(_params(rtype=rtype, ctype=ctype)),
            f"{rtype}/{ctype or 'no content-type'} is still captured",
        )
    print("  ✓ _should_capture_body: assets skipped, API/document traffic kept")


def phase_6_like_escaping(_kernel: Kernel) -> None:
    """`*` is the only wildcard a URL filter offers — no kernel or Chrome.

    LIKE's own metacharacters have to be escaped out of the literal text, and
    `_` (matches any single character) is common enough in URLs that leaving it
    live made `network(url=...)` quietly over-match. Asserted against real
    DuckDB rather than on the pattern string, because the escaping is only worth
    anything paired with the ESCAPE clause on the comparison.
    """
    try:
        import duckdb
    except ImportError:
        print("  - like escaping: duckdb not installed (uv sync --extra browser), skip")
        return

    from repld.browser.tab_query import TabQueryMixin

    urls = [
        "/api/user_profile",
        "/api/userXprofile",
        "/api/user%profile",
        "/static/app.js",
    ]
    con = duckdb.connect()
    con.execute("CREATE TABLE u(url VARCHAR)")
    con.executemany("INSERT INTO u VALUES (?)", [(u,) for u in urls])

    def matches(filt: str) -> list[str]:
        sql = "SELECT url FROM u WHERE url LIKE ?" + TabQueryMixin._LIKE_ESCAPE
        pattern = TabQueryMixin._like_pattern(filt)
        return sorted(r[0] for r in con.execute(sql, [pattern]).fetchall())

    # `_` and `%` are literal; `*` still globs.
    assert_eq(matches("/api/user_profile"), ["/api/user_profile"], "literal underscore")
    assert_eq(matches("/api/user%profile"), ["/api/user%profile"], "literal percent")
    assert_eq(matches("*.js"), ["/static/app.js"], "* still globs")
    assert_eq(
        matches("user*profile"),
        ["/api/user%profile", "/api/userXprofile", "/api/user_profile"],
        "* spans all three",
    )
    # A backslash in the filter must not eat the character after it.
    con.execute(r"INSERT INTO u VALUES ('/api/a\b')")
    assert_eq(matches(r"/api/a\b"), [r"/api/a\b"], "literal backslash")
    print("  ✓ url filter: _ and % are literal, * globs, ESCAPE clause paired")


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

    def _entries(hops: list[tuple[int, str]]) -> tuple[list[tuple], tuple]:
        """Build a redirect chain, returning (rows, final-hop request_headers).

        rows is one (redirect_index, status, state) per hop, oldest first.
        """
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
        if headers is None:
            raise AssertionError(
                f"har_entries has no row for the final hop of {hops!r}"
            )
        return rows, headers

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

        # The registration is `addScriptToEvaluateOnNewDocument`, so surviving a
        # navigation is the whole reason it isn't a one-shot evaluate — and it
        # was the one case nothing asserted. That script runs at document
        # *start*, where `document.body` is still null, so the mount threw
        # before it appended anything and the bar never came back. It looked
        # fine because `runImmediately: True` had already mounted it into the
        # document that was live when the label was set.
        out = _exec(
            "await _t2.cdp('Page.navigate',"
            f" url='data:text/html,<p>{_MARKER}-navigated</p>')\n"
            "await _t2._await_ready_signal(\"document.readyState === 'complete'\")\n"
            "await asyncio.sleep(0.5)\n"
            "print('BARS=', await _t2.js("
            "\"document.querySelectorAll('#__repld_label_bar').length\"))\n"
            "print('TEXT=', await _t2.js("
            "\"(document.getElementById('__repld_label_bar')||{}).textContent\"))"
        )
        assert_true("BARS= 1" in out, f"label bar re-mounts after navigation ({out!r})")
        assert_true("TEXT= phase6b" in out, f"re-mounted bar keeps its text ({out!r})")
        print("  ✓ label bar re-mounts after a navigation (document-start body)")

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

        # Every custom selector form ranks visible matches over hidden ones.
        # `text=` was the only one that considered visibility at all, so
        # `text=Save` skipped the hidden button while `role=button[name="Save"]`
        # returned it and the click landed on nothing. Each pair below puts the
        # hidden match *first* in document order, so a form that ignores
        # visibility picks the wrong one.
        out = _exec(
            f"_t4 = await browser.open('data:text/html,<p>{_MARKER}-sel</p>')\n"
            f"await _t4.js({json.dumps(f'document.body.innerHTML = {json.dumps(_SEL_HTML)}')})\n"
            "from repld.browser.selector import resolve as _rs\n"
            f"for _s, _want in {_SEL_CASES!r}:\n"
            "    _got = await _t4.js('(' + _rs(_s).js + ' || {}).id')\n"
            "    print('SEL', _s, '->', _got, 'want', _want)\n"
        )
        for sel, want in _SEL_CASES:
            assert_true(
                f"SEL {sel} -> {want} want {want}" in out,
                f"{sel} prefers the visible match (got {out!r})",
            )
        print(f"  ✓ all {len(_SEL_CASES)} selector forms rank visible over hidden")

        b.call("tools/call", {"name": "browser_detach", "arguments": {}}, timeout=5.0)
    finally:
        # In `finally`, not after the assertions: a failure above used to leave
        # both tabs open, and repeated failing runs pile them up.
        _close_marked_tabs()
        b.close()


def phase_6_tab_close(kernel: Kernel) -> None:
    """Tab.close() actually closes the Chrome target, not just detaches it.

    detach() (covered by phase_6 below) drops repld's session but leaves the
    tab open in Chrome. close() goes further — verified against Chrome's own
    /json/list rather than repld's own bookkeeping, which a bug under test
    could satisfy by lying to itself.
    """
    if not _chrome_ready("phase 6 tab close"):
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

        out = _exec(
            f"_tc = await browser.open('data:text/html,<p>{_MARKER}-close</p>')\n"
            "print('CHROME_ID=' + _tc._chrome_target_id)\n"
            "await _tc.close()\n"
        )
        m = re.search(r"CHROME_ID=([0-9a-fA-F]+)", out)
        assert_true(
            m is not None, f"captured the closed tab's Chrome target id (got {out!r})"
        )
        assert m is not None
        chrome_id = m.group(1)

        with urllib.request.urlopen(f"{_CDP}/json/list", timeout=2) as r:
            targets = json.load(r)
        assert_true(
            all(t.get("id") != chrome_id for t in targets),
            f"target {chrome_id} no longer in Chrome's own target list",
        )
        print(
            "  ✓ tab.close() removes the target from Chrome, "
            "not just repld's bookkeeping"
        )
    finally:
        _close_marked_tabs()
        b.close()


def phase_6_key_native_activation(kernel: Kernel) -> None:
    """tab.key("Enter") / tab.key("Space") trigger native button activation.

    Reported live (2026-08-11): Input.dispatchKeyEvent without `text` fires the
    DOM keydown/keyup but Chromium never runs the native default action for a
    focused <button> — no click, no form submit — indistinguishable from the
    key doing nothing. Confirmed the fix needs `text: "\\r"` for Enter and
    `text: " "` for Space; every other named key (Escape, Tab, ArrowDown, ...)
    is unaffected since it has no native default action to trigger.
    """
    if not _chrome_ready("phase 6 key native activation"):
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

        html = (
            f"<title>{_MARKER}</title>"
            "<button id=b onclick=window.__clicks=(window.__clicks||0)+1>Go</button>"
        )
        out = _exec(
            f"_tk = await browser.open('data:text/html,{html}')\n"
            "await _tk.js('document.getElementById(\"b\").focus()')\n"
            "await _tk.key('Enter')\n"
            "print('ENTER', await _tk.js('window.__clicks || 0'))\n"
            "await _tk.js('document.getElementById(\"b\").focus()')\n"
            "await _tk.key('Space')\n"
            "print('SPACE', await _tk.js('window.__clicks || 0'))\n"
            "await _tk.js('document.getElementById(\"b\").focus()')\n"
            "await _tk.key('Escape')\n"
            "print('ESCAPE', await _tk.js('window.__clicks || 0'))\n"
        )
        assert_true(
            "ENTER 1" in out, f"key('Enter') triggers native click (got {out!r})"
        )
        assert_true(
            "SPACE 2" in out, f"key('Space') triggers native click (got {out!r})"
        )
        assert_true(
            "ESCAPE 2" in out, f"key('Escape') doesn't spuriously click (got {out!r})"
        )
        print("  ✓ key('Enter')/key('Space') trigger native button activation")
    finally:
        _close_marked_tabs()
        b.close()


def phase_6_shadow_dom_selectors(kernel: Kernel) -> None:
    """selector.py's text=/role=/label= forms pierce shadow DOM.

    Reproduces the chrome://extensions bug (2026-08-10): Lit/Polymer WebUI
    pages are built entirely of shadow roots, and the JS candidate builders in
    selector.py only ever queried the light DOM. Reuses `_SEL_HTML`/`_SEL_CASES`
    from the light-DOM ranking test above, nested two shadow roots deep so the
    fix has to actually recurse rather than just peek one level down.
    """
    if not _chrome_ready("phase 6 shadow DOM selectors"):
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

        build_js = (
            "(function() {"
            " var outer = document.createElement('div');"
            " document.body.appendChild(outer);"
            " var outerRoot = outer.attachShadow({mode: 'open'});"
            " var inner = document.createElement('div');"
            " outerRoot.appendChild(inner);"
            " var innerRoot = inner.attachShadow({mode: 'open'});"
            f" innerRoot.innerHTML = {json.dumps(_SEL_HTML)};"
            " })()"
        )
        out = _exec(
            f"_ts = await browser.open('data:text/html,<p>{_MARKER}-shadow</p>')\n"
            f"await _ts.js({json.dumps(build_js)})\n"
            "from repld.browser.selector import resolve as _rs\n"
            f"for _s, _want in {_SEL_CASES!r}:\n"
            "    _got = await _ts.js('(' + _rs(_s).js + ' || {}).id')\n"
            "    print('SHSEL', _s, '->', _got, 'want', _want)\n"
        )
        for sel, want in _SEL_CASES:
            assert_true(
                f"SHSEL {sel} -> {want} want {want}" in out,
                f"{sel} resolves through a nested shadow root (got {out!r})",
            )
        print(f"  ✓ all {len(_SEL_CASES)} selector forms pierce a nested shadow root")
    finally:
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


def phase_6_ready_classification(_kernel: Kernel) -> None:
    """`ready=` classifies selectors the way `click`/`type_text` do.

    No kernel or Chrome — this is `selector.looks_like_selector`, which exists
    because `Tab._await_ready_signal` used to hold a second opinion:
    `startswith((".", "#", "[", "data-"))`. Under that test a bare tag or a
    custom element took the JS branch, was evaluated as an identifier, came back
    a ReferenceError whose `result.value` is simply absent, and polled silently
    for the full 10 s — while `ready="#app"` on the same page worked. The two
    halves asserted together are the point: bare names must read as selectors
    *and* real expressions must still read as JS.
    """
    from repld.browser.selector import looks_like_selector, resolve

    selectors = [
        ".app-root",
        "#app",
        "[data-testid='root']",
        "data-ready",
        "main",  # bare tag — the regression
        "my-app",  # custom element — the regression
        "div",
        "text=Loaded",
        "role=button[name='Save']",
        "label=Username",
        "button:has-text('OK')",
    ]
    expressions = [
        "window.__ready",
        "app.isLoaded",
        "!!window.store",
        "document.readyState === 'complete'",
        "window.ready && window.ready()",
    ]
    misread = [s for s in selectors if not looks_like_selector(s)]
    assert_eq(misread, [], "every selector form reads as a selector")
    wrong = [e for e in expressions if looks_like_selector(e)]
    assert_eq(wrong, [], "JS expressions still read as JS, not as selectors")

    # And what a selector resolves to is querySelector-shaped, so the
    # `!!(...)` the ready poll wraps it in is a truthiness test on a node.
    assert_true(
        "querySelector" in resolve("main").js,
        f"a bare tag resolves through querySelector (got {resolve('main').js!r})",
    )
    print(
        f"  ✓ ready= classification: {len(selectors)} selector forms, "
        f"{len(expressions)} expressions, no crossover"
    )


def phase_6_since_time_base(_kernel: Kernel) -> None:
    """`since=` is epoch seconds on all four query methods.

    No kernel or Chrome — a real DuckDB with hand-written events is the whole
    apparatus, and it has to be real, because the bug was arithmetic against
    the stored column rather than anything in Python.

    The three source clocks disagree: `har_summary.last_activity` is epoch
    seconds, `Runtime.Timestamp` (console) is epoch *milliseconds*, and
    `Network.MonotonicTime` (sse, lifecycle) counts from an arbitrary origin.
    Comparing the caller's `time.time()` against each raw column meant
    `console(since=now)` matched everything — the exact opposite of the request,
    silently — while `sse`/`lifecycle` matched nothing.
    """
    import json as _json

    from repld.browser.cdp import CDPSession
    from repld.browser.tab import Tab

    # A wall clock and a monotonic clock 1000s apart, so a base confusion can't
    # coincidentally land on the right side of the cutoff.
    t0, mono0 = 1_700_000_000.0, 1_000.0
    cutoff = t0 + 50  # events at +10s are "old", at +90s are "new"

    session = CDPSession(
        send=None, session_id="s1", target_info={"targetId": "t1"}, port=9222
    )
    try:

        def store(method: str, params: dict) -> None:
            session.store_event(
                {"method": method, "params": params},
                method,
                params.get("requestId"),
            )

        for age, rid in ((10.0, "old"), (90.0, "new")):
            # A request pins the wall/monotonic offset and feeds har_summary.
            store(
                "Network.requestWillBeSent",
                {
                    "requestId": rid,
                    "wallTime": t0 + age,
                    "timestamp": mono0 + age,
                    "request": {"url": f"https://x.test/{rid}", "method": "GET"},
                    "type": "XHR",
                },
            )
            store(
                "Network.responseReceived",
                {
                    "requestId": rid,
                    "timestamp": mono0 + age,
                    "response": {"status": 200, "mimeType": "application/json"},
                },
            )
            store(
                "Network.loadingFinished",
                {"requestId": rid, "timestamp": mono0 + age, "encodedDataLength": 1},
            )
            # Console carries epoch milliseconds.
            store(
                "Runtime.consoleAPICalled",
                {
                    "type": "log",
                    "timestamp": (t0 + age) * 1000.0,
                    "args": [{"value": rid}],
                },
            )
            # SSE and lifecycle carry monotonic seconds.
            store(
                "Network.eventSourceMessageReceived",
                {
                    "requestId": rid,
                    "timestamp": mono0 + age,
                    "eventName": "tick",
                    "data": _json.dumps({"n": rid}),
                },
            )
            store("Page.lifecycleEvent", {"name": rid, "timestamp": mono0 + age})

        tab = Tab(session, "t1", 9222)

        # Every method: the +90s row is after the cutoff, the +10s row is not.
        for label, rows_all, rows_since in (
            ("network", tab.network(), tab.network(since=cutoff)),
            ("console", tab.console(), tab.console(since=cutoff)),
            ("sse", tab.sse(), tab.sse(since=cutoff)),
            ("lifecycle", tab.lifecycle(), tab.lifecycle(since=cutoff)),
        ):
            assert_eq(len(rows_all), 2, f"{label}(): both rows stored")
            assert_eq(
                len(rows_since),
                1,
                f"{label}(since=epoch_seconds) keeps only the newer row "
                f"(got {len(rows_since)} of {len(rows_all)})",
            )

        # A cutoff before everything keeps both; after everything keeps none.
        # This is what caught the monotonic views: subtracting the offset has to
        # move the bound with the cutoff, not just happen to exclude one row.
        for label, early, late in (
            ("network", tab.network(since=t0), tab.network(since=t0 + 500)),
            ("console", tab.console(since=t0), tab.console(since=t0 + 500)),
            ("sse", tab.sse(since=t0), tab.sse(since=t0 + 500)),
            ("lifecycle", tab.lifecycle(since=t0), tab.lifecycle(since=t0 + 500)),
        ):
            assert_eq(len(early), 2, f"{label}(since=before everything) keeps both")
            assert_eq(len(late), 0, f"{label}(since=after everything) keeps none")
    finally:
        session.cleanup()
    print("  ✓ since= is epoch seconds across network/console/sse/lifecycle")
