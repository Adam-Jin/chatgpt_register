#!/usr/bin/env python3
from __future__ import annotations

import sys
import importlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
_PKG = _SRC / "chatgpt_register"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
if _PKG.is_dir():
    __path__ = [str(_PKG)]


def main():
    from chatgpt_register.cli import main as package_main

    return package_main()


_LEGACY_REGISTER_EXPORTS = {"ChatGPTRegister", "main", "retry_oauth_only", "run_batch"}


def __getattr__(name: str):
    if name not in _LEGACY_REGISTER_EXPORTS:
        raise AttributeError(name)
    _register = importlib.import_module("chatgpt_register.register")

    return getattr(_register, name)


if __name__ == "__main__":
    raise SystemExit(main())
