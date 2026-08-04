"""`repld gate` — see and answer the human gates a kernel is blocked on.

`ask()` / `confirm()` / `choose()` park a cell until a human answers. A kernel
started as `repld` in a pane reads that answer off its own stdin, and a pinned
browser tab can answer through the pill — but a kernel spawned *for* you (Claude
Code's bridge, `repld restart`, a systemd unit) has neither: it runs
`--no-display` with stdin on /dev/null. This is the surface for that kernel, and
it is now the common one.

Read-only discovery already worked — `repld log -f` renders the prompt, and
since the gate id travels with it you can see what to type. What was missing was
the verb, so this is deliberately small: two JSON-RPC calls over the same IPC
socket every other command uses. The kernel owns the coercion (`gates/resolve`
consults the gate's own kind), so this file never has to know what a confirm is.
"""

import json
import sys
from pathlib import Path

from . import cli_args, paths
from .exec_cmd import _call, _connect
from .render import BOLD, DIM, RESET, gate_hint

_USAGE = """\
repld gate — human gates this project's kernel is waiting on

  repld gate [--json] [--socket PATH]
  repld gate [--socket PATH] answer <gate_id> <value...>

  A kernel you started with `repld` takes answers in its own pane. One that
  was spawned for you has no pane — answer it here.

  <value> is y/n for a confirm, an option name or its 1-based number for a
  choose, and free text for an ask. Find the id in `repld log -f` or in the
  `awaiting human` channel message.

  Everything after <gate_id> is the answer, verbatim — so an ask takes an
  unquoted multi-word one, and flags for this command go before `answer`.
"""

_LABEL = "repld gate"


def _err(msg: str) -> None:
    print(f"{_LABEL}: {msg}", file=sys.stderr, flush=True)


def _report_error(err: dict) -> int:
    """Render a JSON-RPC error, naming version skew when that's what it is.

    A kernel is meant to run for weeks, so upgrading repld leaves the *old* one
    serving this project until someone restarts it — and an old one has no
    `gates/list`. Reporting the raw `method not found` would send you looking
    for a bug in a command that is simply newer than the kernel it's talking to.
    """
    if err.get("code") == -32601:
        _err(
            "this kernel predates `repld gate` — restart it to pick the command "
            "up (`repld restart`), or answer in its pane if it has one"
        )
        return 1
    _err(err.get("message", "unknown error"))
    return 1


def _fmt(gate: dict) -> str:
    kind = gate.get("kind", "?")
    # Via render.py, not rebuilt here: this is a human reading what to type,
    # and it has to name the same options as the pane and `repld log -f`.
    hint = gate_hint(kind, gate.get("options"))
    hint = f" {hint}" if hint else ""
    waited = gate.get("waiting_s", 0)
    return (
        f"{BOLD}{gate.get('gate_id', '?')}{RESET}  {gate.get('prompt', '')}"
        f"{DIM}{hint}  ({kind}, waiting {waited}s){RESET}"
    )


def _split_answer(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split argv into the part flags may be read from, and a verbatim value.

    `answer`'s value is free text, so no flag parser may reach past the gate
    id — and all three that run here did. `paths.resolve_socket_path` consumes
    `--socket PATH` from *anywhere* in argv, `--json` was filtered the same
    way, and `cli_args.wants_help` scans every argument by design. So
    `repld gate answer g1 deploy --socket now` answered `"deploy now"` and
    silently repointed the connection at a kernel called `now`, and any answer
    containing `--json` or `--help` was mangled or swallowed outright. The one
    rule that makes an unquoted multi-word `ask` answer possible is that
    everything after the id is the answer, so the split has to happen first.

    Returns (head, None) for the listing form. The trailing `[--socket PATH]`
    the usage used to show after `<value>` is gone with this — it was never
    distinguishable from an answer that happens to say the same words.
    """
    try:
        i = argv.index("answer")
    except ValueError:
        return argv, None
    # `index` finds the *first* occurrence, so an answer that is itself the
    # word "answer" (`answer g1 answer this`) still splits at the verb.
    return argv[: i + 2], argv[i + 2 :]


def run_gate(argv: list[str]) -> int:
    head, value_words = _split_answer(argv)

    # Ahead of the `answer` branch, so `repld gate answer --help` is a request
    # for usage rather than a gate id of '--help' with no value. Scoped to
    # `head`, so `repld gate answer g1 --help` answers with "--help".
    if cli_args.wants_help(head):
        print(_USAGE)
        return 0

    lock_path, rest = paths.resolve_lock_path(head)
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]

    if rest and rest[0] == "answer":
        if len(rest) < 2 or not value_words:
            # stdout, matching every other subcommand's usage-on-error. `_err`
            # is for runtime failures, where stderr is right.
            print(f"{_LABEL}: answer needs a gate id and a value\n")
            print(_USAGE)
            return 2
        return _answer(lock_path, rest[1], " ".join(value_words), as_json)

    # Only the listing form reaches here — `answer` returned above, and its
    # trailing words never entered `rest` in the first place.
    bad = cli_args.check_args(_LABEL, rest, _USAGE, positionals=0)
    if bad is not None:
        return bad
    return _list(lock_path, as_json)


def _list(lock_path: Path, as_json: bool) -> int:
    conn = _connect(lock_path, label=_LABEL)
    if conn is None:
        return 1
    sock, rfile, wfile, _lock = conn
    try:
        resp = _call(rfile, wfile, "gates/list")
        if resp is None:
            _err("kernel disconnected")
            return 1
        if "error" in resp:
            return _report_error(resp["error"])
        gates = resp.get("result", {}).get("gates", [])
        if as_json:
            print(json.dumps(gates, indent=2))
            return 0
        if not gates:
            print("no gates awaiting an answer")
            return 0
        for g in gates:
            print(_fmt(g))
        print(f"\n{DIM}answer with: repld gate answer <gate_id> <value>{RESET}")
        return 0
    finally:
        sock.close()


def _answer(lock_path: Path, gate_id: str, value: str, as_json: bool) -> int:
    conn = _connect(lock_path, label=_LABEL)
    if conn is None:
        return 1
    sock, rfile, wfile, _lock = conn
    try:
        resp = _call(
            rfile, wfile, "gates/resolve", {"gate_id": gate_id, "value": value}
        )
        if resp is None:
            _err("kernel disconnected")
            return 1
        if "error" in resp:
            return _report_error(resp["error"])
        result = resp.get("result", {})
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"gate {result.get('gate_id')} answered: {result.get('value')!r}")
        return 0
    finally:
        sock.close()
