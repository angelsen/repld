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
    return (
        f"(function() {{"
        f" const text = {json.dumps(text)};"
        f" const all = Array.from(document.querySelectorAll('*'));"
        f" const exact = all.filter(el => el.offsetWidth > 0 && ("
        f"   el.textContent.trim() === text || el.getAttribute('aria-label') === text));"
        f" return exact.sort((a,b) => a.textContent.length - b.textContent.length)[0] || null;"
        f"}})()"
    )


def _role_selector(m: re.Match) -> str:
    role, op, name = m.group(1), m.group(2), m.group(3)
    css = _ROLE_CSS.get(role, f'[role="{role}"]')
    if not name:
        return f"document.querySelector({json.dumps(css)})"
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
    return f"Array.from(document.querySelectorAll({json.dumps(css)})).find(el => {cmp})"


def _label_selector(label_text: str) -> str:
    return (
        f"(function() {{"
        f" const lbl = Array.from(document.querySelectorAll('label'))"
        f"   .find(l => l.textContent.trim() === {json.dumps(label_text)});"
        f" if (!lbl) return null;"
        f" if (lbl.htmlFor) return document.getElementById(lbl.htmlFor);"
        f" return lbl.querySelector('input, textarea, select');"
        f"}})()"
    )


def _has_text_selector(m: re.Match) -> str:
    css_base, text = m.group(1), m.group(2)
    css_expanded = _ROLE_CSS.get(css_base, css_base)
    return (
        f"Array.from(document.querySelectorAll({json.dumps(css_expanded)}))"
        f".find(el => el.textContent.trim().includes({json.dumps(text)})"
        f" || (el.getAttribute('aria-label') || '').includes({json.dumps(text)}))"
    )
