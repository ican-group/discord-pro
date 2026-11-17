"""oauth_state and webhook_auth (spec 008 Stages 6 and 7). Offline; synthetic values."""
import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone

from tests.unit import _support  # noqa: F401
import oauth_state as o
import webhook_auth as w

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


class FakeStates:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = dict(doc)

    async def find_one_and_delete(self, flt):
        doc = self.docs.get(flt["_id"])
        if doc and doc.get("provider") == flt.get("provider"):
            return self.docs.pop(flt["_id"])
        return None


class OAuthStateTests(unittest.TestCase):
    def test_states_are_random_well_formed_and_stored_hashed(self):
        states = {o.new_state() for _ in range(500)}
        self.assertEqual(len(states), 500)
        s = next(iter(states))
        self.assertTrue(o.is_well_formed(s))
        rec = o.state_record(s, provider="discord", mode=o.MODE_INSTALL, user_id="u", instance_id="i", now=NOW)
        self.assertNotIn(s, repr(rec))
        self.assertEqual(rec["_id"], o.state_key(s))
        self.assertEqual(rec["expires_at"] - rec["created_at"], timedelta(minutes=10))
        share = o.state_record(s, provider="discord", mode=o.MODE_SHARE, user_id="u", instance_id="i", now=NOW)
        self.assertEqual(share["expires_at"] - NOW, timedelta(days=7))
        with self.assertRaises(ValueError):
            o.state_record(s, provider="discord", mode="admin", user_id="u", instance_id="i")

    def test_malformed_and_legacy_states(self):
        for bad in ("bot-a", "share:bot-a", "", None, 5, "a" * 39, "a" * 101, "a" * 40 + "!"):
            with self.subTest(bad=bad):
                self.assertFalse(o.is_well_formed(bad))

    def test_liveness(self):
        rec = o.state_record(o.new_state(), provider="discord", mode=o.MODE_INSTALL, user_id="u", instance_id="i", now=NOW)
        self.assertTrue(o.is_live(rec, "discord", now=NOW + timedelta(minutes=9)))
        self.assertFalse(o.is_live(rec, "discord", now=NOW + timedelta(minutes=10)))
        self.assertFalse(o.is_live(rec, "zendesk", now=NOW))
        self.assertFalse(o.is_live({**rec, "mode": "other"}, "discord", now=NOW))
        self.assertFalse(o.is_live({**rec, "expires_at": "2999-01-01"}, "discord", now=NOW))
        naive = {**rec, "expires_at": rec["expires_at"].replace(tzinfo=None)}
        self.assertTrue(o.is_live(naive, "discord", now=NOW))
        self.assertFalse(o.is_live(None, "discord"))

    def test_issue_and_single_use_consume(self):
        col = FakeStates()

        async def flow():
            state = await o.issue(col, provider="discord", mode=o.MODE_SHARE, user_id="u", instance_id="bot-a")
            first = await o.consume(col, state, "discord")
            second = await o.consume(col, state, "discord")
            wrong_provider_state = await o.issue(col, provider="discord", mode=o.MODE_SHARE, user_id="u", instance_id="x")
            other = await o.consume(col, wrong_provider_state, "zendesk")
            return first, second, other, await o.consume(col, "not-a-state", "discord")
        first, second, other, malformed = asyncio.run(flow())
        self.assertEqual((first["instance_id"], first["mode"]), ("bot-a", "share"))
        self.assertIsNone(second)
        self.assertIsNone(other)
        self.assertIsNone(malformed)


class ZendeskSignatureTests(unittest.TestCase):
    SECRET = "zendesk-signing-secret-synthetic-0001"
    BODY = b'{"ticket_id": "42"}'

    def sign(self, ts, body=None, secret=None):
        return w.zendesk_signature(secret or self.SECRET, ts, self.BODY if body is None else body)

    def test_valid(self):
        ts = NOW.isoformat()
        self.assertTrue(w.zendesk_signature_valid(self.SECRET, ts, self.BODY, self.sign(ts), now=NOW))
        z = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(w.zendesk_signature_valid(self.SECRET, z, self.BODY, self.sign(z), now=NOW))
        epoch = str(int(NOW.timestamp()))
        self.assertTrue(w.zendesk_signature_valid(self.SECRET, epoch, self.BODY, self.sign(epoch), now=NOW))

    def test_forged_tampered_and_replayed(self):
        ts = NOW.isoformat()
        good = self.sign(ts)
        cases = [
            (self.SECRET, ts, self.BODY + b" ", good),                      # body changed
            (self.SECRET, (NOW + timedelta(seconds=1)).isoformat(), self.BODY, good),   # timestamp changed
            ("another-secret-value-000000", ts, self.BODY, good),          # wrong secret
            (self.SECRET, ts, self.BODY, "AAAA"), (self.SECRET, ts, self.BODY, ""), (self.SECRET, ts, self.BODY, None),
            (None, ts, self.BODY, good), ("", ts, self.BODY, good), (self.SECRET, None, self.BODY, good),
            (self.SECRET, "not-a-time", self.BODY, self.sign("not-a-time")),
        ]
        for secret, t, body, sig in cases:
            with self.subTest(t=t, sig=str(sig)[:6]):
                self.assertFalse(w.zendesk_signature_valid(secret, t, body, sig, now=NOW))

    def test_replay_window(self):
        for delta in (timedelta(minutes=6), -timedelta(minutes=6), timedelta(days=1)):
            ts = (NOW - delta).isoformat()
            with self.subTest(delta=delta):
                self.assertFalse(w.zendesk_signature_valid(self.SECRET, ts, self.BODY, self.sign(ts), now=NOW))
        ts = (NOW - timedelta(minutes=4)).isoformat()
        self.assertTrue(w.zendesk_signature_valid(self.SECRET, ts, self.BODY, self.sign(ts), now=NOW))

    def test_telegram_secret(self):
        self.assertTrue(w.telegram_secret_valid("abc_DEF-123", "abc_DEF-123"))
        for bad in ("abc_DEF-12", "", None, "ABC_DEF-123"):
            self.assertFalse(w.telegram_secret_valid("abc_DEF-123", bad))
        self.assertFalse(w.telegram_secret_valid("", ""))


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "fastapi not installed")
class BodyLimitTests(unittest.TestCase):
    class Req:
        def __init__(self, chunks, length=None):
            self._chunks = chunks
            self.headers = {} if length is None else {"content-length": str(length)}

        async def stream(self):
            for c in self._chunks:
                yield c

    def test_limits(self):
        from fastapi import HTTPException
        self.assertEqual(asyncio.run(w.read_body_limited(self.Req([b"ab", b"cd"]), 10)), b"abcd")
        for req in (self.Req([b"x"], length=11), self.Req([b"x" * 6, b"x" * 6]), self.Req([b"x"], length="nan")):
            with self.assertRaises(HTTPException):
                asyncio.run(w.read_body_limited(req, 10))


if __name__ == "__main__":
    unittest.main()
