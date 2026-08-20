"""Selector translation — repld selector forms to Playwright engine selectors.

The public grammar is repld's (`text=`, `role=[name=]`, `label=`, `:has-text`,
`aria-ref=`, plain CSS); the vendored InjectedScript engine (inject.py) is the
only thing that ever *resolves* one. translate() maps each form onto the
engine's own selector language, which stays an internal detail — a raw
Playwright-syntax string a caller happens to pass falls through to `css=` and
fails like any bad CSS.
"""

import json
import re

# Only consulted for `:has-text` base expansion now — `role=` proper goes to
# the engine's role engine, which computes implicit roles for real instead of
# approximating them with a CSS list.
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


# A string that is unambiguously a selector rather than a JS expression.
# `translate()` itself can't answer this — its fallback treats *anything*
# unrecognised as CSS, which is right for `click`/`type_text`, where a selector
# is the only thing a caller can mean. `ready=` is the one parameter that
# accepts either, so it needs the question asked separately, and it needs the
# answer to agree with `translate()` about what a selector looks like.
#
# The bare-name branch is the whole reason this exists: `ready="main"` and
# `ready="my-app"` are ordinary CSS, and the hand-rolled
# `startswith((".", "#", "[", "data-"))` test that used to live in
# `Tab._await_ready_signal` sent them down the JS path to be evaluated as
# identifiers. Anchored and dot-free, so a real expression (`window.ready`,
# `app.isLoaded`) still reads as JS.
_BARE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_SELECTOR_PREFIXES = (".", "#", "[", "data-", "text=", "role=", "label=", "aria-ref=")


def looks_like_selector(s: str) -> bool:
    """True if *s* should be resolved as a selector rather than evaluated as JS."""
    if s.startswith(_SELECTOR_PREFIXES):
        return True
    if ":has-text(" in s:
        return True
    return bool(_BARE_NAME.match(s))


def _text_body(text: str, exact: bool) -> str:
    # Port of escapeForTextSelector (packages/isomorphic/stringUtils.ts):
    # JSON-quoted, `s` = case-sensitive exact (whitespace-normalized),
    # `i` = case-insensitive substring.
    return json.dumps(text) + ("s" if exact else "i")


def _js_regex_escape(text: str) -> str:
    # For embedding in a /.../ selector literal: JS regex specials plus the
    # delimiter itself.
    return re.sub(r"[/\\^$.|?*+()\[\]{}]", lambda m: "\\" + m.group(0), text)


_ROLE_RE = re.compile(r'^role=(\w+)(?:\[name([*^]?=)["\']?(.+?)["\']?\])?$')
_HAS_TEXT_RE = re.compile(r"^(.+?):has-text\(['\"](.+?)['\"]\)$")


def translate(selector: str) -> str:
    """Translate one repld selector to the engine's selector language."""
    return translate_fallbacks(selector)[0]


def translate_fallbacks(selector: str) -> list[str]:
    """Ordered engine selectors: primary first, then legacy-compat retries.

    A retry form is consulted only when the one before it matched nothing —
    it widens the search, never re-ranks it. `text=` and `:has-text` carry an
    aria-label retry because the pre-engine implementations matched
    `aria-label` alongside text content, and the engine's text engine does not.
    """
    if selector.startswith("text="):
        text = selector[5:]
        # internal:text, not the public text= engine: only the internal one
        # accepts the `"…"s` exact-case-sensitive body, which is what matches
        # the legacy `===` semantics (verified live — the public engine parses
        # `text="Save"s` as the literal string `"Save"s` and matches nothing).
        return [
            f"internal:text={_text_body(text, exact=True)}",
            f"css=[aria-label={json.dumps(text)}]",
        ]

    m = _ROLE_RE.match(selector)
    if m:
        role, op, name = m.group(1), m.group(2), m.group(3)
        if not name:
            return [f"role={role}"]
        if op == "*=":
            return [f"role={role}[name*={_text_body(name, exact=True)}]"]
        if op == "^=":
            # The role engine's parser stores any op for `name` but only
            # documents `=` and `*=`; the regex form is the guaranteed spelling
            # of starts-with.
            return [f"role={role}[name=/^{_js_regex_escape(name)}/]"]
        return [f"role={role}[name={_text_body(name, exact=True)}]"]

    if selector.startswith("label="):
        return [f"internal:label={_text_body(selector[6:], exact=True)}"]

    m = _HAS_TEXT_RE.match(selector)
    if m:
        base, text = m.group(1), m.group(2)
        expanded = _ROLE_CSS.get(base, base)
        t = json.dumps(text)
        # :has-text() binds to one compound selector, so it is applied to each
        # comma alternative of the role expansion, not to the list as a whole.
        alts = [alt.strip() for alt in expanded.split(",")]
        return [
            "css=" + ", ".join(f"{alt}:has-text({t})" for alt in alts),
            "css=" + ", ".join(f"{alt}[aria-label*={t}]" for alt in alts),
        ]

    if selector.startswith("aria-ref="):
        return [selector]

    return [f"css={selector}"]
