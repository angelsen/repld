---
title: 'The Kernel Never Restarted'
pubDate: 2026-07-20
description: "We rewrote a tmux MCP server from scratch — daemon, wake-up shim, async readiness notifications — in one repld session. The interesting part isn't that it worked. It's how many wrong assumptions we caught before they shipped, because testing them never cost more than a REPL cell."
tags: ['repld', 'mcp', 'fastmcp', 'tmux', 'debugging']
model: 'claude-sonnet-5'
---

We spent today rewriting termtap — a tmux pane manager exposed over MCP — from scratch. New architecture: one async FastMCP daemon instead of a socket-RPC daemon/client split, a minimal stdio shim instead of a companion app, async completion notifications instead of a blocking poll loop. By the end of the session it was installed, registered with Claude Code, and pushing real async notifications from a live tmux pane.

None of that is the interesting part. The interesting part is that the whole thing — prototype to shipped — happened in one repld kernel that never restarted. Every wrong assumption along the way got caught in the same cell it was made in, not discovered three sessions later in production.

## A heuristic that looked right and wasn't

The first real piece was OSC 133 readiness detection: ask tmux which line in a pane's scrollback is a shell prompt, without pattern-matching text. `capture-pane -F` tags each line with flags — `P` for prompt-start, `O` for output-start. The obvious first cut: find the last non-blank line, check if it's flagged `P`.

Tested against a live fish pane immediately:

```
'-' 'fredrik at yoga-fox in ~/P/p/t/p/termtap'
'X' '↪ echo fish-pane-test'
```

Wrong. Fish's prompt spans two lines — the OSC 133 mark lands on the _first_ line (`P`), and the line you actually type on gets `X` (an unrelated color-attribute flag from the wrapping character, nothing to do with readiness). The heuristic found the second line, saw no `P`, and reported "not ready" on a pane that had been sitting at a fresh prompt the whole time.

The fix was positional, not local: track the position of the _last_ `P` line and the _last_ `O` line across the whole capture; ready if `P` comes after `O`, regardless of what's on the very last line. Fixed and reverified in the same cell, against the same pane, thirty seconds after the bug showed up. Nothing about that heuristic would have looked wrong in review — it only broke against a real multi-line prompt, and repld's whole value proposition is that "real" was one line of Python away the entire time.

## Proving a hypothesis that would normally need throwaway infra

Later, we needed to know something fastmcp doesn't document: does `create_proxy` — the machinery bridging our daemon's HTTP session to Claude Code's stdio connection — relay a _non-standard_ server-initiated notification, or silently drop it? Normally this is the kind of question you write a disposable test harness for, in a separate terminal, probably more than once as you get the protocol framing wrong.

Instead: a cell that spawns the actual proxy subprocess, writes raw JSON-RPC over its stdin, and reads raw stdout — bypassing any client-side type validation that might be hiding the real answer.

```python
proc = await asyncio.create_subprocess_exec(
    VENV_PY, "-m", "termtap2.wake_shim",
    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, ...
)
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "ping", "arguments": {}}})
```

First answer: no. `mcp.client.session.ClientSession` validates every incoming notification against a strict, spec-defined type union; anything outside it — including our custom `notifications/claude/channel` — fails validation and gets logged as a warning, never reaching any handler. Not a proxy bug, a client-SDK behavior, three layers down from where we were looking.

The fix was a targeted monkeypatch — override `_receive_notification_type` with a permissive fallback, applied globally but scoped to the shim's own single-purpose process. Reran the exact same cell. This time the raw bytes came through:

```json
{"method":"notifications/claude/channel","params":{"content":"hello from termtap2 channel test", ...}}
```

Proven in two iterations, in the same kernel, without ever leaving Python.

## The tool fixing itself mid-session

Partway through, a `pydantic`/`pydantic-core` version mismatch broke every fastmcp import — repld's own dependency installer resolves each new package's requirements independently, so an earlier install and a later one had silently drifted apart. We traced it into repld's own source, wrote it up in repld's `TODO.md` with the exact file and line numbers, and kept working.

By the next test, it was fixed — we'd patched `install_deps()` to track every requirement ever installed and re-resolve the whole set together on each new install, in the same working session, no separate release cycle. Reinstalling picked up the fix immediately. The tool we were using to build the thing got a real bug fixed by the same conversation that found it.

## grep-ing a compiled binary for an undocumented flag

The last mile was the strangest. Everything about async notifications was now provably correct — the daemon sent them, the relay forwarded them, raw JSON-RPC proved it end to end. And Claude Code still wasn't showing them.

The client-side answer wasn't in any changelog. It was in the compiled `claude` binary itself:

```bash
strings -n 8 /usr/bin/claude | grep -A2 "development-channels"
```

```
--channels <servers...>
MCP servers whose channel notifications (inbound push) should register
this session. Space-separated server names.
--dangerously-load-development-channels <servers...>
```

Undocumented in `--help`, space-separated not comma-separated, and gated behind an explicit per-launch opt-in — nothing on the server side could detect a session that hadn't set it. Restarted with the flag; still nothing. One more raw JSON-RPC check found the actual last gap: our proxy's own `initialize` response declared `"experimental": {}` — empty — even though the daemon behind it had correctly declared `claude/channel`. `create_proxy` doesn't forward a backend's capability declarations to its own identity, and Claude Code gates all notification handling on the _connecting_ server's own declared capabilities, not whatever's further upstream. Added the declaration to the proxy directly, confirmed it showed up in a fresh raw handshake, restarted the real session — and the notification arrived, unprompted, mid-conversation, exactly where it was supposed to.

## What actually made this fast

Not skipping validation. If anything, we validated more than a normal session would — every hypothesis got an actual test, not a plausibility check. What made it fast was that testing a hypothesis never cost more than the hypothesis itself. The tmux panes from the first `list_panes()` call were still the objects in scope for the last one. The daemon we booted at minute ten was still running, still holding state, at minute two hundred. Nothing had to be re-explained to a fresh process, because there never was one — one kernel, one session, and every wrong turn corrected in the same breath it was taken.
