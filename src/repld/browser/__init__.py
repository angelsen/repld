"""repld.browser — CDP integration for repld.

PUBLIC API:
  - LazyBrowser: Descriptor injected into __main__; lazy-bootstraps on first access.
  - Browser: Manages BrowserSession, watch patterns, and Tab resolution.
  - BrowserPool: Multi-port façade over one Browser per Chrome instance.

Usage in kernel:
    setattr(__main__, "browser", LazyBrowser())

Then in user code:
    tab = await browser.get("*github.com*")   # find one tab by glob
    tab = await browser.get("9222:887d3d")    # find one tab by target ID
    await browser.watch("*github.com*")       # watch all matching
    await tab.js("document.title")

The package layout, consumer-first:

    pool.py      BrowserPool (N Chrome instances) + LazyBrowser
    browser.py   Browser — one Chrome instance: socket, patterns, tabs
    session.py   BrowserSession — the WebSocket and sessionId multiplexing
    cdp.py       CDPSession — per-target DuckDB event store
    tab.py       Tab — the JS/DOM facade, with tab_query.py's query surface
    target.py    the short-target-ID vocabulary, importable from anywhere

This module is re-exports and nothing else. It held `Browser`, `BrowserPool`
and `LazyBrowser` inline until they were carved out — the one module that had
not had `tab.py`'s treatment, where `tab_query` / `selector` / `row` / `pin`
were split off the same way.
"""

from .browser import Browser
from .pool import BrowserPool, LazyBrowser
from .target import TabNotFoundError, make_target

__all__ = ["Browser", "BrowserPool", "LazyBrowser", "TabNotFoundError", "make_target"]
