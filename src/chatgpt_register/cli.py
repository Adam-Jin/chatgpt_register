from __future__ import annotations


def main() -> int | None:
    from .register import main as register_main

    return register_main()
