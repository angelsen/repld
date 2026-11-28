# Run log

How each row is known: `ran` (a recorded failing run or measurement, cited), `docs`, `source`
(read, not run), `imagined` (no observed break found). `imagined (review)` marks a fix that
came from a review or "health pass" with no failing run recorded outside its own test: by the
method a review finding is not a break. Citations are a path plus a grep literal, or a commit
hash. `C:` is `CLAUDE.md`, `A:` is `CLAUDE-ARCHIVE.md`, `S2:` is
`~/Projects/private/supervise2/docs`.

## Plan

Decided, not reopened: the 2026-07-28 server is stdlib; the kernel and bridge split stays and
the bridge becomes a thin 2026-07-28 front; this plan lives here.

| # | session | what it does |
| --- | --- | --- |
| 0 | baseline (this file) | read only: inventory, real use, tags, deletion candidates |
| 1 | mcp2 probes in `probes/` | a stdlib spike bridge that exposes `exec` as a task against the real kernel. Rows to run: idle wake, `claude -p` inline, double cancel, a gate as `input_required`, a task across `/clear`, across a bridge restart and across a relaunch, `notify()` over the inbox socket for an interactive and a `--bg` session and how the model reads it, where `instructions` ride with no `initialize` |
| 2 | bridge cutover, clean break | drop handshake, version negotiation and the channel relay; `exec` completion is the task result. The push routing (owed, outbox, park, rebind, successors) is deleted here too, not kept for session 3. From session 1: one kernel connection per request, a dead or restarted kernel answered as a `failed` task, `get_task` kept, and the `elicitation/create` code deleted |
| 3 | ambient pushes | `notify()`, `@every` errors and browser pushes over the inbox socket, for every session kind, keyed on the socket path; anything from the old routing is rebuilt only against a session 1 or 2 break |
| 4 | peers, then gists | one session each, smallest first by use |
| 5 | browser | the largest subsystem; needs its own run of real pages |
| 6 | gates over `input_required` | CLI, dashboard and pill routes stay. Needs the asking task on `gates/list` and a decline (session 1, break 6); a secret gate refuses (plan change 7) |
| 7 | `help.py`, dashboard, TUI | docs last, when the surface they describe has stopped moving |
| last | write `~/.claude/skills/vibe/stacks/repld.md` | toolchain, smallest layout, earned rules |

Changes from the order given, and why:

1. **The push-routing collapse moves into the cutover (2), and session 3 becomes ambient
   pushes.** The routing exists to get a channel push to the right connection. Channels
   cannot ride 2026-07-28 (`S2:research-2026-11-26.md:/## Channels and events/`), so on the
   day of the cutover that code has no caller left to serve; keeping it for one more session
   means keeping dead code green. What does survive the cutover and needs a session of its
   own is the ambient half: 6,408 of the 12,562 pushes seen in transcripts are not
   `task_done` (see Real use), most of them `notify()` kinds a gist chose.
2. **Gates move from 4 to 6.** They are the least used primitive measured: 49 exec cells out
   of 69,748 call `ask`/`confirm`/`choose`/`form`, 75 gate pushes in three months, 34 of
   those inside repld's own development. The `input_required` probe still runs in session 1,
   because it is cheap there and decides whether the elicitation code can go in session 2.
3. **`help.py` moves last.** It documents every other subsystem (3,037 lines), so rewriting
   it before they settle is rework. Its one mcp2 question (where `instructions` go) is a
   session 1 row.
4. **Peers before browser.** Peers are 1,703 lines with 93 cells of use and no channel
   dependence; a short session. The browser is 12,816 lines and 8,600 tool calls and should
   not share a session with anything.

Changes after session 1, and why (rows and breaks are in "Session 1"):

5. **The routing deletion in session 2 stands, with evidence.** A task result reached the
   conversation across `/clear`, across a bridge restart and across exit and `--resume`,
   with no rebind, outbox or park (rows 1 and 2). The one loss left is a relaunch as a new
   conversation (break 5), which the old routing lost too
   (`S2:research-2026-11-27.md:/not delivered/`); `get_task` stays for it.
6. **Session 2 gains four requirements, each from a run.** A kernel connection per request
   (break 8). A task whose kernel died or restarted is answered as `failed`, never as a
   JSON-RPC error or an unknown id (breaks 2 and 3). The server entry a session resumes with
   is unchanged, down to one `env` value, or the client refuses to poll (break 4). Neither
   exit path measured cancelled a cell ("Exit and stop tasks" and SIGTERM, break 1; "Move to
   background and exit" was not run), so nothing in the cutover may assume exit does; a client's
   `notifications/cancelled` for a call held open does arrive and has to reach the kernel
   (row 7b).
7. **The `elicitation/create` code can go in session 2, for both kinds of client.** In a
   tasks-capable client `confirm`, `ask` and `choose` showed as dialogs from
   `input_required` and resolved the kernel gate (row 9). `claude -p` declares elicitation
   and no tasks: given `input_required` on the call itself it answered `cancel` at once and
   asked nobody, the gate stayed open, and `repld gate answer` resolved it (row 9e). That is
   where a `-p` gate ends today (`tests/phases/live_claude.py:/_print_mode_leaves_gate_open/`),
   so nothing a human could use is lost. What the front owes a `-p` caller is the text
   naming the gate and the answer command, which the `awaiting_human` push carries today.
   Session 6 is then three small kernel changes, not a new surface.

   **Decided by the owner (2026-11-28): the secret gate refuses until a real use asks.**
   From session 2 `ask(..., secret=True)` and a secret `form()` field raise at once, naming
   the missing route; no URL-mode route is kept and session 6 builds no page URL. Nothing
   is collected, so no secret passes the client, which is what the spec requires of a
   sensitive input (`client/elicitation.mdx:/MUST\*\* use \[URL mode\]/` in the 2026-07-28
   spec). Use: 6 transcript lines in 3 session files have an `exec` call with `ask(` or
   `form(` and `secret`, all in repld's own directory (ran: `rg -c` over
   `~/.claude/projects`, a regex on the raw line, not a parse). When a use arrives the
   route is the kernel handing the page URL to `gates/list` (break 7) and a URL-mode
   `inputRequest`, which Claude Code has not been seen to render (not run).

   **Session 2 runs one row first:** `form()` through `input_required` (form mode, like
   `ask`; not run in session 1).
8. **Session 3 needs a registration, not a rebind.** The inbox socket path is per process and
   survived `/clear`; a push sent to the id the connection registered with still arrived
   after the id changed (row 4c). With no `initialize` the bridge has to tell the kernel its
   socket some other way; the spike held one kernel connection for it. Gists that key on the
   channel tag's attributes get the same keys as text on the second line (row 4).
9. **Session 4 (gists) needs no new kernel signal.** The kernel already pushes
   `notifications/tools/list_changed` on an initialized connection: the one the spike held
   for change 8 got it at 10:53:23.430, 0.7 s before the spike's own 2 s poll of
   `tools/list` noticed (`.live.local/mcp2/a.jsonl`, `kernel-push`; log removed 2026-11-28). The front relays that
   push onto the client's `subscriptions/listen`, which works (row 11); the poll goes.

## Inventory

Counted at `862b01cc` with `wc -l`. `browser/injected_source.py` (generated, 332,825 bytes in
28 lines) is excluded.

### Source, `src/repld/` (37,122 lines)

| subsystem (CLAUDE.md heading) | lines | files |
| --- | --- | --- |
| Browser | 12,816 | `browser/` 12,171 (`tab.py` 2,559, `cdp.py` 1,737, `observe.py` 1,570, `session.py` 1,117, `inject.py` 865, `pool.py` 729, `browser.py` 659, `har.py` 646, 10 smaller), `browser_dispatch.py` 645 |
| Request flow | 5,757 | `protocol.py` 1,750, `ipc.py` 1,436, `bridge.py` 1,338, `core_schemas.py` 519, `spawn.py` 313, `channel.py` 190, `rebind_cmd.py` 113, `bridge_tools.py` 98 |
| Threading model and kernel core | 4,034 | `kernel.py` 1,896, `tasks.py` 561, `runtime.py` 293, `events.py` 277, `testing.py` 236, `eventlog.py` 207, `loopguard.py` 166, `__init__.py` 123, `bg.py` 82, `killguard.py` 55, 4 smaller |
| Gist system | 3,682 | `gists.py` 1,146, `gist_cmd.py` 853, `gist_deps.py` 616, `gist_lint.py` 471, `gist_links.py` 303, `gist_api.py` 293 |
| Doc system | 3,037 | `help.py` |
| Peers | 1,703 | `peer.py` 1,046, `peer_host.py` 262, `peer_stub.py` 238, `peer_pipe_cmd.py` 157 |
| Dashboard | 1,654 | `dashboard.py` 823, `dashboard_html.py` 755, `dashboard_cmd.py` 76 |
| CLI (Architecture) | 1,362 | `lifecycle_cmd.py` 413, `exec_cmd.py` 367, `tasks_cmd.py` 257, `cli.py` 248, `cli_args.py` 77 |
| State-file layering | 1,187 | `state.py` 429, `bind.py` 296, `paths.py` 295, `sessions.py` 92, `relaunch.py` 75 |
| Builtins: gates | 1,047 | `gates.py` 857, `gate_cmd.py` 190 |
| TUI and log | 843 | `display.py` 448, `log_cmd.py` 201, `render.py` 194 |

### Tests, `tests/` (22,109 lines)

| file | phase | lines | subsystem |
| --- | --- | --- | --- |
| `phases/browser.py` | 6 | 6,934 | browser (83 `phase_6_*` cases) |
| `phases/pure.py` | 2 | 3,554 | mixed, 56 cases (65 private functions with helpers); about a third browser |
| `phases/headless.py` | 15 | 2,384 | request flow: bridge, lazy spawn, rebind, push routing |
| `phases/peers.py` | 19 | 1,096 | peers |
| `phases/links.py` | 12 | 1,083 | gist links |
| `phases/lockfile.py` | 5 | 896 | kernel core, state files |
| `phases/gates.py` | 17 | 703 | gates |
| `phases/core.py` | 3 | 698 | request flow |
| `phases/channels.py` | 4 | 679 | channel pushes |
| `phases/gist_tools.py` | 9 | 625 | gists |
| `phases/venv.py` | 16 | 575 | binding |
| `phases/resources.py` | 8 | 543 | gists, docs |
| `harness.py`, `smoketest.py` | | 537, 421 | |
| `phases/dashboard.py` | 14 | 251 | dashboard |
| `phases/every.py` | 10 | 244 | kernel core |
| `phases/defer.py` | 7 | 208 | kernel core |
| `phases/live_claude.py` | 20 | 206 | gates, live |
| `phases/tasks_cli.py` | 18 | 198 | CLI |
| `phases/sessions.py` | 13 | 143 | state files |
| `phases/shutdown.py` | 11 | 130 | kernel core |

History: 847 commits, 2026-04-16 to 2026-11-25. 88 commit messages name a health pass, a
review, an audit or "findings", 29 "health pass" (`git log -i -E --grep`); in subjects alone, 42 and 24.

## Real use

ran: a read-only scan of `~/.claude/projects/*/*.jsonl` and the subagent files below them
(28,641 files, 27 GB) with `rg` and `jq`, 2026-11-28. Tool calls are `tool_use` blocks in
assistant lines, deduplicated by `tool_use` id. No transcript text is kept here.

| measure | value |
| --- | --- |
| `mcp__repld__*` tool calls | 85,002 in 3,140 sessions, 168 working directories |
| dates covered | 2026-08-24 to 2026-11-28 (45 calls in August, 19,298 in September, 30,315 in October, 35,344 in November) |
| top directories | `supervise` 35,595; `claude_code_research` 21,878; `partbridge` 6,690; `bulletins-chat` 4,724; `allcraft` 3,720; `repld` 2,541; `education` 1,595 |
| from subagents (`isSidechain`) | 8,022 |

### Tool calls by name

| tool | calls | share |
| --- | --- | --- |
| `exec` | 69,827 | 82.1% |
| `browser_*` (28 of 31 registered tools seen) | 8,600 | 10.1% |
| `get_task` | 5,307 | 6.2% |
| gist `_tool_*` tools (42 names; `tv_*` 431, `desktop_*` 409) | 925 | 1.1% |
| `cancel` | 293 | 0.3% |
| `repld_restart` | 50 | 0.1% |

`browser_*`: `js` 2,845, `click` 1,243, `screenshot` 1,089, `cdp` 689, `navigate` 581, `open`
573, `type` 281, `tabs` 234, `tree` 232, `key` 208, `invoke` 106, `pages` 101, `network` 101,
`controls` 68, `detach` 48, `console` 39, `request` 28, `fetch` 26, `body` 24, `hover` 20,
`expect_file_chooser` 18, `watch` 15, `set_files` 13, `release` 6, `dismiss_dialog` 5, `take`
3, `select` 2, `drag` 2. Never called: `browser_clear`, `browser_expect_auth`,
`browser_grant_permissions`.

### Inside `exec` code (69,748 cells with code)

A cell counts once per pattern. Builtin patterns are bare names, not attribute calls.

| pattern | cells | share | where most |
| --- | --- | --- | --- |
| `await` | 27,823 | 39.9% | everywhere |
| `import` or `from` | 34,388 | 49.3% | top modules are gists: `roster` 10,927, `verdict` 5,590, `dispatch` 2,240 |
| `defer(` | 15,376 | 22.0% | `supervise` 10,860, `claude_code_research` 4,015 |
| `browser.` | 2,920 | 4.2% | `get` 1,278, `open` 509, `connect` 444, `pages` 343, `tabs` 292 |
| `no_display(` | 743 | 1.1% | |
| `claude_sessions`, `current_session_id`, `list_claude_connections` | 662 | 0.9% | `supervise` 481 |
| `help(` | 156 | 0.2% | |
| `every.list` / `every.cancel_all` | 151 | 0.2% | |
| `notify(` | 132 | 0.2% | 31 with `session=` |
| `park_pushes`, `unpark_pushes`, `rebind_claude_session` | 109 | 0.2% | `supervise` 71 |
| `peers.` | 93 | 0.1% | `voice-api` 46; targets: `supervise` 57, `bulletins-chat` 13; one `host:` target; 3 with `start=True` |
| tab `.confirm(` / `.choose(` / `.ask(` | 80 | 0.1% | |
| `@every` / `every(` | 51 | 0.1% | |
| `ask(` | 22 | | 8 in `repld` |
| `confirm(` | 19 | | 9 in `repld`, 3 in phase 20's scratch projects |
| `choose(` | 4 | | all in `repld` |
| `form(` | 4 | | all in `repld` |
| `GateDeclined` | 2 | | `repld` |
| `peer_caller` | 1 | | |

`exec` passed no `timeout` in 57,978 calls (83%). The nudge text
(`[task <id> still running after`) names 8,300 distinct task ids. Most `notify()` traffic does
not show in cell code: gists call it (see the push kinds below).

### Channel pushes

Counted as `queue-operation` `enqueue` lines whose content opens with a repld channel tag.
12,562 lines, 10,323 distinct, 2026-08-24 to 2026-11-27, in 1,278 session files across 34
project directories (`supervise` 8,139, `klara` 1,675, `repld` 876). Whether each one started a
turn is not in this count.

| kind | pushes | source |
| --- | --- | --- |
| `task_done` | 6,154 (a distinct-id count gave 6,168, more than the lines; not reconciled) | kernel: `exec` past its timeout, `defer()` |
| `roster_tell`, `pane_route`, `roster`, `continue`, `message`, `dev`, `land_step`, `run_step`, others | about 3,800 (12,562 less the other rows) | `notify()` from gists, kinds the gist chose |
| `console_error` 753, `tab_closed` 486, `dialog` 187, `controls` 127, `download_*` 71, `filechooser` 26, `captcha` 4, `auth` 2, `pin_lost` 1, `tab_crashed` 5 | about 1,660 | browser, per tab |
| `kernel_started` 177, `init_loaded` 75 | 252 | kernel boot |
| `loop_blocked` 87, `loop_unblocked` 73, `loop_kill` 13 | 173 | watchdog; `klara` 35, `supervise` 18, `allcraft` 17 |
| `browser_reconnected` 79, `browser_disconnected` 22 | 101 | browser, broadcast |
| `confirm` 61, `ask` 8, `choose` 4, `form` 2 | 75 | gates; `repld` 34, `ultraportalen` 11, `voice-api` 7 |
| `every` 57, `bg_task_error` 36, `error` 106 | 199 | tickers, background errors |
| `kernel_crashed` | 3 | bridge; `allcraft` 2026-10-15 and 2026-10-16, `repld` 2026-10-17 |
| no `kind` | 145 | |

### Other surfaces

| surface | count | how |
| --- | --- | --- |
| `repld://` resource reads | 83: `docs/browser` 23, `docs/guide` 22, `gists/<name>` 11, `help/*` 21, `docs/peers` 3, `docs/playbook` 2, `browser/controls` 1; `docs/production` and `docs/browser-internals` 0 | ran |
| `repld` CLI in Bash calls (not deduplicated) | `exec` 183, `tasks` 148 (`wait` 18), `gist lint` 138, `status` 79, `log` 38, `gist list` 26, `help` 25, `restart` 23, `gate` 23 (`answer` 22), `bridge` 19, `stop` 12, `rebind` 9, `start` 8, `gist stub` 6, `gist add` 2, `dashboard` 2, `peer-pipe` 2, `gist new` 1, `gist fetch` 0 | ran |
| `REPLD_CODE_MODE` | named in 338 `supervise` session files; whether a bridge ran with it is not visible in transcripts | ran, weak |
| dashboard page, TUI pane, pill clicks, elicitation modals | not visible in transcripts | not measured |

What this says: repld is an `exec` server with background completion. `exec` and `get_task`
are 88% of tool calls, `task_done` is half of all pushes, and two orchestration projects are
two thirds of the calls. The browser is a real second use at a tenth. Gates, `@every`, peers and the
doc resources are each under 0.3%.

## Facts measured elsewhere

Not rerun here.

| fact | how |
| --- | --- |
| a channel push cannot ride a 2026-07-28 connection, and Claude Code 2.1.332 has no Events client | docs and bundle: `S2:research-2026-11-26.md:/## Channels and events/` |
| an MCP task result wakes an idle interactive session and an idle `--bg` session | ran: `S2:research-2026-11-26.md:/## MCP tasks, measured/` |
| `claude -p` sends no tasks capability; a tool that can answer inline does | ran: same section, grep `waited 3s inline` |
| `tasks/cancel` arrives twice in one second | ran: same section, grep `sent twice in one second` |
| `input_required` shows as a dialog, also on `claude attach` to a `--bg` session; under `-p` the client answers `cancel` | ran: same section |
| the inbox socket `cc-socks/<pid>.sock` takes a `user` frame that starts a turn on an idle session; a script's frame reads to the model as "Another Claude session sent a message" with no header | ran: `S2:research-2026-11-26.md:/## The inbox socket, measured/` |
| 2026-07-28 removes `initialize`, `notifications/initialized` and `ping`; server-initiated `elicitation/create` becomes an `input_required` result | docs: `~/.local/share/resources/github.com/modelcontextprotocol/modelcontextprotocol/tree/HEAD/docs/specification/2026-07-28/changelog.mdx`, grep `Make MCP stateless` and `Multi Round-Trip Requests` |
| a repld task result after a relaunch was not delivered; after `/clear` with the rebind hook it was | ran: `S2:research-2026-11-27.md:/not delivered/` |
| a stdlib server over stdio at 2026-07-28 with the tasks extension works in a live session | ran: `~/Projects/private/termtap2/RUNLOG.md:/## The new server/` |
| Claude Code cuts server instructions at 4,096 characters | ran: `~/Projects/private/termtap2/RUNLOG.md:/Claude Code cut off the server instructions/` |

## Tagged invariants

One row per bold rule in `CLAUDE.md` (Key subsystems, Architecture, Key invariants) and per
bold rule that exists only in `CLAUDE-ARCHIVE.md`. The `mcp2` column names what the move
removes or changes: `channel` (the rule exists to make or route a channel push), `handshake`
(`initialize`, version or capability negotiation), `elicitation` (server-initiated
`elicitation/create`).

### Key subsystems

| # | rule | where | tag and evidence | mcp2 |
| --- | --- | --- | --- | --- |
| 1 | Threading model | `C:/Threading model:/` | docs: a description of the shape | |
| 2 | Nothing repld runs may block the shared loop | `C:/may block the shared loop/` | ran: `A:/measured loop stall across the delta/`; 87 `loop_blocked` pushes in real use | |
| 3 | `pre_observe` reads `MAX(rowid)`, views go through `to_thread` | `C:/reads its cutoff from/` | ran: `A:/evaluated a whole CTE chain/` | |
| 4 | The event store ages out by rotation, never `DELETE` | `C:/ages out by rotation/` | ran: `A:/measured 2026-11-21 on duckdb 1.5.2/`, `c919c7fd` | |
| 5 | A wedged loop is reported, and only its holder is cancelled | `C:/A wedged loop is reported/` | ran for the report (173 watchdog pushes in real use). "Never a bystander, never a `repld-` task": imagined (review), `9fbf9220` | channel: `loop_blocked` is a push |
| 6 | Request flow | `C:/Request flow:/` | docs | handshake and channel: discovery from `kernel.cache`, the relay |
| 7 | State-file layering | `C:/State-file layering:/` | docs: a refactor record, `A:/a grab-bag of five modules/` | |
| 8 | `spawn.py` lands the kernel in a transient service, never a scope | `C:/decides where the kernel lands/` | ran: `d8ec2307` (measured cgroup and parentage). Unit name keyed by socket path: imagined | |
| 9 | `channel.py` owns the push; `render.py` shared by pane and log | `C:/so pushers import it at module level/` | docs: import structure | channel: `channel.py` is the push |
| 10 | `bg.py` is `spawn(coro)` and nothing else | `C:/and nothing else/` | imagined (review): `f949dcf7`; no lost task recorded | |
| 11 | The dispatch must be reachable after a failed store | `C:/The dispatch must be reachable too/` | imagined (review): `A:/a raise from the store must not skip both/` | |
| 12 | Off-loop callers pass `loop=`, never `loop.create_task` | `C:/it doesn't wake an idle loop/` | ran, weak: `A:/an idle loop is never woken/` describes the masked failure; test `phase_6_offloop_writes`; no failing run cited | |
| 13 | Builtins; `help` bound to stdout, no pager | `C:/pydoc's default pager forks/` | imagined: present from `cfddebda`, no deadlock run recorded | |
| 14 | Doc system: four surfaces, instructions under 4,096 characters | `C:/INSTRUCTIONS_BUDGET/` | docs: the audit in `3a98a11f`; the cut itself is ran in termtap2 (above) | handshake: instructions are composed "at MCP init" |
| 15 | Topic list derived from `_TOPICS`; every topic a resource | `C:/never hand-write one/` | imagined: a drift guard | |
| 16 | Monkeypatch `Browser` where `pool.py` resolves it | `C:/Monkeypatching:/` | imagined: `A:/silently leaves the real class in play/`, no run cited | |
| 17 | Selector resolution is engine-only | `C:/Selector resolution is engine-only/` | source: Playwright's engine, vendored in `21533ced`; spellings pinned by phase 6's translation table | |
| 18 | A `url=` browser retires on a refusal, not on one transient failure | `C:/is retired on a refusal for good/` | ran, against a fake provider: `A:/Measured before the change, against a fake provider/`, `801fa280` | channel: `browser_disconnected` |
| 19 | A scoped lease's refusal only skips fronting | `C:/SCOPE_REFUSAL_MARKER/` | ran: `54bfa8a3`, `A:/failed before reaching/` | |
| 20 | Captcha detection is vendor fingerprinting, never solving | `C:/Captcha detection is vendor fingerprinting/` | ran: `A:/Prototyped 2026-10-20 on issues.chromium.org/`; 4 pushes in real use | channel: the `solved` push |
| 21 | `_ensure_front` does not await its probe; a warm click is six hops | `C:/sends its visibility probe and does not await it/` | ran: `A:/Measured 2026-10-16 with a 50 ms hold/`, `A:/delivered zero clicks/`, `6a1c19e4` | |
| 22 | Fetch interception on `get()`/`open()`, capture skips assets | `C:/Fetch interception is enabled/` | ran, undated: `A:/Capturing costs a round trip plus/` | |
| 23 | `since=` is epoch seconds across three clocks | `C:/is epoch seconds/` | ran: `A:/silently matches every row/` | |
| 24 | Settle counts `inflight_count()`, never `len()` | `C:/Streamed responses leave/` | imagined (review): `2a019d57` | |
| 25 | Loop-owned state is a `LoopOwned` | `C:/the type carries the rule/` | imagined (review) for the race (`a83b25fa`, `2d3fb0ab`); ran for the guard: `8d997740` (it caught one test writing from the wrong thread) | |
| 26 | The DuckDB close is loop-owned; `_db_lock` | `C:/The DuckDB close is loop-owned too/` | imagined (review): `81dabc88` | |
| 27 | Per-session registrations are replayed in `_reattach_core` | `C:/not at its call sites/` | imagined (review): `be307b30`, `A:/calling an undefined function/` | |
| 28 | `Page.screencastFrame` never reaches `store_event`; handlers serialized | `C:/a frame per repaint would evict/` | ran for serialization: `47f56388` ("handlers ran three at once"). The eviction: imagined | |
| 29 | A tab is `agent` or `shared`; native prompts follow the action window | `C:/native prompts follow the action window/` | ran: `A:/started agent loops/`, `b013333e`; the dialog and `taken` follow-ups are imagined (review): `A:/The review that followed/`, `525b3a02` | channel: the pushes |
| 30 | Browser pushes route by `CDPSession.route_to` | `C:/Browser pushes route/` | ran for the original complaint (same); the ordering of the five claims is imagined (review) | channel: a targeted push needs a session to push to |
| 31 | Pin and gate bridge | `C:/Pin\/gate bridge:/` | docs | |
| 32 | `pin.py`'s two injection routes differ on `document.body` | `C:/two injection routes differ/` | imagined (review): `A:/masks this during manual testing/` | |
| 33 | `save_hint()` merges | `C:/rewriting the browser keys only when/` | imagined (review): `A:/would throw away the previous kernel/` | |
| 34 | `GET /` is authenticated; `POST /api` Bearer-only | `C:/because the page carries the token/` | security, held by the method's rule 6, not by a break: `a4a08b4c` | |
| 35 | Gist MCP tools from typed `_tool_*` functions | `C:/discovers typed/` | docs; 925 gist tool calls in real use | |
| 36 | Gist deps install to a shared versioned dir, appended to path | `C:/never the active venv/` | ran: `dfeefc30` (deps broke once the kernel bound to the project) | |
| 37 | Cross-project links record paths, no copy | `C:/Cross-project links/` | docs; `repld gist add` seen twice | |
| 38 | `gist fetch` copies, enforces three collision scopes | `C:/sibling, not/` | imagined; never called in transcripts | |
| 39 | The gist modules share helpers through an import cycle | `C:/call-time-attribute import cycle/` | imagined: `A:/the failure a module split causes silently/` | |
| 40 | Peers: one connection per call, no `initialize` | `C:/One connection per call/` | source: `ipc.Server._read_loop` serves one request at a time; `0d5684a4` | |
| 41 | Peers: cancel is a hangup, never retried once written | `C:/Cancel is a hangup/` | imagined: design, phase 19 | |
| 42 | Peers: JSON-RPC error never ran, `result.error` ran, EOF unknown | `C:/means the target never ran/` | imagined: design; `177a6aa4` fixed a clean disconnect read where `call_error` was due | |
| 43 | Peers: reach is exec's reach | `C:/Reach is exec's reach/` | docs: a decision | |
| 44 | A peer connection is never a Claude session | `C:/is never a Claude session/` | imagined: design | channel: half the rule is about pushes that have nowhere to go |
| 45 | The caller rides per call in `_meta.caller` | `C:/The caller rides per call in/` | imagined: design | |
| 46 | `start=True` is a subprocess under `clean_env` | `C:/is a subprocess, not/` | imagined: no run of the wrong-project spawn recorded; 3 cells use it | |
| 47 | Stubs are generated from AST | `C:/never imported on the peer/` | imagined: design; `repld gist stub` seen 6 times | |
| 48 | A `host:target` peer rides `ssh host repld peer-pipe`; status line first; forwarded calls ping first | `C:/the status line comes first/` | ran for the forward's speed: `ac00fab1` (30 ms against 320 ms, a Mac on the LAN). The dead-forward ping: imagined. One `host:` call in real use | |
| 49 | Gists reach builtins as `repld.X` | `C:/Gists reach builtins as/` | docs | |

### Architecture

| # | rule | where | tag and evidence | mcp2 |
| --- | --- | --- | --- | --- |
| 50 | `gate answer` has a verbatim argv region | `C:/no flag parser may enter/` | imagined (review): `e15ca7b3`, `A:/silently repointed the connection/` | |
| 51 | `--project` and `--project-git` are global, before the subcommand | `C:/are global options, before the subcommand/` | ran: `27947035` (measured what Claude Code hands a worktree), `3b9019b4` | |
| 52 | Argument validation lives once, in `cli_args.py` | `C:/Argument validation lives once/` | imagined (review): `A:/started a proxy that blocks on stdin forever/`; the archive files its test under input nobody writes on purpose | |

### Key invariants

| # | rule | where | tag and evidence | mcp2 |
| --- | --- | --- | --- | --- |
| 53 | One process, one asyncio loop | `C:/One process, one asyncio loop/` | docs: the product's premise | |
| 54 | A lock across a CDP command has an unlocked core | `C:/must have an unlocked core/` | ran: `A:/confirms the wrapper version hangs/`; found by review (`a83b25fa`, "four sockets, measured") | |
| 55 | `exec` returns fast or defers, decided once (`mark_nudged`) | `C:/decided once/` | imagined (review): `A:/got neither: no push/`, `22002703` | channel: the nudge promises a push; a task result replaces both |
| 56 | The kernel refuses a signal to its own process group | `C:/aimed at its own process group/` | ran: `6a099030` (2026-11-01, a worker's `os.killpg` took every session), `5eb401e5` | |
| 57 | The kernel raises its soft `RLIMIT_NOFILE` at boot | `C:/RLIMIT_NOFILE/` | ran on macOS 27.2: `63dbe79e`, `0512cf7e` | |
| 58 | Stdlib only in core | `C:/Stdlib only in core/` | docs: a decision, kept by decision 1 | |
| 59 | Per-cwd, localhost-only; localhost is not a uid boundary | `C:/localhost is not a uid boundary/` | security, held by the method's rule 6 | |
| 60 | Runtime state lives under XDG; `kernel.cache` answers the next discovery | `C:/Runtime state lives under XDG/` | docs: `e952930c`; the cache surviving a reboot is imagined | handshake: the cache holds the `initialize` answer |
| 61 | Scratch cleanup has two rules | `C:/two rules, no third/` | ran: `8133c87d` (an idle kernel never reclaimed spill files) | |
| 62 | Nothing under `RUNTIME_DIR` is readable by other users | `C:/readable by other users/` | security; found by a cleanup pass, `b6036c65` | |
| 63 | One kernel per project, by flock on a file never replaced | `C:/enforced by flock/` | imagined for the inode reasoning; `048fe81d` fixed a shutdown race; test `_concurrent_boots` | |
| 64 | The kernel binds to the project's interpreter | `C:/never spliced across versions/` | ran: `9f4a5c41` (a kernel could not import its project) | |
| 65 | A rebound process's `sys.executable` is a deleted overlay | `C:/is an overlay its/` | ran: `166c5da4` | |
| 66 | A venv below `PYTHON_FLOOR` is never re-exec'd into | `C:/is never re-exec'd into/` | ran: `ff34c882` (the MCP server failed to start) | |
| 67 | Bridge-served tools are a narrow set | `C:/Bridge-served tools are a narrow set/` | docs: a decision; `repld_restart` 50 calls | |
| 68 | Anything both sides answer lives in `core_schemas.py` | `C:/both sides of the socket answer/` | imagined (review): `A:/cold-start sessions nobody is watching/` | handshake: `PROTOCOL_VERSION`, `negotiate_version`, `CAPABILITIES`, three `initialize` authors |
| 69 | A connection's Claude Code id can change under it (rebind) | `C:/can change under it/` | ran: `307c0a40`; `A:/probing a logging stdio MCP server under Claude Code 2.1.316/`; `2187751b`, `c7ceb7a3`, `e1c9f5e6` (run on macOS) | channel and handshake: the id routes pushes and is re-stamped on `initialize` |
| 70 | A rebind makes `new_id` the heir of the old id | `C:/the heir of the id it replaced/` | ran: `2e3d1c39` (red on the base), `b9dc63bb`; a loss still stands at `C:/One ordering still loses the push/` and `S2:research-2026-11-27.md:/not delivered/` | channel |
| 71 | `park_pushes` holds pushes across a handoff | `C:/holds channel pushes bound for that connection/` | ran: `94c30bbb` (clear gaps of 197 s and 228 s); 109 cells in real use | channel |
| 72 | The bridge is a stateful proxy, not a byte-pipe | `C:/not a byte-pipe/` | ran for stranded requests: `b7987aa6`. `kernel_crashed`: 3 pushes in real use, `d394d71c`. Client cancel: docs, the audit in `3a98a11f` | handshake: caching and replaying `initialize`; channel: `kernel_crashed` |
| 73 | Kernel spawn is lazy | `C:/Kernel spawn is lazy/` | imagined (review): `902f55bc`, `A:/paid the full 5 s spawn-and-wait/` | handshake: `notifications/initialized` tracking, `ping` |
| 74 | The project bootstrap is a file, not a flag | `C:/is a file, not a flag/` | ran, weak: `A:/silently dropped it/`; 75 `init_loaded` pushes | channel: `init_loaded`, `init_error` |
| 75 | Every lazy-spawn trigger waits on the bootstrap | `C:/waits on the bootstrap/` | ran: `f0e73229` (a `NameError` on the first call after a lazy spawn, in the voice-api build) | |
| 76 | A human gate stays answerable with no pane; five callers | `C:/must stay answerable on a kernel with no pane/` | ran: phase 20 `_print_mode_leaves_gate_open`; `repld gate answer` 22 times in real use | channel: `awaiting_human`; elicitation: one of the five callers |
| 77 | A gate is asked over MCP elicitation, un-nested | `C:/never rides the connection that asked/` | ran: `TODO.md:/Probe run 2026-11-06 against Claude Code 2.1.316/`, `e8dc045a` | elicitation: the whole rule |
| 78 | A gate that ends unanswered is announced | `C:/A gate that ends unanswered is announced/` | imagined (review): `f525c1e2` | |
| 79 | The pane's stdin has one reader | `C:/has one reader/` | imagined (review): `234593d9` | |
| 80 | A `bg` session's pushes go to its inbox socket | `C:/go to its inbox socket/` | ran: `e46326a8`, phase 20 `_bg_worker_gets_inbox_push` | channel: this route becomes the only ambient one, for every session kind |
| 81 | Targeted push for call-scoped output, broadcast for ambient; owed pushes | `C:/Targeted push for call-scoped output/` | ran: `773f2d56` (a push to a departed connection was lost for good), `556d6862` | channel |
| 82 | `exec` results are shaped for a script under `REPLD_CODE_MODE=1` | `C:/REPLD_CODE_MODE=1/` | ran for the measurement: `C:/receives that as a string (measured)/`, `ccfa1a03`. Use not measured | handshake: stamped on `initialize` |
| 83 | Anything that outlives its cell clears `tasks._current_task` | `C:/is ambient and must clear/` | ran, weak: `A:/budget and was then dropped by the pane/`, `be307b30` | |

### Only in the archive

| # | rule | where | tag and evidence | mcp2 |
| --- | --- | --- | --- | --- |
| 84 | Writing loop-owned state off-thread needs its own path | `A:/ask which callers reach it, not which tool does/` | imagined (review) | |
| 85 | Version negotiation echoes a supported request | `A:/Version negotiation is echo-what-they-asked-for/` | docs: the 2025-06-18 spec, `21533ced` | handshake: goes |
| 86 | `CAPABILITIES` must not drift between the two authors | `A:/can't paper over a drift either/` | imagined | handshake: capabilities ride each request |
| 87 | Only a version-matched venv may be `addsitedir`'d | `A:/splicing a mismatched one half-works/` | imagined: reasoned, phase 16 | |
| 88 | The pane's claim on stdin releases when its reader ends | `A:/decline for the rest of the process's life/` | imagined (review) | |
| 89 | The two boot prompts share `yes_by_default` | `A:/declined the browser-restore prompt/` | imagined (review) | |
| 90 | `_sessions`/`_browsers` walks snapshot with `list(...)` | `A:/One entry is history rather than current practice/` | superseded by row 25 | |

Totals over the 90 rows, by the first tag in each: 37 `ran` (3 weak; several are `ran` for
one half only and say which), 3 security, 17 `docs` or `source`, 32 `imagined` (18 from a
review), 1 superseded. 25 rows carry an `mcp2` mark.

## Tagged tests

Per phase, from `tests/phases/CLAUDE.md` and `A:/per-phase rationale/`. "Earned" means the
case answers a `ran` row above.

| phase | file, lines | covers | tag | mcp2 |
| --- | --- | --- | --- | --- |
| 2 | `pure.py` 3,554 | 56 pure cases | mixed. Earned: `_nofile_limit` (row 57), `_own_group_signal` (56), `_seat_pid_claims`, `_ps_ancestry` (69), `_taken_id_returns_when_the_taker_closes` (81), `_instruction_budget` (14), `_captcha_describe` (20). Imagined: `_cli_args`, `_socket_path_length`, `_json_types`, `_envelopes` | `_elicitation_pure`, `_strip_output_schemas`, `_display_as_json`, `_seat_pid_claims`, `_ps_ancestry`, `_one_snapshot_per_call`, the two `_taken_id_*`, `_crash_reason`, `_channel_meta_keys` go or change |
| 3 | `core.py` 698 | handshake, `exec`, `get_task`, schema checks, JSON-RPC edges, client cancel, Code Mode | `phase_3` (223 lines): docs (spec conformance). `_phase_3_input_validation`, `_phase_3_jsonrpc_edges`, `_phase_3_client_cancel`: docs, from the audit `3a98a11f`. `phase_3_argv_and_registry` (80), `phase_3_patch_targets` (97), `phase_3_task_spawns` (54): imagined, and the archive says so: `A:/guards that only fire on input nobody writes on purpose/`. `phase_3_own_group_signal`: earned (56) | handshake: version negotiation, `initialize`, `ping`, capabilities assertions; `phase_3_code_mode` |
| 4 | `channels.py` 679 | `task_done` push, `notify()`, pre-`initialized` queue, session lookups, `exclude=`, meta augmenter, channel spill, owed push | `phase_4g_owed_push` (195): earned (81). `phase_4b_pregate`: imagined. The rest: docs of the push surface | channel: the whole file; what `notify()` must still do is re-earned on the inbox socket |
| 5 | `lockfile.py` 896 | flock, bootstrap, permissions, zombies, sweeps, `add_search_dir` | earned: both bootstrap waiters (75), spill eviction (61), `pid_alive` through a zombie (`16396bdf`, observed tearing down kernels), owner watch (`3e2412f3`). Security: permissions. Imagined: a lock-less directory left alone, `add_search_dir` idempotence | |
| 6 | `browser.py` 6,934 | 83 cases, most against a real Chrome | mixed, and the best evidenced: the archive records a measurement for click round trips, click arrival, plan-click, renderer busy, claim routing, auth, captcha, url redial, scoped lease (`A:/Checked to fail with the/`). Imagined: `phase_6_like_escaping`, `phase_6_connect_race` (review, row 54's sibling), `phase_6_injected_source_provenance` (a licence guard) | `phase_6_claim_routing`, `phase_6_download_routing`, `phase_6_owned_tab_action_routing`, `phase_6_reconnect_push` assert channel delivery |
| 7 | `defer.py` 208 | `defer()` push and result recovery | docs of the most used builtin (15,376 cells); `3fdd3c48` records the result being thrown away | channel: the push half |
| 8 | `resources.py` 543 | gist resources; two AST doc-drift guards | imagined: both guards answer drift nobody hit in a run; 83 resource reads in three months | |
| 9 | `gist_tools.py` 625 | `_tool_*` discovery, schema, reload, `list_changed` | docs; 925 calls of real use. Stale `__repld_tools__` reading inert: imagined | `tools/list_changed` delivery changes (`subscriptions/listen`) |
| 10 | `every.py` 244 | tickers | earned: ticker attribution (83). `every(tab=)` refusal: imagined. 51 cells of use | channel: tick errors are pushes |
| 11 | `shutdown.py` 130 | drain within 2 s | ran, weak: memory `session_2026_04_25` names a shutdown drain; no commit located | |
| 12 | `links.py` 1,083 | `gist add`, manifest, stale entries, co-link refusal | imagined for the refusal and warning cases (`A:/itself was missing/` is a review find); `gist add` used twice | |
| 13 | `sessions.py` 143 | session registry file | docs; `117206f7` (one unreadable file crashed `repld status`) | |
| 14 | `dashboard.py` 251 | header bound, Host allowlist, token, cookie, Bearer | security (rows 34, 59). `node --check` of the page: earned, `A:/shipped broken once/` | |
| 15 | `headless.py` 2,384 | lazy spawn, heal, cache discovery, rebind, park, owed pushes, `--project`, `start`, ephemeral | earned: `_rebind_carries_owed_pushes` (152, row 70), `_session_rebind` (284, 69), `_deferred_rebind` (83 lines), `_seat_pid_claim` (120 lines), `_inflight_never_stranded` (row 72), `_project_flags` (row 51), `_bg_inbox_fallback` (row 80). Imagined (review): `_cold_jsonrpc_edges`, `_version_mismatch_cache_discarded`, `_new_project_uses_global_cache`, `_bridge_tool_bypass_error`, `_tasks_version_skew`, `_log_renderer_covers_every_event` | about 1,400 lines assert channel routing or the handshake: the eight rebind cases, `_targeted_push`, `_push_state_broadcast`, `_id_collision_warning`, `_lazy_discovery_from_cache`, the three cache cases, `_elicitation_cancelled_on_kernel_death`, `_autospawn_and_heal`'s replay |
| 16 | `venv.py` 575 | binding, floor, overlay, systemd spawn | earned (64, 65, 66, 8). `REPLD_BOUND` sentinel: imagined | |
| 17 | `gates.py` 703 | CLI, dashboard, elicitation, secret gate, pane mirror | earned: `_elicitation` (77), the CLI route (76). Imagined (review): `_pane_drops_a_timed_out_gate`, `_tty_prompt_declines_once_display_owns_stdin`, flags-as-answer | elicitation: `_elicitation` (149), `_secret_gate` (91) |
| 18 | `tasks_cli.py` 198 | `repld tasks wait` / `cancel` | docs; `tasks wait` 18 times in real use | |
| 19 | `peers.py` 1,096 | two to five kernels, a stand-in `ssh` | imagined for most: the typed-error split, cycles, payload limits and the dead forward were designed, not hit. Earned: a graceful stop reading as a clean disconnect (`177a6aa4`). 93 cells of use, one over ssh | |
| 20 | `live_claude.py` 206 | gates under a real `claude -p` and `--bg` | ran by construction: the only phase that runs the real client | channel and elicitation: both assertions (`queued`, inbox `written`) are about push delivery |

## Deletion candidates

Ranked by lines removed against evidence that the code is needed after the move. Nothing was
deleted in this session. Line counts are rough, from function spans.

| # | what | files and symbols | lines (src + tests) | tag | evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | Push routing by session id: rebind, pending rebind, successors, `_claude_ids`, outbox, park, process-tree ancestry, seat pid | `ipc.py`: `Server.rebind_claude_session` (138), `register_claude_session` (74), `_owe`, `_drain_outbox`, `expire_outbox`, `deliver`, `_succeed_locked`, `_settle_parked`, `Session.park`, `_unpark_and_flush`, `_read_ps_table`, `_bridge_chain`, `_caller_chain`, `park_pushes`, `unpark_pushes`; `rebind_cmd.py` (113); `channel.push_owed`; `bridge._seat_pid`, `_stamp_identity` | about 800 + 1,500 | ran, each for a channel break; mcp2 removes the channel | rows 69, 70, 71, 81. A task result is polled by the client that called. What a task does across `/clear` and a relaunch is session 1's row. Keep out of the cut: `Session.claude_pid` and the ancestry that fills it back `ipc.list_claude_connections()` (662 cells with its siblings, `supervise` 481), and `park_pushes`/`unpark_pushes`/`rebind_claude_session` are called from cells (109, `supervise` 71). Session 2 records what supervise uses instead before deleting either |
| 2 | Bridge handshake and channel relay: cached `initialize`, replay onto a fresh kernel, version negotiation, capability constants, `notifications/initialized` tracking, the relay of `notifications/claude/channel` | `bridge.py`: `_try_bridge_intercept` (117), `_replay_handshake`, `_load_cache`, `_track_kernel_message`, parts of `_attach` and `_read_kernel`; `core_schemas.py`: `negotiate_version`, `SUPPORTED_VERSIONS`, `CAPABILITIES`; `protocol.py`'s `initialize` | about 500 + 450 | docs and imagined (review) | rows 6, 68, 72, 73, 85, 86; `changelog.mdx:/Make MCP stateless/` |
| 3 | The nudge and its paired claim | `tasks.mark_nudged`, `claim_done_push`, `record_push`, `push_state`/`push_delivered` fields, `kernel._maybe_push_done`, the nudge sentence in `protocol._exec` | about 150 + 250 | imagined (review) | row 55. 8,300 nudges in real use is the traffic a task result takes over. `get_task` stays for `claude -p` (5,307 calls) |
| 4 | Elicitation over `elicitation/create` | `gates.py`: `_send_elicitation`, `_cancel_elicitation`, `parse_elicit_result`, `handle_elicit_result`, `_secret_hint`; `ipc._is_elicitable`; `bridge._relay_client_response`, `_kernel_requests`; `gates/elicit_result` in `protocol.py` | about 200 + 300 | ran (row 77); mcp2 removes server-initiated requests | `changelog.mdx:/Multi Round-Trip Requests/`. Replaced only if session 1's `input_required` row holds for a gate inside a task |
| 5 | Crash push, goodbye frame, reattach watch | `bridge.py`: `_on_kernel_gone` (79), `_push_crash`, `_resolve_crash_reason`, `_reason_from_journal`, `_reattach_watch`, `_start_reattach_watch`; `Session.say_goodbye` | about 220 + 150 | ran, low use | 3 `kernel_crashed` pushes in three months. A crash under tasks is a failed task; `-31001` for an in-flight request stays (row 72) |
| 6 | Code Mode result shaping | `Session.code_mode`, `core_schemas.strip_output_schemas`, `_format_spill_pointer`, `runtime.print_display(as_json=)`, `BRIDGE_CODE_MODE_KEY`; `phase_3_code_mode` | about 150 + 150 | ran for the measurement, use not measured | row 82. Decide after asking the owner whether any session runs with it |
| 7 | Phase 3's three guards, and the pure cases with no break | `phase_3_argv_and_registry`, `phase_3_patch_targets`, `phase_3_task_spawns`; `pure.py`: `_cli_args`, `_socket_path_length`, `_json_types`, `_envelopes` | 0 + about 400 | imagined | `A:/guards that only fire on input nobody writes on purpose/` |
| 8 | `gist fetch`, and the less used half of links | `gist_cmd.py` fetch verb, `_GIST_HOSTS`; `gist_links.py` (303); `links.py` (1,083) | about 450 + 1,083 | imagined | rows 37, 38: `gist fetch` 0 calls, `gist add` 2, `gist new` 1 |
| 9 | Peers over ssh: `peer-pipe`, forwards, the probe | `peer_pipe_cmd.py` (157); `peer.py`: `_connect_ssh`, `_ensure_forward`, `_probe_forward`, `request_sync_ssh`, `close_forwards`; the stand-in `ssh` cases of phase 19 | about 450 + 400 | ran for speed only; one real call | row 48. The owner's memory names "another machine is a `host:` peer" as intended use, so ask before cutting |
| 10 | Browser tools and paths with no calls | `browser_clear`, `browser_expect_auth`, `browser_grant_permissions` (0 calls); `browser_select`, `browser_drag` (2 each); `browser_take` 3, `browser_dismiss_dialog` 5, `browser_release` 6; `captcha.py` (249, 4 pushes) | about 500 + 600 | mixed: `drag`, `select` and captcha were measured on real pages, then barely used | Real use, `browser_*` row. The tab methods behind them are still reachable from `exec`; the cut is the MCP tool, not the method |
| 11 | Dashboard | `dashboard.py`, `dashboard_html.py`, `dashboard_cmd.py`; `phases/dashboard.py`; the dashboard caller of gates | 1,654 + about 350 | not measured | `repld dashboard` twice in Bash calls. `repld tasks` and `status --counts` ride its `/api`, so the API may outlive the page |
| 12 | `form()` and the secret gate | `gates.form`, `build_form_schema`, `_field_property`, `_check_field_dict`, `parse_form`, `validate_form`, `_check_field`; `_secret_gate` test | about 130 + 150 | imagined | 4 `form(` cells, all in repld's own development |
| 13 | Discovery cache copies | `paths.state_cache_for`, `GLOBAL_CACHE`, the three cache cases in phase 15 | about 80 + 110 | imagined | row 60. Whether a kernel-less bridge still needs a cache at all depends on what `server/discover` must answer |
| 14 | Doc resources nobody reads | `help.PRODUCTION`, `help.BROWSER_INTERNALS`; the per-topic `repld://help/*` resources | part of 3,037 | not measured as text length | 0 reads of two docs, 21 reads of all help topics together |
| 15 | `--ephemeral` bridge and owner watch | `bridge._ephemeral_socket_path`, `_teardown_ephemeral`, `kernel._watch_owner`; `phase_15_ephemeral_bridge` (121) | about 120 + 150 | ran (`3e2412f3`), use not measured | which `.mcp.json` files pass `--ephemeral` was not read |

Not candidates, by use: `exec`, `defer()`, `get_task`, `no_display`, gist import and reload,
`browser.get/open/connect`, `tab.js/click/screenshot/navigate`, `repld exec`, `repld tasks`,
`repld gist lint`, the watchdog, the bootstrap wait, binding.

## Unmeasured

For session 1, each as a probe row. Every item has a row with the same number in
"Session 1: MCP 2026-07-28 probes".

1. A task returned by `exec`, then `/clear`: does the client keep polling `tasks/get`, and
   does the new conversation get the result? The channel form of this was measured
   (`S2:research-2026-11-27.md:/The repld binding/`); the task form was not.
2. The same across a bridge restart (Claude Code restarts a stale MCP server on `/clear` in a
   worktree, `C:/restarts an MCP server it marks stale/`) and across a relaunch. Task ids live
   in the kernel, so a fresh bridge could answer `tasks/get`; whether a fresh client asks is
   unknown.
3. Whether every session kind sets `CLAUDE_CODE_MESSAGING_SOCKET`. The bridge already stamps
   whatever that env gives (`src/repld/bridge.py:/CLAUDE_CODE_MESSAGING_SOCKET/`); only the
   kernel's `Session._emit` routes to it for kind `bg` alone (`C:/go to its inbox socket/`).
4. How a `notify()` reads to the model over the inbox socket. The channel tag carries `kind`
   and meta as attributes; a script's `user` frame shows no header. About 3,800 of the pushes
   seen are gist `notify()` kinds whose receivers may key on the tag.
5. A broadcast with no session named: which sockets it goes to.
6. A cell that finishes in 0 s as a task costs the model a turn
   (`S2:research-2026-11-26.md:/never inline/`). 83% of `exec` calls pass no timeout and most
   return inline today; the spike must wait briefly and answer inline, and the wait that
   keeps today's feel is unmeasured.
7. `claude -p`: inline up to what limit, and what `exec` returns past it with no tasks
   capability (today: the nudge text and `get_task`).
8. `tasks/cancel` twice against a kernel task, and a cancel for a task that already finished.
9. A gate inside a task as `input_required`: `confirm`, `choose`, `ask`, and a secret; a
   second gate in the same cell; a gate raised by `defer()` with no request to hang on.
10. Where `instructions` ride with no `initialize`, and whether Claude Code still reads them
    from a 2026-07-28 server.
11. `tools/list_changed` after a gist adds a tool, with no handshake to declare it.
12. A subagent's bridge and a nested `claude -p`: which bridge polls whose task.
13. Kernel death mid-task: what the client shows when `tasks/get` fails or the bridge answers
    for a kernel that is gone.
14. Whether a sync `exec` reply and a task can share one bridge connection to the kernel, or
    the bridge needs a connection per call as peers do (`C:/One connection per call/`).

Could not be determined in this session:

- Human use of the dashboard page, the TUI pane, the pill and elicitation modals. Transcripts
  do not show them.
- Whether any real session runs with `REPLD_CODE_MODE=1`, and which projects register the
  bridge with `--ephemeral` or `--project-git`.
- How many of the 12,562 pushes started a turn, and how many arrived in a session that had
  already moved on.
- The commit behind three archive entries (the `mark_nudged` window, the off-loop
  `create_task`, the shutdown drain); they are tagged weak or imagined for that reason.
- The transcript scan covers 2026-08-24 onward only; older transcripts are not on disk, and
  repld's first four months of history have no use data here.
- Tool-call and push counts include repld's own development and test sessions; they were not
  separated beyond the per-directory figures.

## Session 1: MCP 2026-07-28 probes

2026-11-28, Claude Code 2.1.332 (`/usr/bin/claude`, run under `env -i`), Haiku 5.5, repld
0.24.3 at `f24e6b18`, Linux. Nothing in `src/` or `tests/` changed.

Probes: `probes/mcp2_bridge.py` (a stdlib stdio server at 2026-07-28: `server/discover`,
`resultType`, the tasks extension; it forwards to a real kernel over its unix socket with the
kernel's existing JSON-RPC, and the task id it hands out is the kernel's own),
`probes/mcp2_wire.py` (drives that bridge with no Claude, one function per row),
`probes/mcp2_live.sh` (a scratch kernel run from this worktree's source, disposable sessions
on an isolated tmux server, one verb per step). `wire:` below is a row of `mcp2_wire.py`;
`live:` is a session driven with `mcp2_live.sh`, read from the screen and from the bridge's
wire log (`.live.local/mcp2/<name>.jsonl`, not in git; logs removed 2026-11-28). 15 sessions were started: 10
interactive launches, 3 `claude -p`, 2 `claude --bg`. Each live row ran once unless it says
otherwise. Numbers match "Unmeasured".

| step | result | how |
| --- | --- | --- |
| 0. idle interactive session, `exec` of an 8 s cell | the call returned a task after the 2 s wait; the client polled `tasks/get` once a second; the idle session started a turn with `MCP task <id> (repld2/exec) completed` and the cell's value | ran: live, session `a` |
| 1. a task in flight, then `/clear` | the client kept polling on the same bridge process; the new conversation was woken with the result. `sessionId` changed, the bridge's `CLAUDE_CODE_SESSION_ID` did not, and nothing was rebound | ran: live, `a` |
| 2a. a task in flight, then `/mcp` Reconnect | the old bridge was replaced by a new process, which got `tasks/get` for the old id 4.2 s after it started and answered from the kernel; the session was woken with the result | ran: live, `a` |
| 2b. SIGTERM with a task in flight, then `--resume <id>` with the same server entry | the new process polled `tasks/get` for the old id 0.5 s after its bridge started, and the session was woken with the result. Same when the cell finished during a 30 s gap with no client | ran: live, `a`, twice |
| 2c. the same, relaunched as a new conversation | no `tasks/get`; the result reached no conversation (break 5) | ran: live, `a` |
| 2d. the same with a server entry that differs in one `env` value (`MCP2_NOTE=1` to `2`), the original process gone | the task is restored and never polled (break 4) | ran: live, `e`; first seen on `a3` with two variables changed and the original still alive |
| 2e. a second bridge process, no Claude | answers `tasks/get` for a task the first bridge started and left | ran: wire `fresh` |
| 3. `CLAUDE_CODE_MESSAGING_SOCKET` in the bridge's environment | set in all three kinds: interactive (`CLAUDE_CODE_ENTRYPOINT=cli`), `claude -p` (`sdk-cli`), `--bg` (`cli`, plus `CLAUDE_JOB_DIR`, `CLAUDE_PID`). It is `cc-socks/<claude pid>.sock`, so it survives `/clear` and changes on a relaunch | ran: live, the `start` line of `a`, `p1`, `b` |
| 4. kernel `notify("...", kind="probe_note", run="42")` to an idle interactive session, through the kernel's own `Session._write_inbox` | started a turn. The model saw `Another Claude session sent a message:`, then `[repld push kind="probe_note" run="42"]`, the content, and the standing peer warning. Asked, it said: sent by another Claude session, kind `probe_note`, run 42, not typed by the user | ran: live, `a`. The bridge registered its socket with the kernel as kind `bg`, since `_emit` routes only that kind |
| 4b. the same to an idle, unattached `--bg` session | started a turn; same framing; the model named kind and run | ran: live, `b` |
| 4c. `notify(session=<id>)` after `/clear` changed that id | delivered: the kernel connection still carried the id it registered with, and the socket path had not changed | ran: live, `a` |
| 5. `notify()` with no session named, two registered sessions | one frame to each socket; the interactive and the `--bg` session each started a turn | ran: live, `a` and `b` |
| 6. inline wait | with the kernel's own `timeout` as the wait (2.0 s default), cells of 0, 0.5, 1.5 and 1.9 s came back inline in their own time and a 2.1 s cell became a task at 2.00 s. In sessions, calls under the wait cost no extra turn. A longer or shorter wait was not tried | ran: wire `wait`; live, `a` |
| 7. `claude -p` | no tasks capability on the request; a 4 s and a 95 s cell were held open and answered inline. No client limit was reached at 95 s | ran: live, `p1`, `p2` |
| 7b. Escape during a call held open (interactive; the prompt asked for `timeout` 90) | the client sent `notifications/cancelled` with `requestId` and `reason: "AbortError: user-cancel"`; the bridge sent the kernel `requests/cancel` and the cell's `finally` ran. On the wire, both phases: cancelled inside the kernel's own wait the bridge sent `requests/cancel`, past it `tasks/cancel`; the cell's `finally` ran each time and no message with the cancelled id was written (ids 1 and 4 in the log). An earlier spike still answered the second case 5 ms after the cancel | ran: live, `e`; wire `client_cancel`, `.live.local/mcp2/wire-check3.jsonl` (log removed 2026-11-28) |
| 8. `x` in the task view on a running cell | `tasks/cancel` twice, 1 ms apart; the cell's `finally` ran in the kernel. Cancel of a finished task and of an unknown id: acknowledged, the task stays `completed` | ran: live, `a`; wire `cancel` |
| 8b. exit with a task in flight | a dialog: Exit and stop tasks, Move to background and exit, Stay. Choosing the first sent no `tasks/cancel` (break 1). SIGTERM sent none either | ran: live, `a` |
| 9. `confirm()` in a cell, interactive | a dialog `MCP server "repld2" requests your input` with the gate's prompt and schema; Accept sent `tasks/update`, the bridge resolved the kernel gate, the task completed with `answer=True` and the model saw only that | ran: live, `a` |
| 9b. `ask()` then `choose()` in one cell | two dialogs in turn (text field, option list). Declining the second sent `action: decline`. The kernel has no decline over `gates/resolve`: in the live run the spike cancelled the polled task and the model read `cancelled`; the spike now leaves the gate open in the kernel, stops showing it, and the task reads `working` | ran: live, `a` (the cancelling spike); wire `gate` (the current one) |
| 9c. a gate raised by `defer()` after its cell returned | the spike shows every open gate on every running task that is polled, so it appeared on an unrelated task and that task's `tasks/update` answered it (break 6) | ran: wire `gate` |
| 9d. a secret gate | cannot be answered through `tasks/update` (break 7) | ran: wire `secret`, `.live.local/mcp2/wire-check3.jsonl` (log removed 2026-11-28) |
| 9e. `confirm()` in a `claude -p` cell | the request declares `elicitation: {form, url}` and no tasks. The spike answered the `tools/call` itself with `resultType: input_required`; the client retried 13 ms later with `inputResponses: {<gate>: {action: cancel}}` and the `requestState`, and asked nobody. The gate stayed open in the kernel and `repld gate answer <id> y` resolved it after the run had ended. The model's `timeout` was 120, so the kernel held the call that long before the spike looked for a gate | ran: live, `p3`; wire `notask_gate` (cancel and accept) |
| 10. instructions with no `initialize` | the client calls `server/discover` twice at start and reads `instructions` from it: the model quoted the first and last words of the 3,446 characters served (repld's own 3,435 plus the probe's 11-character `MARK-START ` prefix). At 6,000 characters the model's copy ended `MARK-046@4076 MARK-... [truncated]`: the cut is at 4,096 and it is marked | ran: live, `a` and `p1` |
| 11. a gist adds a `_tool_*` function | the client opens `subscriptions/listen` with `toolsListChanged` at start; on `notifications/tools/list_changed` it asked `tools/list` 3 ms later and the model called the new tool on its next turn | ran: live, `a` |
| 12. a subagent calls `exec` and gets a task | one bridge process serves parent and subagent. The subagent's call blocked while the client polled 24 times; the result came back as the tool's own result ("completed inline"), and the parent was not woken by a task | ran: live, `c`. A nested `claude -p` was not run: it is row 7 with its own bridge |
| 13. the kernel is killed with a task in flight | answered as status `failed` with an error: the session was woken with `MCP task ... failed` and the text. Answered as a JSON-RPC error: break 2. After a kernel restart: break 3 | ran: live, `a` and `d` |
| 14. one kernel connection for every request, replies matched by id | a `get_task` written 0.20 s into a 3 s inline `exec` on the same connection was answered at 3.00 s, in the same millisecond as the `exec`; on a connection of its own, at 0.20 s (break 8) | ran: wire `shared`, kernel-side times in the bridge's log |
| the model's own `timeout` argument | 14 of 21 `exec` calls carried one (2 nine times, then 5, 20, 60, 90, 120); the prompt asked for it once (90). Three times it covered the cell, which was then held inline where a task was expected: 20 over an 8 s cell, 60 over 25 s, 90 over 60 s. The 120 held a gate cell open for 120 s (row 9e) | ran: live, the wire logs of `a`, `a3`, `b`, `c`, `d`, `e`, `p1`, `p2`, `p3`, retries with `requestState` left out |
| `--bg` with the prompt after `--mcp-config` | the session reads `state: failed` and no bridge starts: the prompt is taken as a second config. Placed after `-n` it starts | ran: live, the first `b` |
| trust | no trust prompt in the scratch directory under this worktree, interactive or `--bg`; no dev-channels dialog, since the spike declares no channel | ran: live |

**Breaks**

1. **Exit does not cancel the cell.** `/exit` with a task in flight offered "Exit and stop
   tasks"; taking it wrote no `tasks/cancel`, and `repld tasks` listed the cell as active 19 s
   later. The client's own task view says "Runs on the server, and keeps running after you
   exit this session". ran: live, `a`, task `b2bc7b53f24c`.
2. **A JSON-RPC error from `tasks/get` is never shown to the model.** With the kernel dead
   and the bridge answering `-32603` (`MCP2_GONE=rpc_error`), the client polled for 63 s,
   until the kernel was started again: 79 requests, about 1.25 a second, faster than the
   `pollIntervalMs` of 1000 it kept to while the task answered. The task view read "working,
   status requests come back with errors; retrying", and the session stayed idle. The same
   death answered as a `failed` task (`MCP2_GONE` unset) woke it at once. ran: live, `d`
   against `a`.
3. **An id the kernel no longer holds is polled without end.** After the kernel restarted,
   `tasks/get` answered `-32602 Task not found`; the task view read "the server rejected this
   task's id; asking again", and polling went on for 77 s (78 answers) until the task was stopped
   by hand. ran: live, `d`.
4. **A resumed session refuses a task when the server entry changed.** Resumed with an
   `--mcp-config` whose only difference was one `env` value, after the original process had
   exited, the session showed "1 MCP task" and sent no `tasks/get`; the task view read
   "server 'repld2' was reconfigured to a different transport since this task was created;
   refusing to re-bind so its task id cannot leak to the wrong server". With an identical
   entry it polled (row 2b). ran: live, `e`; before that `a3`, with two values changed and
   the original still alive.
5. **A relaunch as a new conversation gets no result.** No `tasks/get` in 20 s; the cell
   finished in the kernel and no session was told. ran: live, `a`.
6. **`gates/list` does not say which task asked.** A gate opened by `defer()` after its cell
   had returned came back as `input_required` on an unrelated task, and that task's
   `tasks/update` answered it. Showing it there was the bridge's own choice: with no tie it
   can show a gate on every polled task or on none. For the same reason a decline cancels
   nothing in the spike. To tie them the kernel would record `tasks.current_task_id()` on
   the `_Gate` when it opens and return it from `open_gates()`; a gate from `defer()` would
   carry the deferred task's id, which no client polls. source:
   `src/repld/gates.py:/def open_gates/`. ran: wire `gate`.
7. **A secret gate has no route through a task.** `gates/list` carries `secret: true` and no
   page URL; `gates/resolve` answers `-32602 ... takes a secret: answer it on its page`; the
   task stayed `input_required`. ran: wire `secret`, `.live.local/mcp2/wire-check3.jsonl`
   (log removed 2026-11-28).
8. **The kernel answers one request at a time per connection.** With the spike writing
   both requests on one socket and matching replies by id, a `get_task` sent 0.20 s into a
   3 s inline `exec` was answered at 3.00 s; on its own connection, at 0.20 s. An earlier
   run of this row held a lock of the spike's across the read and measured that lock, not
   the kernel. source: `src/repld/ipc.py:/def _read_loop/` reads a line, dispatches it and
   writes the reply before it reads the next, and `src/repld/protocol.py:/def _exec/` waits
   out `timeout` inside that dispatch. ran: wire `shared`.
9. **A `--bg` session's first turn said it had no `exec` tool.** The bridge answered
   `tools/list` at 11:04:02.4 UTC and the reply ("there's no exec tool in my toolset") landed
   at 11:04:12; a later turn in the same session called the tool. ran: live, `b`, once.

Not measured: the restart of a stale MCP server that `/clear` causes in a worktree
(`C:/restarts an MCP server it marks stale/`; `/mcp` Reconnect stood in for it), `--continue`,
a frame to a `claude -p` session's socket, "Move to background and exit", a `--bg` session
woken by a task from this bridge (its one call carried its own `timeout`; the wake is in
`S2:research-2026-11-26.md:/## MCP tasks, measured/`), any wait but 2.0 s, a `-p` cell longer
than 95 s, and any version but 2.1.332.

Left behind: nothing running. The scratch kernel, its unit, its state directory and spills,
the tmux server and both `--bg` rows were removed. The 15 probe transcripts remain under
`~/.claude/projects/`. The first runs used the owner's config and state homes, and the
scratch kernel wrote to both: `~/.local/state/repld/last.cache` was rewritten at each boot,
and `~/.config/repld/gist-registry.json` gained a `probe_hello` entry (since removed by
hand) and had the `project` of the global gists `finn`, `vitals`, `msft_partners` and
`gnome_desktop` set to the scratch directory; their earlier values are lost. From the
rerun on, `mcp2_live.sh` gives the kernel its own `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
under `.live.local/mcp2`, and both files kept their mtimes across it. `~/.repld/gists` is
still read, since that path follows `HOME`. Those two scratch homes
(`.live.local/mcp2/xdg-config/`, `xdg-state/`) and the wire logs beside them remain; the
directory is gitignored. (All of it was removed with that worktree, 2026-11-28.)

## Session 2a: cutover design

2026-11-28, on `agent-cutover-design` off `7330c86a`. A design, not a change: nothing in
`src/` or `tests/` moved. Claude Code 2.1.332, Haiku 5.5, repld 0.24.3. Two disposable
interactive sessions (`f`, `g`) on the isolated tmux server of `probes/mcp2_live.sh`, a
scratch kernel with its own config and state homes, no trust prompt. Line counts are from
function spans (`ast`), rounded; "net" is the file before less the file after.

### Evidence rerun

The session 1 wire logs are gone (marked `log removed 2026-11-28` where a row cites one).
The rows the cut leans on were run again; logs are in `probes/logs/` (64 KB in all; the
driver's own environment rows and repeated polls are left out, each file says so).

| row | result | how |
| --- | --- | --- |
| `form()` through `input_required`, interactive (plan change 7, "Session 2 runs one row first") | the call became a task; the dialog `MCP server "repld2" requests your input` showed all four fields, the two required ones starred, the list field as a picker, the bool as a checkbox. `replicas: 9` against `maximum: 5` was refused in the dialog ("Must be an integer between 1 and 5"), so no out-of-range value was sent. Accept sent one `tasks/update` with `{env, replicas, dry_run}` (the optional `note` absent); the kernel gate resolved, the task completed and the idle session was woken with `form={'env': 'prod', 'replicas': 3, 'dry_run': True}`. While the dialog was open the client did not poll: two `tasks/get` in all, 40 s apart, either side of the `tasks/update` | ran: live, `f`, once; `probes/logs/live-form.jsonl` (the two `tasks/get` requests are left out of the file; its note line gives their times, read from the full log before it was removed) |
| the same on the wire, and an answer the kernel refuses | accepted content resolves the gate when the bridge hands `gates/resolve` the whole object as JSON (the spike sent the first field only; fixed in `probes/mcp2_bridge.py:/takes a form's answer as one JSON object/`). Content over a bound: `gates/resolve` answers `-32602 replicas: at most 5`, the spike passes that on as the `tasks/update` error, and the next `tasks/get` still reads `input_required` | ran: wire `form`, twice (the second run for a log trimmed evenly); `probes/logs/wire-form.jsonl` |
| Escape on a gate dialog ("Esc to answer later") | no `tasks/update` at all. The client went back to polling `tasks/get` once a second (16 in the run), the footer read `1 MCP task` and `needs your input`, and the dialog did not reopen on its own. `repld gate answer <id> y` from a shell then resolved the gate; the task completed and the session was woken with `confirm=True` | ran: live, `g`, once; `probes/logs/live-gate-later.jsonl` |
| a gist tool added mid-session | on `notifications/tools/list_changed` the client asked `tools/list` 2 ms later and did not ask `server/discover` again in the 10 s watched: instructions are read at start only | ran: live, `f`, once; `probes/logs/live-list-changed.jsonl` |
| the client's first message | `server/discover` with id `server-discover-probe-1`, then `subscriptions/listen`, a second `server/discover`, `tools/list`. No `initialize` is tried first. A `tools/call` carries `claudecode/toolUseId` and `progressToken` in `_meta` and no session id | ran: live, `f`; `probes/logs/live-form.jsonl` holds the discover, listen and `tools/call` rows; the `tools/list` row is left out of it and named in its note line |
| break 8 (one connection, replies matched by id) | shared: the `tasks/get` and the 3 s inline `exec` both answered at 3.00 s. A connection per request: `tasks/get` at 0.20 s | ran: wire `shared`; `probes/logs/wire-shared.jsonl` |
| row 7b (`notifications/cancelled`, both phases) | no response on the cancelled id in either phase; the cell's `finally` ran both times | ran: wire `client_cancel`; `probes/logs/wire-client_cancel.jsonl` |
| breaks 2 and 3, the bridge's side | kernel killed with a task in flight: `tasks/get` answers status `failed` with the error; under `MCP2_GONE=rpc_error` it answers `-32603`. A new kernel at the same socket answers the old id `-32602 Task not found`. What the client does with each (polls without end on the two errors, wakes on `failed`) was live in session 1 and not rerun | ran: wire `dead` (new row); `probes/logs/wire-dead.jsonl` |

Machine survey for the decisions below (ran: `rg --hidden --no-ignore`, `~/.claude.json`):

| question | found |
| --- | --- |
| how repld is registered | 52 entries in `~/.claude.json`: 46 `repld bridge`, 5 `repld --project-git bridge`, 1 bare `repld`; every `env` is empty. No entry passes `--ephemeral`, `--socket` or a variable. No hook names `repld rebind` |
| does supervise2 call into repld | no. `~/Projects/private/supervise2/bin` has no reference; its run log uses repld only as a registered MCP server (`S2:../RUNLOG.md:/registers it with/`), and names `repld rebind` once as something a clear needs today (`S2:../RUNLOG.md:/with repld registered, a clear needs/`), which row 1 removes |
| who calls the session functions, outside repld and supervise | `repld.current_session_id()`: `devstack/.../gists/devserver.py`, `lend/src/lend/lendexec.py`, `bulletins-chat/gists/lendrig.py`. `channel.set_meta_augmenter`: `mymechanic-prod/repld_init.py` (for the supervise roster). `list_claude_connections`, `park_pushes`, `unpark_pushes`, `rebind_claude_session`, `SUPERVISE_SEAT_PID`: supervise and its pin only |
| `REPLD_CODE_MODE` | set by the owner's `claude` wrapper when a terminal session asks for Code Mode (`~/dotfiles/tests/claude.sh:/code mode:/`), and by supervise's `jobs/dispatch.py`. Whether any session ran with it: still not visible |

### 1. Cut list

"row N" is a Session 1 row, "break N" a Session 1 break, "change N" a plan change, "inv N" a
Tagged invariants row, "cand N" a Deletion candidates row.

**`src/repld/bridge.py`** (1,338 lines; replaced by the new front, estimated at 650; net about 690)

| what | lines | verdict |
| --- | --- | --- |
| cached `initialize`, `_client_init`, `_client_initialized`, `_replay_handshake`, `_stamp_identity`, `BRIDGE_INIT_ID`, the `initialize`/`initialized`/`ping` arms of `_try_bridge_intercept` | about 150 | deleted: `changelog.mdx:/Make MCP stateless/`, cand 2, the client's first message above |
| one attached socket and its bookkeeping: `_forward_to_kernel`, `_read_kernel`, `_track_kernel_message`, `_inflight`, `_generation`, `_drain_inflight`, `_to_kernel`, `_to_client_raw` | about 190 | deleted: break 8. Each request thread owns its connection, so there is no shared set of in-flight ids and no generation to compare |
| the relay of `notifications/claude/channel`, the `kernel_started` push in `_attach` | about 25 | deleted: change 1 (channels cannot ride 2026-07-28) |
| `_on_kernel_gone`, `_push_crash`, `_resolve_crash_reason`, `_reason_from_journal`, `_goodbye_gen` | 165 | deleted: cand 5. A death mid-task is a `failed` task (break 2); 3 `kernel_crashed` pushes in three months |
| `_start_reattach_watch`, `_reattach_watch`, `_REATTACH_*` | 30 | deleted: its reason was rebind, park and targeted pushes finding no connection in the gap, all of which go. The registration is made again on the next request that reaches a kernel (open question 7) |
| `_relay_client_response`, `_kernel_requests`, the `notifications/cancelled` loop for open modals | 45 | deleted: change 7 |
| `_seat_pid`, `PRE_EXEC_PPID` use, the seat and Code Mode arms of `_stamp_identity` | 30 | deleted: supervise1 only (survey). Code Mode: see open question 1 |
| `_cancel_client_request`, `_send_bare` | 37 | kept, because row 7b: the cancel still goes on a connection of its own |
| lazy spawn (`_ensure_kernel`, `_reconnect`, `_connect_excluding`, `_spawn_kernel`), `restart_kernel`, bridge tools, `_static_docs`, `_load_cache`, `--ephemeral`, `run_bridge` | about 330 | kept, because inv 73 (a session that calls no repld tool starts no kernel, `S2:../RUNLOG.md:/calls no repld tool starts none/`), inv 67, and `-31001` for a request whose kernel died under it (inv 72, `b7987aa6`) |

**`src/repld/ipc.py`** (1,436 lines; net about 780)

| what | lines | verdict |
| --- | --- | --- |
| process-tree ancestry and seat pid: `_resolve_ancestry`, `_walk_ancestry`, `_bridge_chain`, `_caller_chain`, `_is_claude_process`, `_find_claudes`, `_read_parent_proc`, `_read_ps_table`, `_resolve_claude_pid`, `_read_tree`, `Session.claude_pid`, `claude_pid_claimed`, `list_claude_connections` | about 125 | deleted: change 5, rows 1 and 2 (no rebind was needed); the only callers are supervise1 (survey). `_resolve_peer_pid` is kept: `requests/cancel` and the home lookup (section 4) match on it |
| rebind: `Server.rebind_claude_session` (138), `_PendingRebind`, `_pending_rebinds`, `_pop_pending_rebind_locked`, `_note_claude_id_locked`, `_find_replaced_id_locked`, `_claude_ids`, `Rebind`, the module wrapper, the `BRIDGE_REBIND_METHOD` post | about 215 | deleted: rows 1, 2a, 2b, 4c |
| successors and outbox: `_succeed`, `_succeed_locked`, `_successors`, `_owe`, `_drain_outbox`, `expire_outbox`, `_outbox`, `_Owed`, `deliver`, `_settle_parked`, `Post.HELD`/`QUEUED` | about 160 | deleted: change 5. A task result is polled by the client that called; the one loss left (break 5) the outbox lost too |
| park: `Session.park`, `unpark`, `take_parked`, `_unpark_and_flush`, `_park_timer`, `parked_queue`, `park_pushes`, `unpark_pushes`, `_run_callbacks` | about 120 | deleted: row 1 (a result reached the new conversation across `/clear` with nothing parked); supervise1 only |
| handshake state: `Session.initialized`, `pending`, `set_initialized`, the pre-`initialized` queue in `post_channel` | about 30 | deleted: no `initialize` |
| `can_elicit`, `can_elicit_url`, `_is_elicitable`, `Session.elicit_form`, `elicit_url` | 25 | deleted: change 7 |
| `Session.say_goodbye`, its call in `Server.stop` | 12 | deleted: the goodbye frame existed to tell a crash from a stop for the crash push |
| the taken-id restore (`claude_bind_seq`, the restore in `_on_reader_done`), the id collision warning | about 45 | deleted: it restored a holder for owed and parked pushes (`862b01cc`), which no longer exist. Latest registration of an id wins |
| `register_claude_session` | 74 to about 20 | kept in reduced form, because rows 4, 4b, 4c, 5: id, project dir, kind and inbox socket of one connection, reached by `sessions/register` (section 4) |
| `Session._emit`, `_write_inbox`, `_check_inbox_socket`, `broadcast_channel`, `post_to`, `find_claude_session`, `list_claude_sessions`, `find_request_task`, `on_session_closed`, `Server._read_loop`, `connect_to_kernel` | about 200 | kept: rows 4, 4b, 5 (the inbox frame), row 7b, inv 29 (a `taken` tab is released when its session's connection closes) |

**`src/repld/channel.py`** (190; net about 35): `push_owed` deleted (change 5); `push_channel`
loses `on_parked_flush` and the parked `None`. `push_channel`, `push_kind`,
`set_meta_augmenter` kept: ambient pushes are session 3's, and 6,408 of the pushes seen are
not `task_done`.

**`src/repld/tasks.py`** (561; net about 70): `mark_nudged`, `record_push`,
`mark_push_delivered`, `nudge_cutoff`, `push_delivered`, `push_state` (task fields,
`snapshot()` and `get_task`'s `outputSchema`) deleted: inv 55, cand 3, row 0 (the task
result replaces the nudge and its push). `claim_done_push` becomes a read of one flag that
only `defer()` sets (see `kernel.py`).

**`src/repld/kernel.py`** (1,896; net about 60): `_maybe_push_done` loses its exec-cell half
and the owed route and keeps the `defer()` completion push (kept, because a deferred task
has no request for a client to poll, and `defer(` is in 15,376 cells); `_Context.mark_nudged`
deleted; the durable half of `_write_cache` and `_strip_project_gists` deleted (cand 13,
open question 4); the `ipc.expire_outbox()` call in the watchdog deleted; the parked
wording in `_notify` deleted. `_notify`, `_claude_sessions`, `_current_session_id` kept
(survey: three projects call `current_session_id`).

**`src/repld/protocol.py`** (1,750; net about 125): `Dispatcher._initialize`, the
`notifications/initialized` arm, `_negotiate_version` use deleted (cand 2; `ping` kept:
`peer.py:/"method": "ping"/` probes a forward with it). The nudge sentence and the
`mark_nudged` dance in `_exec` deleted (cand 3). `_gates_elicit_result`, `_gates_answer`
deleted (change 7). `_sessions_rebind` deleted. `protocolVersion` in
`build_discovery_cache` deleted. `_exec`, `_get_task`, `_cancel`, `_gist_tool`, the browser
tools, `_tasks_wait`, `_tasks_cancel`, `_requests_cancel`, the peer methods kept.

**`src/repld/core_schemas.py`** (519; net about 50): `PROTOCOL_VERSION`,
`SUPPORTED_VERSIONS`, `negotiate_version` deleted for one `VERSION = "2026-07-28"` (inv 68,
85). `CAPABILITIES` rewritten (no `experimental.claude/channel`; the tasks extension; it
rides `server/discover`, so inv 86's two authors become one builder). `BRIDGE_SEAT_PID_KEY`,
`BRIDGE_REBIND_METHOD`, `BRIDGE_GOODBYE_METHOD` deleted. `exec`'s description loses the
nudge sentence. `BRIDGE_SESSION_ID_KEY`, `_PROJECT_DIR_KEY`, `_SESSION_KIND_KEY`,
`_INBOX_SOCKET_KEY` kept as the fields of `sessions/register`.

**`src/repld/gates.py`** (857; net about 160): `_send_elicitation`, `_cancel_elicitation`,
`_elicit_pending`, the `elicit_id` lines of `_gate` deleted (change 7, row 9). The secret
route deleted: `_secret_url`, `_secret_hint`, `_can_elicit_url`, `secret_prompt`,
`submit_secret`, `_find_secret`, `_Gate.secret`/`nonce`/`url_session`, the
`elicitation/complete` write; `ask(secret=True)` and a secret `form()` field raise at once
(change 7, the owner's decision; break 7). `parse_elicit_result` and `handle_elicit_result`
kept in reduced form as the typed answer behind `gates/resolve` (section 4), because the
dialog sends typed content and the kernel's schema is what refuses a bad one (wire `form`).
`_decline_gate` kept (row 9b).

**Whole files and small ones**

| what | lines | verdict |
| --- | --- | --- |
| `src/repld/rebind_cmd.py`, its `cli._SUBCOMMANDS` entry, `help.py:/repld rebind /` | 113 + 6 | deleted: rows 1, 2b; no hook on this machine runs it (survey) |
| `bind.PRE_EXEC_PPID` and its export | 4 | deleted with the seat pid |
| `dashboard.py:/async def _handle_secret/`, its route, the body cap constant; the three secret pages in `dashboard_html.py`; `display.py:/if ev.secret/`; `events.HumanPromptOpen.secret`; the secret arm of `gates.answer_gate` | about 90 | deleted: change 7 |
| `exec_cmd.py`: the handshake in `_connect`, `_handle_notification` | about 25 | deleted: the kernel has no `initialize`; a CLI connection registers nothing, so no push reaches it. That is a loss: `repld exec` prints pushes to stderr today (`src/repld/exec_cmd.py:/def _handle_notification/`), and afterwards only `repld log` shows them; the changelog says so |
| `paths.state_cache_for`, `GLOBAL_CACHE`, `STATE_PROJECTS_DIR` | 16 | deleted: cand 13, inv 60 (the reboot survival is imagined). `paths.cache_for` kept (open question 4) |
| Code Mode: `Session.code_mode`, `BRIDGE_CODE_MODE_KEY`, `strip_output_schemas`, `_format_spill_pointer`, `runtime.print_display(as_json=)`, the code-mode arms of `_exec`, `_cancel`, `_tools_list`, `_tools_call`, `_gist_tool` | about 150 | unmeasured; not decided here (open question 1) |

**`src/repld/help.py`** (3,037): only what would now be false to the model, about 60 lines:
the execution-model paragraph of the instructions and of `static_instructions()` (the nudge
sentence, "pushes a channel message"), `exec`'s tool text, `help.py:/repld rebind /`,
`help.py:/must re-key it/`, the `ask(secret=True)` lines, the Code Mode lines if it goes.
The rest of `help.py` is session 7.

**Tests** (22,109 lines)

| file | deleted | rewritten |
| --- | --- | --- |
| `phases/headless.py` (15) | 1,126: `_bridge_reattaches_to_successor`, `_clean_stop_is_not_a_crash`, `_version_mismatch_cache_discarded`, `_cache_survives_reboot`, `_new_project_uses_global_cache`, `_targeted_push`, `_session_rebind` (284), `_rebind_prefers_nearest_bridge`, `_rebind_stops_at_own_claude`, `_repeat_rebind_arms_nothing`, `_seat_pid_claim`, `_rebind_carries_owed_pushes`, `_rebind_cmd`, `_deferred_rebind`, `_elicitation_cancelled_on_kernel_death`, `_id_collision_warning`, `_push_state_broadcast` | about 450: `_autospawn_and_heal` (no replay; a dead kernel under a task is `failed`), `_lazy_discovery_from_cache` (`server/discover`), `_cold_jsonrpc_edges`, `_inflight_never_stranded`, `_bg_inbox_fallback` (becomes the harness's inbox, for every kind), `_bridge_served_tools`, `phase_15_ephemeral_bridge` |
| `phases/pure.py` (2) | 515: `_elicitation_pure`, `_seat_pid_claims`, `_ps_ancestry`, `_one_snapshot_per_call`, both `_taken_id_*`, `_crash_reason` | `_envelopes` (the `resultType` builders) |
| `phases/channels.py` (4) | 236: `phase_4g_owed_push`, `phase_4b_pregate` | about 250: `phase_4` (an exec past its wait is a task; `notify()` arrives on the inbox), `phase_4c_claude_sessions` (less `list_claude_connections`); `4d`, `4e`, `4f` pass through the harness |
| `phases/core.py` (3) | about 80 of `phase_3` (negotiation, `initialize`, capability assertions) | about 150: `server/discover`, a version the front refuses, inline and task `exec`, `_phase_3_client_cancel` |
| `phases/gates.py` (17) | 109: `_secret_page`, `_secret_gate`, `_elicit_request` | about 165: `_elicitation` becomes `input_required` on `tasks/get` and on a plain call, with accept, decline, a form and a refused value; one case for the secret refusal |
| `phases/live_claude.py` (20) | 0 | about 80: `_print_mode_leaves_gate_open` asserts the text that names the gate and the command; `_bg_worker_gets_inbox_push`; two new cases (section 5) |
| `phases/defer.py`, `every.py`, `gist_tools.py`, `browser.py`, `peers.py` | 0 | about 60: the waits on a pushed `kind=` read the inbox through the harness; `_list_changed` and `_cache_lists` use `subscriptions/listen` |
| `harness.py` | the `initialize` body of `handshake` (the method stays, as `server/discover`: 13 phase files call it) | about 80 added: 2026-07-28 framing in `Bridge.call`, a task wait, an inbox listener behind `wait_notification` |
| Code Mode: `phase_3_code_mode`, `_display_as_json`, `_strip_output_schemas`, the code-mode lines of phase 9 | about 150 | open question 1 |

**Docs**

| file | what goes or is rewritten |
| --- | --- |
| `CLAUDE.md` | deleted: `C:/can change under it/` (the rebind paragraph, the longest in the file), `C:/never rides the connection that asked/`, `C:/REPLD_CODE_MODE=1/` if it goes, the `repld rebind` bullet (`C:/a SessionStart hook command/`). Rewritten: `C:/Request flow:/`, `C:/composed at MCP init/`, `C:/so pushers import it at module level/`, the `repld bridge` bullet, `C:/decided once/`, `C:/both sides of the socket answer/`, `C:/not a byte-pipe/`, `C:/Kernel spawn is lazy/`, `C:/exactly five callers/`, `C:/go to its inbox socket/`, `C:/Targeted push for call-scoped output/`, the cache sentence of `C:/Runtime state lives under XDG/`, "Fifteen CLI subcommands". About 16 of the file's rule paragraphs |
| `CLAUDE-ARCHIVE.md` | the entries under the same headings (about 40 of 150 lines), `A:/Version negotiation is echo-what-they-asked-for/`, `A:/can't paper over a drift either/`, the phase 2 to 4, 15, 17 and 20 rationale lines |
| `tests/phases/CLAUDE.md` | the lines of phases 2, 3, 4, 7, 9, 15, 17, 20 (8 of 26) |
| `README.md`, `site/`, `docs/ARCHITECTURE.md` | 9 lines of `README.md` name channels; the release needs them true. The site is session 7 |
| `CHANGELOG.md` | one breaking entry: needs an MCP 2026-07-28 client (measured: Claude Code 2.1.332), `repld rebind` gone, secret gates refuse, the session functions that went, `repld exec` no longer prints pushes |

Totals, rough: source about 2,300 lines net (2,450 with Code Mode) of 37,122; tests about
2,070 deleted and 1,250 rewritten (2,220 deleted with Code Mode) of 22,109; docs about 16
paragraphs of `CLAUDE.md`, 40 lines of the archive, 8 of the phase list, 60 of `help.py`.

### 2. Kept list

| what | why |
| --- | --- |
| `exec`, the kernel's `{task_id, done}` answer, spill preview and path | 82% of calls; the front maps it (rows 0, 6, 7) |
| `get_task` | break 5 (a relaunch as a new conversation gets no result), `defer()` results, 5,307 calls |
| `cancel`, `tasks/cancel`, `requests/cancel` | rows 7b and 8; a deferred task is not a client task |
| gist `_tool_*` tools, `browser_*` tools | unchanged, always inline. A slow one holds its call as it does today |
| `repld_restart` and the bridge-tool split | inv 67, 50 calls |
| resources (`repld://docs/*`, `help/*`, `gists/*`, browser) | unchanged in content; their 2026-07-28 shape was not run (section 5) |
| gates: `ask`, `confirm`, `choose`, `form`, `GateDeclined`; the pane, pill, `repld gate`, dashboard routes; `HumanPromptClosed` | inv 76, 78; the `g` row (a postponed dialog answered from the CLI) |
| the `awaiting_human` push, for a gate no client task shows | a gate from `defer()` has no task a client polls (break 6) |
| `defer()` and its completion push, `@every`, `notify()`, browser pushes, `set_meta_augmenter` | ambient; over the inbox frame (rows 4, 4b, 5). Their routing is session 3 |
| `claude_sessions()`, `current_session_id()`, `notify(session=)` | three non-supervise callers (survey); the id is the one the bridge registered, which row 4c showed still delivers after `/clear` |
| peers, `ping` | no channel or handshake dependence (inv 40) |
| lazy spawn, the bootstrap wait, binding, `--project`, `--ephemeral` and the owner watch, `kernel.cache` beside the socket | inv 73, 75, 64, 51; `--ephemeral` has no registration on this machine (cand 15), and is not this cut's |
| `repld exec`, `tasks`, `gate`, `status`, `log`, `start`, `stop`, `restart`, the dashboard, the TUI | they speak the kernel's own JSON-RPC, which keeps its methods |

### 3. The new bridge

One file, stdlib, about 650 lines, grown from `probes/mcp2_bridge.py`. One thread per client
message; each kernel request on a connection of its own (break 8); one more connection held
for the registration (section 4). It keeps no task state: a task id is the kernel's, so any
bridge process answers for it (rows 2a, 2b, 2e).

| method | answer |
| --- | --- |
| any request whose `_meta` version is not `2026-07-28`, and `initialize` | `-32022` with `supported: ["2026-07-28"]`. No fallback to the old handshake |
| `server/discover` | `supportedVersions`, capabilities (`tools.listChanged`, `resources.listChanged`, the tasks extension), `instructions`. From the kernel when one is up (`discover`, section 4), else `kernel.cache`, else `static_instructions()`. Never spawns. Held to `INSTRUCTIONS_BUDGET` as today (row 10: the cut is at 4,096 and marked) |
| `tools/list`, `resources/list`, `resources/templates/list` | from the kernel when one is up, else the cache, else the static lists. Never spawns |
| `resources/read` | static docs from `help.py` with no kernel; a gist or browser URI and the two browser docs spawn one, as today |
| `subscriptions/listen` | acknowledged with the granted kinds and kept open. `tools/list_changed` and `resources/list_changed` from the registration connection are relayed onto it (change 9, row 11); the bridge sends both itself when a kernel first comes up after a cold discovery |
| `tools/call` of a bridge tool | answered here, before any kernel is looked for |
| `tools/call` of a gist or browser tool, `get_task`, `cancel` | connect, or spawn and wait 5 s; write once; the kernel's result as `resultType: complete`. A kernel that cannot be started, or that dies under the request, is `-31001` (inv 72) |
| `tools/call exec` | see below |
| `tasks/get` | `get_task`, then `gates/list` while the task runs. `working`; `input_required` with one `inputRequests` entry per open gate whose `task_id` is this task; `completed` with the result (`isError` for a raise); `cancelled` for a `CancelledError`; `failed` when no kernel answers or the kernel does not hold the id, with a message that says which (breaks 2, 3). Never a JSON-RPC error, never a spawn |
| `tasks/update` | per gate: accept sends the typed content to `gates/resolve`, decline sends `action: decline`, cancel sends nothing. A value the kernel refuses comes back as the `tasks/update` error (wire `form`) |
| `tasks/cancel` | `tasks/cancel` to the kernel; any id, a finished task and a repeat are acknowledged (row 8: it arrives twice) |
| `notifications/cancelled` | for a `tools/call` still inside the kernel's wait: `requests/cancel` with that request's id on a new connection, then the request's own socket is closed; no response is written. For a call held open past the wait: `tasks/cancel`. For the `subscriptions/listen` id: the subscription ends (row 7b) |
| anything else | `-32601`; a line that does not parse `-32700`, a non-object `-32600`, and neither spawns |

**`exec`, inline or a task, decided per call.** The bridge reads the capabilities each
request declares; nothing is remembered from an earlier one.

1. The cell goes to the kernel with `timeout` (the caller's, else 2.0 s: row 6). Done inside
   it: the result inline, in the cell's own time.
2. Not done, and the request declares the tasks extension: `resultType: task` with the
   kernel's id, `pollIntervalMs` 1000, `ttlMs` from `tasks._EVICT_AGE`, `createdAt` from
   the kernel's `started_at` (the spike kept a clock per bridge process, which a second
   process does not have).
3. Not done, and no tasks capability (`claude -p`, row 7): the call is held open on
   `tasks/wait` and answered inline when the cell ends, whatever `timeout` said.
4. A gate opened by the cell ends the kernel's wait at once (section 4). With tasks: step
   2, and the next `tasks/get` is `input_required`. Without tasks and with `elicitation`
   declared: `resultType: input_required` on the call itself, `requestState` naming the task
   and gate. On the retry: accept resolves and the hold goes on; decline declines; cancel
   (what `-p` sends 13 ms later, row 9e) ends the call with text that names the gate, its
   prompt, `repld gate answer <gate_id> <value>` and `get_task("<id>")`. A client that
   declares neither gets that text directly.

**The model's own `timeout` (decided, flagged as open question 2).** It stays in the schema
and means one thing: how long a tasks-capable call waits for an inline result before it
becomes a task. It does not bound a call from a client with no tasks, and it no longer
delays a gate. The row's three "held inline where a task was expected" cells are the caller
getting what it asked for; the break in that row is the 120 s before a gate was looked
for, and step 4 is its fix. The tool text tells the model to leave `timeout` out unless it
wants to wait longer inline.

**Nothing per launch in the server entry (break 4).** The entry stays `repld bridge`, with
the flags it has today and an empty `env` (survey: all 52 are so). Identity is read from
the process environment Claude Code sets (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`,
`CLAUDE_JOB_DIR`, `CLAUDE_CODE_MESSAGING_SOCKET`), never from an argument or an entry
variable, and the docs must not suggest one. `serverInfo.name` stays `repld`.

**Exit (break 1).** Nothing in the bridge assumes that its end stops a cell. Stdin EOF
closes the registration connection and exits; cells keep running.

### 4. Kernel-side changes

About 90 lines. Until the flip (commit 6 of section 5) the old bridge, `repld exec` and the
unported phases must see no change, so every new behaviour hangs on one of two things the
old callers never send: the connection sent `sessions/register` ("registered"), or the
request carries a flag. Found by review of this design against the suite: an unconditional
version of items 3, 5, 6 and 7 turns phase 17 red and moves an interactive session's pushes
under the old bridge, which already stamps the inbox socket for every session kind
(`src/repld/bridge.py:/CLAUDE_CODE_MESSAGING_SOCKET/`). source, not run.

1. **`sessions/register`** (bare JSON-RPC, like the gate methods): `{session_id,
   project_dir, kind, inbox_socket}` on a connection the bridge then keeps. It is
   `register_claude_session` without the ancestry, the pending rebind and the outbox. It
   marks the connection registered and calls `set_initialized()`: a registered connection
   never sends `notifications/initialized`, and `Session.post_channel` would otherwise hold
   every push, `tools/list_changed` included, in `pending`
   (`src/repld/ipc.py:/if not self.initialized/`). The kernel keeps `peer_pid -> Session`
   for registered connections ("home"). The bridge registers before it forwards its first
   request to a kernel, and again whenever a request finds the held connection dead.
2. **A request's session is its bridge's home.** `Dispatcher._tools_call` swaps the
   per-request connection for the registered connection with the same `peer_pid`
   (`SO_PEERCRED`, the match `requests/cancel` already uses:
   `src/repld/protocol.py:/Scoping by/`), and falls back to the connection itself (the old
   bridge, a CLI, a test). Without this every per-request connection would be the task's
   `origin` and close at once: `current_session_id()` would read `None`, a `defer()` push
   would have nowhere to go, and a `taken` tab would be released when its `browser_take`
   call returned (`C:/does not outlive its session/`). source, not run.
3. **Pushes, for a registered connection only.** `Session._emit` sends a channel message
   to the inbox socket for every kind (today `bg` only:
   `src/repld/ipc.py:/claude_session_kind == "bg"/`) and drops it when no inbox was
   registered; rows 4 and 4b ran exactly this frame for an interactive and a `--bg`
   session. An unregistered connection keeps today's rule, channel on the wire and inbox
   for `bg`. `tools/list_changed` and `resources/list_changed` go on the wire of the
   registered connection (change 9).
4. **Per-request connections are not sessions.** A connection whose `peer_pid` has a home
   and which is not that home is skipped by `broadcast_channel` (a notification must not
   land on a per-request socket ahead of its reply) and by `list_claude_sessions` (or
   `claude_sessions()` gains a `(None, None, None)` row per call in flight). Old-bridge
   and `repld exec` connections have no home and stay in both. At the flip both walk
   registered connections only.
5. **`discover`** (bare): `{version, instructions}`, what `initialize` returned less the
   negotiation. `initialize` and `notifications/initialized` stay until commit 6.
6. **The asking task on a gate (break 6; cheap, 4 lines).** `_Gate.task_id =
   tasks.current_task_id()` when it opens, returned by `open_gates()`. The front shows a
   gate only on its own task. The `awaiting_human` push is unchanged for every gate except
   one: a gate opened in an exec cell whose origin is registered sends none, since the
   dialog or the call's text carries it. A gate from `defer()` carries the deferred task's
   id, shows on no client task and keeps the push with the answer command, registered or
   not.
7. **No nudge for a registered origin.** `_exec` takes no `mark_nudged` promise when the
   call's origin is registered: the client polls the task, and a `task_done` frame on the
   inbox as well would wake the session twice. The old bridge keeps the nudge until
   commit 6 deletes it. `defer()` completion pushes either way.
8. **A gate ends the inline wait, when the caller asks (the `timeout` row).** One event per
   task, set by `finalize` and by `_gate` on open. `exec` with `until_gate: true` in its
   arguments' `_meta`, and `tasks/wait` with the same flag, return on it with `{task_id,
   done: false, gate_id}`. Only the front sends the flag; without it a gate answered inside
   `timeout` still comes back inline, as today. About 15 lines.
9. **`gates/resolve` takes a typed answer and a decline (row 9b).** `{gate_id, action,
   content}` beside today's `{gate_id, value}`: accept validates `content` against the
   gate's schema (`validate_form`, unwrapping the one field of a wrapper gate), decline is
   `False` for `confirm` and `GateDeclined` for the rest, as elicitation did. This is
   `handle_elicit_result` keyed by gate id. Session 6 loses items 6 and 9.
10. **`snapshot()` gains `started_at`**, in `get_task`'s `outputSchema` too
    (`C:/add a field to both/`).

What `ipc.Session` loses at commit 6: `initialized`, `pending`, `parked`, `parked_queue`,
`_park_timer`, `claude_bind_seq`, `claude_pid`, `claude_pid_claimed`, `elicit_form`,
`elicit_url` (`code_mode` with open question 1). What it keeps: `peer_pid`,
`claude_session_id`, `claude_project_dir`, `claude_session_kind`, `inbox_socket`, `peer`,
`request_tasks`, `cancelled_requests`; the registered mark goes too, once every session is
one.

### 5. Order for 2b

Each commit leaves `uv run python tests/smoketest.py --phase 19` green. The old bridge
stays the default, and unchanged in behaviour, until commit 6; the new one is reachable
only by the harness until then.

| # | commit | suite |
| --- | --- | --- |
| 1 | kernel, additive: section 4 items 1 to 10, each behind the registered mark or the caller's flag | new small cases in phases 3 and 17 against the kernel socket (a registered connection gets `list_changed` at once, a per-request connection is in no listing, decline over `gates/resolve`); every existing case runs unchanged |
| 2 | `ask(secret=True)` and a secret field raise; the secret page and its tests go (change 7) | phase 17: `_secret_gate` replaced by the refusal |
| 3 | the new front as `src/repld/front.py`, started by the harness only. `harness.Bridge` gains 2026-07-28 framing, a task wait and an inbox listener behind `wait_notification`, chosen by a `smoketest.py` switch. `Bridge.handshake()` stays as the harness's name for opening a session: `initialize` under the old framing, `server/discover` under the new | phase 3's and phase 15's cold cases run under both |
| 4 | phases ported under the switch, one commit each: 3, 4, 7 and 10, 9, 15, 17, then the four routing cases of 6 and the two waits of 19 | each phase green under both; its old-only cases are marked for commit 6 |
| 5 | the phases that only open a session and call tools, under the switch: 5, 8, 11, 12, 13, 18 (each calls `handshake()`: `lockfile.py`, `resources.py`, `shutdown.py`, `links.py`, `sessions.py`, `tasks_cli.py`), and the rest of 6. Whatever a case reads from the `initialize` answer is fixed here | `--phase 19` green under both, every phase |
| 6 | the flip: `front.py` becomes `bridge.py`; the switch and the harness's old framing go; the cases section 1 lists as deleted go. In the same commit, because the old bridge was their last caller and leaving them would be a second behaviour: the nudge (`mark_nudged`, the sentence, `push_state`), the unregistered arms of section 4 items 3, 4, 6 and 7, and the `until_gate` flag becomes the only behaviour | all phases on the new front |
| 7 | kernel deletions, one commit each, nothing calls them now: elicitation; rebind, ancestry, seat pid, `rebind_cmd.py`; park, outbox, successors, `push_owed`; `initialize`, negotiation, `exec_cmd`'s handshake; goodbye; the durable cache copies | phase 2's pure cases go with their subject |
| 8 | Code Mode, by the answer to open question 1 | `phase_3_code_mode` and two pure cases, either way |
| 9 | docs: `CLAUDE.md`, `CLAUDE-ARCHIVE.md`, `tests/phases/CLAUDE.md`, the `help.py` lines of section 1, `CHANGELOG.md` | phase 8's doc guards, phase 2's budget |
| 10 | phase 20 rewritten, then the live check below | phase 20 |

Phases rewritten: 3, 4, 15, 17, 20. Touched by a few cases: 2 (515 lines deleted), 6 (four
routing cases), 7, 9, 10, 19. Run through the harness with no or little change to the
file, but ported and checked under the switch in commit 5, since each opens a session
with `handshake()`: 5, 8, 11, 12, 13, 18. Deleted: none whole. Untouched, no `Bridge` in
them: 14, 16.

**The live check 2b runs**, with `probes/mcp2_live.sh` pointed at the real `repld bridge`
from the worktree (scratch kernel, isolated tmux, own config and state homes, Haiku, the
session 1 safety rules). One interactive session:

1. Started with no kernel: the model quotes the first and last words of the instructions,
   and no kernel has spawned.
2. `1+1` inline; an 8 s cell becomes a task and wakes the idle session with its value,
   once (no second `task_done` frame).
3. `confirm()`: dialog, Accept, result. Again with Decline: the cell raises `GateDeclined`
   and the model reads it.
4. `defer()` of a 5 s coroutine, then idle: the inbox frame with `kind="task_done"` starts
   a turn.
5. The kernel killed with a task in flight: the session is woken with `failed`; the next
   `exec` starts a fresh kernel and answers.
6. A cell with `timeout` 60, then Escape: the cell's `finally` ran.
7. A gist tool added: callable on the next turn. A read of `repld://docs/guide`, a
   `get_task` call and a `browser_*` or image-returning tool, since resources,
   `structuredContent` and image content were never run at 2026-07-28 (the spike forwarded
   `content` and `isError` only).

Then one `claude -p`: a 4 s cell inline; `confirm()` returns the text that names the gate
and the command, and `repld gate answer` resolves it. Phase 20 holds the `-p` and `--bg`
halves as cases.

### 6. Open questions for the owner

1. **Code Mode.** Unmeasured twice: whether any session runs with it, and what a Code Mode
   script receives at 2026-07-28 (a `resultType: task`, `structuredContent`). The dotfiles
   wrapper can set it, so it is not supervise1's alone. Recommendation: if you no longer
   use Code Mode, delete it in commit 8 (about 150 source and 150 test lines) and drop the
   wrapper's variable. Otherwise 2b runs one row first, a session with
   `CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1` against the new front, and keeps the shaping only
   if a script still gets the JSON text parsed; the flag then rides `sessions/register`.
2. **`timeout` on `exec`.** Decided as in section 3: kept, it only chooses when a
   tasks-capable call becomes a task. The alternative is to drop it from the schema (a
   fixed 2 s), which costs a wake turn on every cell the caller knew would take 10 s; 17%
   of real calls pass one. Not run: a `-p` cell that never ends now holds its call open,
   where today it returned the nudge after `timeout`. Recommendation: keep as decided, and
   bound the `-p` hold only when a run shows it.
3. **Decline and the asking task move from session 6 into 2b** (section 4, items 6 and 9).
   Without them a declined dialog leaves the cell blocked, where today it raises
   `GateDeclined`. Recommendation: yes; about 20 lines, both from runs (row 9b, break 6).
4. **The discovery cache.** Instructions are read once per session (the `f` row), so a
   bridge with no kernel has to have them at `server/discover` or the session never sees
   the gist list. Recommendation: keep the one `kernel.cache` beside the socket, delete the
   two durable copies under `$XDG_STATE_HOME` (inv 60: imagined). The smaller choice,
   static instructions whenever no kernel is up, loses the gist signatures for every
   session opened before a kernel.
5. **The inbox route for every session kind lands in 2b, not session 3** (section 4, item
   3). `defer()` completion has no client task to ride and is in 22% of cells; the day the
   channel relay goes it needs the inbox or it is silent. Recommendation: yes, as the one
   condition in `Session._emit`; session 3 keeps the keying on the socket path, what a
   broadcast means, and re-earning the tests.
6. **supervise1 breaks at this release.** `list_claude_connections`, `park_pushes`,
   `unpark_pushes`, `rebind_claude_session`, `claude_pid`, `SUPERVISE_SEAT_PID` and `repld
   rebind` go; `claude_sessions()`, `current_session_id()`, `notify(session=)` and
   `set_meta_augmenter` stay. Recommendation: delete as listed; a supervise1 kernel stays
   on the last 0.24 release (a `supervise-pin` checkout exists).
7. **An idle session after someone else restarts the kernel** has no registration until its
   next call, so pushes from the new kernel miss it in that gap; `_reattach_watch` covered
   30 s of it. And a kernel that dies while no task is polled is now silent until the next
   call (`kernel_crashed`: 3 pushes in three months). Recommendation: accept both; rebuild
   against a run.
8. **Older clients.** A client that opens with `initialize` gets `-32022`. Only 2.1.332 was
   measured. Recommendation: the clean break as decided; the changelog names the version
   measured, not a floor nobody ran.

### Left behind

Nothing running: the scratch kernel (four pids over the run, the last for a second wire run of `form`), its unit, its state
directory, its spill files and the tmux server are gone, and `.live.local/` in this
worktree was removed. Two probe transcripts remain under `~/.claude/projects/` (`f`, `g`).
`~/.config/repld/gist-registry.json` kept its mtime. `~/.local/state/repld/last.cache`
did not: it was rewritten at 15:21:03 local, together with the cache of every kernel the
owner had running, one second after `~/.repld/gists/gnome_desktop.py` and
`gnome_desktop_helper.py` changed on disk (not by this session; the scratch kernel's own
copy, in its scratch state home, was written at 15:22:21). The worktree has a `.venv`
(gitignored) from `uv run`.
