from __future__ import annotations

import io
import unittest

from monitor import bus
from monitor.fallback import TextSubscriber


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


if __name__ == "__main__":
    unittest.main()
