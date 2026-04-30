from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

CONFIG_ENV = "CHATGPT_REGISTER_CONFIG"
DATA_DIR_ENV = "CHATGPT_REGISTER_DATA_DIR"
PROJECT_ROOT_ENV = "CHATGPT_REGISTER_PROJECT_ROOT"

CONFIG_NAME = "config.json"
DB_NAME = "data.db"
OUTPUT_NAME = "registered_accounts.txt"
PENDING_OAUTH_NAME = "pending_oauth.txt"
TOKEN_DIR_NAME = "codex_tokens"


def _as_path(value: str | os.PathLike[str] | None) -> Optional[Path]:
    if value is None:
        return None
    text = os.fspath(value).strip()
    if not text:
        return None
    return Path(text).expanduser()


def _absolute(path: Path, *, base: Path | None = None) -> Path:
    if path.is_absolute():
        return path
    return (base or Path.cwd()).joinpath(path).resolve()


def find_project_root(start: str | os.PathLike[str] | None = None) -> Optional[Path]:
    env_root = _as_path(os.environ.get(PROJECT_ROOT_ENV))
    if env_root is not None:
        return _absolute(env_root)

    current = _as_path(start)
    if current is None:
        current = Path(__file__).resolve()
    else:
        current = _absolute(current)
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (
            (candidate / "pyproject.toml").exists()
            or (candidate / "openspec").is_dir()
            or (candidate / "AGENTS.md").exists()
        ):
            return candidate
    return None


def data_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> Path:
    explicit_path = _as_path(explicit)
    if explicit_path is not None:
        return _absolute(explicit_path)

    env_path = _as_path(os.environ.get(DATA_DIR_ENV))
    if env_path is not None:
        return _absolute(env_path)

    root = _as_path(project_root)
    if root is not None:
        root = _absolute(root)
    else:
        root = find_project_root()
    if root is not None:
        return root / "var"
    return Path.cwd().resolve() / "var"


def ensure_parent(path: str | os.PathLike[str]) -> Path:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def legacy_path(name: str, *, project_root: str | os.PathLike[str] | None = None) -> Optional[Path]:
    root = _as_path(project_root)
    if root is not None:
        root = _absolute(root)
    else:
        root = find_project_root()
    if root is None:
        return None
    return root / name


def _runtime_path(
    name: str,
    explicit: str | os.PathLike[str] | None = None,
    *,
    env_var: str | None = None,
    prefer_existing_legacy: bool = True,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
    directory: bool = False,
) -> Path:
    explicit_path = _as_path(explicit)
    if explicit_path is not None:
        return _absolute(explicit_path)

    if env_var:
        env_path = _as_path(os.environ.get(env_var))
        if env_path is not None:
            return _absolute(env_path)

    has_data_dir_override = _as_path(data_directory) is not None or _as_path(os.environ.get(DATA_DIR_ENV)) is not None
    legacy = legacy_path(name, project_root=project_root)
    if (
        prefer_existing_legacy
        and not has_data_dir_override
        and legacy is not None
        and (legacy.is_dir() if directory else legacy.exists())
    ):
        return legacy

    base = data_dir(data_directory, project_root=project_root)
    return base / name


def config_path(
    explicit: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    return _runtime_path(
        CONFIG_NAME,
        explicit,
        env_var=CONFIG_ENV,
        project_root=project_root,
        data_directory=data_directory,
    )


def database_path(
    explicit: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    return _runtime_path(
        DB_NAME,
        explicit,
        project_root=project_root,
        data_directory=data_directory,
    )


def output_file_path(
    value: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    path = _as_path(value)
    if path is not None:
        if path.is_absolute():
            return path
        has_data_dir_override = _as_path(data_directory) is not None or _as_path(os.environ.get(DATA_DIR_ENV)) is not None
        legacy = legacy_path(os.fspath(path), project_root=project_root)
        if not has_data_dir_override and legacy is not None and legacy.exists():
            return legacy
        return data_dir(data_directory, project_root=project_root) / path
    return _runtime_path(
        OUTPUT_NAME,
        project_root=project_root,
        data_directory=data_directory,
    )


def pending_oauth_path(
    value: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    path = _as_path(value)
    if path is not None:
        if path.is_absolute():
            return path
        has_data_dir_override = _as_path(data_directory) is not None or _as_path(os.environ.get(DATA_DIR_ENV)) is not None
        legacy = legacy_path(os.fspath(path), project_root=project_root)
        if not has_data_dir_override and legacy is not None and legacy.exists():
            return legacy
        return data_dir(data_directory, project_root=project_root) / path
    return _runtime_path(
        PENDING_OAUTH_NAME,
        project_root=project_root,
        data_directory=data_directory,
    )


def token_dir_path(
    value: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    path = _as_path(value)
    if path is not None:
        if path.is_absolute():
            return path
        has_data_dir_override = _as_path(data_directory) is not None or _as_path(os.environ.get(DATA_DIR_ENV)) is not None
        legacy = legacy_path(os.fspath(path), project_root=project_root)
        if not has_data_dir_override and legacy is not None and legacy.is_dir():
            return legacy
        return data_dir(data_directory, project_root=project_root) / path
    return _runtime_path(
        TOKEN_DIR_NAME,
        project_root=project_root,
        data_directory=data_directory,
        directory=True,
    )


def resolve_runtime_path(
    value: str | os.PathLike[str],
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    path = _as_path(value)
    if path is None:
        raise ValueError("path value must not be empty")
    if path.is_absolute():
        return path
    return data_dir(data_directory, project_root=project_root) / path


def codex_config_path(
    explicit: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> Path:
    explicit_path = _as_path(explicit)
    if explicit_path is not None:
        return _absolute(explicit_path)

    root = _as_path(project_root)
    if root is not None:
        root = _absolute(root)
    else:
        root = find_project_root()
    if root is not None:
        legacy = root / "codex" / CONFIG_NAME
        if legacy.exists():
            return legacy
    return data_dir(data_directory, project_root=root) / "codex" / CONFIG_NAME
