from __future__ import annotations

import unittest
from unittest import mock

from qq_mail_pool import QQMailPool, _IdleUnsupportedError


class QQMailPoolAddressTests(unittest.TestCase):
    def make_pool(self) -> QQMailPool:
        return QQMailPool(
            host="imap.example.com",
            port=993,
            user="root@example.com",
            authcode="secret",
            domain="example.com",
            poll_interval=1,
            debug=False,
        )

    def test_acquire_email_keeps_forward_domain_style(self):
        pool = self.make_pool()
        with mock.patch.object(pool, "_random_human_local", return_value="john.smith"):
            addr = pool.acquire_email(domain="pandalabs.asia")
        self.assertEqual(addr, "john.smith@pandalabs.asia")

    def test_acquire_email_builds_suffix_alias_from_base_address(self):
        pool = self.make_pool()
        with mock.patch.object(pool, "_random_human_local", return_value="john.smith"):
            addr = pool.acquire_email(base_address="12345@2925.com")
        self.assertEqual(addr, "12345john_smith@2925.com")

    def test_detect_idle_support_from_capability(self):
        pool = self.make_pool()
        imap = mock.Mock()
        imap.capability.return_value = ("OK", [b"IMAP4rev1 IDLE AUTH=PLAIN"])
        self.assertTrue(pool._detect_idle_support(imap))

        imap.capability.return_value = ("OK", [b"IMAP4rev1 AUTH=PLAIN"])
        self.assertFalse(pool._detect_idle_support(imap))

    def test_idle_wait_marks_server_as_unsupported(self):
        pool = self.make_pool()

        class FakeImap:
            def _new_tag(self):
                return b"A001"

            def send(self, data):
                return None

            def readline(self):
                return b"A001 BAD Error: Command 'IDLE' not recognized.\r\n"

        with self.assertRaises(_IdleUnsupportedError):
            pool._idle_wait(FakeImap(), 30)

    def test_loop_falls_back_to_polling_when_idle_is_unsupported(self):
        pool = self.make_pool()
        pool._idle_supported = False
        pool._last_uid = 1

        class FakeStop:
            def __init__(self):
                self.wait_calls = []
                self._is_set = False

            def is_set(self):
                return self._is_set

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                self._is_set = True
                return True

        fake_stop = FakeStop()
        fake_imap = mock.Mock()
        pool._stop = fake_stop

        with mock.patch.object(pool, "_connect", return_value=fake_imap), \
                mock.patch.object(pool, "_select_folder"), \
                mock.patch.object(pool, "_poll_once") as poll_once, \
                mock.patch.object(pool, "_idle_wait") as idle_wait:
            pool._loop()

        poll_once.assert_called_once_with(fake_imap)
        idle_wait.assert_not_called()
        self.assertEqual(fake_stop.wait_calls, [pool.poll_interval])
        fake_imap.logout.assert_called_once()

    def test_debug_logs_are_suppressed_below_threshold(self):
        messages = []
        pool = QQMailPool(
            host="imap.example.com",
            port=993,
            user="root@example.com",
            authcode="secret",
            domain="example.com",
            poll_interval=1,
            debug=False,
            log=messages.append,
            log_level="info",
        )

        pool._log("hidden")

        self.assertEqual(messages, [])

    def test_debug_logs_emit_with_level_aware_callback(self):
        messages = []

        def capture(message, *, level="info"):
            messages.append((level, message))

        pool = QQMailPool(
            host="imap.example.com",
            port=993,
            user="root@example.com",
            authcode="secret",
            domain="example.com",
            poll_interval=1,
            debug=False,
            log=capture,
            log_level="debug",
        )

        pool._log("visible")

        self.assertEqual(messages, [("debug", "[QQMailPool] visible")])


if __name__ == "__main__":
    unittest.main()
