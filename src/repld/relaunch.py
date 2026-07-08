"""`repld browser` — re-exec via `uv run` with the browser extra.

repld's core is stdlib-only (see CLAUDE.md); `duckdb`/`websockets` are
gated behind the `browser` extra so most sessions never pay for them.
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
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    info = json.loads(raw)
    if not info.get("dir_info", {}).get("editable"):
        return None
    return info["url"].removeprefix("file://")


def run_browser(argv: list[str]) -> int:
    """Re-exec `repld <argv>` under `uv run` with duckdb/websockets available."""
    uv = shutil.which("uv")
    if uv is None:
        print("repld browser: `uv` not found on PATH", file=sys.stderr)
        return 1

    path = _editable_path()
    with_arg = (
        ["--with-editable", f"{path}[browser]"]
        if path
        else ["--with", "repld-tool[browser]"]
    )
    os.execvp(uv, [uv, "run", *with_arg, "repld", *argv])
