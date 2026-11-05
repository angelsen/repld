"""What a gist `_tool_*` handler can return besides text and JSON, and the `tool` decorator describing one. Stdlib only: the bridge imports the package."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

_F = TypeVar("_F", bound=Callable)


@dataclass(frozen=True, slots=True)
class Image:
    """Inline MCP `image` content block, rendered in the tool result itself (never spilled).

    Return one from a `_tool_*` handler, or a list mixing it with str/dict
    items: each item becomes its own content block, in order."""

    data: bytes
    mime_type: str = "image/png"


def tool(
    *,
    title: str | None = None,
    read_only: bool | None = None,
    destructive: bool | None = None,
    idempotent: bool | None = None,
    open_world: bool | None = None,
) -> Callable[[_F], _F]:
    """Mark a `_tool_*` handler with a display `title` and MCP behavior hints the client sees at `tools/list`.

    Claude Code runs a `read_only` tool in parallel with others and treats
    it as read-only; a hint left None is not sent. The `_tool_` prefix still
    registers the tool; this only marks the function, returned unwrapped."""
    hints = {
        key: value
        for key, value in (
            ("title", title),
            ("readOnlyHint", read_only),
            ("destructiveHint", destructive),
            ("idempotentHint", idempotent),
            ("openWorldHint", open_world),
        )
        if value is not None
    }

    def mark(func: _F) -> _F:
        func.__repld_tool__ = hints  # pyright: ignore[reportFunctionMemberAccess]
        return func

    return mark
