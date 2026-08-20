"""Injected-engine lifecycle — Playwright's InjectedScript, one instance per document.

The engine (injected_source.SOURCE) is evaluated lazily on first selector use:
an isolated utility world when the page grants one, the main world when it
refuses (`EngineHandle.world` records which). The cached handle is per-document
*and* per-session — `Runtime.executionContextsCleared` (navigation) and
`_reattach_core` (reconnect/reattach: objectIds die with their sessionId) both
call `invalidate`, and any call that hits a stale-context error re-ensures and
retries once, the same shape as `Tab._exec`.

aria-refs (`aria-ref=eN` from a `browser_tree` snapshot) live inside the LIVE
engine instance, so every invalidation kills them — the resolve error tells the
caller to take a fresh snapshot rather than reporting a bare miss.
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import selector as selector_mod
from .injected_source import SOURCE

if TYPE_CHECKING:
    from .tab import Tab

# Unique per kernel so two repld kernels (or repld + Playwright) driving the
# same Chrome never share a world — mirrors Playwright's per-guid world name.
UTILITY_WORLD = f"__repld_utility_{os.getpid()}"

_RESOLVE_TIMEOUT_S = 2.0
_ACTIONABLE_TIMEOUT_S = 2.0
_POLL_S = 0.1

# CDP error texts that mean the cached handle's context or object is gone —
# navigation raced the call, or executionContextsCleared hasn't landed yet.
_STALE_MARKERS = (
    "cannot find context with specified id",
    "execution context was destroyed",
    "could not find object with given id",
    "inspected target navigated or closed",
)


class AmbiguousSelectorError(RuntimeError):
    """Selector matched multiple elements; the message carries the candidate digest."""


class EngineUnavailable(RuntimeError):
    """Neither injection tier works on this page — no selector-based actions."""


@dataclass(frozen=True, slots=True)
class EngineHandle:
    object_id: str
    world: str  # "utility" | "main"


@dataclass(frozen=True, slots=True)
class ResolvedElement:
    object_id: str
    note: str  # "" or "N matches, 1 visible"


def _bootstrap(frame_seq: int) -> str:
    options = json.dumps(
        {
            "isUnderTest": False,
            "sdkLanguage": "javascript",
            "frameSeq": frame_seq,
            "testIdAttributeName": "data-testid",
            "stableRafCount": 1,
            "browserName": "chromium",
            "isUtilityWorld": True,
            "customEngines": [],
        }
    )
    # dom.ts:injectedScript() shape. With the builtin-free __export prefix,
    # module.exports.InjectedScript is a lazy getter — hence the double call.
    return (
        "(() => { const module = {};\n"
        + SOURCE
        + f"\nreturn new (module.exports.InjectedScript())(globalThis, {options}); }})()"
    )


def invalidate(cdp: Any) -> None:
    """Drop the cached engine handle; the next selector call re-instantiates."""
    cdp._injected = None


def _is_stale(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _STALE_MARKERS)


async def ensure_engine(tab: "Tab") -> EngineHandle:
    cdp = tab._session
    if cdp._injected is not None:
        return cdp._injected
    async with cdp._injected_lock:
        if cdp._injected is not None:
            return cdp._injected
        context_id: int | None = None
        try:
            ft = await tab._exec("Page.getFrameTree")
            frame_id = ft["frameTree"]["frame"]["id"]
            world = await tab._exec(
                "Page.createIsolatedWorld",
                {
                    "frameId": frame_id,
                    "worldName": UTILITY_WORLD,
                    "grantUniveralAccess": True,
                },
            )
            context_id = world["executionContextId"]
        except Exception:
            # Tier 2: same engine, main world. Semantics identical — only
            # tamper-isolation is lost.
            context_id = None
        params: dict[str, Any] = {
            "expression": _bootstrap(cdp._frame_seq),
            "returnByValue": False,
        }
        if context_id is not None:
            params["contextId"] = context_id
        try:
            result = await tab._exec("Runtime.evaluate", params)
        except Exception as exc:
            raise EngineUnavailable(
                f"cannot inject selector engine into {tab.url or tab.target_id}: "
                f"{exc} — use tab.js() or coordinate click(x, y) on this page"
            ) from exc
        if "exceptionDetails" in result or "objectId" not in result.get("result", {}):
            detail = result.get("exceptionDetails", {}).get("text", "no objectId")
            raise EngineUnavailable(
                f"selector engine failed to evaluate on {tab.url or tab.target_id}: "
                f"{detail} — use tab.js() or coordinate click(x, y) on this page"
            )
        handle = EngineHandle(
            object_id=result["result"]["objectId"],
            world="utility" if context_id is not None else "main",
        )
        cdp._injected = handle
        return handle


async def call_engine(
    tab: "Tab",
    fn_decl: str,
    args: list[dict],
    *,
    await_promise: bool = False,
    return_by_value: bool = True,
    timeout: float = 30,
) -> dict:
    """Runtime.callFunctionOn against the engine handle, re-ensuring once on staleness.

    `args` entries are CDP CallArguments ({"value": ...} or {"objectId": ...}).
    Returns the raw CDP response (caller inspects result/exceptionDetails).
    """
    handle = await ensure_engine(tab)
    params = {
        "objectId": handle.object_id,
        "functionDeclaration": fn_decl,
        "arguments": args,
        "returnByValue": return_by_value,
        "awaitPromise": await_promise,
    }
    try:
        return await tab._exec("Runtime.callFunctionOn", params, timeout=timeout)
    except RuntimeError as exc:
        if not _is_stale(exc):
            raise
        invalidate(tab._session)
        handle = await ensure_engine(tab)
        params["objectId"] = handle.object_id
        return await tab._exec("Runtime.callFunctionOn", params, timeout=timeout)


# The selection rule, applied to whichever selector in the fallback list
# matches first. Visibility *ranks* candidates, it does not filter them: a
# match that is present but invisible is still a match — apps routinely keep
# the real control off-screen behind a styled proxy — so a single match is
# used whether or not it is visible, and only genuine ambiguity (multiple
# matches with no single visible winner) errors. `__repld_note` is an expando
# on this world's element wrapper, so page JS can't see it from the utility
# world; in the main-world tier it is visible but harmless.
_RESOLVE_FN = """
function(selectors) {
  const injected = this;
  let all = [], used = null;
  for (const sel of selectors) {
    const parsed = injected.parseSelector(sel);
    const found = injected.querySelectorAll(parsed, injected.document);
    if (found.length) { all = found; used = parsed; break; }
  }
  if (!all.length) throw new Error("repld:none");
  let el = all[0];
  if (all.length > 1) {
    const visible = all.filter(e => injected.elementState(e, "visible").matches);
    if (visible.length !== 1)
      throw injected.strictModeViolationError(used, visible.length ? visible : all);
    el = visible[0];
    el.__repld_note = all.length;
  }
  return el;
}
"""


async def resolve_element(
    tab: "Tab", selector: str, *, timeout: float = _RESOLVE_TIMEOUT_S
) -> ResolvedElement:
    """Strict-resolve a repld selector to an element handle. Auto-waits.

    Raises AmbiguousSelectorError immediately on a strict-mode violation (the
    hundredth poll would still be ambiguous), RuntimeError on not-found after
    the deadline.
    """
    return await resolve_engine_selectors(
        tab, selector_mod.translate_fallbacks(selector), selector, timeout=timeout
    )


async def resolve_engine_selectors(
    tab: "Tab",
    selectors: list[str],
    describe: str,
    *,
    timeout: float = _RESOLVE_TIMEOUT_S,
) -> ResolvedElement:
    """resolve_element over already-translated engine selectors.

    `describe` is the caller-facing name used in errors — the repld form, not
    the engine spelling.
    """
    selector = describe
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await call_engine(
            tab, _RESOLVE_FN, [{"value": selectors}], return_by_value=False
        )
        exc_details = result.get("exceptionDetails")
        if exc_details is None:
            obj = result.get("result", {})
            note_r = await call_engine(
                tab,
                "function(el) { const n = el.__repld_note; delete el.__repld_note;"
                " return n || 0; }",
                [{"objectId": obj["objectId"]}],
            )
            n = note_r.get("result", {}).get("value") or 0
            note = f"{n} matches, 1 visible" if n else ""
            return ResolvedElement(object_id=obj["objectId"], note=note)
        desc = exc_details.get("exception", {}).get(
            "description", ""
        ) or exc_details.get("text", "")
        if "strict mode violation" in desc:
            raise AmbiguousSelectorError(
                f"{selector!r} is ambiguous — {_strip_stack(desc)}"
            )
        if selector.startswith("aria-ref="):
            # No point polling: a ref only ever resolves against the engine's
            # last-generated snapshot, and waiting can't bring one back.
            raise RuntimeError(
                f"{selector} not found — refs are from the last browser_tree "
                "snapshot and die on navigation/reattach; take a fresh snapshot"
            )
        if "repld:none" not in desc:
            raise RuntimeError(f"selector {selector!r} failed: {_strip_stack(desc)}")
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"Element not found: {selector}")
        await asyncio.sleep(_POLL_S)


def _strip_stack(desc: str) -> str:
    """First lines of a JS error description, without the '    at …' stack tail."""
    lines = [ln for ln in desc.splitlines() if not ln.lstrip().startswith("at ")]
    return "\n".join(lines).strip()


async def selector_exists(tab: "Tab", selector: str) -> bool:
    """One-shot existence check — no strictness, no visibility, no waiting.

    What `ready=` and `wait_for` poll on: "has it appeared", never "is it
    unambiguous" — a ready signal that matched three nodes has still fired.
    """
    selectors = selector_mod.translate_fallbacks(selector)
    result = await call_engine(
        tab,
        "function(sels) { const injected = this;"
        " for (const s of sels) {"
        "   if (injected.querySelectorAll(injected.parseSelector(s),"
        " injected.document).length) return true; }"
        " return false; }",
        [{"value": selectors}],
    )
    return bool(result.get("result", {}).get("value"))


async def check_actionable(
    tab: "Tab",
    el: ResolvedElement,
    states: tuple[str, ...],
    *,
    selector: str = "",
    timeout: float = _ACTIONABLE_TIMEOUT_S,
) -> None:
    """Wait until the element passes every state check, or raise naming the miss.

    checkElementStates resolves promptly per call ('stable' resolves after
    stableRafCount same-position frames or false on movement — never
    open-ended), so the wait bound lives here. Must run after _ensure_front():
    the stable check counts requestAnimationFrame, which Chrome throttles on
    hidden tabs.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = await call_engine(
            tab,
            "function(node, states) { return this.checkElementStates(node, states); }",
            [{"objectId": el.object_id}, {"value": list(states)}],
            await_promise=True,
        )
        value = result.get("result", {}).get("value")
        if value is None and "exceptionDetails" not in result:
            return
        if value == "error:notconnected":
            raise RuntimeError(
                f"element for {selector!r} detached from the DOM mid-action"
            )
        missing = (value or {}).get("missingState") if isinstance(value, dict) else None
        if loop.time() >= deadline:
            raise RuntimeError(
                f"element not {missing or 'actionable'} after {timeout:.0f}s: {selector}"
            )
        await asyncio.sleep(_POLL_S)


# Pre-dispatch hit test: "what will this click land on" is the honest receipt —
# the post-dispatch DOM may already be re-rendered by the click's own handlers.
_RECEIPT_FN = """
function(target, x, y) {
  const injected = this;
  let hit = injected.document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  const related = !!hit && (target === hit || target.contains(hit) || hit.contains(target));
  const desc = el => {
    let sel = "";
    try { sel = injected.generateSelectorSimple(el); } catch (e) {}
    let preview = "";
    try { preview = injected.previewNode(el); } catch (e) { preview = String(el); }
    return { preview, sel };
  };
  return { related, target: desc(target), hit: hit ? desc(hit) : null };
}
"""


_INTERNAL_SEGMENT = re.compile(
    r'^internal:(role)=(\w+)\[name="(.*)"[si]?\]$'
    r'|^internal:(text)="(.*)"[si]?$'
    r'|^internal:(label)="(.*)"[si]?$'
    r"|^internal:(?:testid|attr)=(\[.+\])$"
)


def _to_repld_selector(sel: str) -> str:
    """Best-effort rewrite of a generated engine selector into repld grammar,
    so a receipt's selector can be pasted straight back into click/type_text.
    Chain segments that don't map (nth=, xpath=) are left in engine spelling —
    honest beats reusable."""
    parts = []
    for seg in sel.split(" >> "):
        m = _INTERNAL_SEGMENT.match(seg)
        if m:
            if m.group(1):
                seg = f'role={m.group(2)}[name="{m.group(3)}"]'
            elif m.group(4):
                seg = f"text={m.group(5)}"
            elif m.group(6):
                seg = f"label={m.group(7)}"
            else:
                seg = m.group(8)
        parts.append(seg)
    return " >> ".join(parts)


async def hit_receipt(tab: "Tab", el: ResolvedElement, x: float, y: float) -> dict:
    """{related, target: {preview, sel}, hit: {preview, sel}|None} for (x, y)."""
    result = await call_engine(
        tab,
        _RECEIPT_FN,
        [{"objectId": el.object_id}, {"value": x}, {"value": y}],
    )
    if "exceptionDetails" in result:
        return {"related": True, "target": {"preview": "", "sel": ""}, "hit": None}
    value = result.get("result", {}).get("value") or {
        "related": True,
        "target": {"preview": "", "sel": ""},
        "hit": None,
    }
    for entry in (value.get("target"), value.get("hit")):
        if entry and entry.get("sel"):
            entry["sel"] = _to_repld_selector(entry["sel"])
    return value


async def aria_snapshot(tab: "Tab") -> str:
    """Playwright ariaSnapshot in 'ai' mode — YAML with [ref=…] handles."""
    result = await call_engine(
        tab,
        "function() { return this.ariaSnapshot(this.document.documentElement,"
        ' {mode: "ai"}); }',
        [],
    )
    if "exceptionDetails" in result:
        detail = result["exceptionDetails"].get("exception", {}).get("description", "")
        raise RuntimeError(f"ariaSnapshot failed: {_strip_stack(detail)}")
    return result.get("result", {}).get("value") or ""


async def read_value(tab: "Tab", el: ResolvedElement) -> str:
    result = await call_engine(
        tab,
        "function(el) { if ('value' in el) return String(el.value);"
        " return el.textContent || ''; }",
        [{"objectId": el.object_id}],
    )
    return result.get("result", {}).get("value") or ""


# The native-setter fallback for framework-controlled inputs. Works from the
# utility world *because* worlds don't share instance-level property
# descriptors: React's _valueTracker patch lives on the main world's wrapper
# and is invisible here, so this reaches the pristine prototype accessor; the
# DOM value itself is world-independent, and the dispatched events cross
# worlds, so React's main-world listeners see an input whose value changed.
_SET_VALUE_FN = """
function(el, text) {
  const win = el.ownerDocument.defaultView;
  const proto = el instanceof win.HTMLTextAreaElement
    ? win.HTMLTextAreaElement.prototype
    : el instanceof win.HTMLInputElement
      ? win.HTMLInputElement.prototype
      : null;
  if (proto) {
    const desc = win.Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, text); else el.value = text;
  } else if (el.isContentEditable) {
    el.textContent = text;
  }
  el.dispatchEvent(new win.Event("input", { bubbles: true }));
  el.dispatchEvent(new win.Event("change", { bubbles: true }));
}
"""


async def set_value_native(tab: "Tab", el: ResolvedElement, text: str) -> None:
    await call_engine(tab, _SET_VALUE_FN, [{"objectId": el.object_id}, {"value": text}])


# Same prototype-setter rationale as _SET_VALUE_FN, for <select>.
_SELECT_NATIVE_FN = """
function(el, option) {
  const win = el.ownerDocument.defaultView;
  const opts = Array.from(el.options);
  let target = opts.find(o => (o.label || o.textContent || "").trim() === option);
  if (!target) target = opts.find(o => o.value === option);
  if (!target)
    return { ok: false,
             options: opts.slice(0, 20).map(o => (o.label || o.textContent || "").trim()) };
  const desc = win.Object.getOwnPropertyDescriptor(win.HTMLSelectElement.prototype, "value");
  if (desc && desc.set) desc.set.call(el, target.value); else el.value = target.value;
  el.dispatchEvent(new win.Event("input", { bubbles: true }));
  el.dispatchEvent(new win.Event("change", { bubbles: true }));
  return { ok: true };
}
"""


async def select_native(tab: "Tab", el: ResolvedElement, option: str) -> dict:
    """Set a native <select> by option label (else value). {ok} or {ok: False, options}."""
    result = await call_engine(
        tab, _SELECT_NATIVE_FN, [{"objectId": el.object_id}, {"value": option}]
    )
    return result.get("result", {}).get("value") or {"ok": False, "options": []}


async def list_option_names(tab: "Tab", limit: int = 20) -> list[str]:
    """Accessible names of the visible role=option elements — the digest that
    makes a select_option miss actionable."""
    result = await call_engine(
        tab,
        "function(limit) { const injected = this;"
        " const els = injected.querySelectorAll("
        "   injected.parseSelector('role=option'), injected.document);"
        " return els.slice(0, limit).map(e => {"
        "   try { return injected.utils.getElementAccessibleNameText(e, false); }"
        "   catch (err) { return (e.textContent || '').trim(); } }); }",
        [{"value": limit}],
    )
    return result.get("result", {}).get("value") or []


async def describe_element(tab: "Tab", el: ResolvedElement) -> tuple[str, str]:
    """(preview, generated selector) for receipts and error digests."""
    result = await call_engine(
        tab,
        "function(el) { let sel = '';"
        " try { sel = this.generateSelectorSimple(el); } catch (e) {}"
        " let preview = ''; try { preview = this.previewNode(el); }"
        " catch (e) { preview = String(el); }"
        " return { preview, sel }; }",
        [{"objectId": el.object_id}],
    )
    value = result.get("result", {}).get("value") or {}
    return value.get("preview", ""), _to_repld_selector(value.get("sel", ""))
