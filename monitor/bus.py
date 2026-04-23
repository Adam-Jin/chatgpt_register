from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


DEFAULT_QUEUE_SIZE = 2048


@dataclass(slots=True)
class Event:
    ts: float
    channel: str
    worker_id: Optional[str]
    level: str
    msg: str
    fields: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self):
        self._subscribers: list[queue.Queue[Event]] = []
        self._lock = threading.Lock()
        self._thread_local = threading.local()
        self._dropped_events = 0
        self._last_ts = 0.0

    def subscribe(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, subscriber: queue.Queue[Event]) -> None:
        with self._lock:
            self._subscribers = [q for q in self._subscribers if q is not subscriber]

    def emit(
        self,
        channel: str,
        msg: str,
        *,
        worker_id: Optional[str] = None,
        level: str = "info",
        **fields: Any,
    ) -> Event:
        with self._lock:
            now = time.time()
            ts = now if now >= self._last_ts else self._last_ts
            self._last_ts = ts
            subscribers = list(self._subscribers)
        event = Event(
            ts=ts,
            channel=str(channel),
            worker_id=worker_id if worker_id is not None else self.current_worker_id(),
            level=str(level).lower(),
            msg=str(msg),
            fields=dict(fields),
        )
        dropped = 0
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                dropped += 1
        if dropped:
            with self._lock:
                self._dropped_events += dropped
        return event

    def channel(
        self,
        name: str,
        *,
        level: str = "info",
        **bound_fields: Any,
    ) -> Callable[[str], None]:
        def _log(msg: str) -> None:
            self.emit(name, msg, level=level, **bound_fields)

        return _log

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "subscribers": len(self._subscribers),
                "dropped_events": self._dropped_events,
            }

    def set_current_worker(self, worker_id: Optional[str]) -> None:
        self._thread_local.worker_id = worker_id

    def clear_current_worker(self) -> None:
        self.set_current_worker(None)

    def current_worker_id(self) -> Optional[str]:
        return getattr(self._thread_local, "worker_id", None)


_BUS = EventBus()


def get_bus() -> EventBus:
    return _BUS


def emit(channel: str, msg: str, *, worker_id: Optional[str] = None, level: str = "info", **fields: Any) -> Event:
    return _BUS.emit(channel, msg, worker_id=worker_id, level=level, **fields)


def channel(name: str, *, level: str = "info", **bound_fields: Any) -> Callable[[str], None]:
    return _BUS.channel(name, level=level, **bound_fields)


def stats() -> dict[str, Any]:
    return _BUS.stats()


def subscribe(maxsize: int = DEFAULT_QUEUE_SIZE) -> queue.Queue[Event]:
    return _BUS.subscribe(maxsize=maxsize)


def unsubscribe(subscriber: queue.Queue[Event]) -> None:
    _BUS.unsubscribe(subscriber)


def set_current_worker(worker_id: Optional[str]) -> None:
    _BUS.set_current_worker(worker_id)


def clear_current_worker() -> None:
    _BUS.clear_current_worker()


def current_worker_id() -> Optional[str]:
    return _BUS.current_worker_id()
