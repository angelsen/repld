---
title: Upgrading to 0.2
description: What to clean up in projects that ran repld 0.1.x.
---

0.2 keeps all its runtime state under `$XDG_RUNTIME_DIR` and no longer writes anything into a project. What 0.1.x left in yours is still there.

## Hand it to your agent

Upgrade, then paste this into the agent already sitting in the project:

```bash
uv tool upgrade repld-tool
```

```text
Migrate this project off repld 0.1.x — 0.2 writes nothing into a project
directory, so everything the old version left here should go. Show me what you
find before changing it, and don't commit.

1. If .pyrepl.lock exists, a kernel from before the upgrade may still be
   running. Read its "pid", confirm with `ps -p <pid> -o pid,command` that it
   really is repld (a stale lockfile's pid may have been reused), show me, then
   kill it.
2. Run `git ls-files | grep '^\.pyrepl\.'` and tell me if any are tracked —
   .pyrepl.dashboard holds a dashboard API token. Then delete .pyrepl.lock,
   .pyrepl.sock and .pyrepl.dashboard.
3. Remove the "# repld runtime state" comment and the .pyrepl.* lines from
   .gitignore.
4. Run `claude mcp add repld -- repld bridge`, then remove the "repld" entry
   from .mcp.json (and the file, if that leaves it empty) and from
   enabledMcpjsonServers in .claude/settings.local.json.
5. Delete <!-- repld:start --> through <!-- repld:end --> from CLAUDE.md.
6. If repl.py is a repld bootstrap, `git mv repl.py repld_init.py` and drop
   --init from whatever launched the kernel. If it's something else, say so.
7. Rewrite any gist declaring __repld_tools__ as typed `_tool_<name>` functions
   — it's ignored now, so the file has silently lost its tools. Put the tool
   description on the docstring's first line and per-parameter descriptions in
   Annotated[T, "..."]. Then run `repld gist lint --local`.

Leave ./gists, ./gists/.links and ./.env alone — those are mine. When you're
done, ask me whether I ran repld in other projects; the same litter is in every
one. If I name a directory, survey the whole tree first and show me one table
before changing anything.
```

## Or by hand

**1. Stop the old kernel.** A `.pyrepl.lock` is removed on a clean exit, so one still sitting there may mean a kernel is running that 0.2 can't see. Confirm the pid before signalling it — a lockfile left by a hard kill can name a pid that since belongs to something else.

```bash
cat .pyrepl.lock              # {"pid": 12345, "socket_path": "..."}
ps -p 12345 -o pid,command
kill 12345
```

**2. Delete the leftovers.** Check whether any are tracked first: `.pyrepl.dashboard` holds that kernel's dashboard API token, and it was only ever gitignored in projects that ran `repld init`.

```bash
git ls-files | grep '^\.pyrepl\.'
rm -f .pyrepl.lock .pyrepl.sock .pyrepl.dashboard
```

**3. Drop the ignore lines.**

```bash
sed -i '/^\.pyrepl\./d; /^# repld runtime state$/d' .gitignore
```

**4. Re-register the MCP server**, then delete the `repld` entry from `.mcp.json` and the matching `"repld"` in `enabledMcpjsonServers` in `.claude/settings.local.json`. The generated entry ran `uv run repld bridge` wherever a `uv.lock` existed, which now costs a `uv sync` per session for an interpreter repld picks itself.

```bash
claude mcp add repld -- repld bridge
```

**5. Remove the `CLAUDE.md` block**, `<!-- repld:start -->` through `<!-- repld:end -->`. Everything in it is in the MCP instructions, composed fresh each session.

**6. Rename the bootstrap** and drop `--init`. It's found by name now, on every kernel — including the ones you don't start yourself.

```bash
mv repl.py repld_init.py
```

**7. Migrate any gist declaring `__repld_tools__`.** It's ignored rather than warned about, so the file quietly loses its tools. `repld gist lint` is the only thing that reports it.

## What moved where

| Path                 | 0.2                                                  |
| -------------------- | ---------------------------------------------------- |
| `.pyrepl.lock`       | `$XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.lock` |
| `.pyrepl.sock`       | `…/kernel.sock`                                      |
| `.pyrepl.dashboard`  | `…/kernel.dashboard`                                 |
| `.gitignore` block   | nothing to ignore                                    |
| `.mcp.json`          | `claude mcp add repld -- repld bridge`               |
| `CLAUDE.md` block    | the MCP `initialize` instructions                    |
| `repl.py` + `--init` | `repld_init.py`, auto-detected                       |

`./gists/`, `./gists/.links` and `./.env` are unchanged — source, not state.

## Several projects at once

```bash
find ~/Projects -name '.pyrepl.*' -not -path '*/.venv/*'
grep -rl '^\.pyrepl\.' --include=.gitignore ~/Projects
```

Check any surviving pid before deleting — a lockfile is not the kernel. Then:

```bash
find ~/Projects -name '.pyrepl.*' -not -path '*/.venv/*' -delete

grep -rl '^\.pyrepl\.' --include=.gitignore ~/Projects | while read -r f; do
  sed -i '/^\.pyrepl\./d; /^# repld runtime state$/d' "$f"
done
```

`.mcp.json`, `CLAUDE.md` and `repl.py` stay per project — each is a file you may have edited since.

## Also worth knowing

- **You no longer start a kernel.** The bridge spawns one on the first tool call and it outlives the session. Run `repld` for the live display; `repld status`, `repld log -f` and `repld stop` handle the rest.
- **Headless kernels need one deliberate gist-dep install.** A kernel with nobody watching no longer answers its own consent prompt — it prints a `uv pip install --target …` command instead.
- **A bookmarked dashboard URL answers 401.** Open it with `repld dashboard`, or `repld dashboard --print` for the URL.
- **`repld[browser]` requires Chrome 140+.**
- **No `XDG_RUNTIME_DIR`?** The fallback root moved to `/tmp/repld-{uid}`. Delete the old `/tmp/repld` once the last 0.1.x kernel is stopped.
