"""Phase 12: Cross-project gist links — add / sibling / boot-import / list / rm / stale / path deps."""

import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

from harness import Bridge, Kernel, assert_eq, assert_true

from repld import gist_deps, gist_lint, gists
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

        # --- scan_deps surfaces a linked gist's declared dependency ---
        g.add_link("needy", gd)
        missing = gist_deps.scan_deps(paths=[src / "needy.py"])
        assert_true(
            any("repld_phantom_pkg_xyz" in d.requirement for d in missing),
            f"scan_deps surfaces linked dep (got {[d.requirement for d in missing]})",
        )
        g.remove_link("needy", gd)  # drop it so the boot kernel doesn't prompt
        print("  ✓ scan_deps(paths=) surfaces linked gist deps")

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
    finally:
        gists.registry = orig_registry
        g._linked.clear()
        shutil.rmtree(other, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)
