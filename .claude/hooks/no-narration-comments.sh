#!/usr/bin/env bash
# PreToolUse hook (Bash tool): blocks `git commit` while staged src/*.py
# additions carry comment-narration markers ("found live", "verified live",
# "discovered live") — the Invariant-comments style's rule that the story
# belongs in the commit message, mechanically enforced for its most common
# signature. exit 2 blocks; everything else passes through.
set -u
input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
case "$cmd" in
*"git commit"*) ;;
*) exit 0 ;;
esac
hits=$(git diff --cached --unified=0 -- 'src/*.py' 2>/dev/null |
  grep -inE '^\+.*(#|""").*\b(found|verified|discovered) live\b' || true)
if [ -n "$hits" ]; then
  {
    echo "Narration markers in staged comments — the invariant stays in the code,"
    echo "the discovery story goes in the commit message (Invariant-comments style):"
    echo "$hits"
  } >&2
  exit 2
fi
exit 0
