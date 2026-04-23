from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from phone_pool import PhonePool, PhonePoolCapacityExhausted
from sms_provider import SmsSession


class FakeProvider:
    def __init__(self):
        self.api_key = "test-key"
        self._next = 0

    def acquire(self, **kwargs):
        self._next += 1
        n = self._next
        return SmsSession(
            provider="herosms",
            number=f"1555000{n:03d}",
            handle=f"act-{n}",
            locale="US",
            cost=0.05,
            extra={"activationEndTime": "2099-01-01T00:00:00+00:00", "service": "oai"},
        )

    def release_ok(self, session):
        return None

    def release_no_sms(self, session):
        return None

    def release_bad(self, session, reason=""):
        return None


class PhonePoolTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="phone-pool-", suffix=".db")
        os.close(fd)
        self.provider = FakeProvider()

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def make_pool(self, **kwargs):
        return PhonePool(
            self.provider,
            db_path=self.db_path,
            max_reuse=kwargs.pop("max_reuse", 2),
            max_active=kwargs.pop("max_active", 0),
            acquire_timeout=kwargs.pop("acquire_timeout", 0.5),
            lease_seconds=kwargs.pop("lease_seconds", 1),
            heartbeat_seconds=kwargs.pop("heartbeat_seconds", 1),
            log=lambda _: None,
        )

    def test_stats_and_reuse_rate(self):
        pool = self.make_pool()
        lease = pool.acquire_or_reuse()
        stats = pool.stats()
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["fresh_total"], 1)
        self.assertEqual(len(stats["leases"]), 1)

        lease.mark_used("sms-1", "123456", "acct-1")
        stats = pool.stats()
        self.assertEqual(stats["leases"], [])
        self.assertEqual(stats["reuse_total"], 0)

        reused = pool.acquire_or_reuse()
        stats = pool.stats()
        self.assertTrue(reused.is_reused)
        self.assertEqual(stats["reuse_total"], 1)
        self.assertAlmostEqual(stats["reuse_rate"], 0.5)

    def test_reuse_bypasses_cap(self):
        pool = self.make_pool(max_active=1, acquire_timeout=0.2)
        lease = pool.acquire_or_reuse()
        lease.mark_used("sms-1", "123456", "acct-1")
        reused = pool.acquire_or_reuse()
        self.assertTrue(reused.is_reused)

    def test_mark_dead_unblocks_waiter(self):
        pool = self.make_pool(max_active=1, acquire_timeout=2.0)
        lease = pool.acquire_or_reuse()
        result = {}

        def worker():
            result["lease"] = pool.acquire_or_reuse()

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.2)
        lease.mark_dead("reject")
        thread.join(timeout=1.5)
        self.assertFalse(thread.is_alive())
        self.assertIn("lease", result)

    def test_timeout_raises_capacity_exhausted(self):
        pool = self.make_pool(max_active=1, acquire_timeout=0.2)
        _ = pool.acquire_or_reuse()
        with self.assertRaises(PhonePoolCapacityExhausted):
            pool.acquire_or_reuse()

    def test_unlimited_mode_matches_old_behavior(self):
        pool = self.make_pool(max_active=0)
        first = pool.acquire_or_reuse()
        second = pool.acquire_or_reuse()
        self.assertFalse(first.is_reused)
        self.assertFalse(second.is_reused)
        self.assertNotEqual(first.activation_id, second.activation_id)

    def test_reconcile_expiry_unblocks_waiter(self):
        pool = self.make_pool(max_active=1, acquire_timeout=2.0)
        lease = pool.acquire_or_reuse()
        result = {}

        def worker():
            result["lease"] = pool.acquire_or_reuse()

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.2)

        remote = [{
            "activationId": lease.activation_id,
            "phoneNumber": lease.phone_number,
            "activationCost": 0.05,
            "countryCode": 1,
            "serviceCode": "oai",
            "estDate": "2000-01-01 00:00:00",
        }]
        with mock.patch("herosms_pool.get_active_activations", return_value=remote), mock.patch("herosms_pool.finish_activation", return_value=None):
            pool.reconcile()
        thread.join(timeout=1.5)
        self.assertFalse(thread.is_alive())
        self.assertIn("lease", result)


if __name__ == "__main__":
    unittest.main()
