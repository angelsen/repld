---
title: "Your Viewport Is Lying to You"
pubDate: 2026-07-14
description: "We spent an hour debugging a mobile layout bug against Chrome's device emulation. The emulation was the bug. Here's what we found when we plugged in a real phone."
tags: ["browser", "mobile", "cdp", "debugging"]
model: "claude-opus-4-6"
---

We shipped a landing page. It looked fine on desktop. DevTools' responsive mode said it looked fine on mobile. A real Pixel 7 Pro said otherwise: the nav sheet was offset, the backdrop was too wide, the FAB floated in the wrong place. Every `position: fixed; left: 0; right: 0` element was sized against a viewport that didn't match the screen.

The page was horizontally overflowing by about 20 pixels, and `window.innerWidth` had silently latched onto that wider value at first paint.

## The latch

Android Chrome does something that's easy to miss if you've only ever tested in emulation: at initial page load, if `document.scrollWidth` exceeds the physical viewport width — even momentarily, even from an element that's invisible — `window.innerWidth` gets **latched** to the wider value. It stays there for the lifetime of the page. Every fixed-position element that sizes itself with `left: 0; right: 0` (which means "span the full `innerWidth`") now spans the *wrong* width.

This isn't a bug. It's documented behavior.[^cssom] But it creates a class of layout problem you'll never see in desktop DevTools, because desktop Chrome's device emulation doesn't reproduce the latch. `innerWidth` in emulation always reports the override value you set, regardless of overflow. The latch is a real-device-only phenomenon.

## Two invisible overflow sources

The actual overflow came from two places, neither obvious:

**1. A monospace code listing that was 3 pixels too wide.** The terminal mock on the landing page has a gist listing rendered at `font-size: 12px`. The longest line — 62 characters of monospace — computed to a few pixels wider than its container's content box at 390px viewport width. No visible scrollbar, no visual cue. Just enough to push `scrollWidth` past the viewport.

**2. GSAP animation targets sitting off-screen before their reveal.** The page uses scroll-triggered reveals — elements start `visibility: hidden` and get animated in as you scroll. But `visibility: hidden` doesn't remove an element from layout. If an element is translated off-screen as part of its pre-reveal state, its box still contributes to `scrollWidth` during the initial paint window — right when the latch happens.

Neither of these causes a visible scrollbar. Neither shows up in any layout inspector as "wrong." But together, they inflated `scrollWidth` at exactly the wrong moment, and `innerWidth` locked to the inflated value for the rest of the session.

## CDP emulation made it worse

Our first instinct was to debug this in the browser we already had wired up — repld's CDP-connected Chrome. Set `Emulation.setDeviceMetricsOverride` to `{width: 390, height: 844, mobile: true, deviceScaleFactor: 1}`, take a screenshot, inspect the tree.

This worked on the first application. On the second — after changing viewport dimensions on the same tab — `document.documentElement.clientWidth` and `window.innerWidth` started disagreeing with each other. A state that a real browser on a real device literally cannot produce on a fresh page load. We were now debugging two bugs: the original layout issue, and phantom metrics from the emulation layer.

The CDP spec doesn't promise that `setDeviceMetricsOverride` is idempotent on a long-lived tab.[^cdp] Reapplying it can leave internal state inconsistent. We lost about an hour before realizing the tool was lying to us.

## ADB + repld: real hardware, same API

The fix was to stop emulating and plug in the phone. ADB forward gives you a raw CDP port on the device's actual Chrome:

```bash
adb forward tcp:9333 localabstract:chrome_devtools_remote
```

From the repld kernel:

```python
mobile = browser.connect(9333)
tab = mobile.tabs[0]
```

Same Tab API. Same `tab.js()`, `tab.tree()`, `tab.screenshot()`. But now the numbers are real — no emulation layer, no viewport override, no latch discrepancy. The device's Chrome is rendering the page exactly as a user would see it.

We ran a diagnostic sweep through `tab.js()`:

```python
await tab.js("""
  [...document.querySelectorAll('*')]
    .filter(el => el.scrollWidth > document.documentElement.clientWidth)
    .map(el => ({
      tag: el.tagName,
      class: el.className,
      scrollWidth: el.scrollWidth
    }))
""")
```

The gistlist showed up immediately. A second pass with `PerformanceObserver` tracking `layout-shift` entries pinpointed the GSAP reveal targets contributing to the initial-paint overflow.

## The two-line fix

Once we knew the real causes, the fix was trivial:

1. **`overflow-x: hidden` on `<html>`** — not `<body>`. In standards mode, `<html>` is the root scroller. Setting `overflow-x` on `<body>` alone doesn't prevent `scrollWidth` from reflecting descendants that extend past the viewport. The `<html>` element is what actually controls the latch.

2. **Shrink the gistlist** to `font-size: 7px` at the mobile breakpoint, so the longest line fits within the container at 390px without overflow.

That's it. The GSAP targets no longer matter because the root-level overflow clip prevents them from inflating `scrollWidth` in the first place.

## The lesson

Device emulation is a convenience for screenshots and quick visual checks. It is not a substitute for real hardware when you need to trust viewport metrics. The emulation layer doesn't reproduce platform-specific behaviors like the `innerWidth` latch, and reapplying overrides on the same tab can produce self-contradicting state.

If you're debugging a mobile layout issue and the numbers don't add up, plug in the phone. With ADB forwarding and a CDP-capable tool, you get the exact same debugging API you'd use against desktop Chrome — but the answers are real.

[^cssom]: The CSSOM View spec defines `window.innerWidth` as the viewport width including scrollbar, but defers actual scrollbar and overflow behavior to the UA. Chrome's implementation on Android latches the initial `scrollWidth` into `innerWidth` for the page lifetime — observable but not prominently documented outside Chromium source.
[^cdp]: Chrome DevTools Protocol's `Emulation.setDeviceMetricsOverride` is designed for single-shot testing. The [CDP docs](https://chromedevtools.github.io/devtools-protocol/tot/Emulation/#method-setDeviceMetricsOverride) note that it "overrides values" but don't guarantee consistency when reapplied with different parameters on a tab that already has an active override.
