from __future__ import annotations

import unittest

from chatgpt_register.log_config import normalize_log_level, should_log


class LogConfigTests(unittest.TestCase):
    def test_normalize_log_level_uses_default_for_invalid_values(self):
        self.assertEqual(normalize_log_level(None), "info")
        self.assertEqual(normalize_log_level(""), "info")
        self.assertEqual(normalize_log_level("verbose"), "info")
        self.assertEqual(normalize_log_level("warning"), "warn")
        self.assertEqual(normalize_log_level("ERR"), "error")

    def test_should_log_honors_threshold(self):
        self.assertTrue(should_log("error", "info"))
        self.assertTrue(should_log("success", "info"))
        self.assertFalse(should_log("debug", "info"))
        self.assertFalse(should_log("info", "warn"))
        self.assertTrue(should_log("warn", "warn"))


if __name__ == "__main__":
    unittest.main()
