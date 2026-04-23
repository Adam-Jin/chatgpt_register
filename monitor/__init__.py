from __future__ import annotations

import sys
from typing import Any, Callable, Optional

from . import bus
from .fallback import TextSubscriber


def get_bus():
    return bus.get_bus()


def emit(*args, **kwargs):
    return bus.emit(*args, **kwargs)


def channel(*args, **kwargs):
    return bus.channel(*args, **kwargs)


def subscribe(*args, **kwargs):
    return bus.subscribe(*args, **kwargs)


def stats():
    return bus.stats()


def set_current_worker(worker_id: Optional[str]) -> None:
    bus.set_current_worker(worker_id)


def clear_current_worker() -> None:
    bus.clear_current_worker()


def current_worker_id() -> Optional[str]:
    return bus.current_worker_id()


def run_with_monitor(
    run_callable: Callable[[], Any],
    *,
    tui_enabled: bool,
    max_workers: int,
    pool_getter: Optional[Callable[[], Any]] = None,
    summary_getter: Optional[Callable[[], dict[str, Any]]] = None,
    inflight_getter: Optional[Callable[[], Any]] = None,
    intake_paused=None,
    shutdown_event=None,
):
    wants_tui = bool(tui_enabled) and _stdout_is_tty()
    if not wants_tui:
        subscriber = TextSubscriber().start()
        try:
            return run_callable()
        finally:
            subscriber.stop()

    try:
        from .app import RegisterMonitorApp

        app = RegisterMonitorApp(
            run_callable,
            max_workers=max_workers,
            pool_getter=pool_getter,
            summary_getter=summary_getter,
            inflight_getter=inflight_getter,
            intake_paused=intake_paused,
            shutdown_event=shutdown_event,
        )
        return app.run()
    except Exception as exc:
        subscriber = TextSubscriber().start()
        try:
            bus.emit("system", f"TUI init failed, fallback to text: {exc}", level="warn")
            return run_callable()
        finally:
            subscriber.stop()


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False
