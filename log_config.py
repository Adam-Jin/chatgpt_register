from __future__ import annotations

from typing import Any


DEFAULT_LOG_LEVEL = "info"
LOG_LEVELS = {
    "debug": 10,
    "info": 20,
    "success": 25,
    "warn": 30,
    "error": 40,
}
_ALIASES = {
    "warning": "warn",
    "err": "error",
}


def normalize_log_level(value: Any, *, default: str = DEFAULT_LOG_LEVEL) -> str:
    fallback = str(default or DEFAULT_LOG_LEVEL).strip().lower() or DEFAULT_LOG_LEVEL
    if fallback not in LOG_LEVELS:
        fallback = DEFAULT_LOG_LEVEL
    if value is None:
        return fallback
    level = str(value).strip().lower()
    if not level:
        return fallback
    level = _ALIASES.get(level, level)
    if level in LOG_LEVELS:
        return level
    return fallback


def should_log(level: Any, min_level: Any = DEFAULT_LOG_LEVEL) -> bool:
    normalized_level = normalize_log_level(level)
    normalized_min = normalize_log_level(min_level)
    return LOG_LEVELS[normalized_level] >= LOG_LEVELS[normalized_min]
