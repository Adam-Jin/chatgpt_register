## Context

The repository is currently a hybrid of script-style Python and partially package-style code. The main flow lives in `chatgpt_register.py` at the repository root, most supporting modules are also root-level files, `monitor/` and `codex/` are directories, and tests import modules directly from the repository root. Runtime state is also rooted beside source files: `config.json`, `data.db`, `registered_accounts.txt`, `pending_oauth.txt`, and `codex_tokens/`.

The highest-risk areas are not the file moves themselves; they are import resolution and filesystem path behavior. Current modules use root-level imports such as `from phone_pool import ...`, and several modules compute config or database paths from `os.path.dirname(os.path.abspath(__file__))`. After package migration, `__file__` will point under `src/chatgpt_register/`, so path handling must be centralized before or during the move.

## Goals / Non-Goals

**Goals:**

- Move implementation code into a standard Python package under `src/chatgpt_register/`.
- Preserve existing operator commands through root-level compatibility wrappers.
- Add `pyproject.toml` so the project can be installed and tested as a package.
- Replace root-directory import assumptions with package-relative or package-qualified imports.
- Centralize config/data/output path resolution and keep runtime artifacts outside package source.
- Keep the migration incremental enough that OAuth, SMS, mail, phone pool, sentinel, and TUI flows can be smoke-tested independently.

**Non-Goals:**

- Do not rewrite the registration/OAuth/SMS/mail business flows as part of the layout change.
- Do not change database schema except for path relocation behavior.
- Do not require users to immediately move existing `config.json`, `data.db`, token files, or pending queues.
- Do not remove old root-level commands in the first implementation pass.
- Do not publish the package to PyPI as part of this change.

## Decisions

### D1. Use `src/` layout

**Decision:** Use `src/chatgpt_register/` as the canonical package root.

Target shape:

```text
chatgpt_register/
  pyproject.toml
  README.md
  AGENTS.md
  chatgpt_register.py        # compatibility wrapper
  src/
    chatgpt_register/
      __init__.py
      __main__.py
      cli.py
      register.py
      paths.py
      browser_configs.py
      log_config.py
      landbridge_runtime.py
      sms_provider.py
      phone_pool.py
      addy_pool.py
      qq_mail_pool.py
      herosms_pool.py
      quackr_pool.py
      sentinel_solver.py
      monitor/
      codex/
  tests/
  docs/
  openspec/
  var/                       # ignored runtime state
```

**Rationale:** `src/` layout prevents tests from accidentally importing root files just because the working directory is the repository root. It makes local test behavior closer to installed-package behavior and gives runtime files a clear boundary away from importable source.

**Alternative considered:** Flat package layout (`chatgpt_register/` at repository root). This is simpler to browse, but it keeps package code near root scripts and runtime files, and it is easier for tests to pass while packaging metadata is wrong.

### D2. Keep only the main root script as a compatibility wrapper

**Decision:** Keep `chatgpt_register.py` at the repository root as the only compatibility wrapper. It prepends `<repo>/src` to `sys.path` when running from a source checkout, then imports and calls the package-owned CLI function. Helper entrypoints are provided as console scripts (`phone-pool`, `sentinel-solver`) after `pip install -e .`.

**Rationale:** The main command has the highest compatibility value because it appears in shell history and operational runbooks. Helper wrappers duplicate console scripts and keep extra root-level Python files around, so they are removed now that the project expects editable installation.

**Alternative considered:** Remove all root scripts and require `python -m chatgpt_register` or console scripts immediately. This is cleaner long-term but too disruptive for the main operational entrypoint.

### D3. Preserve module names inside the package for the first pass

**Decision:** Move current modules into the package mostly with their current names (`phone_pool.py`, `sms_provider.py`, `qq_mail_pool.py`, etc.). Rename only the root main implementation from `chatgpt_register.py` to `register.py` to avoid confusion with the package name.

**Rationale:** Keeping module names stable reduces the migration surface. Larger semantic reorganizations such as `providers/`, `mail/`, or `oauth/` can happen later after packaging and path resolution are stable.

**Alternative considered:** Fully domain-organize modules during the same change. That would produce a nicer tree, but it combines behavior-preserving packaging work with broad import churn and makes regressions harder to isolate.

### D4. Use explicit package imports

**Decision:** Internal imports must become relative imports (`from .phone_pool import PhonePool`) or package-qualified imports (`from chatgpt_register.phone_pool import PhonePool`). Tests should import through `chatgpt_register.*`.

**Rationale:** Root-level imports are the main reason a source checkout can behave differently from an installed package. Explicit package imports make dependencies visible and prevent accidental shadowing by root files.

**Alternative considered:** Use `PYTHONPATH=src` and keep bare imports. This would work locally but preserve the fragile assumption that modules are globally importable by short names.

### D5. Introduce a shared runtime path resolver

**Decision:** Add `src/chatgpt_register/paths.py` to resolve config, database, output, queue, and token paths. No module should compute these paths from its own `__file__` after migration.

Path precedence:

1. Explicit CLI argument, where a command exposes one.
2. Specific environment variable, such as `CHATGPT_REGISTER_CONFIG` or `CHATGPT_REGISTER_DATA_DIR`.
3. Existing legacy root file in a source checkout, for compatibility.
4. Default data directory (`<project-root>/var` in a source checkout; `./var` when no project root is discoverable).

Relative paths from config should resolve against the active data directory, not against package source.

**Rationale:** This preserves existing root files while stopping package modules from writing into `src/` or site-packages. It also gives tests one place to override paths.

**Alternative considered:** Automatically move legacy files into `var/`. This is risky for sensitive data and makes rollback harder. The first pass should read legacy files in place and document optional manual migration.

### D6. Use `pyproject.toml` as the dependency source of truth

**Decision:** Add `pyproject.toml` with build metadata, package discovery, Python version, project dependencies, optional landbridge extra, and console script entrypoints. Project dependencies live in `[project].dependencies`; separate `requirements.txt` / `requirements_solver.txt` files are not kept.

**Rationale:** This is a single application package, so split requirement files add a second dependency source that can drift from package metadata. Installing with `pip install -e .` exercises the same dependency metadata used by console entrypoints and editable development.

**Alternative considered:** Keep one or more requirement files as compatibility shims. That reduces short-term change for old commands, but it preserves duplicate dependency lists and makes future package installs harder to reason about.

### D7. Root `chatgpt_register.py` mimics the package via `__path__` + `__getattr__`

**Decision:** The root script `chatgpt_register.py` shares its name with the `chatgpt_register` package. To prevent it from shadowing the package on `import chatgpt_register` from a source checkout, the wrapper does three things:

1. Prepend `<repo>/src` to `sys.path` so the real package wins normal package resolution.
2. Set its own module-level `__path__ = [<repo>/src/chatgpt_register]`. This converts the script into a namespace alias for the package directory: `from chatgpt_register.cli import main` then resolves submodules from the real package source even when the script was already loaded as `chatgpt_register` via `python3 chatgpt_register.py`.
3. Provide a module-level `__getattr__` that lazily re-exports the legacy public names (`ChatGPTRegister`, `main`, `retry_oauth_only`, `run_batch`) from `chatgpt_register.register` for code that still does `from chatgpt_register import ChatGPTRegister` against the script.

**Rationale:** D2 keeps the root script for operator continuity but D2 alone is not enough — the script and the package collide on the name `chatgpt_register`. Renaming the root script (e.g., to `run.py`) was rejected as breaking shell history; the alternative is this controlled mimicry, scoped to the four legacy exports listed in `_LEGACY_REGISTER_EXPORTS`. The wrapper still contains no business logic.

**Constraints this places on the wrapper:**

- The wrapper MUST insert `<repo>/src` at `sys.path[0]`, never anywhere else, so the package wins ahead of any inherited `sys.path` entries.
- The `__path__` assignment MUST point at the real package directory so submodule imports resolve to the package, not back to the script.
- `__getattr__` MUST be limited to a closed allowlist (`_LEGACY_REGISTER_EXPORTS`) and MUST raise `AttributeError` for unknown names so the rest of the protocol (e.g., `hasattr`) stays correct.
- Adding new public exports to `chatgpt_register.register` does NOT automatically extend the wrapper allowlist; either update the allowlist explicitly, or have callers import from the package directly.

**Alternative considered:** Rename the root script to `run.py` (or no `.py` suffix) so the package keeps the name uncontested. This is structurally cleaner but breaks every shell alias, README example, and operational runbook that references `python3 chatgpt_register.py`. We accepted the wrapper mimicry as the lower-blast-radius option.

### D8. Path resolver semantics: lazy in callers, frozen at module import only when unavoidable

**Decision:** Modules SHOULD call `paths.<name>_path()` at the point of use rather than caching the result in a module-level constant. Module-level capture is permitted only when the surrounding setup itself runs at import (e.g., `register.py` already loads `_CONFIG = _load_config()` and configures landbridge at import); in that case the capture is consistent with the rest of the module's import-time work.

**Rationale:** A module-level constant `CONFIG_PATH = str(_paths.config_path())` freezes the resolved value at first import. Tests or callers that change `CHATGPT_REGISTER_CONFIG` / `CHATGPT_REGISTER_DATA_DIR` after import will silently use the stale value, which directly contradicts the env-override scenarios in the `runtime-path-management` spec. Calling the resolver inside the function that opens the file keeps env overrides honored throughout the process lifetime.

**Constraints:**

- New code under `src/chatgpt_register/` MUST resolve runtime paths inside the function or method that uses them, not at module top level.
- Default arg values MUST NOT capture resolver output (`def __init__(..., db_path: str = DB_PATH)` is disallowed). Use `db_path: Optional[str] = None` and resolve when `None` inside the body.
- Where module-level capture is unavoidable (notably `register.py` config block), document the env-vars-must-be-set-before-import constraint near the capture site and surface it in the README.

**Alternative considered:** Refactor every module-level config/path capture in `register.py` to be lazy. This is a larger surgery — `_CONFIG` and ~30 derived constants are read across the file — and was deferred as scope creep beyond layout migration. D8 commits to the lazy rule for new code and for the easy cases (`phone_pool`, `quackr_pool`, `herosms_pool`); the `register.py` import-time block stays as a documented exception.

## Risks / Trade-offs

- [Risk] Wrapper import shadowing from root `chatgpt_register.py` can cause `chatgpt_register is not a package` errors. -> Mitigation: the main wrapper must insert `<repo>/src` at the front of `sys.path` before importing package modules, and tests must cover wrapper execution.
- [Risk] Path behavior changes could create a second empty `data.db` or miss an existing `pending_oauth.txt`. -> Mitigation: centralize path precedence, prefer existing legacy files, and add tests for each path case.
- [Risk] Broad import updates can break optional modules that are only imported in specific modes. -> Mitigation: update imports mechanically, then run targeted smoke tests for main, retry-oauth, phone pool CLI, sentinel CLI, and monitor tests.
- [Risk] Keeping the main compatibility wrapper leaves one duplicate-looking filename at root and under `src/`. -> Mitigation: the wrapper must contain only bootstrap code and comments pointing to package modules; the root `chatgpt_register.py` additionally uses the controlled `__path__` + `__getattr__` mimicry described in D7, scoped to a closed export allowlist.
- [Risk] Module-level `CONFIG_PATH = _paths.config_path()` captures freeze env-override semantics at import time. -> Mitigation: D8 requires lazy resolution at the point of use; `register.py`'s existing import-time config block is the documented exception.
- [Risk] Browser-helper dependencies (`patchright`, `quart`) make editable installs heavier than a minimal core-only install. -> Mitigation: accept the cost because this repository is operated as one project; keep only private/less-common landbridge dependencies behind an optional extra.

## Migration Plan

1. Add `pyproject.toml`, `src/chatgpt_register/`, and package scaffolding.
2. Add `paths.py` and tests for config/data/output path precedence before moving modules that touch files.
3. Move root modules into `src/chatgpt_register/` while preserving names where possible.
4. Convert imports to explicit package-relative imports.
5. Replace the root main entrypoint with a thin compatibility wrapper and expose helper entrypoints through console scripts.
6. Update tests to import `chatgpt_register.*` and run under editable install or `PYTHONPATH=src`.
7. Update docs and `.gitignore` for `var/` and legacy sensitive artifacts.
8. Run unit tests and targeted CLI smoke tests.

Rollback strategy: because the root main wrapper keeps the old primary command name, rollback can be a normal git revert. Runtime files are not auto-moved, so user data does not need rollback.

## Resolved Questions

- **Root compatibility wrapper deprecation schedule.** Decision: keep only `chatgpt_register.py` indefinitely for the primary command. Helper wrappers are removed in favor of console scripts.
- **Placement of `codex/protocol_keygen.py`.** Decision: move under `chatgpt_register.codex.protocol_keygen` in this change and run it with `python -m chatgpt_register.codex.protocol_keygen`. A second top-level import namespace would multiply the same packaging problems we just fixed.
- **Introduce a `migrate-runtime` command in this change?** Decision: no. Document manual migration in the README. Add a command later only if real users hit friction; doing it now bundles unrelated UX work into a layout change.
