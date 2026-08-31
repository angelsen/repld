"""Phase 8: Gist resources — resources/list includes gists, resources/read returns API.

Also the two doc-drift guards, which split by who wrote the doc rather than by
what they check.

`phase_8_doc_surfaces` covers the agent-facing surfaces in `help.py`. CLAUDE.md
names keeping them in sync as an invariant ("Never let the two drift"), and it
was the one named invariant with nothing enforcing it — `BROWSER_GUIDE` and
`_TOPICS["browser"]` are two independently hand-written prose blocks covering
the same API, so nothing but a reader would have caught the next divergence.

`phase_8_site_signatures` covers the human-facing ones: the site's browser
pages and README, which duplicate those same signatures a third time with
nothing reading them. It asks a different question — not "does this name
exist" but "is this call shaped right" — because that is the drift those files
actually accumulated, and two of its three rules describe code that raises.
"""

import ast
import pathlib
import re

from harness import Bridge, Kernel, assert_eq, assert_true

# Attribute reference in prose: `tab.click`, `browser.watch`. Lowercase-only so
# it can't match a class (`Tab.foo`) or a sentence that happens to end in the
# word "tab" before a capitalised one.
_ATTR_RE = r"\b(?:tab|browser)\.([a-z_][a-z0-9_]*)"


def _class_members(pkg: pathlib.Path, *specs: tuple[str, str]) -> set[str]:
    """Public+private member names of the named classes, read from source.

    AST rather than import: the browser package needs the `browser` extra
    (duckdb/websockets/pillow), and a doc check has no business requiring it.
    """
    return set(_member_kinds(pkg, *specs))


def _member_kinds(pkg: pathlib.Path, *specs: tuple[str, str]) -> dict[str, str]:
    """Member name → "async" | "sync" | "attr", read from source.

    "attr" covers properties and plain assignments alike: what the doc check
    downstream cares about is only whether writing `name(...)` is correct, and
    both answer no. A `@property` wins over the `@name.setter` sharing its name.
    """
    out: dict[str, str] = {}
    for rel, cls in specs:
        tree = ast.parse((pkg / rel).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name == cls):
                continue
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = {ast.unparse(d) for d in m.decorator_list}
                    if any(
                        d == "property" or d.endswith(("_property", ".setter"))
                        for d in decorators
                    ):
                        kind = "attr"
                    else:
                        kind = (
                            "async" if isinstance(m, ast.AsyncFunctionDef) else "sync"
                        )
                    # A property's setter is visited second and must not
                    # downgrade it; nothing else in these classes collides.
                    if out.get(m.name) != "attr":
                        out[m.name] = kind
                elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
                    out.setdefault(m.target.id, "attr")
                elif isinstance(m, ast.Assign):
                    for t in m.targets:
                        if isinstance(t, ast.Name):
                            out.setdefault(t.id, "attr")
    return out


def _python_fences(text: str) -> list[str]:
    """Lines inside ```python fences, which are the only runnable claims.

    Prose says `tab.fetch()` to name a method; a fence says it to show a call,
    and only the second one is wrong without an `await`.
    """
    return [
        line
        for block in re.findall(
            r"^```(?:python|py)\n(.*?)^```", text, re.MULTILINE | re.DOTALL
        )
        for line in block.splitlines()
    ]


def phase_8_doc_surfaces(_kernel: Kernel) -> None:
    """The agent-facing docs describe the API that exists, on every surface.

    No kernel needed — `help.py` is pure stdlib data and the API is read from
    source. Three drift modes, each of which has a distinct failure:

    1. A doc names a method that was renamed or removed. This is the one that
       actively misleads: the agent reads `instructions`/`repld help` and calls
       something that isn't there.
    2. A method is documented on one browser surface but not the other.
    3. A new public `Tab` method is documented on neither.
    """
    import repld.help as h

    pkg = pathlib.Path(h.__file__).parent
    tab = _class_members(
        pkg, ("browser/tab.py", "Tab"), ("browser/tab_query.py", "TabQueryMixin")
    )
    pool = _class_members(
        pkg, ("browser/pool.py", "BrowserPool"), ("browser/pool.py", "LazyBrowser")
    )
    assert_true(
        "click" in tab and "watch" in pool,
        f"AST member extraction found the classes (tab={len(tab)}, pool={len(pool)})",
    )

    # Every module-level str in help.py, plus every topic. Deliberately
    # reflective rather than a hand-listed set: a doc constant added later is
    # covered the day it lands, which a list would not be.
    surfaces = {
        n: v
        for n, v in vars(h).items()
        if isinstance(v, str) and not n.startswith("__")
    }
    surfaces.update({f'_TOPICS["{k}"]': v for k, v in h._TOPICS.items()})
    assert_true(
        len(surfaces) > 8, f"found the doc surfaces to scan (got {len(surfaces)})"
    )

    # 1. No phantom API.
    phantom = [
        f"{name}: {obj}.{attr}"
        for name, text in sorted(surfaces.items())
        for obj, real in (("tab", tab), ("browser", pool))
        for attr in sorted(set(re.findall(rf"\b{obj}\.([a-z_][a-z0-9_]*)", text)))
        if attr not in real
    ]
    assert_eq(phantom, [], "docs reference no API that doesn't exist")
    print(f"  ✓ {len(surfaces)} doc surfaces reference no phantom tab.*/browser.* API")

    # 2. The two browser surfaces cover the same symbols.
    guide = set(re.findall(_ATTR_RE, h.BROWSER_GUIDE))
    topic = set(re.findall(_ATTR_RE, h._TOPICS["browser"]))
    assert_eq(sorted(guide - topic), [], 'in BROWSER_GUIDE but not _TOPICS["browser"]')
    assert_eq(sorted(topic - guide), [], 'in _TOPICS["browser"] but not BROWSER_GUIDE')
    print(f"  ✓ BROWSER_GUIDE and _TOPICS['browser'] agree on {len(guide)} symbols")

    # 3. Every public Tab method reaches both. Tab's internals are `_`-prefixed
    # by convention, so a new name tripping this wants either the underscore or
    # a doc line — which is the prompt this check exists to deliver.
    undocumented = sorted(
        m
        for m in tab
        if not m.startswith("_")
        and (m not in h.BROWSER_GUIDE or m not in h._TOPICS["browser"])
    )
    assert_eq(
        undocumented, [], "every public Tab method is documented on both surfaces"
    )
    print(
        f"  ✓ all {sum(1 for m in tab if not m.startswith('_'))} public Tab methods documented on both surfaces"
    )


def phase_8_site_signatures(_kernel: Kernel) -> None:
    """The hand-written docs call the browser API the way it is actually shaped.

    `help.py` has `phase_8_doc_surfaces` above; the site's browser pages and
    README have had nothing, and they duplicate the same signatures by hand.
    That is where the whole 0.2 crop of drift landed — five `browser` lines
    that raise rather than mislead (`patterns` is a property and the page
    called it; `pages`/`detach`/`disconnect` are `async def` and the page
    dropped the `await`), plus a `tab.screenshot` return type that had never
    been right. `browser.patterns()` is the one that argues for a test: it is
    listed as fixed in the 0.1.0 changelog, because it *was* fixed — in the
    topics, which is the copy `help.py` owns. The site was written later, from
    the pre-fix shape, and inherited it. Two hand-written copies of one API
    drift back apart unless something reads both.

    Three rules, all decidable from the AST — no kernel, no browser extra:
    calling a property, awaiting a sync method, and calling an async one bare.
    The last is the quiet one: it yields a coroutine and raises nothing, so a
    reader gets no signal at all that the doc was wrong.

    `docs/browser.md` is deliberately not scanned. It is a design spec that
    keeps its pre-build draft on purpose and says so in its own header ("Where
    this document and those disagree, those are right"), so its sync-shaped
    `browser.watch(...)` is a record, not a claim. Nor are the marketing pages
    under `site/src/pages/`: their snippets sit in JSX template literals rather
    than fences, and they name methods illustratively — `production.astro` uses
    a bare `browser.fetch()` as a table label for an auth mode, which is prose
    wearing a code font, not a call anyone runs.
    """
    from harness import REPO

    import repld.help as h

    pkg = pathlib.Path(h.__file__).parent
    kinds = {
        "tab": _member_kinds(
            pkg, ("browser/tab.py", "Tab"), ("browser/tab_query.py", "TabQueryMixin")
        ),
        "browser": _member_kinds(
            pkg, ("browser/pool.py", "BrowserPool"), ("browser/pool.py", "LazyBrowser")
        ),
    }
    assert_eq(
        kinds["browser"].get("patterns"), "attr", "AST reads patterns as a property"
    )
    assert_eq(kinds["browser"].get("pages"), "async", "AST reads pages as async")
    assert_eq(kinds["tab"].get("network"), "sync", "AST reads tab.network as sync")

    docs = sorted((REPO / "site/src/content/docs").rglob("*.md")) + [REPO / "README.md"]
    docs = [p for p in docs if p.is_file()]
    if not docs:
        # An sdist checkout has no site/. The README alone still exercises it.
        print("  ⚠ no doc sources found — skipping site signature check")
        return

    # Only a bare `tab` / `browser` root. The lookbehind drops two things that
    # look identical to a regex and are not: `repld.browser.get(...)`, where
    # the `await` binds to the whole chain and never reaches our group, and
    # `docs/browser.md`, a file path whose "attribute" is a suffix.
    root = r"(?<![.\w/])"
    # `await` is optional in the match so a bare async call is a *hit* to
    # classify, not a miss to skip — that being the failure with no symptom.
    call_re = re.compile(
        rf"(await\s+)?{root}\b(tab|browser)\.([a-z_][a-z0-9_]*)\s*(\()?"
    )
    problems: list[str] = []
    checked = 0
    for path in docs:
        rel = path.relative_to(REPO)
        text = path.read_text()

        # 1. No phantom API, over the whole file — a prose mention of a method
        #    that was renamed away is just as wrong as a fenced one.
        for obj, real in kinds.items():
            for attr in sorted(
                set(re.findall(rf"{root}\b{obj}\.([a-z_][a-z0-9_]*)", text))
            ):
                if attr not in real:
                    problems.append(f"{rel}: {obj}.{attr} does not exist")

        # 2/3. Shape rules, fenced Python only.
        for line in _python_fences(text):
            for awaited, obj, attr, called in call_re.findall(line):
                kind = kinds[obj].get(attr)
                if kind is None:
                    continue  # already reported as phantom
                checked += 1
                where = f"{rel}: {line.strip()}"
                if kind == "attr" and called:
                    problems.append(f"{where}\n      → {obj}.{attr} is a property")
                elif kind == "async" and called and not awaited:
                    problems.append(f"{where}\n      → {obj}.{attr} is async")
                elif kind == "sync" and awaited:
                    problems.append(f"{where}\n      → {obj}.{attr} is not async")

    assert_eq(problems, [], "hand-written docs match the browser API's real shape")
    print(
        f"  ✓ {len(docs)} doc file(s), {checked} tab.*/browser.* call(s): "
        "no phantom API, no awaited-sync, no bare-async, no called-property"
    )


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

        # resources/templates/list advertises the gist URI shape — what tells a
        # client the pattern exists before any concrete gist has been listed.
        resp = b.call("resources/templates/list")
        templates = resp["result"]["resourceTemplates"]
        assert_eq(
            [t["uriTemplate"] for t in templates],
            ["repld://gists/{name}"],
            "resources/templates/list advertises the gist template",
        )
        print("  ✓ resources/templates/list: repld://gists/{name}")

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
