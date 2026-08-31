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
# The hidden member of each pair comes first in document order. The first
# label's *input* is hidden along with it (a hidden duplicate form section):
# accessible names come from the label association whether or not the label is
# rendered, so a visible input under a hidden label is a genuine second match
# — strict resolution would rightly call that ambiguous rather than rank it.
_SEL_HTML = (
    "<button id=hb style=display:none>Save</button>"
    "<button id=vb>Save</button>"
    "<label id=l1 for=i1 style=display:none>Name</label>"
    "<input id=i1 style=display:none>"
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
        except TimeoutError:
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
            self._injected = "stale-engine-handle"
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

    # The injected-engine handle is session-scoped the same way, but as cache
    # rather than registration: its objectId died with the old sessionId, so
    # _reattach_core drops it and the next selector call re-instantiates.
    assert_true(
        unlabelled._injected is None,
        "reattach invalidates the injected-engine handle",
    )
    print("  ✓ reattach drops the stale injected-engine handle")


class _EngineFakeSession:
    """CDPSession stand-in that answers the injected-engine protocol.

    Canned per-method responses; Runtime.callFunctionOn is answered by matching
    a distinctive substring of the functionDeclaration, so each engine call
    gets a shaped result without a page. Enough for click/tap/type_text to run
    end to end over a fake, which is what the kernel-less tests need.
    """

    def __init__(self, visibility: str = "visible", isolated_world: bool = True):
        self.visibility = visibility
        self.isolated_world = isolated_world
        self.bootstrap_evals = 0
        self.fail_call_fn_once: str | None = None  # error text to raise once
        self.fail_bootstrap = False
        self.sent: list[str] = []
        self.target_info = {"url": "fake://page", "targetId": "abcdef0123456789"}
        self._injected = None
        self._injected_lock = asyncio.Lock()
        self._frame_seq = 1
        self._dialog_log: list[dict] = []

    async def execute(
        self, method: str, params: dict | None = None, timeout: float = 30
    ) -> dict:
        self.sent.append(method)
        params = params or {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1"}}}
        if method == "Page.createIsolatedWorld":
            if not self.isolated_world:
                raise RuntimeError("Cannot create isolated world on this target")
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            if params.get("expression") == "document.visibilityState":
                return {"result": {"value": self.visibility}}
            # The engine bootstrap is the only other evaluate on these paths.
            self.bootstrap_evals += 1
            if self.fail_bootstrap:
                raise RuntimeError("evaluate refused on this target")
            return {"result": {"objectId": f"engine-{self.bootstrap_evals}"}}
        if method == "Runtime.callFunctionOn":
            if self.fail_call_fn_once:
                msg, self.fail_call_fn_once = self.fail_call_fn_once, None
                raise RuntimeError(msg)
            fn = params.get("functionDeclaration", "")
            if "strictModeViolationError" in fn:
                return {"result": {"objectId": "el-1"}}  # resolve → one match
            if "__repld_note" in fn:
                return {"result": {"value": 0}}
            if "checkElementStates" in fn:
                return {"result": {}}  # every state passes
            if "elementFromPoint" in fn:
                return {
                    "result": {
                        "value": {
                            "related": True,
                            "target": {"preview": "<button>", "sel": "#x"},
                            "hit": None,
                        }
                    }
                }
            return {"result": {"value": ""}}
        if method == "DOM.getContentQuads":
            return {"quads": [[0.0, 0.0, 2.0, 0.0, 2.0, 4.0, 0.0, 4.0]]}
        return {}


def phase_6_engine_world_tiers(_kernel: Kernel) -> None:
    """Injection tiers: isolated world, then main world, then a loud error.

    No Chrome — the fake session answers the engine's CDP protocol. The clean
    break means there is no legacy selector path left: when the utility world
    is refused the same bundle evaluates in the main world (identical
    semantics, no tamper isolation), and only when that fails too does the
    action error — never a silent downgrade to something weaker.
    """
    from repld.browser import inject
    from repld.browser.tab import Tab

    # Tier 1: isolated world.
    session = _EngineFakeSession()
    tab = Tab(session, "abcdef0123456789", port=9222)  # type: ignore[arg-type]
    handle = asyncio.run(inject.ensure_engine(tab))
    assert_eq(handle.world, "utility", "isolated world grants → utility tier")
    assert_true(
        "Page.createIsolatedWorld" in session.sent,
        "tier 1 goes through Page.createIsolatedWorld",
    )

    # Tier 2: isolated world refused → same engine, main world.
    session = _EngineFakeSession(isolated_world=False)
    tab = Tab(session, "abcdef0123456789", port=9222)  # type: ignore[arg-type]
    handle = asyncio.run(inject.ensure_engine(tab))
    assert_eq(handle.world, "main", "createIsolatedWorld refusal → main-world tier")
    assert_true(
        session.bootstrap_evals == 1,
        "the bundle still evaluates (main world, no contextId)",
    )
    print("  ✓ engine tiers: utility world first, main world when refused")

    # Both tiers dead → EngineUnavailable propagates from the action.
    session = _EngineFakeSession(isolated_world=False)
    session.fail_bootstrap = True
    tab = Tab(session, "abcdef0123456789", port=9222)  # type: ignore[arg-type]
    try:
        asyncio.run(tab.click("#x"))
        raise AssertionError("click on an uninjectable page must raise")
    except inject.EngineUnavailable as exc:
        assert_true(
            "tab.js()" in str(exc),
            f"the error names the escape hatches (got {exc})",
        )
    print("  ✓ both tiers failing is a loud EngineUnavailable, not a fallback")


def phase_6_stale_context_retry(_kernel: Kernel) -> None:
    """A stale engine handle re-ensures and retries exactly once.

    No Chrome. Navigation kills the engine's execution context; when the
    executionContextsCleared event hasn't landed yet, the next call fails with
    a stale-context CDP error. call_engine must invalidate, re-instantiate,
    and retry once — the same retry-once shape as Tab._exec.
    """
    from repld.browser import inject
    from repld.browser.tab import Tab

    session = _EngineFakeSession()
    tab = Tab(session, "abcdef0123456789", port=9222)  # type: ignore[arg-type]
    asyncio.run(inject.ensure_engine(tab))
    assert_eq(session.bootstrap_evals, 1, "engine bootstrapped once")

    session.fail_call_fn_once = "Cannot find context with specified id"
    result = asyncio.run(inject.call_engine(tab, "function() { return 1; }", []))
    assert_eq(session.bootstrap_evals, 2, "stale context → engine re-instantiated")
    assert_true("result" in result, "and the retried call succeeded")

    # A non-stale error propagates without a re-ensure.
    session.fail_call_fn_once = "Some other CDP failure"
    try:
        asyncio.run(inject.call_engine(tab, "function() { return 1; }", []))
        raise AssertionError("non-stale errors must propagate")
    except RuntimeError as exc:
        assert_true("Some other CDP failure" in str(exc), "verbatim propagation")
    assert_eq(session.bootstrap_evals, 2, "and no needless re-instantiation")
    print("  ✓ stale-context calls re-ensure the engine and retry exactly once")


def phase_6_selector_translation(_kernel: Kernel) -> None:
    """translate_fallbacks(): repld grammar → engine selectors.

    Pure functions, no kernel or Chrome. The `internal:` spellings are load-
    bearing: the public text= engine parses `"Save"s` as a literal string and
    matches nothing, and there is no public label engine at all — both
    verified against a live engine before this table was written.
    """
    from repld.browser.selector import translate_fallbacks

    def translate(sel):
        return translate_fallbacks(sel)[0]

    cases = [
        ("#app .btn", "css=#app .btn"),
        ("main", "css=main"),
        ("text=Save", 'internal:text="Save"s'),
        ("role=button", "role=button"),
        ('role=button[name="Update workflow"]', 'role=button[name="Update workflow"s]'),
        ("role=option[name*=Nor]", 'role=option[name*="Nor"s]'),
        ("role=link[name^=Home]", "role=link[name=/^Home/]"),
        ("role=link[name^=A+B]", "role=link[name=/^A\\+B/]"),
        ("label=Username", 'internal:label="Username"s'),
        ("aria-ref=f2e5", "aria-ref=f2e5"),
        ('text=He said "hi"', 'internal:text="He said \\"hi\\""s'),
        # Non-ASCII must pass through verbatim: json.dumps' default \u-escapes
        # read as literal characters to the engine's selector parser, so
        # `text=Pågår` matched nothing on a real Norwegian page.
        ("text=Pågår", 'internal:text="Pågår"s'),
        ("role=button[name='Pågår status.']", 'role=button[name="Pågår status."s]'),
        ("label=Fødselsdato", 'internal:label="Fødselsdato"s'),
        (
            "placeholder=Search or create",
            'internal:attr=[placeholder="Search or create"s]',
        ),
        (
            "testid=toolbar.add-status",
            'internal:testid=[data-testid="toolbar.add-status"s]',
        ),
        # Playwright locator calls, so the strict error's own `aka getBy…`
        # suggestions are pasteable back in. Mirrors locatorUtils.ts: default
        # is case-insensitive (`i`), exact: true flips to `s`.
        ("getByTestId('x.y')", 'internal:testid=[data-testid="x.y"s]'),
        (
            "getByRole('button', { name: 'Add status' })",
            'role=button[name="Add status"i]',
        ),
        (
            "getByRole('button', { name: 'Add', exact: true })",
            'role=button[name="Add"s]',
        ),
        ("getByRole('navigation')", "role=navigation"),
        ("getByText('Pågår')", 'internal:text="Pågår"i'),
        ("getByLabel('Username')", 'internal:label="Username"i'),
        ("getByPlaceholder('Search')", 'internal:attr=[placeholder="Search"i]'),
        ('getByText("He said \\"hi\\"")', 'internal:text="He said \\"hi\\""i'),
        ("locator('#demo-select')", "css=#demo-select"),
    ]
    for repld_form, engine_form in cases:
        assert_eq(translate(repld_form), engine_form, f"translate({repld_form!r})")
    for repld_form in ("text=Pågår", "button:has-text('Pågår')", "aria-ref=e1"):
        for form in translate_fallbacks(repld_form):
            assert_true("\\u" not in form, f"no \\u escapes in any form (got {form!r})")

    # :has-text distributes over every comma alternative of the role expansion.
    ht = translate("button:has-text('OK')")
    assert_true(
        ht.startswith("css=")
        and 'button:has-text("OK")' in ht
        and '[role="button"]:has-text("OK")' in ht,
        f"role expansion distributes :has-text (got {ht!r})",
    )

    # Retry forms widen, in order: text= falls back to exact aria-label.
    fb = translate_fallbacks("text=Save")
    assert_eq(
        fb, ['internal:text="Save"s', 'css=[aria-label="Save"]'], "text= retry form"
    )
    assert_eq(translate_fallbacks("#x"), ["css=#x"], "plain CSS has no retry form")

    # A chained/complex locator call errors with guidance instead of falling
    # through to css= and failing as nonsense.
    try:
        translate("getByRole('button').filter({ hasText: 'x' })")
        raise AssertionError("chained locator must not translate silently")
    except ValueError as exc:
        assert_true("simple quoted-argument" in str(exc), "chain error explains itself")
    print(f"  ✓ selector translation: {len(cases)} forms + has-text + retries + getBy")


def phase_6_request_compaction(_kernel: Kernel) -> None:
    """browser_request's default view caps cookie and long header values.

    Noise control, not redaction — repld's contract is the agent working with
    the user's real sessions, so full=true (and tab.request() in exec) return
    everything. What the cap removes is the ~150-line cookie/JWT block that
    dominated every request dump. Pure function, no kernel or Chrome.
    """
    import json as _json

    from repld.browser_dispatch import _compact_credentials

    jwt = "e" * 900
    entry = {
        "request": {
            "headers": {
                "Cookie": f"tenant.session.token={jwt}; theme=dark",
                "Authorization": "Bearer " + "t" * 500,
                "Accept": "application/json",
            },
            "cookies": [{"name": "tenant.session.token", "value": jwt}],
        },
        "response": {"headers": {"content-type": "text/html"}},
    }
    out = _compact_credentials(entry)
    dumped = _json.dumps(out)
    assert_true(jwt not in dumped, "long cookie value capped")
    assert_true("t" * 200 not in dumped, "long bearer token capped")
    assert_true(
        "tenant.session.token=" in out["request"]["headers"]["Cookie"],
        "cookie *names* stay visible",
    )
    assert_true(
        "theme=dark" in out["request"]["headers"]["Cookie"],
        "short cookie values untouched",
    )
    assert_eq(
        out["request"]["headers"]["Accept"],
        "application/json",
        "ordinary headers untouched",
    )
    assert_true("full=true" in dumped, "the cap says how to get the rest")
    assert_true(
        jwt in entry["request"]["headers"]["Cookie"],
        "the original entry is never mutated (tab.request() stays full)",
    )
    print("  ✓ browser_request compaction: names kept, values capped, original intact")


def phase_6_injected_source_provenance(_kernel: Kernel) -> None:
    """The vendored bundle carries its license, pin, and no node leakage.

    No kernel or Chrome. injected_source.py is generated (make injected) —
    this guards the properties a regeneration must preserve: the Apache-2.0
    header the license requires, a COMMIT that matches the build script's pin
    (a drifted pin means the bundle and its recorded provenance disagree),
    and a browser-only bundle (a require() of a node builtin would throw at
    injection time on every page).
    """
    import re as _re
    from pathlib import Path

    from repld.browser import injected_source

    assert_true(
        "Apache License" in (injected_source.__doc__ or ""),
        "generated module carries the Apache-2.0 header",
    )
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "build_injected.py"
    ).read_text()
    m = _re.search(r'PLAYWRIGHT_COMMIT = "([0-9a-f]{40})"', script)
    assert_true(m is not None, "build script declares a full-hash pin")
    assert_eq(
        injected_source.COMMIT,
        m.group(1),  # type: ignore[union-attr]
        "bundle COMMIT matches the build script's pin",
    )
    src = injected_source.SOURCE
    assert_true(len(src) > 100_000, "bundle is the real engine, not a stub")
    assert_true("InjectedScript" in src, "bundle exports InjectedScript")
    for builtin in ('require("fs")', "require('fs')", 'require("path")'):
        assert_true(builtin not in src, f"no node builtin leakage: {builtin}")
    print("  ✓ injected_source: Apache header, pin agreement, browser-only bundle")


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

    def _make_tab(visibility: str) -> tuple[Tab, "_EngineFakeSession"]:
        session = _EngineFakeSession(visibility=visibility)
        tab = Tab(session, "abcdef0123456789", port=9222)  # type: ignore[arg-type]
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
        tool_names = {t["name"] for t in resp["result"]["tools"]}
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
    document replacement mid-wait (regression: stale DOM.getDocument root);
    ready_confirmed distinguishes an explicit ready= from the readyState
    fallback and survives Tab re-wrapping the same way label does.

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

        # ready_confirmed: True only when an explicit ready= was polled and
        # satisfied; False for the bare readyState fallback. State lives on
        # CDPSession, so a hot re-query (no wait at all) preserves the
        # original attach's value rather than resetting on the fresh Tab.
        out = _exec(
            f"_rc1 = await browser.open('data:text/html,<p>{_MARKER}-rc1</p>')\n"
            "print('RC1=' + repr(_rc1.ready_confirmed))\n"
            "_rc2 = await browser.open("
            f"'data:text/html,<div id=\"rc-el\">{_MARKER}-rc2</div>',"
            " ready='#rc-el')\n"
            "print('RC2=' + repr(_rc2.ready_confirmed))\n"
            "_rc3 = await browser.get(_rc2.target_id)\n"
            "print('RC3=' + repr(_rc3.ready_confirmed))"
        )
        assert_true(
            "RC1=False" in out, f"no ready= -> ready_confirmed=False (got {out!r})"
        )
        assert_true(
            "RC2=True" in out, f"explicit ready= -> ready_confirmed=True (got {out!r})"
        )
        assert_true(
            "RC3=True" in out,
            f"re-query preserves ready_confirmed via CDPSession (got {out!r})",
        )
        print(
            "  ✓ ready_confirmed: False by default, True with ready=, survives re-query"
        )

        # Every custom selector form ranks visible matches over hidden ones —
        # each pair in _SEL_HTML puts the hidden match *first* in document
        # order, so a resolution that ignores visibility picks the wrong one.
        # Under the engine this is the strictness policy's ranking half:
        # multiple matches with exactly one visible resolve to it.
        out = _exec(
            f"_t4 = await browser.open('data:text/html,<p>{_MARKER}-sel</p>')\n"
            f"await _t4.js({json.dumps(f'document.body.innerHTML = {json.dumps(_SEL_HTML)}')})\n"
            "from repld.browser import inject as _inj\n"
            f"for _s, _want in {_SEL_CASES!r}:\n"
            "    _el = await _inj.resolve_element(_t4, _s)\n"
            "    _r = await _inj.call_engine(_t4, 'function(el){return el.id;}',"
            " [{'objectId': _el.object_id}])\n"
            "    print('SEL', _s, '->', _r['result']['value'], 'want', _want)\n"
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

        # Editing keys dispatch on windowsVirtualKeyCode, not the key/code
        # strings: without it Backspace fired keydown/keyup but deleted
        # nothing and arrows never moved the caret. Modifier combos ride the
        # modifiers bitmask ("Ctrl+A" select-all), and keys([...]) chains a
        # whole flow in one call.
        out = _exec(
            f"_te = await browser.open('data:text/html,<title>{_MARKER}</title>"
            "<input id=i value=abcdef>')\n"
            'await _te.js(\'const el = document.getElementById("i");'
            " el.focus(); el.setSelectionRange(6, 6)')\n"
            "await _te.key('Backspace')\n"
            "print('BKSP', await _te.js('document.getElementById(\"i\").value'))\n"
            "await _te.key('ArrowLeft')\n"
            "print('CARET', await _te.js('document.getElementById(\"i\").selectionStart'))\n"
            "await _te.keys(['Ctrl+A', 'Backspace'])\n"
            "print('CLEARED', repr(await _te.js('document.getElementById(\"i\").value')))\n"
            "await _te.keys(['x', 'y', 'z'])\n"
            "print('TYPED', await _te.js('document.getElementById(\"i\").value'))\n"
        )
        assert_true("BKSP abcde" in out, f"Backspace deletes (got {out!r})")
        assert_true("CARET 4" in out, f"ArrowLeft moves the caret (got {out!r})")
        assert_true("CLEARED ''" in out, f"Ctrl+A then Backspace clears (got {out!r})")
        assert_true("TYPED xyz" in out, f"keys() types characters (got {out!r})")
        print("  ✓ Backspace/arrows edit, Ctrl+A selects, keys() sequences")
    finally:
        _close_marked_tabs()
        b.close()


def phase_6_shadow_dom_selectors(kernel: Kernel) -> None:
    """The engine's selector forms pierce shadow DOM, ranked visible-first.

    Reproduces the chrome://extensions bug (2026-08-10): Lit/Polymer WebUI
    pages are built entirely of shadow roots. Reuses `_SEL_HTML`/`_SEL_CASES`
    from the light-DOM ranking test, nested two shadow roots deep so the
    resolution has to actually recurse rather than just peek one level down —
    the engine's css/role/text engines pierce open shadow roots natively, and
    this is the check that keeps that true across bundle bumps.
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
            "from repld.browser import inject as _inj\n"
            f"for _s, _want in {_SEL_CASES!r}:\n"
            "    _el = await _inj.resolve_element(_ts, _s)\n"
            "    _r = await _inj.call_engine(_ts, 'function(el){return el.id;}',"
            " [{'objectId': _el.object_id}])\n"
            "    _got = _r['result']['value']\n"
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
            "browser_select",
            "browser_hover",
            "browser_drag",
            "browser_console",
            "browser_screenshot",
            "browser_cdp",
            "browser_clear",
            "browser_controls",
            "browser_invoke",
            "browser_dismiss_dialog",
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
    from repld.browser.selector import looks_like_selector, translate_fallbacks

    def translate(sel):
        return translate_fallbacks(sel)[0]

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
        "aria-ref=e12",
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

    # And a bare tag translates as CSS for the engine, so the existence poll
    # queries it rather than evaluating it as an identifier.
    assert_eq(
        translate("main"), "css=main", "a bare tag translates to a css engine query"
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


class _BridgeHarness:
    """Initialize-and-exec boilerplate shared by the engine's Chrome tests."""

    def __init__(self, kernel: Kernel) -> None:
        self.b = Bridge(kernel.cwd)
        self.b.call("initialize", {"protocolVersion": "2024-11-05"})
        self.b.send("notifications/initialized", {}, notif=True)

    def exec(self, code: str, timeout: float = 20) -> str:
        resp = self.b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": code, "timeout": timeout}},
            timeout=timeout + 10,
        )
        return resp["result"]["content"][0]["text"]

    def tool(self, name: str, args: dict, timeout: float = 30) -> dict:
        """Raw response — callers pick result or error by what they assert."""
        return self.b.call(
            "tools/call", {"name": name, "arguments": args}, timeout=timeout
        )

    def open_tab(self, html: str) -> str:
        """browser_open a data: URL (marker appended), return its target id."""
        url = f"data:text/html,{html}<i>{_MARKER}</i>"
        resp = self.tool("browser_open", {"url": url})
        text = resp["result"]["content"][0]["text"]
        m = re.search(r"target: (\S+)", text)
        assert m, f"browser_open observation carries no target ({text[:200]!r})"
        return m.group(1)

    def close(self) -> None:
        _close_marked_tabs()
        self.b.close()


def phase_6_strict_violation(kernel: Kernel) -> None:
    """Ambiguous selectors error with a candidate digest; ranked cases still pass.

    The first half is the Jira-session failure mode: two visible "Save"
    buttons used to be resolved silently (first match wins), and the misclick
    cost a screenshot round-trip to notice. The second half is the compat
    guard on the strictness policy: a hidden+visible pair is *not* ambiguous —
    visibility ranks, and the receipt carries the "[2 matches, 1 visible]"
    note instead of an error.
    """
    if not _chrome_ready("phase 6 strict violation"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab("<button id=s1>Save</button><button id=s2>Save</button>")
        resp = h.tool("browser_click", {"target": tid, "selector": "text=Save"})
        msg = resp.get("error", {}).get("message", "")
        assert_true(
            "resolved to 2 elements" in msg and "s1" in msg and "s2" in msg,
            f"two visible matches error with both candidates (got {msg[:200]!r})",
        )
        print("  ✓ ambiguous selector fails loudly with a candidate digest")

        tid = h.open_tab(_SEL_HTML)
        resp = h.tool("browser_click", {"target": tid, "selector": "text=Save"})
        text = resp["result"]["content"][0]["text"]
        first = text.splitlines()[0]
        assert_true(
            first.startswith("clicked:") and "vb" in first,
            f"hidden+visible pair resolves to the visible one (got {first!r})",
        )
        assert_true(
            "[2 matches, 1 visible]" in first,
            f"and the receipt says the ranking happened (got {first!r})",
        )
        print("  ✓ visible-over-hidden ranking survives, announced in the receipt")
    finally:
        h.close()


def phase_6_click_receipt(kernel: Kernel) -> None:
    """Every click reports what it hit; an intercepted click warns, same call.

    The receipt is taken pre-dispatch (the click's own handlers may re-render
    the DOM) and the intercepted click still dispatches — repld observes and
    reports, it does not refuse.
    """
    if not _chrome_ready("phase 6 click receipt"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab(
            '<button id=tgt onclick="window.hit=(window.hit||0)+1">Buy</button>'
        )
        resp = h.tool("browser_click", {"target": tid, "selector": "#tgt"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith("clicked:") and "tgt" in first,
            f"receipt names the element hit (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\nprint('HIT', await _t.js('window.hit'))"
        )
        assert_true("HIT 1" in out, f"and the click actually landed (got {out!r})")

        h.exec(
            f"_t = await browser.get({tid!r})\n"
            "await _t.js(\"const d = document.createElement('div'); d.id = 'ov';"
            " d.style.cssText = 'position:fixed;inset:0';"
            ' document.body.appendChild(d)")'
        )
        # Occluded left-click: the coordinate path would hit the overlay (or
        # worse, start a drag on a pan layer), so it switches to
        # element.click() — the receipt names the path and the interceptor,
        # and the click actually lands. Found live: an accessible button list
        # rendered under an SVG diagram was resolvable but never
        # coordinate-clickable.
        resp = h.tool("browser_click", {"target": tid, "selector": "#tgt"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith("clicked via element.click():")
            and "ov" in first
            and "occluded" in first,
            f"an occluded click falls back to element.click() (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\nprint('HIT', await _t.js('window.hit'))"
        )
        assert_true("HIT 2" in out, f"and the fallback click landed (got {out!r})")
        # Modified clicks have no element.click() equivalent — they keep the
        # coordinate dispatch and the warning.
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "_r = await _t.click('#tgt', button='right')\n"
            "print('RECEIPT', _r, '| warning', _r.warning)"
        )
        assert_true(
            "RECEIPT warning:" in out and "| warning True" in out,
            f"a modified occluded click still warns (got {out!r})",
        )
        print(
            "  ✓ click receipts: named target; occlusion falls back to element.click()"
        )
    finally:
        h.close()


def phase_6_dialog_policy(_kernel: Kernel) -> None:
    """`_handle_dialog`'s accept/reject decision, in isolation — no Chrome.

    A real confirm()/beforeunload dialog is flaky to trigger deterministically
    (beforeunload in particular needs a prior user gesture Chrome won't grant
    headlessly), so the policy itself is exercised directly against a fake
    CDPSession, and only the end-to-end hang-fix is proven live below.
    """

    class _Cdp:
        def __init__(self, *, pinned: bool = False, guard_unload: bool = True) -> None:
            self.port = 9222
            self.chrome_target_id = "abcdef123456"
            self._pinned = pinned
            self._pin_guard_unload = guard_unload
            self._dialog_policy: dict | None = None
            self._dialog_log: list[dict] = []
            self.sent: list[dict] = []

        async def execute(self, method: str, params: dict | None = None) -> dict:
            self.sent.append(params or {})
            return {}

    from repld.browser.cdp import _handle_dialog

    async def _run() -> None:
        # alert: only one option, always accepted.
        cdp = _Cdp()
        await _handle_dialog(cdp, {"type": "alert", "message": "Done"})  # type: ignore[arg-type]
        assert_eq(cdp.sent[-1], {"accept": True}, "alert() accepts")
        assert_eq(cdp._dialog_log[-1]["source"], "auto", "alert logged as auto")

        # confirm: rejected by default.
        cdp = _Cdp()
        await _handle_dialog(cdp, {"type": "confirm", "message": "Sure?"})  # type: ignore[arg-type]
        assert_eq(cdp.sent[-1], {"accept": False}, "confirm() rejects by default")

        # prompt: rejected by default.
        cdp = _Cdp()
        await _handle_dialog(cdp, {"type": "prompt", "message": "Name?"})  # type: ignore[arg-type]
        assert_eq(cdp.sent[-1], {"accept": False}, "prompt() rejects by default")

        # beforeunload: accepted when not pinned.
        cdp = _Cdp()
        await _handle_dialog(cdp, {"type": "beforeunload", "message": ""})  # type: ignore[arg-type]
        assert_eq(cdp.sent[-1], {"accept": True}, "beforeunload accepts when unpinned")

        # beforeunload: rejected on a pin with guard_unload=True.
        cdp = _Cdp(pinned=True, guard_unload=True)
        await _handle_dialog(cdp, {"type": "beforeunload", "message": ""})  # type: ignore[arg-type]
        assert_eq(
            cdp.sent[-1], {"accept": False}, "beforeunload rejects — pin's guard wins"
        )

        # beforeunload: still accepted on a pin with guard_unload=False.
        cdp = _Cdp(pinned=True, guard_unload=False)
        await _handle_dialog(cdp, {"type": "beforeunload", "message": ""})  # type: ignore[arg-type]
        assert_eq(
            cdp.sent[-1],
            {"accept": True},
            "beforeunload accepts — guard_unload=False opts out",
        )

        # A pre-armed policy overrides the default once, then is consumed.
        cdp = _Cdp()
        cdp._dialog_policy = {"accept": True, "promptText": "Bob"}
        await _handle_dialog(cdp, {"type": "prompt", "message": "Name?"})  # type: ignore[arg-type]
        assert_eq(
            cdp.sent[-1],
            {"accept": True, "promptText": "Bob"},
            "pre-armed policy accepts with the given prompt text",
        )
        assert_eq(cdp._dialog_policy, None, "pre-arm is consumed after one dialog")
        assert_eq(cdp._dialog_log[-1]["source"], "pre-armed", "logged as pre-armed")

    asyncio.run(_run())
    print("  ✓ dialog policy: alert/confirm/prompt defaults, pin guard, pre-arm")


def phase_6_dialog(kernel: Kernel) -> None:
    """A click that opens a native confirm() completes instantly instead of
    hanging for the watchdog's full timeout — the actual regression this
    covers — and the reject/pre-arm/accept round trip lands on the real page.
    """
    if not _chrome_ready("phase 6 dialog"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab(
            "<button id=del onclick=\"if(confirm('Delete?'))"
            " this.textContent='Deleted'\">Delete</button>"
            "<button id=al onclick=\"alert('Done');"
            " this.textContent='Alerted'\">Alert</button>"
        )

        # confirm() rejects by default — the call errors, naming the dialog,
        # instead of returning a receipt for an action the page declined.
        started = time.monotonic()
        resp = h.tool("browser_click", {"target": tid, "selector": "#del"})
        elapsed = time.monotonic() - started
        assert_true(
            elapsed < 5, f"click returned promptly, not after a hang ({elapsed:.1f}s)"
        )
        msg = resp.get("error", {}).get("message", "")
        assert_true(
            "confirm" in msg and "Delete?" in msg and "browser_dismiss_dialog" in msg,
            f"rejected confirm() errors, naming the dialog (got {msg!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('TEXT', await _t.js('document.getElementById(\"del\").textContent'))"
        )
        assert_true(
            "TEXT Delete" in out,
            f"declined confirm() left the page unchanged ({out!r})",
        )

        # Pre-arm accept, then repeat — this time confirm() returns true.
        resp = h.tool("browser_dismiss_dialog", {"target": tid, "accept": True})
        assert_true(
            "accept" in resp["result"]["content"][0]["text"],
            "pre-arm call confirms the armed outcome",
        )
        resp = h.tool("browser_click", {"target": tid, "selector": "#del"})
        text = resp["result"]["content"][0]["text"]
        assert_true(
            text.splitlines()[0].startswith("clicked:"),
            f"pre-armed accept returns a normal receipt (got {text[:200]!r})",
        )
        assert_true(
            "dialog: confirm 'Delete?' → accepted (pre-armed)" in text,
            f"observation names the pre-armed accept (got {text[:400]!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('TEXT', await _t.js('document.getElementById(\"del\").textContent'))"
        )
        assert_true(
            "TEXT Deleted" in out, f"accepted confirm() let the page proceed ({out!r})"
        )

        # alert() has no other option — always accepts, never errors.
        resp = h.tool("browser_click", {"target": tid, "selector": "#al"})
        text = resp["result"]["content"][0]["text"]
        assert_true(
            text.splitlines()[0].startswith("clicked:"),
            f"alert() click returns a normal receipt (got {text[:200]!r})",
        )
        assert_true(
            "dialog: alert 'Done' → accepted (auto)" in text,
            f"observation names the auto-accepted alert (got {text[:400]!r})",
        )
        print("  ✓ dialog: confirm rejects + errors, pre-arm accepts, alert is silent")

        # The same guarantee applies to a raw tab.click() from exec/gists,
        # not just the browser_click MCP wrapper — the gap Tab._ensure_front's
        # own dialog check (not just browser_dispatch's) exists to close.
        out = h.exec(f"_t = await browser.get({tid!r})\nawait _t.click('#del')")
        assert_true(
            "confirm" in out and "Delete?" in out and "browser_dismiss_dialog" in out,
            f"raw tab.click() also raises on a rejected confirm() (got {out[:400]!r})",
        )
        h.tool("browser_dismiss_dialog", {"target": tid, "accept": True})
        out = h.exec(
            f"_t = await browser.get({tid!r})\nprint((await _t.click('#del')).line)"
        )
        assert_true(
            "clicked:" in out,
            f"pre-armed accept via raw tab.click() returns normally (got {out[:400]!r})",
        )
        print("  ✓ dialog: raw tab.click() (exec/gist path) carries the same guarantee")
    finally:
        h.close()


def phase_6_click_arrival(kernel: Kernel) -> None:
    """click() dispatches a mouseMoved arrival before mousePressed/
    mouseReleased.

    Found live: on a Jira SVG diagram editor, mousePressed/mouseReleased
    with no preceding move dispatched pointerdown/mousedown/pointerup/
    mouseup but never native `click` — confirmed via direct instrumentation
    that no JS-observable cause explains it (pressed-element identity and
    DOM membership held throughout, no position shift, no
    preventDefault()/setPointerCapture() anywhere in the chain). Matches
    Playwright's own crInput.ts, which routes a plain move through its
    DragManager but skips that specifically for a click's move ("click
    relies on move-down-up protocol commands being sent synchronously") —
    Chromium's native drag-detection state machine, above the DOM event
    model, can preempt click invisibly to any JS instrumentation. This
    asserts the dispatch order directly (what's actually in repld's
    control) rather than trying to reproduce the Chromium-internal
    consequence, which needs page-specific drag machinery no offline
    fixture engages.
    """
    if not _chrome_ready("phase 6 click arrival"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab(
            "<button id=btn>Press me</button>"
            "<script>window.order=[];"
            "for (const t of ['mousemove','pointermove','mousedown','pointerdown'])"
            " document.addEventListener(t, () => window.order.push(t),"
            " {capture:true});"
            "</script>"
        )
        resp = h.tool("browser_click", {"target": tid, "selector": "#btn"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(first.startswith("clicked:"), f"click receipt (got {first!r})")
        out = h.exec(
            f"_t = await browser.get({tid!r})\nprint('ORDER', await _t.js('window.order'))"
        )
        m = re.search(r"ORDER \[(.*)\]", out)
        assert_true(m is not None, f"event order captured (got {out!r})")
        order = [s.strip(" '\"") for s in m.group(1).split(",")] if m else []
        assert_true(
            order and order[0] in ("mousemove", "pointermove"),
            f"the mouse arrives before any press event (got {order!r})",
        )
        assert_true(
            "mousedown" in order and order.index("mousedown") > 0,
            f"press comes strictly after arrival (got {order!r})",
        )
        print("  ✓ click() arrives (mouseMoved) before pressing")
    finally:
        h.close()


def phase_6_actionability(kernel: Kernel) -> None:
    """Input waits for visible/enabled/stable — and fails naming the miss.

    A disabled control errors inside the bounded wait instead of clicking a
    dead element; a control enabled 500 ms later is waited for and clicked.
    """
    if not _chrome_ready("phase 6 actionability"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab(
            "<button id=dead disabled>Go</button>"
            '<button id=slow disabled onclick="window.went=1">Go slow</button>'
        )
        resp = h.tool("browser_click", {"target": tid, "selector": "#dead"})
        msg = resp.get("error", {}).get("message", "")
        assert_true(
            "not enabled" in msg,
            f"a disabled control errors naming the missing state (got {msg!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            'await _t.js("setTimeout(() =>'
            " document.getElementById('slow').disabled = false, 500)\")\n"
            "_r = await _t.click('#slow')\n"
            "print('RECEIPT', _r)\n"
            "print('WENT', await _t.js('window.went'))"
        )
        assert_true(
            "RECEIPT clicked:" in out and "WENT 1" in out,
            f"a 500ms-delayed enable is waited out and clicked (got {out!r})",
        )
        print("  ✓ actionability: disabled fails loudly, delayed-enable is waited for")
    finally:
        h.close()


def phase_6_react_controlled_input(kernel: Kernel) -> None:
    """type_text lands text in a framework-controlled input via the fallback.

    The fixture hand-rolls react-dom's behavior: trusted input events are
    reverted (the app owns the value), synthetic ones accepted into app state.
    Raw keystrokes therefore change nothing — the signature type_text detects
    before switching to the native-setter + synthetic-events path, which works
    cross-world because instance-level descriptor patches don't cross worlds.
    """
    if not _chrome_ready("phase 6 react controlled input"):
        return
    h = _BridgeHarness(kernel)
    try:
        fixture = (
            "<input id=ri><script>"
            "const el = document.getElementById('ri');"
            "window.appState = '';"
            "const desc = Object.getOwnPropertyDescriptor("
            "HTMLInputElement.prototype, 'value');"
            "el.addEventListener('input', (e) => {"
            " if (e.isTrusted) desc.set.call(el, window.appState);"
            " else window.appState = el.value; });"
            "</script>"
        )
        tid = h.open_tab(fixture)
        resp = h.tool(
            "browser_type",
            {"target": tid, "selector": "#ri", "text": "styrbord"},
        )
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            "native-setter fallback" in first,
            f"receipt says the fallback path was taken (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('STATE', await _t.js('window.appState'))"
        )
        assert_true(
            "STATE styrbord" in out,
            f"the app's own state saw the text (got {out!r})",
        )
        # Clearing is the same fallback path in miniature: typing "" dispatches
        # zero keystrokes, so the value is unchanged — the exact signature the
        # verify step reads as "controlled input swallowed it" — and the
        # native-setter fallback writes the empty string. This was a bug worth
        # its own guard pre-engine: selectAll-then-type-nothing left the old
        # value selected but intact.
        resp = h.tool("browser_type", {"target": tid, "selector": "#ri", "text": ""})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('CLEARED', repr(await _t.js(\"document.getElementById('ri').value\")),"
            " repr(await _t.js('window.appState')))"
        )
        assert_true(
            "CLEARED '' ''" in out,
            f"type_text('') clears the field and the app state (got {out!r})",
        )
        assert_true(
            "fallback" in first,
            f"and the receipt says the fallback did it (got {first!r})",
        )
        print("  ✓ controlled input: fallback fires, app state follows, clear works")
    finally:
        h.close()


def phase_6_select_option(kernel: Kernel) -> None:
    """browser_select drives native <select> and custom listboxes; a miss lists options."""
    if not _chrome_ready("phase 6 select option"):
        return
    h = _BridgeHarness(kernel)
    try:
        # The custom half is react-select-shaped on purpose: the chosen value
        # renders into a sibling inside the [role=combobox] container, never
        # into any input's .value — which is exactly what the verify step has
        # to read, or a visibly-landed selection reports "not verified".
        tid = h.open_tab(
            '<select id=sel onchange="window.picked=this.value">'
            "<option>Norway</option><option>Sweden</option></select>"
            "<div role=combobox>"
            "<button id=dd onclick=\"document.getElementById('lb')"
            ".style.display='block'\">Choose city</button>"
            "<span id=val></span>"
            "</div>"
            "<div id=lb role=listbox style=display:none>"
            '<div role=option onclick="window.city=this.textContent;'
            "document.getElementById('val').textContent=this.textContent\">Oslo</div>"
            '<div role=option onclick="window.city=this.textContent;'
            "document.getElementById('val').textContent=this.textContent\">Bergen</div>"
            "</div>"
        )
        resp = h.tool(
            "browser_select", {"target": tid, "selector": "#sel", "option": "Sweden"}
        )
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith('selected "Sweden"'),
            f"native select receipt (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('PICKED', await _t.js('window.picked'))"
        )
        assert_true(
            "PICKED Sweden" in out,
            f"native select fired change into the app (got {out!r})",
        )

        resp = h.tool(
            "browser_select", {"target": tid, "selector": "#dd", "option": "Bergen"}
        )
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith('selected "Bergen"'),
            f"custom listbox receipt (got {first!r})",
        )
        assert_true(
            "not verified" not in first,
            f"a value rendered in the combobox container verifies (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('CITY', await _t.js('window.city'))"
        )
        assert_true("CITY Bergen" in out, f"the option click landed (got {out!r})")

        resp = h.tool(
            "browser_select",
            {"target": tid, "selector": "#dd", "option": "Trondheim"},
        )
        msg = resp.get("error", {}).get("message", "")
        assert_true(
            "Oslo" in msg and "Bergen" in msg,
            f"a miss lists the visible options (got {msg[:200]!r})",
        )
        print("  ✓ select_option: native, custom listbox, and an actionable miss")
    finally:
        h.close()


def phase_6_aria_ref_roundtrip(kernel: Kernel) -> None:
    """browser_tree refs act as selectors, die on navigation with a hint, and
    mode='ax' keeps the ref-less CDP tree."""
    if not _chrome_ready("phase 6 aria-ref roundtrip"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab('<button id=cm onclick="window.hit=1">Click me</button>')
        resp = h.tool("browser_tree", {"target": tid})
        tree = resp["result"]["content"][0]["text"]
        m = re.search(r'button "Click me" \[ref=(f\d+e\d+)\]', tree)
        assert_true(m is not None, f"aria snapshot carries a button ref ({tree!r})")
        ref = m.group(1)  # type: ignore[union-attr]

        resp = h.tool("browser_click", {"target": tid, "selector": f"aria-ref={ref}"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(first.startswith("clicked:"), f"ref click resolves (got {first!r})")
        out = h.exec(
            f"_t = await browser.get({tid!r})\nprint('HIT', await _t.js('window.hit'))"
        )
        assert_true("HIT 1" in out, f"the ref click landed (got {out!r})")

        h.tool(
            "browser_navigate",
            {"target": tid, "url": f"data:text/html,<p>{_MARKER}-next</p>"},
        )
        resp = h.tool("browser_click", {"target": tid, "selector": f"aria-ref={ref}"})
        msg = resp.get("error", {}).get("message", "")
        assert_true(
            "fresh snapshot" in msg,
            f"a dead ref explains itself instead of a bare miss (got {msg!r})",
        )

        resp = h.tool("browser_tree", {"target": tid, "mode": "ax"})
        ax = resp["result"]["content"][0]["text"]
        assert_true(
            "[ref=" not in ax,
            f"mode='ax' keeps the ref-less CDP tree (got {ax[:200]!r})",
        )
        print("  ✓ aria-ref roundtrip: click by ref, dead-ref hint, ax mode intact")
    finally:
        h.close()


def phase_6_engine_reinjection(kernel: Kernel) -> None:
    """The engine re-instantiates per document and per session.

    Navigation destroys the execution context (a fresh handle appears on the
    next resolve); a reattach kills the objectId with its sessionId, so
    `_reattach_core` must drop the cache — this drives the real
    `reattach_session` path against live Chrome, the same route a WebSocket
    reconnect takes.
    """
    if not _chrome_ready("phase 6 engine reinjection"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab("<button id=x>X</button>")
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "from repld.browser import inject as _inj\n"
            "await _inj.resolve_element(_t, '#x')\n"
            "_h1 = _t._session._injected\n"
            f"await _t.navigate('data:text/html,<button id=y>Y</button><i>{_MARKER}</i>')\n"
            "await _inj.resolve_element(_t, '#y')\n"
            "_h2 = _t._session._injected\n"
            "print('NAV', _h1 is not None, _h2 is not None, _h1 is not _h2)\n"
            "await _t._session.browser_session.reattach_session(_t._session)\n"
            "print('REATTACH-CLEARED', _t._session._injected is None)\n"
            "await _inj.resolve_element(_t, '#y')\n"
            "print('REATTACH-RESOLVED', _t._session._injected is not None)",
            timeout=30,
        )
        assert_true(
            "NAV True True True" in out,
            f"navigation yields a fresh engine instance (got {out!r})",
        )
        assert_true(
            "REATTACH-CLEARED True" in out,
            f"reattach_session drops the stale handle (got {out!r})",
        )
        assert_true(
            "REATTACH-RESOLVED True" in out,
            f"and the next resolve re-instantiates (got {out!r})",
        )
        print(
            "  ✓ engine reinjection: per document (navigate) and per session (reattach)"
        )
    finally:
        h.close()


def phase_6_viewport_param(kernel: Kernel) -> None:
    """browser_open's viewport= pins the page to WxH at deviceScaleFactor 1.

    Without it, every measurement and screenshot needed the manual
    Emulation.setDeviceMetricsOverride dance, and forgetting it meant
    coordinate-multiplier math against a scaled capture.
    """
    if not _chrome_ready("phase 6 viewport param"):
        return
    h = _BridgeHarness(kernel)
    try:
        url = f"data:text/html,<p>{_MARKER}-viewport</p>"
        resp = h.tool("browser_open", {"url": url, "viewport": "1234x777"})
        text = resp["result"]["content"][0]["text"]
        m = re.search(r"target: (\S+)", text)
        assert_true(
            m is not None, f"open with viewport returns a target ({text[:120]!r})"
        )
        tid = m.group(1)  # type: ignore[union-attr]
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "_w = await _t.js('innerWidth'); _h = await _t.js('innerHeight')\n"
            "_dpr = await _t.js('devicePixelRatio')\n"
            # Chrome reports the emulated height off by a pixel and the ratio
            # with float wobble (1.0000000149…) — assert what matters: the
            # requested size within a pixel, at effectively scale 1.
            "print('VP-OK', _w == 1234 and abs(_h - 777) <= 1"
            " and abs(_dpr - 1) < 1e-3)"
        )
        assert_true(
            "VP-OK True" in out,
            f"viewport pinned to ~1234x777 at scale 1 (got {out!r})",
        )
        print("  ✓ browser_open viewport=: fixed size, scale 1, no multiplier math")
    finally:
        h.close()


def phase_6_observation_diff(kernel: Kernel) -> None:
    """Mutation observations carry a changes: AX-diff — appeared/gone/state —
    and suppress it across a navigation, where the whole tree is new."""
    if not _chrome_ready("phase 6 observation diff"):
        return
    h = _BridgeHarness(kernel)
    try:
        tid = h.open_tab(
            '<button id=go onclick="this.disabled=true;'
            "document.getElementById('bye').remove();"
            "for(let i=0;i<3;i++){const b=document.createElement('button');"
            "b.textContent='port';document.body.appendChild(b)}\">Go</button>"
            "<a href='x' id=bye>Bye</a>"
            "<button id=noop>Noop</button>"
        )
        resp = h.tool("browser_click", {"target": tid, "selector": "#go"})
        text = resp["result"]["content"][0]["text"]
        assert_true(
            "changes: 3 appeared, 1 gone, 1 changed" in text,
            f"diff header counts the mutation (got {text[:400]!r})",
        )
        assert_true(
            "+ button 'port' ×3" in text,
            f"identical arrivals aggregate with a multiplier (got {text[:400]!r})",
        )
        assert_true(
            "- link 'Bye'" in text,
            f"the removed link reads as gone (got {text[:400]!r})",
        )
        assert_true(
            "~ button 'Go' [none] → [disabled]" in text,
            f"same-name prop flip reads as a state change (got {text[:400]!r})",
        )

        resp = h.tool("browser_click", {"target": tid, "selector": "#noop"})
        text = resp["result"]["content"][0]["text"]
        assert_true(
            "changes: none" in text,
            f"an inert click says the tree didn't move (got {text[:300]!r})",
        )

        resp = h.tool(
            "browser_navigate",
            {"target": tid, "url": f"data:text/html,<p>{_MARKER}-nav</p>"},
        )
        text = resp["result"]["content"][0]["text"]
        assert_true(
            "changes: (page navigated" in text,
            f"a navigation suppresses the diff instead of dumping it (got {text[:300]!r})",
        )
        print("  ✓ observation diff: appeared ×N, gone, state change, none, navigation")
    finally:
        h.close()


def phase_6_hover_and_drag(kernel: Kernel) -> None:
    """browser_hover parks the pointer (hover-revealed UI enters the diff and
    stays clickable, and a presentational reveal falls back to the dom: line);
    browser_drag presses, moves with buttons=1, releases — micro-stepping out
    of a small origin, dwelling at the drop point for a debounced drop
    handler, and re-resolving a drop zone that mounts on dragstart."""
    if not _chrome_ready("phase 6 hover and drag"):
        return
    h = _BridgeHarness(kernel)
    try:
        # No literal '#' anywhere in the markup: everything past one is the
        # data: URL's fragment, and the page silently truncates there —
        # attribute selectors and named colors instead.
        tid = h.open_tab(
            "<style>div[id=menu]{display:none}"
            "div[id=zone]:hover div[id=menu]{display:block}</style>"
            "<div id=zone><button id=trig>Trigger</button>"
            "<div id=menu><button onclick='window.picked=1'>Reveal</button></div>"
            "</div>"
            '<div id=src style="position:absolute;left:20px;top:120px;'
            'width:40px;height:40px;background:blue"></div>'
            '<div id=dst style="position:absolute;left:220px;top:120px;'
            'width:60px;height:60px;background:green"></div>'
            '<div id=src2 style="position:absolute;left:20px;top:220px;'
            'width:40px;height:40px;background:red"></div>'
            '<div id=port style="position:absolute;left:400px;top:120px;'
            'width:10px;height:10px;background:black"></div>'
            '<div id=pdst style="position:absolute;left:400px;top:220px;'
            'width:40px;height:40px;background:gray"></div>'
            '<div id=jsrev style="position:absolute;left:400px;top:20px;'
            'width:60px;height:30px;background:orange"></div>'
            '<div id=dsrc style="position:absolute;left:440px;top:380px;'
            'width:20px;height:20px;background:teal"></div>'
            '<div id=dtgt style="position:absolute;left:700px;top:380px;'
            'width:30px;height:30px;background:pink"></div>'
            "<script>"
            "let down=0,moves=0;"
            "document.getElementById('src').addEventListener('mousedown',()=>{down=1});"
            "document.addEventListener('mousemove',e=>{if(down&&e.buttons===1)moves++});"
            "document.addEventListener('mouseup',e=>{if(down){"
            "window.moveCount=moves;"
            "const d=document.getElementById('dst').getBoundingClientRect();"
            "window.dropped=e.clientX>=d.left&&e.clientX<=d.right"
            "&&e.clientY>=d.top&&e.clientY<=d.bottom;down=0}});"
            "document.getElementById('src2').addEventListener('mousedown',()=>{"
            "const z=document.createElement('div');z.id='dz';"
            "z.style.cssText='position:absolute;left:220px;top:220px;"
            "width:60px;height:60px;background:yellow';"
            "z.addEventListener('mouseup',()=>{window.dropped2=1});"
            "document.body.appendChild(z)});"
            # A 10px port with a dnd slop threshold: the first move after
            # mousedown must land inside the port AND >=1.5px away to arm —
            # a first move outside disarms for good. The failure browser_drag
            # shipped with: distance/steps px on step one exits the origin.
            "let pd=null,armed=false;"
            "document.getElementById('port').addEventListener('mousedown',e=>{"
            "pd={x:e.clientX,y:e.clientY};armed=false;window.slopFail=0});"
            "document.addEventListener('mousemove',e=>{if(!pd||armed)return;"
            "const r=document.getElementById('port').getBoundingClientRect();"
            "const inside=e.clientX>=r.left&&e.clientX<=r.right"
            "&&e.clientY>=r.top&&e.clientY<=r.bottom;"
            "if(inside){if(Math.hypot(e.clientX-pd.x,e.clientY-pd.y)>=1.5)armed=true}"
            "else{window.slopFail=1;pd=null}});"
            "document.addEventListener('mouseup',e=>{if(pd&&armed){"
            "const d=document.getElementById('pdst').getBoundingClientRect();"
            "window.edge=(e.clientX>=d.left&&e.clientX<=d.right"
            "&&e.clientY>=d.top&&e.clientY<=d.bottom)?1:0}pd=null});"
            # Presentational reveal: mounts three empty divs — invisible to
            # the AX tree, so only the dom: fallback line can report it.
            "document.getElementById('jsrev').addEventListener('mouseenter',()=>{"
            "for(let i=0;i<3;i++){document.body.appendChild("
            "document.createElement('div'))}});"
            # A drop handler that debounces its own hit-detection on move
            # events, the shape browser_drag's dwell_ms exists for: armed
            # only after 80ms of continuous inside-target movement, and a
            # release before that doesn't count as a drop.
            "document.getElementById('dsrc').addEventListener('mousedown',()=>{"
            "window.dwellDropped=undefined;window.__dwellIn=null;"
            "window.__dwellArmed=false});"
            "document.addEventListener('mousemove',e=>{if(e.buttons!==1)return;"
            "const r=document.getElementById('dtgt').getBoundingClientRect();"
            "const inside=e.clientX>=r.left&&e.clientX<=r.right"
            "&&e.clientY>=r.top&&e.clientY<=r.bottom;"
            "if(inside){if(window.__dwellIn===null)window.__dwellIn=performance.now();"
            "if(performance.now()-window.__dwellIn>=80)window.__dwellArmed=true;}"
            "else{window.__dwellIn=null;window.__dwellArmed=false}});"
            "document.getElementById('dtgt').addEventListener('mouseup',()=>{"
            "window.dwellDropped=window.__dwellArmed?1:0});"
            "</script>"
        )

        # Coordinate hover first, while the pointer is still parked at the
        # tab's origin: (430,35) is jsrev's center, and the reveal it
        # triggers is AX-invisible — both quirks in one call.
        resp = h.tool("browser_hover", {"target": tid, "selector": "430,35"})
        text = resp["result"]["content"][0]["text"]
        first = text.splitlines()[0]
        assert_true(
            first.startswith("hovering: (430,35)"),
            f"coordinate hover receipt (got {first!r})",
        )
        assert_true(
            "changes: none in the AX tree — dom: +3" in text,
            f"AX-silent reveal falls back to the dom: line (got {text[:400]!r})",
        )

        resp = h.tool("browser_hover", {"target": tid, "selector": "#trig"})
        text = resp["result"]["content"][0]["text"]
        first = text.splitlines()[0]
        assert_true(first.startswith("hovering:"), f"hover receipt (got {first!r})")
        assert_true(
            "+ button 'Reveal'" in text,
            f"the diff reports what the hover revealed (got {text[:400]!r})",
        )
        resp = h.tool("browser_click", {"target": tid, "selector": "text=Reveal"})
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('PICKED', await _t.js('window.picked'))"
        )
        assert_true(
            "PICKED 1" in out,
            f"hover-revealed UI stayed up for the click (got {out!r})",
        )

        resp = h.tool("browser_drag", {"target": tid, "from": "#src", "to": "#dst"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith("dragged:") and "warning" not in first,
            f"drag receipt names both endpoints cleanly (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('DROP', await _t.js('window.dropped'),"
            " await _t.js('window.moveCount'))"
        )
        assert_true(
            "DROP True" in out,
            f"the release landed inside the drop target (got {out!r})",
        )
        moves = int(out.split("DROP True", 1)[1].split()[0])
        assert_true(
            moves >= 10,
            f"the gesture was paced moves with buttons=1, not a teleport ({moves})",
        )

        resp = h.tool("browser_drag", {"target": tid, "from": "#src2", "to": "#dz"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith("dragged:"),
            f"deferred-target drag receipt (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('DROP2', await _t.js('window.dropped2'))"
        )
        assert_true(
            "DROP2 1" in out,
            f"a drop zone that mounts on dragstart is reachable (got {out!r})",
        )

        resp = h.tool("browser_drag", {"target": tid, "from": "#port", "to": "#pdst"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith("dragged:"),
            f"small-origin drag receipt (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('EDGE', await _t.js('window.edge'),"
            " 'SLOP', await _t.js('window.slopFail'))"
        )
        assert_true(
            "EDGE 1 SLOP 0" in out,
            f"micro-steps arm a 10px origin's slop threshold before leaving it"
            f" (got {out!r})",
        )

        resp = h.tool(
            "browser_drag",
            {"target": tid, "from": "#dsrc", "to": "#dtgt", "dwell_ms": 0},
        )
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(first.startswith("dragged:"), f"dwell_ms=0 receipt (got {first!r})")
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('DWELL0', await _t.js('window.dwellDropped'))"
        )
        assert_true(
            "DWELL0 0" in out,
            f"arrive-then-release doesn't arm a debounced drop handler (got {out!r})",
        )

        resp = h.tool("browser_drag", {"target": tid, "from": "#dsrc", "to": "#dtgt"})
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith("dragged:"), f"default-dwell receipt (got {first!r})"
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('DWELL1', await _t.js('window.dwellDropped'))"
        )
        assert_true(
            "DWELL1 1" in out,
            f"the default dwell arms a debounced drop handler before release"
            f" (got {out!r})",
        )
        print(
            "  ✓ hover parks + reveals (dom: fallback, x,y form); drag is atomic,"
            " paced, micro-stepped, dwells for debounced drop handlers, and"
            " resolves mid-gesture"
        )
    finally:
        h.close()


def phase_6_select_type_filter(kernel: Kernel) -> None:
    """select_option types into an aria-autocomplete field so an option below
    a virtualized list's render window becomes reachable."""
    if not _chrome_ready("phase 6 select type-filter"):
        return
    h = _BridgeHarness(kernel)
    try:
        # 30 statuses, list renders only the first 5 matching the filter —
        # the Jira Replace-status shape, where "Eskalert" sits below the
        # render window until the filter narrows it in.
        tid = h.open_tab(
            "<div role=combobox>"
            "<input id=cb aria-autocomplete=list oninput='filt(this.value)'"
            " onfocus='filt(this.value)'>"
            "<span id=vv></span></div>"
            "<div id=lb role=listbox></div>"
            "<script>"
            "const ALL=[...Array(30)].map((_,i)=>'Status'+i).concat(['Eskalert']);"
            "function filt(q){const lb=document.getElementById('lb');"
            "lb.innerHTML='';"
            "ALL.filter(o=>o.toLowerCase().includes(q.toLowerCase())).slice(0,5)"
            ".forEach(o=>{const d=document.createElement('div');"
            "d.setAttribute('role','option');d.textContent=o;"
            "d.onclick=()=>{window.chosen=o;"
            "document.getElementById('vv').textContent=o};"
            "lb.appendChild(d)})}"
            "</script>"
        )
        resp = h.tool(
            "browser_select",
            {"target": tid, "selector": "#cb", "option": "Eskalert"},
        )
        first = resp["result"]["content"][0]["text"].splitlines()[0]
        assert_true(
            first.startswith('selected "Eskalert"'),
            f"the below-the-fold option was reached (got {first!r})",
        )
        assert_true(
            "(typed to filter)" in first,
            f"the receipt says the filter path did it (got {first!r})",
        )
        assert_true(
            "not verified" not in first,
            f"the rendered value verifies (got {first!r})",
        )
        out = h.exec(
            f"_t = await browser.get({tid!r})\n"
            "print('CHOSEN', await _t.js('window.chosen'))"
        )
        assert_true("CHOSEN Eskalert" in out, f"the option click landed (got {out!r})")
        print("  ✓ select_option: aria-autocomplete filter reaches virtualized options")
    finally:
        h.close()
