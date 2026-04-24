from __future__ import annotations

import io
import queue
import sys
import threading
from collections import deque
from typing import Optional, TextIO

from . import bus
from .render import colorize_plain_event, format_event_plain


class TextSubscriber:
    def __init__(self, stream: Optional[TextIO] = None, *, queue_size: int = 4096):
        self.stream = stream or sys.stdout
        self.queue = bus.subscribe(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._use_color = bool(getattr(self.stream, "isatty", lambda: False)())

    def start(self) -> "TextSubscriber":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="monitor-fallback", daemon=True)
        self._thread.start()
        return self

    def stop(self, *, drain: bool = True) -> None:
        self._stop.set()
        if drain:
            self.drain_once(limit=None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        bus.unsubscribe(self.queue)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.drain_once(limit=200)
            self._stop.wait(0.1)

    def drain_once(self, *, limit: Optional[int] = 200) -> int:
        drained = 0
        while limit is None or drained < limit:
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            line = colorize_plain_event(event) if self._use_color else format_event_plain(event)
            self.stream.write(line + "\n")
            drained += 1
        if drained:
            self.stream.flush()
        return drained


class MemoryBufferSubscriber:
    def __init__(self, *, capacity: int = 5000, queue_size: int = 4096):
        self.capacity = max(1, int(capacity))
        self.queue = bus.subscribe(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._lines: deque[str] = deque(maxlen=self.capacity)
        self._discarded = 0

    def start(self) -> "MemoryBufferSubscriber":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="monitor-memory-buffer", daemon=True)
        self._thread.start()
        return self

    def stop(self, *, drain: bool = True) -> None:
        self._stop.set()
        if drain:
            self.drain_once(limit=None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        bus.unsubscribe(self.queue)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.drain_once(limit=200)
            self._stop.wait(0.1)

    def drain_once(self, *, limit: Optional[int] = 200) -> int:
        drained = 0
        while limit is None or drained < limit:
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            self._append_line(format_event_plain(event))
            drained += 1
        return drained

    def _append_line(self, line: str) -> None:
        with self._lock:
            if len(self._lines) >= self.capacity:
                self._discarded += 1
            self._lines.append(line)

    def snapshot(self) -> tuple[list[str], int]:
        with self._lock:
            return list(self._lines), self._discarded

    def replay(self, stream: Optional[TextIO] = None) -> int:
        lines, discarded = self.snapshot()
        if not lines:
            return 0
        target = stream or getattr(sys, "__stdout__", None) or sys.stdout
        target.write("\n=== Recent Logs ===\n")
        if discarded:
            target.write(f"(discarded {discarded} older lines)\n")
        for line in lines:
            target.write(line + "\n")
        target.flush()
        return len(lines)


class StreamCapture(io.TextIOBase):
    def __init__(self, channel_name: str, level: str = "info"):
        self.channel_name = channel_name
        self.level = level
        self._buffer = ""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        text = str(s)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                bus.emit(self.channel_name, line, level=self.level)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            bus.emit(self.channel_name, self._buffer.strip(), level=self.level)
        self._buffer = ""
