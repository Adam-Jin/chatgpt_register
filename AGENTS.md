# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Python toolset centered on `chatgpt_register.py`. Supporting modules live at the repo root: `sms_provider.py`, `phone_pool.py`, `qq_mail_pool.py`, `herosms_pool.py`, `quackr_pool.py`, `sentinel_solver.py`, and `browser_configs.py`. Runtime outputs are also stored at the root, including `config.json`, `data.db`, `registered_accounts.txt`, `pending_oauth.txt`, `ak.txt`, `rk.txt`, and `codex_tokens/`.

## Reference Docs
- `docs/oauth_flow.md` — Codex OAuth 完整链路图、必带 authorize 参数、workspace/select 三种返回的处理分支、历史踩坑。改动 `chatgpt_register.py` 中 `perform_codex_oauth_login_http` / `_oauth_submit_workspace_and_org` / `_oauth_follow_for_code` / `_oauth_allow_redirect_extract_code` 之前 **必读**。

## Build, Test, and Development Commands
There is no build step. Use Python directly:

- `python3 chatgpt_register.py` starts the main flow.
- `python3 chatgpt_register.py --retry-oauth` retries queued OAuth work.
- `python3 sentinel_solver.py --thread 2` starts the local browser helper.
- `python3 phone_pool.py <command>` manages the SMS phone pool CLI.

Install dependencies as needed with `pip install curl_cffi` and `pip install -r requirements_solver.txt`.

## Coding Style & Naming Conventions
Use Python 3.10+ style, 4-space indentation, and `snake_case` for functions, variables, and modules. Prefer clear docstrings and small helper functions. Keep generated or secret-bearing files out of version control. If you add a new module, place it at the repo root unless it clearly belongs to a new package.

## Testing Guidelines
There is no formal automated test suite in the repository. When changing behavior, validate by running the relevant CLI path in a safe test environment and checking the resulting files or logs. If you add tests, place them under `tests/` and name them `test_*.py`.

## Commit & Pull Request Guidelines
Recent history uses short subjects, often `feat:` plus a brief description, sometimes with Chinese summaries. Keep commits focused and descriptive. PRs should explain the behavior change, list any new config or environment variables, and mention verification steps. Include screenshots or logs only when they clarify a user-visible change.

## Security & Configuration Tips
Treat API keys, cookies, tokens, phone data, and `data.db` as sensitive. Prefer environment variables over hardcoding secrets, and do not commit generated account or token files.
