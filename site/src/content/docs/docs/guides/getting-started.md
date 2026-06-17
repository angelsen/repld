---
title: Getting started
description: Install repld, start the kernel, and connect Claude Code.
---

```bash
uv tool install repld-tool   # or: uvx repld-tool
repld init                   # scaffold .mcp.json + .gitignore entries
repld                        # start the kernel in your project directory
```

Claude Code picks up the `repld` MCP server from `.mcp.json` and connects
through a short-lived stdio bridge. You and the agent now share one Python
process.

> Placeholder — full guide to be written.
