## Why

The project has outgrown a single-directory script layout: core modules, CLI entrypoints, tests, documentation, configuration, SQLite data, token output, and queue files are mixed at the repository root. This makes imports fragile, makes packaging and test execution less representative of real installs, and increases the chance that sensitive runtime artifacts are edited or committed by mistake.

## What Changes

- Adopt a standard Python package layout, preferably `src/` layout, with application code under `src/chatgpt_register/`.
- Convert root-level modules (`chatgpt_register.py`, `phone_pool.py`, `sms_provider.py`, provider pools, mail pools, monitor modules, browser/log/runtime helpers) into package modules with explicit intra-package imports.
- Preserve the existing user-facing CLI commands while adding package/module entrypoints:
  - `python3 chatgpt_register.py`
  - `python3 chatgpt_register.py --retry-oauth`
  - `sentinel-solver --thread 2`
  - `phone-pool <command>`
  - `python -m chatgpt_register`
- Add packaging metadata in `pyproject.toml`, including Python version, runtime dependencies, optional landbridge dependencies, and console script entrypoints where appropriate.
- Separate runtime state from source code by introducing a configurable data directory for `config.json`, `data.db`, `registered_accounts.txt`, `pending_oauth.txt`, and `codex_tokens/`.
- Keep backward compatibility for existing root-level runtime files during the transition, including clear path resolution precedence and migration guidance.
- Move or wrap script-style CLIs into package-owned CLI modules without changing their behavior.
- Update tests to import the installed package path rather than relying on root-directory imports.
- Update README, AGENTS guidance, and operational docs to describe the new layout, commands, config/data path behavior, and migration notes.

## Capabilities

### New Capabilities

- `python-package-layout`: Defines the source tree, package boundaries, import rules, compatibility wrappers, and supported CLI entrypoints for the refactored Python project layout.
- `runtime-path-management`: Defines how configuration, database files, queue files, output files, and token directories are located, overridden, migrated, and kept out of source-controlled package code.

### Modified Capabilities

(none. `openspec/specs/` is currently empty, so this change establishes the initial layout and runtime path specifications.)

## Impact

- **Code layout**: root modules move into `src/chatgpt_register/`; `monitor/` and `codex/` become package subpackages or clearly documented tool packages.
- **Imports**: root absolute imports such as `from phone_pool import ...` become package-relative or package-qualified imports.
- **Entrypoints**: the root main script remains as a thin compatibility wrapper while helper CLIs are exposed through console scripts.
- **Configuration and data**: code that currently resolves `config.json` or `data.db` from `os.path.dirname(__file__)` must use a shared path resolver.
- **Packaging**: add `pyproject.toml` and make it the single source of truth for project dependencies.
- **Tests**: update imports and test setup so `pytest`/`unittest` exercise the packaged code path.
- **Docs**: update README project tree, setup instructions, runtime file descriptions, and security notes.
- **Operational risk**: OAuth, SMS provider, phone pool, sentinel solver, and TUI monitor flows rely on module imports and filesystem paths; implementation must be incremental and verified through targeted CLI smoke tests.
