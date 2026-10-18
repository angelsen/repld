"""What a gist `_tool_*` handler can return besides text and JSON, and the `tool` decorator describing one. Stdlib only: the bridge imports the package."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

_F = TypeVar("_F", bound=Callable)


@dataclass(frozen=True, slots=True)
class Image:
    """An image for the caller to see in the tool result itself, as an MCP `image` content block (never spilled).
    Return one, or a list mixing it with str/dict items: each item becomes its own content block, in order."""

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
    """Describe a `_tool_*` handler to the MCP client: a display `title` and the spec's behavior hints.
    Claude Code runs a `read_only` tool in parallel with others and treats it as read-only; a hint left None is not sent.
    The `_tool_` prefix still registers the tool; this only marks the function, which is returned unwrapped."""
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
