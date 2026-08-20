"""Observation bundle: accessibility tree, settle loop, network/console deltas.

Pipeline:
  pre_observe(tab, session) → PreObservation
  <perform mutation>
  post_observe(tab, session, pre, timeout, quiet) → str
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .row import size_str
from .session import BrowserSession
from .tab import Tab
from .target import make_target

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


# A tree signature: (role, name, props) multiset over the same nodes the
# renderer emits, used by the observation diff.
TreeSig = Counter


def _build_lines(
    nodes_by_id: dict[str, dict],
    children_map: dict[str, list[str]],
    node_id: str,
    depth: int,
    max_depth: int,
    lines: list[str],
    sig: TreeSig,
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
            _build_lines(
                nodes_by_id, children_map, child_id, depth, max_depth, lines, sig
            )
        return

    name = _node_name(node)
    props = _node_props(node)
    indent = "  " * depth
    label = f"{indent}{role}"
    if name:
        label += f" {name!r}"
    label += props
    lines.append(label)
    sig[(role, name, props)] += 1

    if role in LEAF_ROLES:
        return

    for child_id in children_map.get(node_id, []):
        _build_lines(
            nodes_by_id, children_map, child_id, depth + 1, max_depth, lines, sig
        )


async def build_tree_sig(tab: "Tab", max_depth: int = 6) -> tuple[list[str], TreeSig]:
    """Compact accessibility tree from CDP Accessibility.getFullAXTree.

    Returns (indented text lines, signature multiset). The signature counts
    exactly the nodes the lines render — same roles, same depth cap — so a
    diff of two signatures describes what a reader of the two trees would see
    change.
    """
    result = await tab._exec("Accessibility.getFullAXTree", {})

    sig: TreeSig = Counter()
    nodes = result.get("nodes", [])
    if not nodes:
        return ["(empty tree)"], sig

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
        _build_lines(nodes_by_id, children_map, root_id, 0, max_depth, lines, sig)

    return lines or ["(empty tree)"], sig


async def build_tree(tab: "Tab", max_depth: int = 6) -> list[str]:
    lines, _ = await build_tree_sig(tab, max_depth)
    return lines


async def build_aria_tree(tab: "Tab") -> list[str]:
    """Playwright ariaSnapshot ('ai' mode) — YAML lines with [ref=…] handles.

    Refs are usable as `aria-ref=<ref>` selectors in click/type until the next
    snapshot, navigation, or reattach. Falls back to the AX tree when the
    engine can't inject on this page (the AX tree needs no in-page JS).
    """
    from . import inject

    try:
        text = await inject.aria_snapshot(tab)
    except inject.EngineUnavailable:
        return [
            "(selector engine unavailable on this page — AX tree instead)"
        ] + await build_tree(tab)
    return text.splitlines() or ["(empty tree)"]


async def compose_aria_tree(
    tab: "Tab", session: "BrowserSession"
) -> tuple[list[str], list["Tab"]]:
    """build_aria_tree with OOPIF iframe children inlined, compose_tree-style.

    Each child session's engine carries its own frameSeq, so child refs render
    as f<seq>eN and never collide with the parent's; the `→ <target_id>`
    annotation says which target to pass when acting on a child's ref.
    """
    lines = await build_aria_tree(tab)
    iframe_children = await _discover_iframe_children(tab, session)
    if not iframe_children:
        return lines, []

    child_trees: dict[str, list[str]] = {}
    for child in iframe_children:
        child_trees[child.target_id] = await build_aria_tree(child)
    return (
        _inline_iframe_lines(lines, iframe_children, child_trees),
        iframe_children,
    )


def _inline_iframe_lines(
    lines: list[str],
    iframe_children: list["Tab"],
    child_trees: dict[str, list[str]],
) -> list[str]:
    """Insert child tree lines under iframe lines with a → target_id marker.

    parentFrameId already guarantees these are this tab's children; pairing a
    specific iframe line with a specific child is best-effort document order.
    """
    result_lines: list[str] = []
    used: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.lower().lstrip("- ").startswith("iframe"):
            matched_child: "Tab | None" = None
            for child in iframe_children:
                if child.target_id not in used:
                    matched_child = child
                    break
            if matched_child is not None:
                ctid = matched_child.target_id
                used.add(ctid)
                result_lines.append(f"{line} → {ctid}")
                child_indent = indent + "  "
                for child_line in child_trees.get(ctid, []):
                    result_lines.append(child_indent + child_line)
                continue
        result_lines.append(line)
    return result_lines


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
) -> tuple[list[str], list["Tab"], dict[str, TreeSig]]:
    """Build accessibility tree with iframe children inlined.

    Returns (lines, iframe_child_tabs, signatures keyed by target_id).
    """

    # Get base tree
    lines, sig = await build_tree_sig(tab, max_depth=max_depth)
    sigs: dict[str, TreeSig] = {tab.target_id: sig}
    iframe_children = await _discover_iframe_children(tab, session)

    if not iframe_children:
        return lines, [], sigs

    child_trees: dict[str, list[str]] = {}
    for child in iframe_children:
        child_lines, child_sig = await build_tree_sig(child, max_depth=max_depth - 2)
        child_trees[child.target_id] = child_lines
        sigs[child.target_id] = child_sig
    return (
        _inline_iframe_lines(lines, iframe_children, child_trees),
        iframe_children,
        sigs,
    )


# ---------------------------------------------------------------------------
# Settle loop
# ---------------------------------------------------------------------------


async def settle(
    tabs: list["Tab"],
    timeout: float = 5.0,
    quiet: float = 0.5,
) -> int:
    """Wait for network idle across all tabs.

    Polls each session's in-memory _inflight map (maintained by
    CDPSession._handle_event) — O(1) per iteration, no DuckDB round-trip
    on the kernel loop.  Returns settle time in ms.

    Reads it through inflight_count() rather than len(), which is what drops
    streamed responses and ages out requests that never report a terminal event —
    without it, one SSE connection pins the tab at the full deadline forever.
    """
    start = time.monotonic()
    deadline = start + timeout
    last_activity = time.monotonic()

    while True:
        now = time.monotonic()
        if now >= deadline:
            break

        # Count inflight requests across all tabs
        inflight = sum(tab._session.inflight_count() for tab in tabs)

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
    # None → no diff available (browser_open, or the page navigated);
    # [] → diff computed, nothing changed.
    changes: list[str] | None = None


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
    # target_id → AX-tree signature for the observation diff. None (the
    # browser_open path constructs PreObservation directly) suppresses the
    # diff rather than reporting a fresh document as all-appeared.
    tree_sigs: dict[str, TreeSig] | None = None
    url: str = ""
    # True when the engine's MutationObserver was armed — post_observe reads
    # it back to catch presentational reveals the AX diff can't see.
    dom_watch: bool = False


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
    """Capture state before a mutation.

    The signature capture fetches the same AX trees post_observe will — same
    depths (compose_tree's 8, children at 6) — so the diff compares like with
    like. Best-effort: a page the AX domain chokes on still gets its mutation
    observed, just without the changes section.
    """
    from . import inject

    iframe_children = await _discover_iframe_children(tab, session)
    all_tabs = [tab] + iframe_children
    tree_sigs: dict[str, TreeSig] | None = {}
    try:
        _, tree_sigs[tab.target_id] = await build_tree_sig(tab, max_depth=8)
        for child in iframe_children:
            _, tree_sigs[child.target_id] = await build_tree_sig(child, max_depth=6)
    except Exception:
        tree_sigs = None
    return PreObservation(
        iframe_children=iframe_children,
        snapshots=_snapshot_max_ids(all_tabs),
        tree_sigs=tree_sigs,
        url=tab.url,
        dom_watch=await inject.start_dom_watch(tab),
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
# Tree diff
# ---------------------------------------------------------------------------

_DIFF_MAX_LINES = 12


def _diff_one(pre: TreeSig, post: TreeSig) -> tuple[Counter, Counter, list[tuple]]:
    """(appeared, gone, changed) between two signatures.

    An entry removed and added under the same (role, name) with different
    props is a state change, not a departure plus an arrival — that pairing
    is what turns "− button 'Save' [disabled] / + button 'Save'" into
    "~ button 'Save' [disabled] → enabled".
    """
    added = post - pre
    removed = pre - post
    changed: list[tuple] = []
    removed_by_rn: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for (role, name, props), cnt in removed.items():
        removed_by_rn.setdefault((role, name), []).append((props, cnt))
    for key in list(added):
        role, name, new_props = key
        buckets = removed_by_rn.get((role, name))
        if not buckets:
            continue
        old_props, old_cnt = buckets[0]
        n = min(added[key], old_cnt)
        changed.append((role, name, old_props, new_props, n))
        added[key] -= n
        removed[(role, name, old_props)] -= n
        if old_cnt - n:
            buckets[0] = (old_props, old_cnt - n)
        else:
            buckets.pop(0)
            if not buckets:
                del removed_by_rn[(role, name)]
    return +added, +removed, changed


def _entry_str(role: str, name: str, props: str) -> str:
    label = role
    if name:
        label += f" {name!r}"
    return label + props


def tree_diff_lines(
    pre_sigs: dict[str, TreeSig],
    post_sigs: dict[str, TreeSig],
    main_target: str,
) -> list[str]:
    """Render the appeared/gone/changed summary, main tab first.

    An iframe present on only one side reads as its whole tree
    appearing/disappearing, which is accurate: the frame itself came or went.
    """
    detail: list[str] = []
    totals = [0, 0, 0]
    targets = [main_target] + [t for t in post_sigs if t != main_target]
    targets += [t for t in pre_sigs if t not in post_sigs]
    for target in targets:
        added, removed, changed = _diff_one(
            pre_sigs.get(target) or Counter(), post_sigs.get(target) or Counter()
        )
        totals[0] += sum(added.values())
        totals[1] += sum(removed.values())
        totals[2] += sum(n for *_, n in changed)
        prefix = "" if target == main_target else f"{target}  "
        for (role, name, props), cnt in sorted(added.items(), key=lambda kv: -kv[1]):
            mult = f" ×{cnt}" if cnt > 1 else ""
            detail.append(f"+ {prefix}{_entry_str(role, name, props)}{mult}")
        for role, name, old, new, n in changed:
            mult = f" ×{n}" if n > 1 else ""
            old_s = old.strip() or "[none]"
            new_s = new.strip() or "[none]"
            detail.append(
                f"~ {prefix}{_entry_str(role, name, '')} {old_s} → {new_s}{mult}"
            )
        for (role, name, props), cnt in sorted(removed.items(), key=lambda kv: -kv[1]):
            mult = f" ×{cnt}" if cnt > 1 else ""
            detail.append(f"- {prefix}{_entry_str(role, name, props)}{mult}")

    if not detail:
        return []
    header = f"changes: {totals[0]} appeared, {totals[1]} gone, {totals[2]} changed"
    if len(detail) > _DIFF_MAX_LINES:
        hidden = len(detail) - _DIFF_MAX_LINES
        detail = detail[:_DIFF_MAX_LINES] + [f"… +{hidden} more (full tree below)"]
    return [header] + ["  " + d for d in detail]


def _dom_delta_line(rec: dict) -> str | None:
    """The AX-silent fallback line — presentational nodes came or went."""
    added = rec.get("added") or 0
    removed = rec.get("removed") or 0
    if not added and not removed:
        return None
    counts = Counter(rec.get("samples") or [])
    sample = ", ".join(f"{s} ×{n}" if n > 1 else s for s, n in counts.most_common(4))
    line = f"changes: none in the AX tree — dom: +{added} −{removed} elements"
    if sample:
        line += f" ({sample})"
    return line


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_observation(obs: Observation) -> str:
    """Render observation as plain text."""
    parts: list[str] = []

    # Header
    parts.append(f"url: {obs.url} (settled in {obs.settle_ms}ms)")
    parts.append("")

    # Changes — what this mutation did to the AX tree, before the full tree.
    if obs.changes is not None:
        if obs.changes:
            parts.extend(obs.changes)
        else:
            parts.append("changes: none (AX tree unchanged)")
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
    tree_lines, fresh_iframes, post_sigs = await compose_tree(tab, session)

    # Diff needs a pre signature *and* the same document — after a navigation
    # the whole tree is new, and reporting it as a few hundred appearances
    # would bury the one section that exists to be short.
    from . import inject

    dom_rec = await inject.read_dom_watch(tab) if pre.dom_watch else None
    changes: list[str] | None = None
    if pre.tree_sigs is not None:
        if tab.url == pre.url:
            changes = tree_diff_lines(pre.tree_sigs, post_sigs, tab.target_id)
            if not changes and dom_rec:
                line = _dom_delta_line(dom_rec)
                if line:
                    changes = [line]
        else:
            changes = ["changes: (page navigated — tree replaced)"]

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
        changes=changes,
    )

    text = format_observation(obs)

    # Detect blocking parent dialogs (iframe targets only)
    warnings = await _detect_parent_dialogs(tab, session)
    if warnings:
        text += "\n\n" + "\n".join(warnings)

    if extra_header:
        text = extra_header + "\n\n" + text

    return text
