---
title: "The Quickest Way to Harness Anything"
pubDate: 2026-07-14
description: "\"Agent harness\" became this year's word for the loop that drives an LLM. There's an older, plainer sense of the word repld actually lives in — and it's not that one."
tags: ["agent-harness", "positioning", "controls", "gists"]
model: "claude-sonnet-5"
---

This is the year "harness" became the word. Every model release comes with a harness post attached. LangChain wrote an anatomy of one.[^langchain] Martin Fowler wrote a mental model for building your own.[^fowler] Hugging Face had to publish a glossary because nobody at ICLR could agree what it meant.[^hf] Cursor blogs about "continually improving our agent harness." OpenAI shipped an entire engineering post just titled *Harness Engineering*.

The definition that's converged is precise, and it's useful: **Agent = Model + Harness**.[^hf] The model takes text in, produces text out, and forgets everything the moment the call returns. The harness is everything else — the loop that calls the model, parses its tool calls, decides when to stop, decides what it's allowed to touch. Claude Code's own docs say it outright: *"Claude Code serves as the agentic harness around Claude."*[^hf] Codex, Cursor, Antigravity — same shape, different choices. Two products wrapped around the same model can feel completely different, because the harness is where all the actual design happens.

It's a good word, precisely used. But somewhere in the last decade of it drifting into AI-agent jargon, we lost the original, plainer meaning: **a harness is something you put on a thing to make it drivable.** You don't harness the driver. You harness the horse.

That's the sense repld actually lives in.

## Not a harness. A thing to harness other things with.

repld doesn't call an LLM. It doesn't decide when to stop, doesn't parse tool calls, doesn't own an agentic loop. Claude Code already does all of that — repld is a persistent kernel and an MCP server that Claude Code (or Codex, or whatever harness you're running) calls *into*. In the current jargon, that makes repld infrastructure the harness uses, not a harness itself. Calling it one would be a category error, and a confusing one — it'd read as "repld competes with Claude Code," which it doesn't.

What repld actually does is put a harness — the old, literal kind — on whatever the agent needs to act on. A web app with no public API. Your own in-house tool. Eventually, anything that runs as a process at all. The target gets harnessed; the agent stays free to drive it.

That's a real, useful distinction, and it splits into three points on a spectrum, depending on how much the target has to cooperate.

## Zero cooperation: gists

Most of what an agent wants to automate is a web app that was never built to be automated. It has no public API, no docs, sometimes no stable selectors. It doesn't need to cooperate, though — it just needs to be *watched*. repld attaches to a real, logged-in Chrome tab over CDP, and every request the app's own frontend makes is visible: the actual private API, with the actual auth already attached, because it's riding your real session.

The pattern is: click around once, watch the network tab fill up with the real calls, then write a thin Python wrapper around them. That wrapper is a **gist** — a plain file in `./gists/` that the kernel hot-reloads the moment you save it. Once it exists, it's not a one-off script. It's reusable across the rest of the session, linkable into other projects, and — when it's proven out — gradable into a real FastMCP or FastAPI service with the browser-specific wiring stripped back out.

This is the pattern most agent harnesses reach for as "bash + code exec, but for a specific app"[^langchain] — except most of them hand the agent a stateless sandbox that's torn down at the end of the call.[^langchain] repld's kernel doesn't tear down. It's a standing process, one shared `__main__` between you and the agent, for the life of the project.

## Full cooperation: controls

Reverse-engineering only gets you so far when the target is *your own* app, and you'd rather just tell it what to expose. That's what **controls** are for: your app defines a `window.controls` object — named actions with typed parameters, named properties with live getters — and repld discovers it automatically. Two MCP tools show up: `browser_controls` lists the schema, `browser_invoke` runs an action and hands back `{returned, stateBefore, stateAfter, duration}` plus a full observation pipeline (accessibility tree delta, network delta, console delta).

This is the same shape Martin Fowler calls a **sensor** in his harness-engineering framing — a feedback control that observes *after* the agent acts, so you find out which stage of a ten-step pipeline actually broke instead of guessing from "it didn't work."[^fowler] The difference is that repld's sensor isn't a linter or a test suite bolted onto CI — it's the UI state machine your app already has, made observable, live, in the same conversation where the agent is driving it.

Controls are the cooperative end of the spectrum on purpose. They're for the apps you can change: your own product, an internal tool, anything where writing twenty lines of `defineControl()` is cheaper than reverse-engineering it blind.

## Not yet built: anything that isn't a browser tab

Gists and controls both live inside what CDP can reach — a page rendered in a browser. That's most of what people automate, but not all of it. Mobile apps. Native desktop binaries. Electron shells. Anything running as an actual process has the same shape of problem — you want to attach live, observe its real behavior, and wrap what you find in something typed and reusable — but CDP structurally can't get there.

Dynamic instrumentation frameworks like Frida can: attach to a running process, hook exported functions, read and write memory, call into the target directly, all without touching its source. It's the zero-cooperation end of the spectrum again, just at a different layer — the same "discover once, wrap forever" idea gists already do for HTTP, extended to anything that runs. Nothing here is built yet. But the shape of the extension is obvious enough that it's worth saying out loud: the browser was never the point. It was just the first thing worth attaching to.

## The actual pitch

Not "repld is an agent harness" — that sentence claims something it doesn't do and picks a fight with tools it's not competing with. The real sentence is the older, plainer one:

**repld is the quickest way to harness anything.**

Whatever the agent needs to act on — a web app that wasn't built for this, your own product, eventually a process that isn't even a browser tab — repld's job is to put a harness on it fast enough that reverse-engineering it stops being the bottleneck.

[^hf]: [Harness, Scaffold, and the AI Agent Terms Worth Getting Right](https://huggingface.co/blog/agent-glossary) — Hugging Face's glossary grounding "harness," "scaffold," "agent," and "environment" against the confusion at ICLR 2026.
[^langchain]: [The Anatomy of an Agent Harness](https://www.langchain.com/blog/the-anatomy-of-an-agent-harness) — LangChain's breakdown of harness components (filesystem, sandboxes, bash/code exec, memory) derived from what a raw model can't do on its own.
[^fowler]: [Harness engineering for coding agent users](https://martinfowler.com/articles/harness-engineering.html) — Martin Fowler's feedforward/feedback (guides/sensors) mental model for the "outer harness" users build around a coding agent.
