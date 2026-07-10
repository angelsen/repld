"""Phase 12: Cross-project gist links — add / sibling / boot-import / list / rm / stale."""

import json
import shutil
import tempfile
from pathlib import Path

from harness import Bridge, Kernel, assert_eq, assert_true

from repld import gist_deps, gists
from repld import gist_links as g


def phase_12_gist_links(kernel: Kernel) -> None:
    """Link a gist from another project: manifest, sibling co-link, boot import, prune."""
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
