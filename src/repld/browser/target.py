"""How a tab is named — and the two things both browser facades need above them.

The short target ID (`"9222:887d3d"`) is the only handle an agent ever holds on
a tab: it appears in every `browser_*` tool argument, in channel pushes, and in
`browser.tabs`. Its format lives here, in one leaf module that imports nothing
else from the package, so `tab.py` and `observe.py` can take `make_target` at
module level instead of reaching for a call-time `from . import make_target` to
dodge a cycle.

`TabNotFoundError`, `_NO_TABS` and `_print_browser_help` are here for the
narrower reason that `Browser` and `BrowserPool` both need them and live in
separate modules now — `browser.py` cannot import `pool.py`, since `pool.py`
imports `browser.py`. Same carve-out as `channel.py` and `render.py` in the
parent package: small, depended on from both sides, and cheaper here than the
cycle the alternative costs.
"""

import re

__all__ = ["TabNotFoundError", "make_target"]


class TabNotFoundError(RuntimeError):
    """Raised when a target ID or glob pattern matches no tab — distinct from
    other RuntimeErrors (CDP/ready-signal/reattach failures) so BrowserPool.get()
    can retry across browsers on this specific error without masking real ones."""


_TARGET_ID_RE = re.compile(r"^\d+:[0-9a-f]{6}$", re.IGNORECASE)

# Empty state of format_tabs_nested. A constant because BrowserPool compares
# against it to drop the empty per-port renders before joining: reworded in one
# place only, the pool would emit one of these lines per connected browser.
_NO_TABS = "(no attached tabs)"


def _is_target_id(s: str) -> bool:
    """True if s looks like a short target ID (e.g. '9222:a81998')."""
    return bool(_TARGET_ID_RE.match(s))


def _split_target(target: str) -> tuple[str, str]:
    """Split a short target ID like '9222:a1b2c3' into (port_str, prefix).

    Prefix is lowercased — short IDs are canonically lowercase hex
    (make_target()), but _is_target_id() accepts either case, so any
    case-insensitive comparison downstream needs a normalized prefix.
    """
    port_str, _, prefix = target.partition(":")
    return port_str, prefix.lower()


def make_target(port: int, chrome_id: str) -> str:
    """Create short target ID from port and Chrome target ID.

    Format: "{port}:{6-char-lowercase-hex}"
    Example: make_target(9222, "887D3D7FA9473DCF...") -> "9222:887d3d"
    """
    return f"{port}:{chrome_id[:6].lower()}"


def _print_browser_help() -> None:
    """Print the Python API reference for the browser object."""
    from ..help import _TOPICS

    print(_TOPICS["browser"])
