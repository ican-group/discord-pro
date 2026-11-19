"""F-A: atomic sliding-window throttle (core)."""
import asyncio
import unittest
from datetime import datetime, timedelta

from tests.unit._support import BACKEND_DIR  # noqa: F401


def run(coro):
    return asyncio.run(coro)


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        from core.hierarchy import InMemoryStore
        self.store = InMemoryStore()
        self.t0 = datetime.fromisoformat("2026-11-18T10:00:00+00:00")       # aligned to a 60 s window

    def at(self, seconds):
        return (self.t0 + timedelta(seconds=seconds)).isoformat()

    async def hits(self, n, *, at, key="k", limit=10, window=60):
        from core import throttle
        return await asyncio.gather(*[throttle.hit(self.store, key, limit=limit, window_seconds=window, now=at)
                                      for _ in range(n)])

    def test_exact_limit_under_concurrency(self):
        res = run(self.hits(300, at=self.at(5), limit=50))
        self.assertEqual(sum(d.allowed for d in res), 50)                       # never more, never fewer

    def test_boundary_burst_is_refused(self):
        """A plain fixed window allows 2x the limit around its boundary; the
        sliding estimate does not."""
        async def scenario():
            first = await self.hits(10, at=self.at(59))                          # end of window 0
            second = await self.hits(10, at=self.at(61))                          # start of window 1
            return first, second
        first, second = run(scenario())
        self.assertEqual(sum(d.allowed for d in first), 10)
        self.assertLessEqual(sum(d.allowed for d in second), 1)                  # ~ (1 - 1/60) * 10 still counted

    def test_burst_straddling_a_boundary_never_exceeds_the_limit(self):
        """Phase 6A / B1 root cause: attempts that straddle a window boundary
        (e.g. a 15-minute login window at hh:15:00) must still be refused at
        the limit."""
        from core import throttle

        async def scenario():
            await self.hits(29, at=(self.t0 + timedelta(seconds=59.6)).isoformat(), limit=30)
            await self.hits(1, at=(self.t0 + timedelta(seconds=60.2)).isoformat(), limit=30)
            return await throttle.blocked(self.store, "k", limit=30, window_seconds=60,
                                          now=(self.t0 + timedelta(seconds=60.4)).isoformat())
        self.assertFalse(run(scenario()).allowed)                                # 30 attempts made: refuse the 31st

    def test_boundary_sweep_never_allows_more_than_the_limit(self):
        """Phase 6B regression (Phase 6A gate run 1): every split of `limit` attempts
        across a window boundary, at many sub-second offsets, must leave the key
        blocked. The pre-fix estimate (fractional previous weight, no ceiling)
        allowed one extra attempt for most of these splits."""
        from core import throttle

        async def scenario(split, before, after, limit, window):
            store = type(self.store)()
            b = self.t0 + timedelta(seconds=window)                              # the boundary
            for _ in range(split):
                await throttle.hit(store, "k", limit=limit, window_seconds=window,
                                   now=(b - timedelta(seconds=before)).isoformat())
            for _ in range(limit - split):
                await throttle.hit(store, "k", limit=limit, window_seconds=window,
                                   now=(b + timedelta(seconds=after)).isoformat())
            return await throttle.blocked(store, "k", limit=limit, window_seconds=window,
                                          now=(b + timedelta(seconds=after + 0.05)).isoformat())

        for window, limit in ((60, 30), (900, 30), (3600, 5)):
            for split in range(limit + 1):
                for before, after in ((0.01, 0.01), (0.4, 0.2), (2.0, 1.5), (window / 3, 0.7)):
                    d = run(scenario(split, before, after, limit, window))
                    self.assertFalse(d.allowed, (window, limit, split, before, after))

    def test_login_window_at_quarter_hour_mirrors_the_gate_failure(self):
        """The exact Phase 6A failure shape: 30 login attempts (15-minute window)
        made from hh:14:58 to hh:15:00.x; the 31st must be refused."""
        from core import throttle

        async def scenario():
            t = datetime.fromisoformat("2026-11-18T23:14:58+00:00")
            for i in range(30):
                now = (t + timedelta(milliseconds=100 * i)).isoformat()            # 23:14:58.0 → 23:15:00.9
                await throttle.hit(self.store, "login", limit=30, window_seconds=900, now=now)
            return await throttle.hit(self.store, "login", limit=30, window_seconds=900,
                                      now=(t + timedelta(seconds=3.2)).isoformat())
        self.assertFalse(run(scenario()).allowed)

    def test_previous_window_weight_decays(self):
        async def scenario():
            await self.hits(10, at=self.at(30))
            mid = await self.hits(10, at=self.at(90))                            # half of the previous counts
            late = await self.hits(10, at=self.at(179))                          # previous window: window 1 (mid)
            return mid, late
        mid, late = run(scenario())
        self.assertEqual(sum(d.allowed for d in mid), 5)
        self.assertGreaterEqual(sum(d.allowed for d in late), 9)

    def test_refused_attempts_are_not_counted_and_blocked_agrees(self):
        from core import throttle

        async def scenario():
            await self.hits(15, at=self.at(10), limit=10)
            d = await throttle.blocked(self.store, "k", limit=10, window_seconds=60, now=self.at(11))
            doc = await self.store.get(throttle.COLLECTION, "k@" + str(int(self.t0.timestamp() // 60)))
            return d, doc
        d, doc = run(scenario())
        self.assertFalse(d.allowed)
        self.assertEqual(doc["count"], 10)

    def test_counters_expire_after_two_windows_and_keys_are_isolated(self):
        from core import throttle

        async def scenario():
            await self.hits(10, at=self.at(10), key="tenant-a")
            b = await self.hits(10, at=self.at(10), key="tenant-b")
            doc = await self.store.get(throttle.COLLECTION, "tenant-a@" + str(int(self.t0.timestamp() // 60)))
            return b, doc
        b, doc = run(scenario())
        self.assertEqual(sum(d.allowed for d in b), 10)                         # another key is unaffected
        self.assertEqual(doc["expires_at_dt"], self.t0 + timedelta(seconds=120))
        self.assertEqual(doc["key"], "tenant-a")

    def test_reset_clears_every_window(self):
        from core import throttle

        async def scenario():
            await self.hits(10, at=self.at(10))
            await throttle.reset(self.store, "k")
            return await self.hits(1, at=self.at(11))
        self.assertTrue(run(scenario())[0].allowed)


if __name__ == "__main__":
    unittest.main()
