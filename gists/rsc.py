"""RSC — React Server Components wire format parser for como-based apps (LinkedIn)."""

import json
import re

__repld_help__ = "Parse RSC rehydration payloads from como-framework HTML responses."
__repld_usage__ = "from rsc import parse_rehydration, walk_text"


def parse_rehydration(html: str) -> dict[str, str]:
    """Extract __como_rehydration__ from HTML into assembled RSC lines.

    Returns a dict mapping line ID (hex string) to its data payload.
    Chunks split across array elements are concatenated automatically.
    """
    match = re.search(
        r"window\.__como_rehydration__\s*=\s*(\[.*?\])\s*;?\s*</script>",
        html,
        re.DOTALL,
    )
    if not match:
        return {}
    elements = json.loads(match.group(1))
    lines: dict[str, str] = {}
    for el in elements:
        if not isinstance(el, str):
            continue
        for row in el.split("\n"):
            m = re.match(r"^([0-9a-f]+):", row)
            if m:
                lid = m.group(1)
                data = row[m.end() :]
                if lid in lines:
                    lines[lid] += data
                else:
                    lines[lid] = data
    return lines


def parse_tree(data: str) -> list | dict | None:
    """Parse a single RSC line's data payload as JSON."""
    try:
        return json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None


def walk_text(node) -> list[dict]:
    """Walk an RSC component tree and extract all text entries.

    Each entry: {'text': str, 'weight': str, 'size': str, 'slug': str|None}
    - weight: 'bold', 'normal', etc (from fontWeight)
    - slug: LinkedIn public_id if the text was a profile link
    """
    results: list[dict] = []
    _walk_text_inner(node, results)
    return results


def _walk_text_inner(node, results: list):
    if isinstance(node, str):
        return
    if isinstance(node, list):
        if len(node) >= 4 and node[0] == "$":
            props = node[3] if isinstance(node[3], dict) else {}
            tp = props.get("textProps")
            if tp and isinstance(tp, dict):
                entry = _extract_text_entry(tp)
                if entry:
                    results.append(entry)
            for v in props.values():
                if isinstance(v, (list, dict)):
                    _walk_text_inner(v, results)
        else:
            for item in node:
                _walk_text_inner(item, results)
    elif isinstance(node, dict):
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk_text_inner(v, results)


def _extract_text_entry(text_props: dict) -> dict | None:
    """Extract text + metadata from a textProps dict."""
    children = text_props.get("children", [])
    parts: list[str] = []
    slug: str | None = None

    if isinstance(children, str):
        parts.append(children)
    elif isinstance(children, list):
        for child in children:
            if isinstance(child, str):
                parts.append(child)
            elif isinstance(child, list) and len(child) >= 4 and child[0] == "$":
                cp = child[3] if isinstance(child[3], dict) else {}
                # Linked text
                link_children = cp.get("children", [])
                if isinstance(link_children, list):
                    for lc in link_children:
                        if isinstance(lc, str):
                            parts.append(lc)
                elif isinstance(link_children, str):
                    parts.append(link_children)
                # Extract /in/ slug from navigation action
                for act in cp.get("action", {}).get("actions", []):
                    url = (
                        act.get("value", {})
                        .get("content", {})
                        .get("url", {})
                        .get("url", "")
                    )
                    m = re.search(r"/in/([^/]+)", url)
                    if m:
                        slug = m.group(1)

    text = "".join(parts).strip()
    if not text:
        return None
    return {
        "text": text,
        "weight": text_props.get("fontWeight", ""),
        "size": text_props.get("fontSize", ""),
        "slug": slug,
    }


def find_snippet_ids(data: str) -> list[str]:
    """Extract ordered member IDs from snippetSlots in an RSC line."""
    return re.findall(r'"(ACoAA[^"]+)":"SearchResultssnippet_', data)
