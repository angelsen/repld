"""Regenerate src/repld/browser/injected_source.py from the pinned Playwright clone.

Dev-time only — node/npm are needed here and nowhere else; the wheel ships the
generated .py. Run via `make injected`. To bump Playwright: update the clone,
change PLAYWRIGHT_COMMIT, rebuild, and re-run the phase-6 browser tests.
"""

import shutil
import subprocess
import sys
from pathlib import Path

# The build refuses to run against any other checkout: the bundle must be
# reproducible from the recorded pin, or the vendored engine can't be audited.
PLAYWRIGHT_COMMIT = "4126cf5a4a22cc1cfab7d535bfceba6bcf131946"
PLAYWRIGHT_CLONE = Path(
    "~/.local/share/resources/github.com/microsoft/playwright/tree/HEAD"
).expanduser()
# Matches the clone's own devDependency, so our bundle and Playwright's differ
# only by the CSS loader (we use --loader:.css=text; they minify first).
ESBUILD_VERSION = "0.28.1"

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "src" / "repld" / "browser" / "injected_source.py"
ESBUILD_CACHE = Path("~/.cache/repld-build").expanduser() / f"esbuild-{ESBUILD_VERSION}"

# Verbatim from utils/generate_injected.js in the clone. esbuild's stock
# helpers call Object.defineProperty and friends off the page's globals, which
# page JS can redefine (playwright#17029) — these use only syntax.
MODULE_PREFIX = """
var __commonJS = obj => {
  let required = false;
  let result;
  return function __require() {
    if (!required) {
      required = true;
      let fn;
      for (const name in obj) { fn = obj[name]; break; }
      const module = { exports: {} };
      fn(module.exports, module);
      result = module.exports;
    }
    return result;
  }
};
var __export = (target, all) => {for (var name in all) target[name] = all[name];};
var __toESM = mod => ({ ...mod, 'default': mod });
var __toCommonJS = mod => ({ ...mod, __esModule: true });
"""

HEADER_TEMPLATE = '''"""Playwright's InjectedScript engine, bundled — GENERATED, do not hand-edit.

Regenerate with `make injected` (runs scripts/build_injected.py against the
pinned microsoft/playwright clone). A JS bundle in a Python module for the same
reason as dashboard_html.py: it ships in the wheel with no package-data entry.

Vendored from https://github.com/microsoft/playwright
(packages/injected/src/injectedScript.ts and its packages/isomorphic/ deps),
commit {commit}. See THIRD_PARTY_LICENSES.md.

Copyright (c) Microsoft Corporation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

COMMIT = "{commit}"

SOURCE = {source!r}
'''


def _fail(msg: str) -> "None":
    sys.exit(f"build_injected: {msg}")


def _check_pin() -> None:
    if not PLAYWRIGHT_CLONE.is_dir():
        _fail(f"playwright clone not found at {PLAYWRIGHT_CLONE}")
    head = subprocess.run(
        ["git", "-C", str(PLAYWRIGHT_CLONE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != PLAYWRIGHT_COMMIT:
        _fail(
            f"clone HEAD {head[:12]} != pinned {PLAYWRIGHT_COMMIT[:12]}.\n"
            "Either check out the pin in the clone, or (for a deliberate bump) "
            "update PLAYWRIGHT_COMMIT here, rebuild, and re-run phase-6 tests."
        )


def _esbuild_bin() -> Path:
    exe = ESBUILD_CACHE / "node_modules" / ".bin" / "esbuild"
    if exe.exists():
        return exe
    if shutil.which("npm") is None:
        _fail("npm not found — node is required to (re)build the bundle")
    ESBUILD_CACHE.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "npm",
            "install",
            "--prefix",
            str(ESBUILD_CACHE),
            f"esbuild@{ESBUILD_VERSION}",
            "--no-audit",
            "--no-fund",
        ],
        check=True,
    )
    return exe


def _bundle(esbuild: Path) -> str:
    entry = PLAYWRIGHT_CLONE / "packages" / "injected" / "src" / "injectedScript.ts"
    proc = subprocess.run(
        [
            str(esbuild),
            str(entry),
            "--bundle",
            "--format=cjs",
            "--platform=browser",
            "--target=es2019",
            "--loader:.css=text",
        ],
        capture_output=True,
        text=True,
        # esbuild writes cwd-relative path comments into the bundle; anchored
        # at the clone they read `packages/injected/src/...` instead of
        # leaking this machine's directory layout.
        cwd=PLAYWRIGHT_CLONE,
        check=False,
    )
    if proc.returncode != 0:
        _fail(f"esbuild failed:\n{proc.stderr}")
    return proc.stdout


def _replace_header(content: str) -> str:
    # Port of utils/generate_injected.js:replaceEsbuildHeader. The first
    # `__toCommonJS` occurrence is its one-line definition, the last helper in
    # esbuild's preamble; cut through that line and substitute MODULE_PREFIX.
    start = content.find("__toCommonJS")
    if start != -1:
        start = content.find("\n", start)
    if start == -1:
        _fail("did not find esbuild preamble end (__toCommonJS) in bundle")
    return MODULE_PREFIX + content[start:]


def main() -> None:
    _check_pin()
    source = _replace_header(_bundle(_esbuild_bin()))
    if "InjectedScript" not in source:
        _fail("bundle lacks InjectedScript export")
    OUT.write_text(HEADER_TEMPLATE.format(commit=PLAYWRIGHT_COMMIT, source=source))
    print(f"wrote {OUT} ({len(source):,} bytes of JS)")


if __name__ == "__main__":
    main()
