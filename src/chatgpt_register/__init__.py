"""Package entrypoint for the chatgpt_register toolset."""

import importlib

__all__ = ["__version__"]
__version__ = "0.1.0"

_LEGACY_REGISTER_EXPORTS = {"ChatGPTRegister", "main", "retry_oauth_only", "run_batch"}


def __getattr__(name: str):
    if name not in _LEGACY_REGISTER_EXPORTS:
        raise AttributeError(name)
    register = importlib.import_module("chatgpt_register.register")

    return getattr(register, name)
