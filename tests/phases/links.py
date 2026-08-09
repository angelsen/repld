"""Phase 12: Cross-project gist links — add / sibling / boot-import / list / rm / stale / path deps."""

import contextlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from harness import Bridge, Kernel, assert_eq, assert_true

from repld import gist_cmd, gist_deps, gist_lint, gists
from repld import gist_links as g


def phase_12_gist_links(kernel: Kernel) -> None:
    """Link a gist from another project: manifest, sibling co-link, boot import, prune, path deps."""
    other = Path(tempfile.mkdtemp(prefix="repld-link-src-"))
    proj = Path(tempfile.mkdtemp(prefix="repld-link-proj-"))
    gd = proj / "gists"
    orig_registry = gists.registry
    try:
        # --- fake "other project" with a gist + sibling import (dep-free) ---
        src = other / "gists"
        src.mkdir(parents=True)
        (src / "sib.py").write_text('"""Sibling gist."""\nVALUE = 7\n')
        (src / "widget.py").write_text(
            '"""Widget gist."""\nimport sib\n\n\ndef val():\n    return sib.VALUE\n'
        )
        # ...and a separate gist with a dependency, for the scan_deps check.
        (src / "needy.py").write_text(
            '"""Needy gist."""\n__repld_deps__ = ["repld_phantom_pkg_xyz"]\n'
        )
        gists.registry = lambda: {
            "widget": {"path": str(src / "widget.py"), "project": str(other)},
            "needy": {"path": str(src / "needy.py"), "project": str(other)},
        }

        # --- link_targets follows the sibling ---
        targets = dict(g.link_targets("widget"))
        assert_true(
            set(targets) == {"widget", "sib"},
            f"link_targets includes sibling (got {sorted(targets)})",
        )
        print("  ✓ link_targets follows same-dir sibling import")

        # --- add_link writes the manifest with both ---
        g.add_link("widget", gd)
        manifest = json.loads((gd / ".links").read_text())
        assert_true(
            set(manifest) == {"widget", "sib"},
            f"manifest has widget + sib (got {sorted(manifest)})",
        )
        print("  ✓ gist add records target + sibling in ./gists/.links")

        # --- a co-linked sibling may not steal a name already linked ---
        # The manifest is a flat name->path map, so `add` pulling in a sibling
        # called `sib` from somewhere else used to repoint the existing entry
        # and report it as nothing but "+ siblings: sib" — silently breaking
        # the gist that was already linked against it. `new` and `fetch` both
        # refuse this scope; the command that writes the manifest did not.
        elsewhere = Path(tempfile.mkdtemp(prefix="repld-link-other2-")) / "gists"
        elsewhere.mkdir(parents=True)
        (elsewhere / "sib.py").write_text('"""A different sib entirely."""\n')
        prev_registry = gists.registry
        gists.registry = lambda: {
            **prev_registry(),
            "sib": {
                "path": str(elsewhere / "sib.py"),
                "project": str(elsewhere.parent),
            },
        }
        raised = ""
        try:
            g.add_link("sib", gd)
        except FileExistsError as exc:
            raised = str(exc)
        assert_true(
            "already linked" in raised,
            f"relinking a linked name to a different file is refused (got {raised!r})",
        )
        assert_eq(
            json.loads((gd / ".links").read_text())["sib"],
            str((src / "sib.py").resolve()),
            "and the manifest still points at the original",
        )
        gists.registry = prev_registry
        print("  ✓ add refuses to repoint a name another linked gist depends on")

        # --- scan_deps surfaces a linked gist's declared dependency ---
        g.add_link("needy", gd)
        missing = gist_deps.scan_deps(paths=[src / "needy.py"])
        assert_true(
            any("repld_phantom_pkg_xyz" in d.requirement for d in missing),
            f"scan_deps surfaces linked dep (got {[d.requirement for d in missing]})",
        )
        g.remove_link("needy", gd)  # drop it so the boot kernel doesn't prompt
        print("  ✓ scan_deps(paths=) surfaces linked gist deps")

        # --- installing is gated on a real tty. Every auto-spawned kernel runs
        # headless with stdin on /dev/null, where readline() returns "" — which
        # a [Y/n] prompt would otherwise read as consent, so a linked gist's
        # __repld_deps__ would get resolved and installed with nobody watching.
        needy = [gist_deps._DepInfo("repld_phantom_pkg_xyz", ["needy"])]
        orig_stdin = sys.__stdin__
        orig_run = subprocess.run
        calls: list[list[str]] = []

        def _spy(cmd, *a, **kw):
            calls.append(cmd)
            raise AssertionError("install_deps shelled out without consent")

        class _FakeStdin:
            def __init__(self, tty: bool) -> None:
                self._tty = tty

            def isatty(self) -> bool:
                return self._tty

            def readline(self) -> str:
                return ""  # what /dev/null gives you, and what a bare Enter gives

        try:
            subprocess.run = _spy
            sys.__stdin__ = _FakeStdin(tty=False)  # pyright: ignore[reportAttributeAccessIssue]
            assert_true(not gist_deps._can_prompt(), "no tty → cannot prompt")
            assert_eq(gist_deps._prompt_dep_selection(needy), [], "no tty → declines")
            assert_eq(gist_deps.install_deps(needy), False, "no tty → installs nothing")
            assert_eq(calls, [], "no tty → never reached the installer")

            # The guard must be a tty check, not a blanket refusal: at a real
            # terminal a bare Enter still means yes.
            sys.__stdin__ = _FakeStdin(tty=True)  # pyright: ignore[reportAttributeAccessIssue]
            assert_true(gist_deps._can_prompt(), "tty → can prompt")
            assert_eq(
                gist_deps._prompt_dep_selection(needy),
                needy,
                "tty + bare Enter → consent preserved",
            )
        finally:
            subprocess.run = orig_run
            sys.__stdin__ = orig_stdin  # pyright: ignore[reportAttributeAccessIssue]
        print("  ✓ headless kernel declines gist-dep install; a tty still consents")

        # --- _parse_pkg_name splits at the earliest specifier ---
        pkg = gist_deps._parse_pkg_name
        assert_eq(pkg("foo>=1.0,!=1.2"), "foo", "multi-clause req")
        assert_eq(pkg("bar~=2.0"), "bar", "single-specifier req")
        assert_eq(pkg("baz"), "baz", "bare package name")
        assert_eq(pkg("httpx[http2]>=0.27"), "httpx", "extras + specifier")
        assert_eq(pkg("httpx[http2]"), "httpx", "bare extras")
        print("  ✓ _parse_pkg_name handles multi-clause requirements and extras")

        # --- _is_importable falls back to a distribution's real import name
        # (e.g. pyyaml -> yaml) instead of reporting it missing forever ---
        orig_pd = importlib.metadata.packages_distributions
        importlib.metadata.packages_distributions = lambda: {
            "json": ["phantom-dist-match"],  # stdlib, always importable
            "no_such_module_xyz": ["phantom-dist-nomatch"],
        }
        gist_deps._dist_to_import = None
        try:
            assert_true(
                gist_deps._is_importable("phantom-dist-match"),
                "falls back to the distribution's real import name",
            )
            assert_true(
                not gist_deps._is_importable("phantom-dist-nomatch"),
                "still missing when neither the name nor its import name resolves",
            )
        finally:
            importlib.metadata.packages_distributions = orig_pd
            gist_deps._dist_to_import = None
        print("  ✓ _is_importable falls back to the distribution's real import name")

        # --- the shared gist-deps dir is keyed by interpreter version and goes
        # on sys.path under *any* prefix. It used to be added only inside the
        # uv tool venv, which hid every installed gist dep from a kernel that
        # bind.py had re-execed into a `uv run` overlay. ---
        orig_deps_root = gist_deps._DEPS_ROOT
        try:
            gist_deps._DEPS_ROOT = other / "shared-deps"
            expected = f"py{sys.version_info[0]}.{sys.version_info[1]}"
            assert_eq(gist_deps._deps_dir().name, expected, "deps dir is version-keyed")
            assert_eq(
                gist_deps._manifest_path().parent,
                gist_deps._deps_dir(),
                "manifest lives inside the versioned dir",
            )

            d = str(gist_deps._deps_dir())
            gist_deps.ensure_deps_on_path()
            assert_true(d not in sys.path, "absent dir isn't added to sys.path")

            gist_deps._deps_dir().mkdir(parents=True)
            marker = "/repld-fake-project-site-packages"
            sys.path.insert(0, marker)
            gist_deps.ensure_deps_on_path()
            assert_true(
                d in sys.path, "existing deps dir is added regardless of prefix"
            )
            assert_true(
                sys.path.index(d) > sys.path.index(marker),
                "appended, not prepended — the project's own packages win",
            )
            gist_deps.ensure_deps_on_path()
            assert_eq(sys.path.count(d), 1, "idempotent across repeat calls")
            sys.path.remove(d)
            sys.path.remove(marker)
            print("  ✓ gist-deps dir: version-keyed, ungated, appended, idempotent")

            # --- an editable dep (`__repld_deps__ = ["."]`) installs as a bare
            # `.pth` and no package directory, so the dir has to go on the path
            # via addsitedir. A plain sys.path.append left exactly the one dep
            # form that exists for the unbound case unimportable. ---
            src_tree = other / "editable-src"
            src_tree.mkdir()
            (gist_deps._deps_dir() / "probe.pth").write_text(f"{src_tree}\n")
            gist_deps.ensure_deps_on_path()
            assert_true(
                str(src_tree) in sys.path,
                ".pth in the deps dir is processed, not just listed",
            )
            sys.path.remove(str(src_tree))
            sys.path.remove(d)
            print("  ✓ editable gist deps resolve — .pth files are processed")

            _fetch_checks()
        finally:
            gist_deps._DEPS_ROOT = orig_deps_root

        # --- `repld exec` must not pay for a bind: it resolves a lock path and
        # talks IPC, and the code runs in the kernel, which is already bound ---
        from repld import cli

        assert_true(
            "exec" not in cli.BINDING_COMMANDS,
            f"exec doesn't re-exec through uv (got {sorted(cli.BINDING_COMMANDS)})",
        )
        assert_true("bridge" in cli.BINDING_COMMANDS, "...but bridge still does")
        print("  ✓ only kernel-running commands bind to the project interpreter")

        # --- path: dep resolves relative to project root, lands on sys.path ---
        vendor = other / "vendor" / "mylib"
        vendor.mkdir(parents=True)
        (vendor / "common.py").write_text("VALUE = 42\n")
        (src / "pathdep.py").write_text(
            '"""Path-dep gist."""\n__repld_deps__ = ["path:vendor/mylib"]\n'
        )
        missing = gist_deps.scan_deps(paths=[src / "pathdep.py"])
        assert_eq(missing, [], "path: dep produces no installable _DepInfo")
        resolved_str = str(vendor.resolve())
        assert_true(resolved_str in sys.path, "path: dep prepended to sys.path")
        print("  ✓ path: dep resolves relative to project root, lands on sys.path")

        # --- idempotent: a second gist declaring the same path doesn't duplicate it ---
        (src / "pathdep2.py").write_text(
            '"""Second path-dep gist."""\n__repld_deps__ = ["path:vendor/mylib"]\n'
        )
        gist_deps.scan_deps(paths=[src / "pathdep2.py"])
        assert_eq(
            sys.path.count(resolved_str), 1, "path: dep not duplicated in sys.path"
        )
        sys.path.remove(resolved_str)
        print("  ✓ path: dep is idempotent across multiple declaring gists")

        # --- missing path: dep warns instead of crashing, no _DepInfo produced ---
        (src / "ghostdep.py").write_text(
            '"""Missing path-dep gist."""\n__repld_deps__ = ["path:vendor/nope"]\n'
        )
        missing = gist_deps.scan_deps(paths=[src / "ghostdep.py"])
        assert_eq(missing, [], "missing path: dep produces no _DepInfo, doesn't raise")
        print("  ✓ missing path: dep warns instead of crashing")

        # --- gist lint: path: dep suppresses the deps rule's false positive ---
        (src / "usespath.py").write_text(
            '"""Uses vendored common."""\n'
            '__repld_deps__ = ["path:vendor/mylib"]\n'
            "from common import VALUE\n"
        )
        findings = gist_lint.lint_file(src / "usespath.py")
        deps_findings = [f for f in findings if f.rule == "deps"]
        assert_eq(
            deps_findings,
            [],
            f"path: dep suppresses deps finding (got {deps_findings})",
        )
        print("  ✓ gist lint: path: dep suppresses false-positive deps finding")

        # --- gist lint: a declaration is a *distribution* name, an import is a
        # *module* name. Comparing them directly flagged correctly-declared
        # gists (pillow/PIL, resvg-py/resvg_py). Neither package is installed
        # for the test run, so this pins the static tiers specifically. ---
        (src / "aliased.py").write_text(
            '"""Aliased deps."""\n'
            '__repld_deps__ = ["pillow>=12", "resvg-py>=0.3"]\n'
            "import resvg_py\n"
            "from PIL import Image\n"
        )
        aliased = [
            f for f in gist_lint.lint_file(src / "aliased.py") if f.rule == "deps"
        ]
        assert_eq(aliased, [], f"dist-vs-import names reconciled (got {aliased})")

        # --- gist lint: a try/except ImportError import is a soft dependency by
        # construction; demanding a __repld_deps__ entry for it defeats the
        # point. The fallback import in the handler is *not* guarded. ---
        (src / "optional.py").write_text(
            '"""Optional deps."""\n'
            "try:\n"
            "    import repld_phantom_soft_xyz\n"
            "except ImportError:\n"
            "    repld_phantom_soft_xyz = None\n"
            "try:\n"
            "    from repld_phantom_tuple_xyz import thing\n"
            "except (ValueError, ModuleNotFoundError):\n"
            "    thing = None\n"
            "try:\n"
            "    import repld_phantom_broad_xyz\n"
            "except Exception:\n"
            "    repld_phantom_broad_xyz = None\n"
        )
        soft = [f for f in gist_lint.lint_file(src / "optional.py") if f.rule == "deps"]
        assert_eq(soft, [], f"guarded imports aren't undeclared deps (got {soft})")

        (src / "fallback.py").write_text(
            '"""Fallback import in the handler is a hard dep."""\n'
            "try:\n"
            "    import json\n"
            "except ImportError:\n"
            "    import repld_phantom_fallback_xyz\n"
        )
        fb = [f for f in gist_lint.lint_file(src / "fallback.py") if f.rule == "deps"]
        assert_eq(len(fb), 1, f"handler-body import is still flagged (got {fb})")
        assert_true(
            "repld_phantom_fallback_xyz" in fb[0].message,
            "...and it names the fallback package",
        )

        (src / "unguarded.py").write_text(
            '"""Try block that catches something unrelated."""\n'
            "try:\n"
            "    import repld_phantom_unguarded_xyz\n"
            "except ValueError:\n"
            "    pass\n"
        )
        ug = [f for f in gist_lint.lint_file(src / "unguarded.py") if f.rule == "deps"]
        assert_eq(
            len(ug), 1, f"try that can't catch ImportError still flags (got {ug})"
        )
        print("  ✓ gist lint: guarded optional imports aren't undeclared deps")

        # --- gist lint: the '.' self-dep resolves to the project's own package
        # instead of being skipped. Its own tmpdir — dropping a pyproject.toml
        # into `other` would change what the rest of this phase sees. ---
        selfproj = Path(tempfile.mkdtemp(prefix="repld-link-self-"))
        (selfproj / "pyproject.toml").write_text(
            '[project]\nname = "repld-self-dep-fixture"\nversion = "0"\n'
        )
        selfgists = selfproj / "gists"
        selfgists.mkdir()
        (selfgists / "usesself.py").write_text(
            '"""Uses its own project."""\n'
            '__repld_deps__ = ["."]\n'
            "from repld_self_dep_fixture import thing\n"
        )
        dot = [
            f
            for f in gist_lint.lint_file(selfgists / "usesself.py")
            if f.rule == "deps"
        ]
        assert_eq(dot, [], f"'.' dep resolves to the project package (got {dot})")
        shutil.rmtree(selfproj, ignore_errors=True)

        # --- gist lint: an underscore-prefixed file is hidden from gist
        # listings but still imports fine from the same directory, so it's a
        # sibling for the deps rule's purposes. ---
        (src / "_helper.py").write_text("VALUE = 1\n")
        (src / "usesprivate.py").write_text(
            '"""Uses a private sibling."""\nfrom _helper import VALUE\n'
        )
        priv = [
            f for f in gist_lint.lint_file(src / "usesprivate.py") if f.rule == "deps"
        ]
        assert_eq(priv, [], f"private sibling import isn't a deps finding (got {priv})")

        # --- ...and the rule still bites. Without this, any over-broad fix to
        # the three above would turn the deps rule into a no-op silently. ---
        (src / "undeclared.py").write_text(
            '"""Undeclared dep."""\n'
            '__repld_deps__ = ["httpx>=0.27"]\n'
            "import httpx\n"
            "import repld_phantom_pkg_xyz\n"
        )
        undecl = [
            f for f in gist_lint.lint_file(src / "undeclared.py") if f.rule == "deps"
        ]
        assert_eq(len(undecl), 1, f"genuinely undeclared import still flagged {undecl}")
        assert_true(
            "repld_phantom_pkg_xyz" in undecl[0].message,
            "the finding names the undeclared package, not the declared one",
        )
        print("  ✓ gist lint: deps rule reconciles dist/import names, '.', privates")

        # --- gist lint: the shape rule checks for an '->' and nothing more.
        # The looseness is deliberate (`-> [DnsRecord(...)]`, `-> the new id`),
        # so the message says arrow, not brace. ---
        (src / "prose.py").write_text(
            '"""Prose shape."""\n'
            "def fetch() -> dict:\n"
            '    """Fetch it. -> product id and title."""\n'
            "    return {}\n"
        )
        prose = [f for f in gist_lint.lint_file(src / "prose.py") if f.rule == "shape"]
        assert_eq(prose, [], f"prose '->' note satisfies the shape rule (got {prose})")
        print("  ✓ gist lint: shape rule accepts any '->' note, as its message says")

        # --- gist lint: nothing reads __repld_tools__ and nothing warns about
        # one at tool-call time, so the rule is the only thing that reports a
        # file still carrying one. Asserted on what the message *tells you to
        # do*, not on its wording — pinning a phrase makes the test fail on a
        # rewording rather than on a behaviour change. ---
        (src / "oldtools.py").write_text(
            '"""Stale declaration."""\n'
            '__repld_tools__ = [{"name": "a", "description": "d"}]\n'
            "def _tool_a(query: str):\n"
            '    """Do a."""\n'
            "    return {}\n"
        )
        old = [
            f for f in gist_lint.lint_file(src / "oldtools.py") if f.rule == "legacy"
        ]
        assert_eq(len(old), 1, "legacy rule flags a stale __repld_tools__")
        assert_true(
            "__repld_tools__" in old[0].message and "_tool_" in old[0].message,
            f"finding names the dunder and the replacement (got {old[0].message!r})",
        )

        # The handler signature is deliberately not flagged: one dict parameter
        # is an ordinary object argument.
        (src / "dictparam.py").write_text(
            '"""One object argument."""\n'
            "def _tool_b(payload: dict):\n"
            '    """Do b. -> {ok}"""\n'
            "    return {}\n"
        )
        dictp = [
            f for f in gist_lint.lint_file(src / "dictparam.py") if f.rule == "legacy"
        ]
        assert_eq(dictp, [], f"a single dict param is not flagged (got {dictp})")

        (src / "typedtools.py").write_text(
            '"""Typed tools."""\n'
            "def _tool_c(query: str, limit: int = 10):\n"
            '    """Do c. -> {ok}"""\n'
            "    return {}\n"
        )
        typed = [
            f for f in gist_lint.lint_file(src / "typedtools.py") if f.rule == "legacy"
        ]
        assert_eq(typed, [], f"typed handler is not flagged (got {typed})")

        (src / "ignored.py").write_text(
            '"""Suppressed stale declaration."""\n'
            "__repld_tools__ = []  # gistlint: ignore=legacy\n"
            "def _tool_d(query: str):\n"
            '    """Do d."""\n'
            "    return {}\n"
        )
        supp = [
            f for f in gist_lint.lint_file(src / "ignored.py") if f.rule == "legacy"
        ]
        assert_eq(supp, [], f"gistlint: ignore=legacy suppresses it (got {supp})")
        print(
            "  ✓ gist lint: legacy rule names __repld_tools__ as removed, not stale sigs"
        )

        # A file lint cannot even read must not abort the run. The default
        # scope spans global + local + linked, so the offending file may sit in
        # a project you never opened — a traceback there is unactionable.
        (src / "binary.py").write_bytes(b'"""Doc."""\nX = "\xff\xfe not utf-8"\n')
        undecodable = gist_lint.lint_file(src / "binary.py")
        assert_eq(len(undecodable), 1, f"undecodable file -> one finding {undecodable}")
        assert_eq(undecodable[0].rule, "syntax", "reported under the syntax rule")
        assert_true(
            "cannot read" in undecodable[0].message,
            f"and says so (got {undecodable[0].message!r})",
        )
        mixed = gist_lint.lint_paths(
            [src / "typedtools.py", src / "binary.py", src / "dictparam.py"]
        )
        assert_true(
            any(f.rule == "syntax" for f in mixed),
            "lint_paths reports it rather than raising mid-list",
        )
        (src / "binary.py").unlink()
        print("  ✓ gist lint: an unreadable file is a finding, not a traceback")

        # --- path: dep modules get gist-style mtime auto-reload, without the
        # gist-registry/API-summary side effects a real gist import triggers ---
        finder = gists._GistFinder([])  # empty gist dirs — only the path-dep tier fires
        common_path = (vendor / "common.py").resolve()
        spec = finder.find_spec("common", None)
        assert_true(spec is not None, "finder resolves 'common' via a path: dep dir")
        assert_eq(
            gists._managed.get("common"), common_path, "tracked under the resolved path"
        )
        assert_true("common" in gists._path_dep_modules, "flagged as a path-dep module")

        registered_before = set(gists._registered)
        hook = gists._GistImportHook(lambda *a, **k: None)
        hook("common")
        assert_eq(
            gists._registered,
            registered_before,
            "path-dep import doesn't write to the gist registry",
        )
        print("  ✓ path: dep import skips _register()/introspect() side effects")

        stale_mtime = gists._mtimes["common"]
        common_path.write_text("VALUE = 99\n")
        os.utime(common_path, (stale_mtime + 1, stale_mtime + 1))
        sys.modules["common"] = types.ModuleType(
            "common"
        )  # stand in for a prior import
        gists._check_reload("common")
        assert_true(
            "common" not in sys.modules, "changed path-dep module evicted like a gist"
        )
        print("  ✓ path: dep modules get gist-style mtime auto-reload")

        sys.modules.pop("common", None)
        del gists._managed["common"]
        gists._mtimes.pop("common", None)
        gists._path_dep_modules.discard("common")

        # --- first-sight dep scan: a gist created after boot gets __repld_deps__
        # checked the moment it's first imported, not just on a later edit ---
        scanned: list[list[Path] | None] = []
        orig_scan_deps = gist_deps.scan_deps
        gist_deps.scan_deps = lambda paths=None: (
            scanned.append(paths),
            orig_scan_deps(paths=paths),
        )[1]
        try:
            (src / "freshgist.py").write_text('"""Fresh gist."""\nVALUE = 1\n')
            finder = gists._GistFinder([src])
            spec = finder.find_spec("freshgist", None)
            assert_true(spec is not None, "finder resolves the new gist")
            assert_eq(len(scanned), 1, "first sight of a new gist triggers a dep scan")
            finder.find_spec("freshgist", None)  # already tracked -- no rescan
            assert_eq(
                len(scanned), 1, "already-managed gist isn't rescanned on find_spec"
            )
        finally:
            gist_deps.scan_deps = orig_scan_deps
            gists._managed.pop("freshgist", None)
            gists._mtimes.pop("freshgist", None)
        print(
            "  ✓ first-sight dep scan: new gist checked on first import, "
            "not rescanned after"
        )

        # --- boot a fresh kernel in the project: linked gist imports + sibling resolves ---
        sub = Kernel(proj)
        b = Bridge(proj)
        try:
            b.call("initialize", {"protocolVersion": "2024-11-05"})
            b.send("notifications/initialized", {}, notif=True)
            resp = b.call(
                "tools/call",
                {
                    "name": "exec",
                    "arguments": {"code": "import widget\nprint('VAL=', widget.val())"},
                },
            )
            text = resp["result"]["content"][0]["text"]
            assert_true(
                "VAL= 7" in text,
                f"linked gist imports at boot + sibling resolves (got {text!r})",
            )
            print("  ✓ linked gist imports at kernel boot, sibling resolves")
        finally:
            b.close()
            sub.stop()

        # --- rm drops the target, keeps the shared sibling ---
        assert_true(g.remove_link("widget", gd), "remove_link returns True")
        remaining = json.loads((gd / ".links").read_text())
        assert_eq(sorted(remaining), ["sib"], "rm keeps shared sibling")
        print("  ✓ gist rm drops target, leaves shared sibling")

        # --- corrupt manifest: read raises, add refuses, boot warns — never clobbered ---
        links_path = gd / ".links"
        good = links_path.read_text()
        links_path.write_text('{"widget": \n')  # truncated JSON
        raised = False
        try:
            g.read_links(gd)
        except ValueError:
            raised = True
        assert_true(raised, "read_links raises on corrupt manifest")
        raised = False
        try:
            g.add_link("widget", gd)
        except ValueError:
            raised = True
        assert_true(raised, "add_link refuses on corrupt manifest")
        assert_eq(
            links_path.read_text(), '{"widget": \n', "corrupt manifest not clobbered"
        )
        g._load_links(gd)  # warns on stderr, loads nothing, doesn't raise
        assert_eq(dict(g._linked), {}, "corrupt manifest loads no links")
        links_path.write_text(good)
        print("  ✓ corrupt manifest → loud error, add refuses, never clobbered")

        # --- registry entry whose file is gone: add errors instead of false success ---
        gists.registry = lambda: {
            "ghost": {"path": str(src / "ghost.py"), "project": str(other)}
        }
        raised = False
        try:
            g.link_targets("ghost")
        except LookupError as e:
            raised = True
            assert_true("gone" in str(e), f"error says the file is gone (got {e!r})")
        assert_true(raised, "link_targets raises on gone registry path")
        print("  ✓ gone registry path → LookupError, not silent empty link")

        # --- stale: delete the source, load skips it, rm --stale prunes it ---
        shutil.rmtree(other)
        g._load_links(gd)
        assert_true("sib" not in g._linked, "stale link skipped at load")
        dropped = g.remove_stale_links(gd)
        assert_eq(dropped, ["sib"], "remove_stale_links drops the dead entry")
        assert_eq(json.loads((gd / ".links").read_text()), {}, "manifest emptied")
        print("  ✓ stale link skipped at load + pruned by rm --stale")

        # --- lint scope: which files get checked at all. Unlike the rule tests
        # above, these drive discovery (and the CLI), so they need the module's
        # gist dirs, $HOME and cwd pointed somewhere known -- all restored in
        # the inner finally so nothing leaks into later phases. ---
        orig_dirs = gists._installed_dirs
        orig_linked = dict(g._linked)
        orig_path = list(sys.path)
        orig_home = os.environ.get("HOME")
        orig_cwd = os.getcwd()
        finder = next(
            (f for f in sys.meta_path if isinstance(f, gists._GistFinder)), None
        )
        orig_finder_dirs = finder._dirs if finder is not None else None
        scope = Path(tempfile.mkdtemp(prefix="repld-link-scope-"))
        try:
            sd = scope / "gists"
            sd.mkdir()
            (sd / "pub.py").write_text('"""Public gist."""\nVALUE = 1\n')
            # Wrapped first line *and* an undeclared import: firstline should
            # fire on one of these files and not the other, deps on both.
            privtext = (
                '"""Private helper\n\nwrapped onto a second line.\n"""\n'
                "import repld_phantom_pkg_xyz\n"
            )
            (sd / "_priv.py").write_text(privtext)
            gists._installed_dirs = [sd]
            g._linked.clear()

            assert_eq(
                [p.name for p in gists._iter_gist_files()],
                ["pub.py"],
                "privates stay out of the default iteration",
            )
            assert_eq(
                sorted(p.name for p in gists._iter_gist_files(include_private=True)),
                ["_priv.py", "pub.py"],
                "include_private=True yields them",
            )

            # --- the private is linted, but firstline is skipped on it: its
            # docstring is never extracted, so there's nothing to truncate ---
            priv_rules = sorted(f.rule for f in gist_lint.lint_file(sd / "_priv.py"))
            assert_eq(
                priv_rules, ["deps"], f"private: deps but no firstline {priv_rules}"
            )
            (sd / "pubwrap.py").write_text(privtext)
            pub_rules = sorted(f.rule for f in gist_lint.lint_file(sd / "pubwrap.py"))
            assert_eq(
                pub_rules, ["deps", "firstline"], f"same content, public {pub_rules}"
            )
            (sd / "pubwrap.py").unlink()
            print("  ✓ gist lint: privates checked, firstline skipped on them")

            # --- a private helper's __repld_deps__ is a real declaration: it's
            # imported by its siblings, so scan_deps has to see it ---
            (sd / "_deps.py").write_text(
                '"""Private with deps."""\n'
                '__repld_deps__ = ["repld_phantom_priv_xyz"]\n'
            )
            missing = gist_deps.scan_deps()
            assert_true(
                any("repld_phantom_priv_xyz" in d.requirement for d in missing),
                f"scan_deps sees a private's deps (got {[d.requirement for d in missing]})",
            )
            (sd / "_deps.py").unlink()

            # --- link_targets co-links siblings with no public/private check,
            # so a private can land in the manifest. It must not then surface
            # as a gist -- the linked branch used to have no filter at all ---
            outside = scope / "elsewhere"
            outside.mkdir()
            (outside / "_shared.py").write_text('"""Shared private."""\nV = 1\n')
            g._linked["_shared"] = outside / "_shared.py"
            assert_true(
                "_shared.py" not in [p.name for p in gists._iter_gist_files()],
                "co-linked private isn't listed as a gist",
            )
            assert_true(
                "_shared.py"
                in [p.name for p in gists._iter_gist_files(include_private=True)],
                "...but stays reachable for deps + lint",
            )
            g._linked.clear()
            print("  ✓ co-linked private stays out of gist listings")

            # --- --local: a dirty *global* gist must not fail this project's
            # gate. $HOME is redirected so _gist_lint's hardcoded
            # ~/.repld/gists resolves into the fixture. ---
            (sd / "_priv.py").unlink()  # leave ./gists clean
            globaldir = scope / ".repld" / "gists"
            globaldir.mkdir(parents=True)
            (globaldir / "bad.py").write_text(
                '"""Bad gist."""\nimport repld_phantom_pkg_xyz\n'
            )
            os.environ["HOME"] = str(scope)
            os.chdir(scope)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_local = gist_cmd._gist_lint(["--local"])
                rc_all = gist_cmd._gist_lint([])
                rc_reject = gist_cmd._gist_lint(["--local", "bad"])
            assert_eq(
                rc_local, 0, f"--local passes on a clean ./gists\n{buf.getvalue()}"
            )
            assert_eq(rc_all, 1, "default run still fails on the dirty global gist")
            assert_eq(rc_reject, 2, "--local rejects a name resolving outside ./gists")
            print("  ✓ gist lint --local scopes to ./gists, rejects outside names")
        finally:
            os.chdir(orig_cwd)
            if orig_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = orig_home
            gists._installed_dirs = orig_dirs
            if finder is not None and orig_finder_dirs is not None:
                finder._dirs = orig_finder_dirs
            g._linked.clear()
            g._linked.update(orig_linked)
            sys.path[:] = orig_path
            shutil.rmtree(scope, ignore_errors=True)

        _registry_summary_already_here_check()
    finally:
        gists.registry = orig_registry
        g._linked.clear()
        shutil.rmtree(other, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


def _fetch_checks() -> None:
    """`repld gist fetch` — ref parsing, and the write/collision rules.

    The network call is stubbed. What can break here is which id a URL resolves
    to and where the file is allowed to land; hitting GitHub would test neither
    and would skip silently without a network, reporting green. Runs in its own
    project and its own fake HOME so it cannot disturb the linked-gist state the
    rest of the phase is still using.
    """
    parse = gist_cmd._parse_gist_ref
    gid = "bad64ef5b7e413c5c9abcc8ac0a59ca8"
    for ref in (
        gid,
        f"https://gist.github.com/{gid}",
        f"https://gist.github.com/angelsen/{gid}",
        f"https://gist.githubusercontent.com/angelsen/{gid}/raw/abc/hybel.py",
    ):
        assert_eq(parse(ref), gid, f"gist ref resolves: {ref[:46]}")
    for bad in ("https://example.com/evil.py", "https://github.com/a/b", "nope", ""):
        try:
            parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted a non-gist ref: {bad!r}")
    print("  ✓ gist fetch: gist ids parse, anything else is refused")

    root = Path(tempfile.mkdtemp(prefix="repld-fetch-"))
    proj, home = root / "proj", root / "home"
    (home / ".repld" / "gists").mkdir(parents=True)
    proj.mkdir()
    stub = ("Fixture gist", {"probe_fetched.py": '"""A probe -> dict."""\nX = 1\n'})
    orig_fetch, orig_home, cwd = gist_cmd._fetch_gist, Path.home, os.getcwd()
    try:
        gist_cmd._fetch_gist = lambda _id, timeout=20.0: stub  # pyright: ignore[reportAttributeAccessIssue]
        Path.home = staticmethod(lambda: home)  # pyright: ignore[reportAttributeAccessIssue]
        os.chdir(proj)
        fetch = lambda *a: _quiet(gist_cmd._gist_fetch, list(a))  # noqa: E731

        assert_eq(fetch(gid), 0, "fetch writes into ./gists")
        body = (proj / "gists" / "probe_fetched.py").read_text()
        assert_true(
            body.startswith(f"# source: https://gist.github.com/{gid}\n"),
            "header names the source",
        )
        assert_true(
            body.endswith('"""A probe -> dict."""\nX = 1\n'),
            "content follows the header verbatim",
        )

        assert_eq(fetch(gid), 1, "a second fetch refuses rather than clobbering")
        assert_eq(fetch(gid, "--force"), 0, "--force overwrites")
        assert_eq(
            (proj / "gists" / "probe_fetched.py").read_text().count("# source:"),
            1,
            "--force doesn't stack headers",
        )

        # All three ways a name can already resolve for a kernel booted here.
        assert_eq(fetch(gid, "--global"), 1, "--global refuses a locally-present name")
        assert_eq(
            fetch(gid, "--global", "--name", "probe_global"),
            0,
            "--global writes to ~/.repld/gists",
        )
        assert_true(
            (home / ".repld" / "gists" / "probe_global.py").is_file(),
            "the global file landed in the global dir",
        )
        assert_eq(
            fetch(gid, "--name", "probe_global"),
            1,
            "local fetch refuses a globally-present name",
        )
        g.write_links(proj / "gists", {"probe_linked": str(root / "probe_linked.py")})
        assert_eq(
            fetch(gid, "--name", "probe_linked"),
            1,
            "fetch refuses a name that is already linked",
        )

        assert_eq(fetch(gid, "--name", "not-an-identifier"), 2, "--name must import")
        assert_eq(fetch(gid, "--name"), 2, "--name without a value is an error")
        assert_eq(fetch(gid, "--nope"), 2, "unknown flag is an error")
        assert_eq(fetch(), 2, "no ref is an error")

        gist_cmd._fetch_gist = lambda _id, timeout=20.0: ("", {})  # pyright: ignore[reportAttributeAccessIssue]
        assert_eq(
            fetch(gid, "--name", "probe_empty"), 1, "a gist with no .py files errors"
        )
        print("  ✓ gist fetch: header, --force, --name, and all three collision scopes")
    finally:
        gist_cmd._fetch_gist = orig_fetch  # pyright: ignore[reportAttributeAccessIssue]
        Path.home = orig_home  # pyright: ignore[reportAttributeAccessIssue]
        os.chdir(cwd)
        shutil.rmtree(root, ignore_errors=True)


def _quiet(fn, argv: list[str]) -> int:
    """Run a gist subcommand with its stdout swallowed — usage blocks are noise."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(argv)


def _registry_summary_already_here_check() -> None:
    """`repld://gists/_registry` marks a name already resolvable here.

    Regression: the resource used to render a blanket "repld gist add <name>"
    for every registry entry, with no check against what this project could
    already import — so a name registered from another project but already
    present locally (or globally, or linked) read as something to link, and
    the only place that surfaced otherwise was `add`'s own collision refusal,
    after the fact.
    """
    orig_dirs = gists._installed_dirs
    orig_registry = gists.registry
    scope = Path(tempfile.mkdtemp(prefix="repld-registry-summary-"))
    try:
        sd = scope / "gists"
        sd.mkdir()
        (sd / "already_here.py").write_text('"""Already resolvable locally."""\n')
        gists._installed_dirs = [sd]
        gists.registry = lambda: {
            "already_here": {
                "path": "/somewhere/else/already_here.py",
                "project": "/somewhere/else",
                "last_used": "2026-01-01T00:00:00+00:00",
                "description": "seen elsewhere too",
            },
            "elsewhere_only": {
                "path": "/somewhere/else/elsewhere_only.py",
                "project": "/somewhere/else",
                "last_used": "2026-01-01T00:00:00+00:00",
                "description": "genuinely not here",
            },
        }
        summary = gists.registry_summary()
        lines = {
            ln.split()[0]: ln for ln in summary.splitlines() if ln.startswith("  ")
        }
        assert_true(
            "(already here)" in lines["already_here"],
            f"a locally-resolvable name is marked (got {lines['already_here']!r})",
        )
        assert_true(
            "(already here)" not in lines["elsewhere_only"],
            f"a genuinely-elsewhere name is not (got {lines['elsewhere_only']!r})",
        )
        print("  ✓ repld://gists/_registry marks a name already resolvable here")
    finally:
        gists._installed_dirs = orig_dirs
        gists.registry = orig_registry
        shutil.rmtree(scope, ignore_errors=True)
