"""Selector resolution — Playwright-style selectors to CDP or JS expressions.

resolve() returns a Selector with two fields:
  css  — raw CSS string for DOM.querySelector (CDP path, no JS eval)
  js   — JS expression for Runtime.evaluate (fallback path)

Plain CSS selectors populate both; custom selectors (text=, role=, label=,
:has-text) set css=None — the caller must use the JS path.
"""

import json
import re
from dataclasses import dataclass

_ROLE_CSS: dict[str, str] = {
    "button": 'button, [role="button"], input[type="button"], input[type="submit"]',
    "link": 'a[href], [role="link"]',
    "textbox": 'input:not([type]), input[type="text"], input[type="email"], input[type="search"], input[type="url"], input[type="password"], textarea, [role="textbox"]',
    "checkbox": 'input[type="checkbox"], [role="checkbox"]',
    "radio": 'input[type="radio"], [role="radio"]',
    "heading": 'h1, h2, h3, h4, h5, h6, [role="heading"]',
    "listitem": 'li, [role="listitem"]',
    "tab": '[role="tab"]',
    "tabpanel": '[role="tabpanel"]',
    "option": 'option, [role="option"]',
    "combobox": 'select, [role="combobox"]',
}


@dataclass(frozen=True, slots=True)
class Selector:
    css: str | None
    js: str


# Visibility *ranks* candidates, it does not filter them: a match that is
# present but invisible is still a match — apps routinely keep the real control
# off-screen behind a styled proxy — so the rule is "prefer a visible match,
# fall back to the first one".
#
# Every custom form runs this same rule. `text=` used to be the only one that
# looked at visibility at all, and it looked at `offsetWidth` alone, so
# `text=Save` skipped a hidden button while `role=button[name="Save"]` returned
# it and the click landed on nothing. `offsetHeight` is in the test because a
# short-and-wide element is as visible as a tall-and-narrow one.
#
# Every builder below must return a single *expression* — `Tab._resolve` wraps
# it as `!!(expr)` — which is why these are IIFEs rather than statements.
_PICK_FN = (
    "function(els){"
    " return els.find(function(el){"
    " return el.offsetWidth > 0 || el.offsetHeight > 0; }) || els[0] || null; }"
)


def _pick(candidates_js: str) -> str:
    """Wrap a candidate-array expression in the shared visibility-ranked pick."""
    return f"(function() {{ var pick = {_PICK_FN}; return pick({candidates_js}); }})()"


# Shadow-piercing querySelectorAll. Chrome's own WebUI pages (chrome://extensions,
# chrome://settings) and any web-component-heavy app are built entirely of shadow
# roots, which plain querySelectorAll never descends into — a named function
# expression so it can recurse from inside an IIFE. Costs nothing extra where
# there is no shadow DOM: it only recurses past an element with a real
# `el.shadowRoot` (open-mode; closed roots are unreachable from outside by design).
_DEEP_QSA_FN = (
    "function deepQSA(root, sel) {"
    " var out = Array.prototype.slice.call(root.querySelectorAll(sel));"
    " var all = root.querySelectorAll('*');"
    " for (var i = 0; i < all.length; i++) {"
    " if (all[i].shadowRoot) out = out.concat(deepQSA(all[i].shadowRoot, sel)); }"
    " return out; }"
)


def _deep_qsa(sel_js: str, root: str = "document") -> str:
    """Shadow-piercing querySelectorAll(sel_js), as a JS array-valued expression."""
    return f"(function() {{ var deepQSA = {_DEEP_QSA_FN}; return deepQSA({root}, {sel_js}); }})()"


# A string that is unambiguously a selector rather than a JS expression.
# `resolve()` itself can't answer this — its fallback treats *anything*
# unrecognised as CSS, which is right for `click`/`type_text`, where a selector
# is the only thing a caller can mean. `ready=` is the one parameter that
# accepts either, so it needs the question asked separately, and it needs the
# answer to agree with `resolve()` about what a selector looks like.
#
# The bare-name branch is the whole reason this exists: `ready="main"` and
# `ready="my-app"` are ordinary CSS, and the hand-rolled
# `startswith((".", "#", "[", "data-"))` test that used to live in
# `Tab._await_ready_signal` sent them down the JS path to be evaluated as
# identifiers. Anchored and dot-free, so a real expression (`window.ready`,
# `app.isLoaded`) still reads as JS.
_BARE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_SELECTOR_PREFIXES = (".", "#", "[", "data-", "text=", "role=", "label=")


def looks_like_selector(s: str) -> bool:
    """True if *s* should be resolved as a selector rather than evaluated as JS."""
    if s.startswith(_SELECTOR_PREFIXES):
        return True
    if ":has-text(" in s:
        return True
    return bool(_BARE_NAME.match(s))


def resolve(selector: str) -> Selector:
    """Resolve a Playwright-style selector string.

    Supported patterns:
      text=Submit               → text content match (JS only)
      button:has-text('OK')     → CSS base + text filter (JS only)
      role=button[name="Save"]  → ARIA role + accessible name (JS only)
      label=Username            → input by associated label (JS only)
      .css-selector             → DOM.querySelector (CDP + JS)
    """
    if selector.startswith("text="):
        return Selector(css=None, js=_text_selector(selector[5:]))

    m = re.match(r'^role=(\w+)(?:\[name([*^]?=)["\']?(.+?)["\']?\])?$', selector)
    if m:
        return Selector(css=None, js=_role_selector(m))

    if selector.startswith("label="):
        return Selector(css=None, js=_label_selector(selector[6:]))

    m = re.match(r"^(.+?):has-text\(['\"](.+?)['\"]\)$", selector)
    if m:
        return Selector(css=None, js=_has_text_selector(m))

    return Selector(css=selector, js=f"document.querySelector({json.dumps(selector)})")


def _text_selector(text: str) -> str:
    # Sorted before the pick, not after: shortest textContent is the tightest
    # match (the button, not the <div> wrapping it), so ranking by visibility
    # within that order returns the tightest *visible* one.
    t = json.dumps(text)
    return _pick(
        f"{_deep_qsa(json.dumps('*'))}"
        f".filter(function(el) {{ return el.textContent.trim() === {t}"
        f" || el.getAttribute('aria-label') === {t}; }})"
        f".sort(function(a, b) {{ return a.textContent.length - b.textContent.length; }})"
    )


def _role_selector(m: re.Match) -> str:
    role, op, name = m.group(1), m.group(2), m.group(3)
    css = _ROLE_CSS.get(role, f'[role="{role}"]')
    if not name:
        return _pick(_deep_qsa(json.dumps(css)))
    n = json.dumps(name)
    if op == "*=":
        cmp = (
            f"el.textContent.trim().includes({n})"
            f" || (el.getAttribute('aria-label') || '').includes({n})"
            f" || (el.getAttribute('title') || '').includes({n})"
        )
    elif op == "^=":
        cmp = (
            f"el.textContent.trim().startsWith({n})"
            f" || (el.getAttribute('aria-label') || '').startsWith({n})"
            f" || (el.getAttribute('title') || '').startsWith({n})"
        )
    else:
        cmp = (
            f"el.textContent.trim() === {n}"
            f" || el.getAttribute('aria-label') === {n}"
            f" || el.getAttribute('title') === {n}"
            f" || el.value === {n}"
            f" || (el.labels && Array.from(el.labels).some(l => l.textContent.trim() === {n}))"
        )
    return _pick(
        f"{_deep_qsa(json.dumps(css))}.filter(function(el) {{ return {cmp}; }})"
    )


def _label_selector(label_text: str) -> str:
    # The pick ranks the *labels*, then the control is read off the winner —
    # a hidden label's control is hidden with it, so ranking the controls
    # instead would only re-derive the same order one indirection later.
    return (
        f"(function() {{"
        f" var pick = {_PICK_FN};"
        f" var deepQSA = {_DEEP_QSA_FN};"
        f" var lbl = pick(deepQSA(document, 'label')"
        f"   .filter(function(l) {{"
        f"     return l.textContent.trim() === {json.dumps(label_text)}; }}));"
        f" if (!lbl) return null;"
        # `for=` referencing an id inside the same shadow root: getRootNode()
        # is the ShadowRoot in that case, and DocumentOrShadowRoot gives it
        # its own getElementById — the global `document` one can't see in.
        f" if (lbl.htmlFor) return lbl.getRootNode().getElementById(lbl.htmlFor);"
        f" return lbl.querySelector('input, textarea, select');"
        f"}})()"
    )


def _has_text_selector(m: re.Match) -> str:
    css_base, text = m.group(1), m.group(2)
    css_expanded = _ROLE_CSS.get(css_base, css_base)
    t = json.dumps(text)
    return _pick(
        f"{_deep_qsa(json.dumps(css_expanded))}"
        f".filter(function(el) {{ return el.textContent.trim().includes({t})"
        f" || (el.getAttribute('aria-label') || '').includes({t}); }})"
    )
