"""Content a gist `_tool_*` handler can return besides text and JSON. Stdlib only: the bridge imports the package."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Image:
    """An image for the caller to see in the tool result itself, as an MCP `image` content block (never spilled).
    Return one, or a list mixing it with str/dict items: each item becomes its own content block, in order."""

    data: bytes
    mime_type: str = "image/png"
