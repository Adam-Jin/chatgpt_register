from __future__ import annotations

import io
import unittest

from chatgpt_register.monitor import bus
from chatgpt_register.monitor.fallback import MemoryBufferSubscriber, TextSubscriber


class MonitorBusTests(unittest.TestCase):
    def tearDown(self) -> None:
        bus.clear_current_worker()

    def test_emit_ordering(self):
        q = bus.subscribe(maxsize=10)
        try:
            first = bus.emit("worker", "one")
            second = bus.emit("worker", "two")
            self.assertEqual(q.get_nowait(), first)
            self.assertEqual(q.get_nowait(), second)
            self.assertGreaterEqual(second.ts, first.ts)
        finally:
            bus.unsubscribe(q)

    def test_drop_counter_under_backpressure(self):
        q = bus.subscribe(maxsize=1)
        try:
            before = bus.stats()["dropped_events"]
            bus.emit("worker", "one")
            bus.emit("worker", "two")
            after = bus.stats()["dropped_events"]
            self.assertGreaterEqual(after, before + 1)
        finally:
            bus.unsubscribe(q)

    def test_channel_routes_worker_context(self):
        q = bus.subscribe(maxsize=10)
        try:
            bus.set_current_worker("W1")
            bus.channel("sms")("hello")
            event = q.get_nowait()
            self.assertEqual(event.channel, "sms")
            self.assertEqual(event.worker_id, "W1")
            self.assertEqual(event.msg, "hello")
        finally:
            bus.unsubscribe(q)
            bus.clear_current_worker()

    def test_fallback_line_format(self):
        stream = io.StringIO()
        subscriber = TextSubscriber(stream=stream)
        try:
            bus.emit("worker", "hello", worker_id="W2", level="warn")
            subscriber.drain_once(limit=None)
            line = stream.getvalue().strip()
            self.assertIn("[WARN][worker][W2] hello", line)
        finally:
            subscriber.stop(drain=False)

    def test_memory_buffer_keeps_recent_lines(self):
        subscriber = MemoryBufferSubscriber(capacity=2)
        try:
            bus.emit("worker", "one")
            bus.emit("worker", "two")
            bus.emit("worker", "three")
            subscriber.drain_once(limit=None)
            lines, discarded = subscriber.snapshot()
            self.assertEqual(len(lines), 2)
            self.assertEqual(discarded, 1)
            self.assertIn("two", lines[0])
            self.assertIn("three", lines[1])
        finally:
            subscriber.stop(drain=False)

    def test_memory_buffer_replay(self):
        stream = io.StringIO()
        subscriber = MemoryBufferSubscriber(capacity=2)
        try:
            bus.emit("system", "hello", level="warn")
            subscriber.drain_once(limit=None)
            replayed = subscriber.replay(stream=stream)
            self.assertEqual(replayed, 1)
            output = stream.getvalue()
            self.assertIn("=== Recent Logs ===", output)
            self.assertIn("[WARN][system] hello", output)
        finally:
            subscriber.stop(drain=False)


if __name__ == "__main__":
    unittest.main()
