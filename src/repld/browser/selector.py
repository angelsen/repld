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
        f"Array.from(document.querySelectorAll('*'))"
        f".filter(function(el) {{ return el.textContent.trim() === {t}"
        f" || el.getAttribute('aria-label') === {t}; }})"
        f".sort(function(a, b) {{ return a.textContent.length - b.textContent.length; }})"
    )


def _role_selector(m: re.Match) -> str:
    role, op, name = m.group(1), m.group(2), m.group(3)
    css = _ROLE_CSS.get(role, f'[role="{role}"]')
    if not name:
        return _pick(f"Array.from(document.querySelectorAll({json.dumps(css)}))")
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
        f"Array.from(document.querySelectorAll({json.dumps(css)}))"
        f".filter(function(el) {{ return {cmp}; }})"
    )


def _label_selector(label_text: str) -> str:
    # The pick ranks the *labels*, then the control is read off the winner —
    # a hidden label's control is hidden with it, so ranking the controls
    # instead would only re-derive the same order one indirection later.
    return (
        f"(function() {{"
        f" var pick = {_PICK_FN};"
        f" var lbl = pick(Array.from(document.querySelectorAll('label'))"
        f"   .filter(function(l) {{"
        f"     return l.textContent.trim() === {json.dumps(label_text)}; }}));"
        f" if (!lbl) return null;"
        f" if (lbl.htmlFor) return document.getElementById(lbl.htmlFor);"
        f" return lbl.querySelector('input, textarea, select');"
        f"}})()"
    )


def _has_text_selector(m: re.Match) -> str:
    css_base, text = m.group(1), m.group(2)
    css_expanded = _ROLE_CSS.get(css_base, css_base)
    t = json.dumps(text)
    return _pick(
        f"Array.from(document.querySelectorAll({json.dumps(css_expanded)}))"
        f".filter(function(el) {{ return el.textContent.trim().includes({t})"
        f" || (el.getAttribute('aria-label') || '').includes({t}); }})"
    )
