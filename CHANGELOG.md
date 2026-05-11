# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Central gist registry (`~/.config/repld/gist-registry.json`) — tracks every gist import across all projects with path, description, project, and last-used timestamp
- Migrated all gists from lazy `from __main__` imports to `import repld`

## [0.0.5] - 2026-05-11

### Added

- Kernel primitives (`notify`, `defer`, `every`, `ask`, `confirm`, `choose`, `browser`) importable via `import repld` — gists no longer need lazy `from __main__` imports
- Type stubs in `__init__.py` for IDE/pyright visibility of kernel primitives
- Gmail gist: `headers=False` for fast snippet-only search
- Gmail gist: OAuth2 with auto-refresh, full CRUD
- Google Messages gist: ADB-first with SMS/MMS/RCS dump to SQLite, web opt-in for writes

### Changed

- Gist docstrings document return shapes

### Fixed

- Browser smoketest skips gracefully when `websockets` extra is not installed
- Trusted Types safe pill injection for Google domains with CSP

## [0.0.4] - 2026-05-03

### Added

- Gist introspection shows `async` prefix on async methods
- NameError hints: suggests `__repld_usage__` when a gist variable name is undefined
- MCP instructions include dependency guidance (uv project vs locked environment)

### Changed

- `tab.screenshot()` saves PNG to spill dir and returns path instead of raw bytes
- `browser_screenshot` MCP tool returns file path instead of base64

### Fixed

- Unawaited coroutine warnings now appear in the cell that caused them, not later cells


## [0.0.3] - 2026-05-01

### Changed

- Expanded GUIDE resource: exec patterns, project context, live introspection with `--init`, `tab.fetch()` return shape, API discovery workflow
- Removed unnecessary `# noqa: S307` from runtime.py

### Fixed

- Aligned trailing comments across source files


## [0.0.2] - 2026-04-30

### Added

- `--socket` flag and `REPLD_SOCKET` env var for custom socket/lock paths (kernel, bridge, exec)
- `repld init` auto-detects uv projects (`uv.lock`) and writes `uv run repld bridge` in `.mcp.json`
- GUIDE MCP resource (`repld://docs/guide`) — working guide for gist patterns and conventions
- Meny.no grocery gist with nutrition/pricing/allergen parsing

### Fixed

- Instagram gist: URL-encode query parameters with `urllib.parse.urlencode`

## [0.0.1] - 2026-04-28

Initial release.

### Added

- Persistent Python kernel with top-level await and shared `__main__` namespace
- MCP stdio bridge with channel push notifications
- Core tools: `exec`, `get_task`, `cancel`
- Human gates: `ask`, `confirm`, `choose`, `notify`
- Background primitives: `defer(coro)`, `@every(seconds)`
- Browser integration (`repld-tool[browser]`): CDP attach, network capture, JS eval, trusted input
- Gist system: auto-reload modules, MCP tool registration, resource templates
- CLI: `repld`, `repld bridge`, `repld exec`, `repld init`, `repld help`, `repld gist`
