"""`repld browser` — re-exec via `uv run` with the browser + http extras.

repld's core is stdlib-only (see CLAUDE.md); `duckdb`, `websockets` and
`pillow` are gated behind the `browser` extra so most sessions never pay
for them. All three are load-bearing — `browser/png.py` imports PIL at
module scope for `Tab.screenshot`, and `kernel._inject_builtins` catches
the resulting ImportError as "extra not installed", so a partial install
costs the whole browser builtin rather than just screenshots. `http` rides
along too — `Tab.http_client()` is meant to be reached for immediately
after `browser.acquire()`/`get()`, so a session started via `repld browser`
should have it without a second install step.
This subcommand is the escape hatch: run `repld browser` instead of
`repld` and get them for this invocation without adding repld-tool to
the project's dependencies at all.
"""

import json
import os
import shutil
import sys
from importlib import metadata


def _editable_path() -> str | None:
    """Local checkout path if repld-tool is installed in editable mode.

    uv/pip write `direct_url.json` distribution metadata for editable
    installs (`{"dir_info": {"editable": true}, "url": "file://..."}`).
    Used so `repld browser` re-launches with your local edits intact
    instead of silently falling back to the published PyPI version.
    """
    try:
        dist = metadata.distribution("repld-tool")
    except metadata.PackageNotFoundError:
        return None
    # Everything below is best-effort. `bind.rebind_exec` calls this before
    # `repld bridge` and `repld restart` do anything at all, and documents that
    # failures degrade to an unbound kernel — so a `direct_url.json` that is
    # missing, unreadable, corrupt, or simply shaped differently by some future
    # installer must return None here rather than take the MCP server down at
    # startup.
    try:
        raw = dist.read_text("direct_url.json")
    except OSError:
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(info, dict):
        return None
    if not isinstance(info.get("dir_info"), dict):
        return None
    if not info["dir_info"].get("editable"):
        return None
    url = info.get("url")
    return url.removeprefix("file://") if isinstance(url, str) else None


def run_browser(argv: list[str]) -> int:
    """Re-exec `repld <argv>` under `uv run` with the browser extra available."""
    uv = shutil.which("uv")
    if uv is None:
        print("repld browser: `uv` not found on PATH", file=sys.stderr)
        return 1

    path = _editable_path()
    with_arg = (
        ["--with-editable", f"{path}[browser,http]"]
        if path
        else ["--with", "repld-tool[browser,http]"]
    )
    os.execvp(uv, [uv, "run", *with_arg, "repld", *argv])
