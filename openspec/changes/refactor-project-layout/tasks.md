## 1. Packaging Foundation

- [x] 1.1 Add `pyproject.toml` with project metadata, Python version, package discovery for `src/`, project dependencies, optional landbridge extra, and console script entrypoints.
- [x] 1.2 Create `src/chatgpt_register/` with `__init__.py`, `__main__.py`, and a package-owned CLI dispatch module.
- [x] 1.3 Add a minimal editable-install verification path for local development, documenting whether tests use `pip install -e .` or `PYTHONPATH=src`.

## 2. Runtime Path Resolver

- [x] 2.1 Implement `src/chatgpt_register/paths.py` with project-root detection, active data directory resolution, config path resolution, and helpers for database, output, pending queue, and token paths.
- [x] 2.2 Add environment override support for `CHATGPT_REGISTER_CONFIG` and `CHATGPT_REGISTER_DATA_DIR`.
- [x] 2.3 Preserve legacy root-file compatibility for existing `config.json`, `data.db`, `registered_accounts.txt`, `pending_oauth.txt`, and `codex_tokens/`.
- [x] 2.4 Add tests for path precedence: explicit override, environment override, legacy root file, source-checkout default `var/`, installed/cwd default `var/`, relative path, and absolute path.

## 3. Move Package Modules

- [x] 3.1 Move `chatgpt_register.py` implementation into `src/chatgpt_register/register.py` without changing CLI behavior.
- [x] 3.2 Move support modules into `src/chatgpt_register/`: `sms_provider.py`, `phone_pool.py`, `qq_mail_pool.py`, `addy_pool.py`, `herosms_pool.py`, `quackr_pool.py`, `sentinel_solver.py`, `browser_configs.py`, `log_config.py`, and `landbridge_runtime.py`.
- [x] 3.3 Move `monitor/` into `src/chatgpt_register/monitor/` and update package imports.
- [x] 3.4 Move `codex/` implementation code into `src/chatgpt_register/codex/`, preserving any documented CLI or script behavior.
- [x] 3.5 Keep non-code documentation and OpenSpec artifacts in their current top-level directories.

## 4. Import Migration

- [x] 4.1 Replace root-level imports in package code with relative or package-qualified imports.
- [x] 4.2 Update tests to import from `chatgpt_register.*` instead of root-level module names.
- [x] 4.3 Check optional import fallbacks in the main flow so missing optional dependencies still produce the existing degraded behavior.
- [x] 4.4 Run an import smoke check that imports the package, main CLI module, phone pool module, sentinel module, monitor module, and codex helper module.

## 5. Compatibility Wrappers

- [x] 5.1 Replace root `chatgpt_register.py` with a thin wrapper that prepends `<repo>/src` when needed and delegates to the package main CLI.
- [x] 5.2 Remove root `sentinel_solver.py`; expose the package sentinel CLI through the `sentinel-solver` console script.
- [x] 5.3 Remove root `phone_pool.py`; expose the package phone pool CLI through the `phone-pool` console script.
- [x] 5.4 Verify the main wrapper contains no business logic and does not shadow the package during import.

## 6. Runtime Path Integration

- [x] 6.1 Update main config loading and config persistence to use the shared path resolver.
- [x] 6.2 Update OAuth pending queue read/write paths to use the shared path resolver unless an explicit file is provided.
- [x] 6.3 Update token JSON output path resolution to use the active data directory for relative paths.
- [x] 6.4 Update `phone_pool` and SMS provider CLIs to use the shared database/config paths instead of module-local `__file__` paths.
- [x] 6.5 Update `sentinel_solver`, mail pools, addy pool, landbridge runtime, and codex helper path references to use the shared resolver where they access project runtime files.
- [x] 6.6 Resolve runtime paths lazily at the point of use; remove module-level `CONFIG_PATH`/`DB_PATH` constants and default-arg captures, except where the surrounding module-level setup already runs at import.

## 7. Tests And Validation

- [x] 7.1 Run the existing unit tests under the packaged import path and fix import/path issues.
- [x] 7.2 Add entrypoint smoke checks for `python3 chatgpt_register.py --help`, `sentinel-solver --help`, and `phone-pool --help`.
- [x] 7.3 Smoke test `python -m chatgpt_register --help` from an environment where the package is importable.
- [x] 7.4 Smoke test `--retry-oauth` path resolution against a temporary pending queue.
- [x] 7.5 Smoke test phone pool database creation against a temporary data directory.

## 8. Documentation And Cleanup

- [x] 8.1 Update README project structure, install instructions, command examples, and runtime file locations.
- [x] 8.2 Update AGENTS.md to describe the new package layout and path resolver expectations.
- [x] 8.3 Update docs that reference root `config.json`, `data.db`, `pending_oauth.txt`, or token paths.
- [x] 8.4 Update `.gitignore` to cover `var/` and any generated sensitive runtime artifacts that remain possible at the root.
- [x] 8.5 Move requirements fully into `pyproject.toml`: built-in project commands use `[project].dependencies`; private/less-common landbridge dependencies remain in the `landbridge` extra; standalone requirement files are removed.

## 9. OpenSpec Validation

- [x] 9.1 Run `openspec validate refactor-project-layout --strict` after each artifact edit and before each implementation milestone, fixing proposal/spec/task issues as they appear.
- [x] 9.2 Run `openspec status --change refactor-project-layout` and confirm all apply-required artifacts are complete; this gate runs both before implementation begins (artifacts must exist) and after implementation finishes (artifacts must still match the change).
