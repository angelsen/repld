"""Captcha detection: vendor fingerprints read from the page's own document.

Detection and hand-off only, never solving. Each vendor leaves a container,
a hidden response field and an iframe from its own origin in the page that
embeds it, so nothing here enters the captcha's cross-origin frame. A
captcha inside a same-process child iframe of the page is not seen (the
query does not cross frames); one inside an attached iframe *target* is,
when that target is the tab observed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import bg
from .cdp import DialogPendingError, _push_tab

if TYPE_CHECKING:
    from .tab import Tab

logger = logging.getLogger(__name__)

# How long the solved watcher polls after a hand-off, and how often.
WATCH_S = 600.0
POLL_S = 1.0

VENDOR_NAMES = {
    "recaptcha_v2": "reCAPTCHA v2",
    "recaptcha_v3": "reCAPTCHA v3",
    "hcaptcha": "hCaptcha",
    "turnstile": "Turnstile",
    "arkose": "Arkose FunCaptcha",
}

# The iframe origin names the vendor; the response field names the state. hCaptcha's
# drop-in mode reuses the `g-recaptcha-response` field, so the field alone never
# decides the vendor. reCAPTCHA v3 has no widget: only `grecaptcha.execute` exists.
_DETECT_JS = """\
(() => {
  const frames = Array.from(document.querySelectorAll('iframe'));
  const withSrc = s => frames.filter(f => (f.src || '').includes(s));
  const shown = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0
    && getComputedStyle(el).visibility !== 'hidden';
  const filled = name => {
    const f = document.querySelector('[name="' + name + '"]');
    return f ? (f.value || '').length > 0 : null;
  };
  const key = sel => document.querySelector(sel)?.dataset?.sitekey || '';
  const out = [];
  const anchors = withSrc('/recaptcha/api2/anchor').concat(withSrc('/recaptcha/enterprise/anchor'));
  if (anchors.length) {
    const bframes = withSrc('/recaptcha/api2/bframe').concat(withSrc('/recaptcha/enterprise/bframe'));
    const solved = filled('g-recaptcha-response') === true;
    out.push({vendor: 'recaptcha_v2',
              state: solved ? 'solved' : bframes.some(shown) ? 'challenge' : 'unsolved',
              visible: anchors.some(shown), iframe: anchors[0].src,
              sitekey: key('.g-recaptcha[data-sitekey]')});
  }
  const hc = withSrc('hcaptcha.com/');
  if (hc.length) {
    const solved = filled('h-captcha-response') === true
      || (!anchors.length && filled('g-recaptcha-response') === true);
    const challenge = hc.filter(f => f.src.includes('frame=challenge')).some(shown);
    out.push({vendor: 'hcaptcha',
              state: solved ? 'solved' : challenge ? 'challenge' : 'unsolved',
              visible: hc.some(shown), iframe: hc[0].src,
              sitekey: key('.h-captcha[data-sitekey]')});
  }
  const ts = withSrc('challenges.cloudflare.com');
  if (ts.length) {
    out.push({vendor: 'turnstile',
              state: filled('cf-turnstile-response') === true ? 'solved' : 'unsolved',
              visible: ts.some(shown), iframe: ts[0].src,
              sitekey: key('.cf-turnstile[data-sitekey]')});
  }
  const ak = withSrc('arkoselabs.com');
  if (ak.length) {
    out.push({vendor: 'arkose',
              state: filled('fc-token') === true ? 'solved' : 'unsolved',
              visible: ak.some(shown), iframe: ak[0].src, sitekey: ''});
  }
  if (!anchors.length && typeof window.grecaptcha?.execute === 'function'
      && !document.querySelector('.g-recaptcha[data-sitekey]')) {
    out.push({vendor: 'recaptcha_v3', state: 'invisible', visible: false, iframe: '', sitekey: ''});
  }
  return out;
})()
"""


@dataclass(frozen=True)
class Captcha:
    """One captcha widget on a page, as its embedding document shows it."""

    vendor: str  # a VENDOR_NAMES key
    state: str   # unsolved | challenge | solved | invisible
    visible: (
        bool  # the widget itself is on screen (False: invisible mode, fires on submit)
    )
    iframe: str  # the vendor iframe's src, '' for reCAPTCHA v3
    sitekey: str

    @property
    def needs_human(self) -> bool:
        return self.state in ("unsolved", "challenge")

    @property
    def name(self) -> str:
        return VENDOR_NAMES.get(self.vendor, self.vendor)


def parse(raw: Any) -> list[Captcha]:
    """`_DETECT_JS`'s value as records; anything malformed is skipped."""
    if not isinstance(raw, list):
        return []
    out: list[Captcha] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("vendor"):
            continue
        out.append(
            Captcha(
                vendor=str(item["vendor"]),
                state=str(item.get("state") or "unsolved"),
                visible=bool(item.get("visible")),
                iframe=str(item.get("iframe") or ""),
                sitekey=str(item.get("sitekey") or ""),
            )
        )
    return out


def describe(c: Captcha) -> str:
    """The observation / tree line for one captcha."""
    if c.state == "solved":
        return f"captcha: {c.name} → solved"
    if c.state == "invisible":
        return f"captcha: {c.name} → scored invisibly on submit, nothing to tick"
    what = "challenge open" if c.state == "challenge" else "unsolved"
    if c.state == "unsolved" and not c.visible:
        what += ", widget hidden (invisible mode, fires on submit)"
    return (
        f"captcha: {c.name} → {what}; needs a human, hand off and "
        "await tab.wait_captcha()"
    )


async def detect(tab: Tab) -> list[Captcha]:
    """Captchas in `tab`'s document. One `Runtime.evaluate`, no activation granted."""
    raw = await tab.js(_DETECT_JS, user_gesture=False, await_promise=False)
    return parse(raw)


async def wait_solved(tab: Tab, timeout: float = WATCH_S) -> list[Captcha]:
    """Poll until no captcha on `tab` needs a human; the list as last seen.

    Raises TimeoutError naming what is still unsolved.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        found = await detect(tab)
        pending = [c for c in found if c.needs_human]
        if not pending:
            return found
        if loop.time() >= deadline:
            names = ", ".join(f"{c.name} {c.state}" for c in pending)
            raise TimeoutError(f"captcha still unsolved after {timeout:.0f}s: {names}")
        await asyncio.sleep(POLL_S)


def report(tab: Tab, found: list[Captcha]) -> None:
    """Announce captchas that need a human and start the solved watcher.

    The announcement is receipt-carried (the observation names it), so inside
    an action window it is log-only; the solved push is what the hand-off
    is for, routed to the tab's claim at the moment it lands.
    """
    pending = [c for c in found if c.needs_human]
    if not pending:
        return
    cdp = tab._session
    short_id = f"{cdp.port}:{cdp.chrome_target_id[:6].lower()}"
    for c in pending:
        _push_tab(
            cdp,
            f"[captcha] {short_id}: {c.name} {c.state}, needs a human",
            {
                "kind": "captcha",
                "target": short_id,
                "vendor": c.vendor,
                "state": c.state,
            },
            in_receipt=True,
        )
    watch = cdp._captcha_watch
    if watch is not None and not watch.done():
        return  # it re-detects every tick, so one per tab is enough
    cdp._captcha_watch = bg.spawn(
        _watch(tab, {c.vendor for c in pending}), name=f"repld-captcha-{short_id}"
    )


async def _watch(tab: Tab, vendors: set[str]) -> None:
    cdp = tab._session
    short_id = f"{cdp.port}:{cdp.chrome_target_id[:6].lower()}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WATCH_S
    while loop.time() < deadline:
        await asyncio.sleep(POLL_S)
        try:
            found = await detect(tab)
        except (DialogPendingError, TimeoutError):
            continue  # the human's dialog or a busy page; the captcha is still theirs
        except Exception as exc:
            logger.debug("captcha watch on %s ended: %s", short_id, exc)
            return
        for c in found:
            if c.state == "solved" and c.vendor in vendors:
                vendors.discard(c.vendor)
                _push_tab(
                    cdp,
                    f"[captcha] {short_id}: {c.name} solved",
                    {
                        "kind": "captcha",
                        "target": short_id,
                        "vendor": c.vendor,
                        "state": "solved",
                    },
                )
        if not any(c.needs_human for c in found):
            return
