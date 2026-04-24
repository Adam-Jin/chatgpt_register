from __future__ import annotations

import unittest

from textual.widgets import RichLog

from monitor import bus
from monitor.app import RegisterMonitorApp, VIEW_LOGS, VIEW_POOL, VIEW_WORKERS
from monitor.widgets import PoolStatsPanel, WorkerListPanel


class MonitorAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        bus.clear_current_worker()

    async def test_uppercase_shortcuts_switch_views_and_focus(self):
        app = RegisterMonitorApp(lambda: None, max_workers=2)

        async with app.run_test(size=(100, 24)) as pilot:
            self.assertEqual(app._view_mode, VIEW_LOGS)
            self.assertEqual(app.focused.id if app.focused else None, "main-log")

            await pilot.press("W")
            await pilot.pause()
            self.assertEqual(app._view_mode, VIEW_WORKERS)
            self.assertIsInstance(app.focused, WorkerListPanel)

            await pilot.press("S")
            await pilot.pause()
            self.assertEqual(app._view_mode, VIEW_POOL)
            self.assertIsInstance(app.focused, PoolStatsPanel)

            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._view_mode, VIEW_LOGS)
            self.assertIsInstance(app.focused, RichLog)

    async def test_log_review_mode_toggles_follow_state(self):
        app = RegisterMonitorApp(lambda: None, max_workers=1)

        async with app.run_test(size=(100, 20)) as pilot:
            for index in range(80):
                bus.emit("system", f"line {index}")
            await pilot.pause()

            log = app.query_one("#main-log", RichLog)
            bottom = log.max_scroll_y
            self.assertEqual(log.scroll_y, bottom)
            self.assertTrue(app._follow_logs)

            app.action_log_page_up()
            await pilot.pause()
            self.assertFalse(app._follow_logs)
            self.assertLess(log.scroll_y, bottom)

            page_up_scroll = log.scroll_y
            app.action_log_page_down()
            await pilot.pause()
            self.assertFalse(app._follow_logs)
            self.assertGreaterEqual(log.scroll_y, page_up_scroll)

            app.action_log_end()
            await pilot.pause()
            self.assertTrue(app._follow_logs)
            self.assertEqual(log.scroll_y, log.max_scroll_y)

    async def test_mouse_scroll_up_switches_log_to_review_mode(self):
        app = RegisterMonitorApp(lambda: None, max_workers=1)

        async with app.run_test(size=(100, 20)):
            log = app.query_one("#main-log", RichLog)
            app._set_follow_logs(True)
            self.assertTrue(app._follow_logs)

            app._handle_log_scroll_change(log, 30, 12)

            self.assertFalse(app._follow_logs)


if __name__ == "__main__":
    unittest.main()
