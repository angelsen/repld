"""Argument validation shared by every `repld` subcommand.

`cli.py` routes *between* subcommands; this validates the argv *within* one, so
it sits beside the dispatcher rather than inside it — a command module
importing the thing that dispatches to it would be backwards.

Both halves of the job were already solved, once, in `gist_cmd.py`: reject
what the verb doesn't understand instead of silently doing something else
(`gist new x --global` scaffolded locally while its sibling `fetch` honours
that exact flag), and scan *every* argument for `-h`/`--help` rather than just
the first (`gist rm foo --help` used to fall through and unlink foo). The other
five commands — `stop`, `restart`, `status`, `dashboard`, `gate` — each
open-coded the same two checks, correctly but separately, which is five places
for the next flag to be forgotten in. Same job, one owner.

`log_cmd` deliberately doesn't use this: `-n N` takes a value, so its argv
needs a real parser loop rather than a flag-set membership test, and squeezing
that in here would cost more than the eight lines it saved.
"""


def wants_help(argv: list[str]) -> bool:
    """Whether any argument asks for usage.

    Every argument, not just ``argv[0]``: a `--help` typed after a positional
    is still a request for help, not a value.
    """
    return any(a in ("-h", "--help") for a in argv)


def check_args(
    prog: str,
    argv: list[str],
    usage: str,
    *,
    flags: tuple[str, ...] = (),
    positionals: int | None = 1,
) -> int | None:
    """Reject unknown flags and surplus positionals. Exit code, or None if fine.

    *prog* is the full command label for the error line (``"repld stop"``,
    ``"repld gist new"``). *flags* is every option the caller understands;
    anything else starting with ``-`` is refused. ``positionals=None`` means
    the command takes any number of names.

    Call it *after* `paths.resolve_socket_path`, which consumes the
    ``--socket`` flag every client subcommand accepts — otherwise this refuses
    it as unknown.
    """
    unknown = [a for a in argv if a.startswith("-") and a not in flags]
    if unknown:
        print(f"{prog}: unknown argument {unknown[0]!r}\n")
        print(usage)
        return 2
    names = [a for a in argv if not a.startswith("-")]
    if positionals is not None and len(names) > positionals:
        print(f"{prog}: unexpected argument {names[positionals]!r}\n")
        print(usage)
        return 2
    return None
