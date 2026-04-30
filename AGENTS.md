# Repository Guidelines

## Project Structure & Module Organization
This repository is a Python package using `src/` layout. Importable code lives under `src/chatgpt_register/`, including `register.py`, `sms_provider.py`, `phone_pool.py`, `qq_mail_pool.py`, `herosms_pool.py`, `quackr_pool.py`, `sentinel_solver.py`, `browser_configs.py`, `monitor/`, and `codex/`. Root `chatgpt_register.py` is the only compatibility wrapper; helper CLIs are exposed through console scripts such as `phone-pool` and `sentinel-solver`. Runtime paths are resolved through `src/chatgpt_register/paths.py`; existing root `config.json`, `data.db`, `registered_accounts.txt`, `pending_oauth.txt`, and `codex_tokens/` remain compatible, while new state defaults to `var/` when no legacy file exists.

## Reference Docs
- `docs/oauth_flow.md` — Codex OAuth 完整链路图、必带 authorize 参数、workspace/select 三种返回的处理分支、历史踩坑。改动 `src/chatgpt_register/register.py` 中 `perform_codex_oauth_login_http` / `_oauth_submit_workspace_and_org` / `_oauth_follow_for_code` / `_oauth_allow_redirect_extract_code` 之前 **必读**。

## Build, Test, and Development Commands
There is no build step. Use Python directly:

- `python3 chatgpt_register.py` starts the main flow.
- `python -m chatgpt_register` starts the same packaged main flow.
- `python3 chatgpt_register.py --retry-oauth` retries queued OAuth work.
- `sentinel-solver --thread 2` starts the local browser helper.
- `phone-pool <command>` manages the SMS phone pool CLI.

Install dependencies with `pip install -e .`; `pyproject.toml` is the single source of truth for project dependencies. For local package-path testing without installing, run commands with `PYTHONPATH=src`.

## Coding Style & Naming Conventions
Use Python 3.10+ style, 4-space indentation, and `snake_case` for functions, variables, and modules. Prefer clear docstrings and small helper functions. Keep generated or secret-bearing files out of version control. If you add a new application module, place it under `src/chatgpt_register/` unless it is a deliberate root compatibility wrapper.

## Testing Guidelines
Run `python -m unittest discover -s tests` for the current test suite. When changing behavior, also validate the relevant CLI path in a safe test environment and checking the resulting files or logs. If you add tests, place them under `tests/` and name them `test_*.py`.

## Commit & Pull Request Guidelines
Recent history uses short subjects, often `feat:` plus a brief description, sometimes with Chinese summaries. Keep commits focused and descriptive. PRs should explain the behavior change, list any new config or environment variables, and mention verification steps. Include screenshots or logs only when they clarify a user-visible change.

## Security & Configuration Tips
Treat API keys, cookies, tokens, phone data, and `data.db` as sensitive. Prefer environment variables over hardcoding secrets, and do not commit generated account or token files. Prefer `CHATGPT_REGISTER_DATA_DIR` for isolated test runs so generated state stays out of the source tree.
