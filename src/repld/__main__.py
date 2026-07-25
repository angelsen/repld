"""`python -m repld` entry point.

The bridge spawns kernels as `[sys.executable, "-m", "repld", ...]` — the
console script may not be on PATH in the environment a client hands us, but
the interpreter running the bridge always has the package importable.
"""

from .cli import main

main()
