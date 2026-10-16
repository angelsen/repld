#!/usr/bin/env python3
"""Block termtap2 run() commands that contain a $variable.

The client substitutes $NAME from its own environment before the shell sees the
command, even inside quotes or after a backslash, and blanks any name it does not
know (fish locals, loop variables, $status, $pipestatus).
"""

import json
import re
import sys

command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
if re.search(r"\$[A-Za-z_{]", command):
    print(
        "termtap2 run(): a $variable is substituted or blanked before the shell sees it, "
        "and no quoting escapes it. Write the literal value (~ for home) or script it in a file.",
        file=sys.stderr,
    )
    sys.exit(2)
