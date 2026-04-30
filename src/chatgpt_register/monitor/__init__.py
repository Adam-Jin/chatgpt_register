from __future__ import annotations

import sys
import contextlib
from typing import Any, Callable, Optional

from . import bus
from .fallback import MemoryBufferSubscriber, TextSubscriber


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

    app = None
    replay_buffer = MemoryBufferSubscriber().start()
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
        result = app.run()
        _replay_buffer(replay_buffer)
        return result
    except KeyboardInterrupt:
        if shutdown_event is not None:
            shutdown_event.set()
        with contextlib.suppress(Exception):
            app._drain_bus()
        with contextlib.suppress(Exception):
            app._restore_stream_capture()
        with contextlib.suppress(Exception):
            app._cleanup_bus()
        _force_restore_terminal(app)
        _replay_buffer(replay_buffer)
        raise
    except Exception as exc:
        _force_restore_terminal(locals().get("app"))
        with contextlib.suppress(Exception):
            replay_buffer.stop()
        subscriber = TextSubscriber().start()
        try:
            bus.emit("system", f"TUI init failed, fallback to text: {exc}", level="warn")
            return run_callable()
        finally:
            subscriber.stop()
    finally:
        with contextlib.suppress(Exception):
            replay_buffer.stop()


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _force_restore_terminal(app=None) -> None:
    driver = getattr(app, "_driver", None)
    if driver is not None:
        with contextlib.suppress(BaseException):
            driver.stop_application_mode()
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with contextlib.suppress(Exception):
        stream.write("\x1b[?2004l\x1b[?7h\x1b[<u\x1b[?1049l\x1b[?25h\x1b[?1004l\x1b[0m\r\n")
        stream.flush()


def _replay_buffer(buffer: MemoryBufferSubscriber) -> None:
    with contextlib.suppress(Exception):
        buffer.stop()
    with contextlib.suppress(Exception):
        buffer.replay()
