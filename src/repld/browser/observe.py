"""Observation bundle: accessibility tree, settle loop, network/console deltas.

Pipeline:
  pre_observe(tab, session) → PreObservation
  <perform mutation>
  post_observe(tab, session, pre, timeout, quiet) → str
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .row import size_str
from .session import BrowserSession
from .tab import Tab

# ---------------------------------------------------------------------------
# Role filtering sets
# ---------------------------------------------------------------------------

SKIP_ROLES: frozenset[str] = frozenset(
    {
        "StaticText",
        "InlineTextBox",
        "generic",
        "none",
        "presentational",
        "LineBreak",
        "ignored",
        "unknown",
    }
)

LEAF_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "switch",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "cell",
        "gridcell",
        "columnheader",
        "rowheader",
        "slider",
        "spinbutton",
        "meter",
        "progressbar",
        "image",
        "img",
    }
)


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


def _node_name(node: dict) -> str:
    """Extract the best human-readable name from an AX node."""
    val = node.get("name", {}).get("value", "")
    return val[:55].strip()


def _node_role(node: dict) -> str:
    return node.get("role", {}).get("value", "")


def _node_props(node: dict) -> str:
    """Extract interesting boolean properties as a compact string."""
    props: list[str] = []
    for prop in node.get("properties") or []:
        pname = prop.get("name", "")
        pval = prop.get("value", {}).get("value")
        if pname in (
            "checked",
            "disabled",
            "expanded",
            "selected",
            "pressed",
            "invalid",
        ):
            if pval not in (None, False, "false", "mixed"):
                props.append(
                    f"{pname}={pval!r}" if pval not in (True, "true") else pname
                )
    return (" [" + ", ".join(props) + "]") if props else ""


def _build_lines(
    nodes_by_id: dict[str, dict],
    children_map: dict[str, list[str]],
    node_id: str,
    depth: int,
    max_depth: int,
    lines: list[str],
) -> None:
    if depth > max_depth:
        return
    node = nodes_by_id.get(node_id)
    if node is None:
        return
    role = _node_role(node)
    if role in SKIP_ROLES:
        # Still recurse through skipped roles
        for child_id in children_map.get(node_id, []):
            _build_lines(nodes_by_id, children_map, child_id, depth, max_depth, lines)
        return

    name = _node_name(node)
    props = _node_props(node)
    indent = "  " * depth
    label = f"{indent}{role}"
    if name:
        label += f" {name!r}"
    label += props
    lines.append(label)

    if role in LEAF_ROLES:
        return

    for child_id in children_map.get(node_id, []):
        _build_lines(nodes_by_id, children_map, child_id, depth + 1, max_depth, lines)


async def build_tree(tab: "Tab", max_depth: int = 6) -> list[str]:
    """Compact accessibility tree from CDP Accessibility.getFullAXTree.

    Returns list of indented text lines.
    """
    result = await tab._exec("Accessibility.getFullAXTree", {})

    nodes = result.get("nodes", [])
    if not nodes:
        return ["(empty tree)"]

    nodes_by_id: dict[str, dict] = {n["nodeId"]: n for n in nodes}
    children_map: dict[str, list[str]] = {}

    for node in nodes:
        nid = node["nodeId"]
        child_ids = node.get("childIds") or []
        children_map[nid] = child_ids

    # Find roots: nodes that are not a child of any other node
    all_children: set[str] = set()
    for cids in children_map.values():
        all_children.update(cids)
    root_ids = [n["nodeId"] for n in nodes if n["nodeId"] not in all_children]

    lines: list[str] = []
    for root_id in root_ids:
        _build_lines(nodes_by_id, children_map, root_id, 0, max_depth, lines)

    return lines or ["(empty tree)"]


# ---------------------------------------------------------------------------
# Iframe discovery + composed tree
# ---------------------------------------------------------------------------


async def _discover_iframe_children(
    tab: "Tab", session: "BrowserSession"
) -> list["Tab"]:
    """Find attached tabs whose parentFrameId matches this tab's target.

    Uses CDP target metadata directly — no JS eval or URL heuristics.
    """
    parent_id = tab._session.chrome_target_id
    children: list["Tab"] = []
    for cdp_session in session._sessions.values():
        info = cdp_session.target_info
        if info.get("type") != "iframe":
            continue
        if info.get("parentFrameId") == parent_id:
            target_id = info.get("targetId", "")
            children.append(Tab(cdp_session, target_id, tab._port))
    return children


# ---------------------------------------------------------------------------
# Parent dialog detection (iframe observations only)
# ---------------------------------------------------------------------------

_DIALOG_DETECT_JS = """\
Array.from(document.querySelectorAll(
    '[role="dialog"][aria-modal="true"], dialog[open]'
))
.filter(el => el.offsetWidth > 0)
.map(el => ({
    title: (el.querySelector('h1,h2,h3,[class*="Title"]') || {}).textContent?.trim() || '',
    buttons: Array.from(el.querySelectorAll('button'))
        .map(b => b.textContent.trim()).filter(Boolean)
}))
"""


async def _detect_parent_dialogs(tab: "Tab", session: "BrowserSession") -> list[str]:
    """Report a visible modal on the page that *owns* this iframe.

    Only that page. Scanning every attached page-type target instead was wrong
    twice over: a modal in some unrelated tab got reported as this iframe's
    parent dialog, and the sweep cost one `Runtime.evaluate` round trip per
    attached page on every iframe observation — which scales with how many tabs
    happen to be attached, not with anything about the page being observed.

    `parentFrameId` names the real parent, and is the same field
    `_discover_iframe_children` matches on in the other direction.
    """
    info = tab._session.target_info
    if info.get("type") != "iframe":
        return []
    parent_frame = info.get("parentFrameId")
    if not parent_frame:
        return []

    parent_cdp = next(
        (
            s
            for s in session._sessions.values()
            if s.target_info.get("targetId") == parent_frame
            and s.target_info.get("type") == "page"
        ),
        None,
    )
    if parent_cdp is None:
        return []

    from . import make_target

    parent_tab = Tab(parent_cdp, parent_frame, tab._port)
    try:
        result = await parent_tab.js(_DIALOG_DETECT_JS)
    except Exception:
        return []
    if not isinstance(result, list):
        return []

    parent_tid = make_target(tab._port, parent_frame)
    warnings: list[str] = []
    for dialog in result:
        if not isinstance(dialog, dict):
            continue
        title = dialog.get("title") or "untitled"
        buttons = dialog.get("buttons", [])
        btn_str = " / ".join(f"[{b}]" for b in buttons) if buttons else ""
        line = f"warning: parent dialog ({parent_tid}): {title}"
        if btn_str:
            line += f" -- {btn_str}"
        warnings.append(line)
    return warnings


async def compose_tree(
    tab: "Tab",
    session: "BrowserSession",
    max_depth: int = 8,
) -> tuple[list[str], list["Tab"]]:
    """Build accessibility tree with iframe children inlined.

    Returns (lines, iframe_child_tabs).
    """

    # Get base tree
    lines = await build_tree(tab, max_depth=max_depth)
    iframe_children = await _discover_iframe_children(tab, session)

    if not iframe_children:
        return lines, []

    # Get trees for all children
    child_trees: dict[str, list[str]] = {}
    for child in iframe_children:
        child_lines = await build_tree(child, max_depth=max_depth - 2)
        child_trees[child.target_id] = child_lines

    # Inline child trees under Iframe nodes in the base tree
    # We annotate Iframe lines with → target_id and insert child lines after
    result_lines: list[str] = []
    used: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        if stripped.lower().startswith("iframe"):
            # Assign next unmatched child (parentFrameId already
            # guarantees correct children; order is best-effort)
            matched_child: "Tab | None" = None
            for child in iframe_children:
                if child.target_id not in used:
                    matched_child = child
                    break

            if matched_child is not None:
                ctid = matched_child.target_id
                used.add(ctid)
                # Annotate the Iframe line
                result_lines.append(f"{line} → {ctid}")
                # Insert child tree lines with extra indent
                child_indent = indent + "  "
                for child_line in child_trees.get(ctid, []):
                    result_lines.append(child_indent + child_line)
                continue

        result_lines.append(line)

    return result_lines, iframe_children


# ---------------------------------------------------------------------------
# Settle loop
# ---------------------------------------------------------------------------


async def settle(
    tabs: list["Tab"],
    timeout: float = 5.0,
    quiet: float = 0.5,
) -> int:
    """Wait for network idle across all tabs.

    Polls each session's in-memory _inflight set (maintained by
    CDPSession._handle_event) — O(1) per iteration, no DuckDB round-trip
    on the kernel loop.  Returns settle time in ms.
    """
    start = time.monotonic()
    deadline = start + timeout
    last_activity = time.monotonic()

    while True:
        now = time.monotonic()
        if now >= deadline:
            break

        # Count inflight requests across all tabs
        inflight = sum(len(tab._session._inflight) for tab in tabs)

        if inflight > 0:
            last_activity = now
        elif now - last_activity >= quiet:
            # Settled
            break

        await asyncio.sleep(0.05)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return elapsed_ms


# ---------------------------------------------------------------------------
# Observation data structures
# ---------------------------------------------------------------------------


@dataclass
class NetworkEntry:
    target: str
    method: str
    status: int
    path: str
    time_ms: int | None
    size: int
    is_asset: bool


@dataclass
class Observation:
    url: str
    settle_ms: int
    tree: list[str]
    network: list[NetworkEntry]
    console: list[str]


# ---------------------------------------------------------------------------
# Pre/post observation
# ---------------------------------------------------------------------------


@dataclass
class PreObservation:
    """State captured before the mutation."""

    iframe_children: list["Tab"] = field(default_factory=list)
    # target_id → events.rowid high-water mark. One cutoff for both the network
    # and console deltas — see _snapshot_max_ids for why that is sufficient.
    # These were two separate fields until the cutoff moved to `events`; they
    # only ever held the same values, in both construction sites.
    snapshots: dict[str, int] = field(default_factory=dict)


def _snapshot_max_ids(tabs: list["Tab"]) -> dict[str, int]:
    """Record each tab's event-log high-water mark, keyed by target_id.

    Read from `events` rather than from `har_entries` / `console_entries`,
    which is both cheaper and sufficient. Cheaper because those are views over
    a CTE chain — `MAX(id)` on them evaluates the whole thing (measured 22 ms
    at 2k events, 82 ms at 40k) where `MAX(rowid)` on the base table is a
    counter read (~1.5 ms flat). This runs synchronously on the kernel loop,
    once per tab *and* once per iframe child, before every observed mutation,
    so a page with three iframes was stalling the loop for a fifth of a second
    before the click even fired.

    Sufficient because both views derive their `id` from `events.rowid`
    (`har_entries` as `rh.rowid` / `ws.first_rowid`, `console_entries` as
    `rowid`), and rowids are monotonic — the FIFO prune deletes the oldest
    rows and DuckDB does not reuse their ids. So an events-level cutoff sits
    at or above either view's own max, and `WHERE id > cutoff` selects exactly
    the rows added after the snapshot: no pre-existing row can be above it,
    and no new row can be below it. One query now covers both deltas.
    """
    result: dict[str, int] = {}
    for tab in tabs:
        rows = tab._session.query("SELECT COALESCE(MAX(rowid), 0) FROM events")
        result[tab.target_id] = rows[0][0]
    return result


async def pre_observe(tab: "Tab", session: "BrowserSession") -> PreObservation:
    """Capture state before a mutation. Fast — no blocking."""
    iframe_children = await _discover_iframe_children(tab, session)
    all_tabs = [tab] + iframe_children
    return PreObservation(
        iframe_children=iframe_children,
        snapshots=_snapshot_max_ids(all_tabs),
    )


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

# URL path truncation length
_PATH_TRUNCATE = 80


def _truncate_path(url: str) -> str:
    """Extract path + truncated query from a URL."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    query = parsed.query
    if query:
        if len(query) > 40:
            query = query[:40] + "…"
        path = f"{path}?{query}"
    return path[:_PATH_TRUNCATE]


def network_delta(tabs: list["Tab"], pre_ids: dict[str, int]) -> list[NetworkEntry]:
    """Query each tab's DuckDB for entries with id > snapshot."""
    entries: list[NetworkEntry] = []
    for tab in tabs:
        min_id = pre_ids.get(tab.target_id, 0)
        rows = tab._session.query(
            """SELECT method, status, url, time_ms, size, is_asset
               FROM har_summary
               WHERE id > ?
               ORDER BY id ASC""",
            [min_id],
        )

        for row in rows:
            method = row[0] or ""
            status = row[1] or 0
            url = row[2] or ""
            time_ms = row[3]
            size = row[4] or 0
            is_asset = bool(row[5])

            path = _truncate_path(url)

            entries.append(
                NetworkEntry(
                    target=tab.target_id,
                    method=method,
                    status=status,
                    path=path,
                    time_ms=time_ms,
                    size=size,
                    is_asset=is_asset,
                )
            )

    return entries


def console_delta(tabs: list["Tab"], pre_ids: dict[str, int]) -> list[str]:
    """Query each tab's console_entries for new entries since snapshot.

    Returns lines tagged with target + level.
    """
    lines: list[str] = []
    for tab in tabs:
        min_id = pre_ids.get(tab.target_id, 0)
        rows = tab._session.query(
            "SELECT level, text FROM console_entries WHERE id > ? ORDER BY id ASC",
            [min_id],
        )

        for row in rows:
            level = row[0] or "log"
            text = (row[1] or "")[:120]
            lines.append(f"{tab.target_id}  {level}: {text}")

    return lines


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_observation(obs: Observation) -> str:
    """Render observation as plain text."""
    parts: list[str] = []

    # Header
    parts.append(f"url: {obs.url} (settled in {obs.settle_ms}ms)")
    parts.append("")

    # Tree
    tree_count = len(obs.tree)
    parts.append(f"tree ({tree_count} nodes):")
    for line in obs.tree:
        parts.append("  " + line)

    # Network
    parts.append("")
    api_entries = [e for e in obs.network if not e.is_asset]
    asset_entries = [e for e in obs.network if e.is_asset]

    if api_entries or asset_entries:
        total = len(obs.network)
        parts.append(f"network ({total} requests):")
        for e in api_entries:
            time_str = f"{e.time_ms}ms" if e.time_ms is not None else "?"
            parts.append(
                f"  {e.target}  {e.method}  {e.status} {e.path}  {time_str} {size_str(e.size)}"
            )
        if asset_entries:
            total_asset_bytes = sum(e.size for e in asset_entries)
            parts.append(
                f"  + {len(asset_entries)} assets ({size_str(total_asset_bytes)})"
            )
    else:
        parts.append("network (0 requests)")

    # Console
    parts.append("")
    if obs.console:
        parts.append(f"console ({len(obs.console)} messages):")
        for msg in obs.console:
            parts.append(f"  {msg}")
    else:
        parts.append("console (0 messages)")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


async def post_observe(
    tab: "Tab",
    session: "BrowserSession",
    pre: PreObservation,
    *,
    timeout: float = 5.0,
    quiet: float = 0.5,
    extra_header: str | None = None,
) -> str:
    """Settle, build tree, compute deltas, format. Returns observation text.

    extra_header is prepended (e.g. 'target: 9222:f52dfc' for browser_open).
    """
    all_tabs = [tab] + pre.iframe_children

    # Settle across target + iframe children
    settle_ms = await settle(all_tabs, timeout=timeout, quiet=quiet)

    # Build composed tree — re-discovers iframes, which may differ from
    # pre.iframe_children if the mutation added/removed one.
    tree_lines, fresh_iframes = await compose_tree(tab, session)

    # A mutation can spawn a brand-new iframe (e.g. an OAuth popup frame)
    # that wasn't in pre.iframe_children and so never got a quiet-period
    # wait above — settle it too before taking deltas, or its own
    # in-flight requests get counted as "already done" prematurely.
    # _discover_iframe_children makes a fresh Tab per call, so compare by
    # target_id rather than object identity.
    pre_ids = {t.target_id for t in all_tabs}
    new_iframes = [f for f in fresh_iframes if f.target_id not in pre_ids]
    if new_iframes:
        settle_ms += await settle(new_iframes, timeout=timeout, quiet=quiet)

    # Deltas use the post-mutation iframe set so a newly-appeared iframe's
    # network/console activity isn't silently dropped (a stale/missing
    # target_id in pre.snapshots just means "everything is new" — see
    # network_delta/console_delta's pre_ids.get(..., 0) default).
    all_tabs_post = [tab] + fresh_iframes
    # Off the loop. Unlike the pre-snapshot, these genuinely need the views —
    # they return rows, not a high-water mark — so `har_summary WHERE id > ?`
    # re-evaluates the CTE chain at 39–56 ms per tab, once more per iframe,
    # after every observed mutation. `CDPSession.query` takes a fresh cursor
    # precisely so it can be called from another thread while the loop keeps
    # writing through the main connection.
    net_entries, console_lines = await asyncio.gather(
        asyncio.to_thread(network_delta, all_tabs_post, pre.snapshots),
        asyncio.to_thread(console_delta, all_tabs_post, pre.snapshots),
    )

    obs = Observation(
        url=tab.url,
        settle_ms=settle_ms,
        tree=tree_lines,
        network=net_entries,
        console=console_lines,
    )

    text = format_observation(obs)

    # Detect blocking parent dialogs (iframe targets only)
    warnings = await _detect_parent_dialogs(tab, session)
    if warnings:
        text += "\n\n" + "\n".join(warnings)

    if extra_header:
        text = extra_header + "\n\n" + text

    return text
