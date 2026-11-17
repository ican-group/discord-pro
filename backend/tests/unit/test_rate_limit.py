"""Rate limiting and MongoDB attempt counters (spec 008, R-RL.1–R-RL.5).

No MongoDB is used. ``PipelineCollection`` evaluates the exact aggregation
pipeline that ``MongoCounter`` sends, atomically per call (as MongoDB applies a
single-document update atomically), so the tests exercise the real update logic.
"""
import asyncio
import copy
import importlib.util
import io
import threading
import unittest
from datetime import datetime, timedelta, timezone

from tests.unit import _support  # noqa: F401
import manage
import rate_limit as rl

T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# ── Minimal pipeline evaluator (only the operators MongoCounter uses) ───────
_MISSING = object()


def _bson_type(v):
    if v is _MISSING:
        return "missing"
    if isinstance(v, datetime):
        return "date"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, str):
        return "string"
    return "object"


def _eval(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        return doc.get(expr[1:], _MISSING)
    if isinstance(expr, dict) and len(expr) == 1:
        (op, arg), = expr.items()
        if op == "$cond":
            return _eval(arg[1] if _eval(arg[0], doc) else arg[2], doc)
        if op == "$and":
            return all(_eval(a, doc) for a in arg)
        if op == "$eq":
            return _eval(arg[0], doc) == _eval(arg[1], doc)
        if op == "$type":
            return _bson_type(_eval(arg, doc))
        if op == "$gt":
            a, b = _eval(arg[0], doc), _eval(arg[1], doc)
            if a is _MISSING or b is _MISSING or _bson_type(a) != _bson_type(b):
                return False  # BSON cross-type ordering never makes missing/string > date
            if isinstance(a, datetime):
                a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
                b = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
            return a > b
        if op == "$add":
            return sum(_eval(a, doc) for a in arg)
        if op == "$ifNull":
            v = _eval(arg[0], doc)
            return _eval(arg[1], doc) if v is _MISSING or v is None else v
        raise AssertionError(f"unsupported operator {op}")
    return expr


class DupKey(Exception):
    pass


class PipelineCollection:
    def __init__(self, naive_dates=False):
        self.docs = {}
        self.calls = []
        self.fail_next_upserts = 0
        self.naive_dates = naive_dates  # Motor without tz_aware returns naive UTC
        self._lock = asyncio.Lock()

    def _out(self, doc):
        doc = copy.deepcopy(doc)
        if self.naive_dates:
            for k, v in doc.items():
                if isinstance(v, datetime):
                    doc[k] = v.astimezone(timezone.utc).replace(tzinfo=None)
        return doc

    async def find_one(self, flt):
        self.calls.append("find_one")
        doc = self.docs.get(flt["_id"])
        return None if doc is None else self._out(doc)

    async def find_one_and_update(self, flt, pipeline, upsert=False, return_document=False):
        self.calls.append("find_one_and_update")
        assert isinstance(pipeline, list) and upsert and return_document is True
        await asyncio.sleep(0)  # let other coroutines interleave before the atomic step
        async with self._lock:
            _id = flt["_id"]
            if _id not in self.docs and self.fail_next_upserts:
                self.fail_next_upserts -= 1
                self.docs[_id] = {"_id": _id}  # the competing upsert won the insert
                raise DupKey()
            current = self.docs.get(_id, {"_id": _id})
            new = dict(current)
            for stage in pipeline:
                (op, fields), = stage.items()
                assert op == "$set"
                computed = {k: _eval(v, current) for k, v in fields.items()}
                new.update(computed)
            self.docs[_id] = new
            return self._out(new)

    async def delete_one(self, flt):
        self.calls.append("delete_one")
        self.docs.pop(flt["_id"], None)


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def counter(col, clock, limit=5, window=900, scope="login"):
    return rl.MongoCounter(col, scope, limit, window, now_fn=clock, duplicate_key_errors=(DupKey,))


# ── Sliding window ───────────────────────────────────────────────────────────
class SlidingWindowTests(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.lim = rl.SlidingWindowLimiter(3, 60, clock=lambda: self.t[0])

    def test_threshold(self):
        self.assertTrue(all(self.lim.hit("k").allowed for _ in range(3)))
        d = self.lim.hit("k")
        self.assertFalse(d.allowed)
        self.assertEqual(d.retry_after, 60)

    def test_denied_attempts_are_not_recorded(self):
        for _ in range(3):
            self.lim.hit("k")
        for _ in range(10):
            self.assertFalse(self.lim.hit("k").allowed)
        self.t[0] += 60.001
        self.assertTrue(self.lim.hit("k").allowed)

    def test_sliding_not_fixed_window(self):
        self.lim.hit("k")            # t=1000
        self.t[0] += 30
        self.lim.hit("k")            # t=1030
        self.lim.hit("k")            # t=1030
        self.t[0] += 30.5            # t=1060.5: the t=1000 hit has expired
        d = self.lim.hit("k")
        self.assertTrue(d.allowed)
        self.assertFalse(self.lim.hit("k").allowed)
        self.assertEqual(self.lim.peek("k").retry_after, 30)  # oldest live hit (1030) + 60 - 1060.5

    def test_keys_independent_and_reset(self):
        for _ in range(3):
            self.lim.hit("a")
        self.assertTrue(self.lim.hit("b").allowed)
        self.lim.reset("a")
        self.assertTrue(self.lim.hit("a").allowed)

    def test_peek_does_not_record(self):
        for _ in range(10):
            self.assertTrue(self.lim.peek("k").allowed)
        self.assertEqual(self.lim.hit("k").count, 1)

    def test_memory_bounded(self):
        lim = rl.SlidingWindowLimiter(1, 60, clock=lambda: self.t[0], max_keys=100)
        for i in range(1000):
            lim.hit(f"ip-{i}")
        self.assertLessEqual(len(lim), 100)

    def test_concurrent_threads_never_exceed_limit(self):
        lim = rl.SlidingWindowLimiter(100, 60)
        allowed = []
        lock = threading.Lock()
        start = threading.Barrier(20)

        def worker():
            start.wait()
            n = sum(1 for _ in range(50) if lim.hit("shared").allowed)
            with lock:
                allowed.append(n)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(allowed), 100)

    def test_invalid_parameters(self):
        for args in ((0, 60), (1, 0), (1, -1)):
            with self.assertRaises(ValueError):
                rl.SlidingWindowLimiter(*args)


# ── MongoDB counters ─────────────────────────────────────────────────────────
class MongoCounterTests(unittest.TestCase):
    def setUp(self):
        self.col = PipelineCollection()
        self.clock = Clock()
        self.c = counter(self.col, self.clock)

    def test_threshold_blocks_after_limit_failures(self):
        for i in range(1, 5):
            d = run(self.c.record_failure("acct"))
            self.assertTrue(d.allowed, i)
            self.assertEqual(d.count, i)
        d = run(self.c.record_failure("acct"))
        self.assertFalse(d.allowed)
        self.assertEqual(d.count, 5)
        self.assertEqual(d.retry_after, 900)
        self.assertFalse(run(self.c.check("acct")).allowed)

    def test_window_not_extended_by_further_failures(self):
        for _ in range(5):
            run(self.c.record_failure("acct"))
        self.clock.advance(600)
        d = run(self.c.record_failure("acct"))
        self.assertFalse(d.allowed)
        self.assertEqual(d.retry_after, 300)

    def test_expiry_checked_explicitly_even_if_ttl_has_not_deleted(self):
        for _ in range(5):
            run(self.c.record_failure("acct"))
        self.clock.advance(900)  # exactly at expires_at: no longer live
        self.assertIn("login:acct", self.col.docs)  # document still present (TTL not run)
        self.assertTrue(run(self.c.check("acct")).allowed)
        d = run(self.c.record_failure("acct"))
        self.assertTrue(d.allowed)
        self.assertEqual(d.count, 1)  # fresh window, not 6
        self.assertEqual(rl._as_utc(self.col.docs["login:acct"]["expires_at"]), self.clock.now + timedelta(seconds=900))

    def test_stale_document_with_huge_count_is_ignored(self):
        self.col.docs["login:acct"] = {"_id": "login:acct", "count": 10_000,
                                       "expires_at": T0 - timedelta(days=3)}
        self.assertTrue(run(self.c.check("acct")).allowed)
        self.assertEqual(run(self.c.record_failure("acct")).count, 1)

    def test_malformed_expiry_starts_fresh_window(self):
        self.col.docs["login:acct"] = {"_id": "login:acct", "count": 99, "expires_at": "2999-01-01"}
        self.assertTrue(run(self.c.check("acct")).allowed)
        self.assertEqual(run(self.c.record_failure("acct")).count, 1)

    def test_naive_datetimes_from_motor_handled(self):
        c = counter(PipelineCollection(naive_dates=True), self.clock)
        for _ in range(5):
            last = run(c.record_failure("acct"))
        self.assertFalse(last.allowed)
        self.assertFalse(run(c.check("acct")).allowed)
        self.clock.advance(901)
        self.assertTrue(run(c.check("acct")).allowed)

    def test_reset(self):
        for _ in range(5):
            run(self.c.record_failure("acct"))
        run(self.c.reset("acct"))
        self.assertTrue(run(self.c.check("acct")).allowed)
        self.assertNotIn("login:acct", self.col.docs)

    def test_scopes_and_keys_isolated(self):
        other = counter(self.col, self.clock, scope="reset_code")
        for _ in range(5):
            run(self.c.record_failure("acct"))
        self.assertTrue(run(other.check("acct")).allowed)
        self.assertTrue(run(self.c.check("someone-else")).allowed)

    def test_concurrent_increments_are_not_lost(self):
        async def burst():
            return await asyncio.gather(*(self.c.record_failure("acct") for _ in range(50)))
        results = run(burst())
        self.assertEqual(sorted(d.count for d in results), list(range(1, 51)))
        self.assertEqual(self.col.docs["login:acct"]["count"], 50)
        self.assertEqual(sum(1 for d in results if d.allowed), 4)

    def test_record_failure_is_a_single_atomic_update(self):
        run(self.c.record_failure("acct"))
        self.assertEqual(self.col.calls, ["find_one_and_update"])  # no read-modify-write

    def test_concurrent_first_insert_duplicate_key_is_retried(self):
        self.col.fail_next_upserts = 1
        d = run(self.c.record_failure("acct"))
        self.assertEqual(d.count, 1)
        self.assertEqual(self.col.calls, ["find_one_and_update", "find_one_and_update"])

    def test_persistent_duplicate_key_error_propagates(self):
        class AlwaysDup(PipelineCollection):
            async def find_one_and_update(self, *a, **k):
                self.calls.append("find_one_and_update")
                raise DupKey()
        c = counter(AlwaysDup(), self.clock)
        with self.assertRaises(DupKey):
            run(c.record_failure("acct"))

    def test_stored_document_contains_no_raw_identifier(self):
        key = rl.account_key("Victim@Example.com")
        run(self.c.record_failure(key))
        blob = repr(self.col.docs)
        self.assertNotIn("victim", blob.lower())
        self.assertNotIn("example.com", blob.lower())

    def test_invalid_parameters(self):
        for kwargs in ({"limit": 0}, {"window": 0}, {"scope": ""}, {"scope": "a:b"}):
            args = {"limit": 5, "window": 60, "scope": "login", **kwargs}
            with self.assertRaises(ValueError):
                rl.MongoCounter(self.col, args["scope"], args["limit"], args["window"])


class PipelineShapeTests(unittest.TestCase):
    def test_pipeline_uses_explicit_expiry_check(self):
        pipe = rl.increment_pipeline("login", T0, 60)
        text = repr(pipe)
        self.assertIn("$expires_at", text)
        self.assertIn("'$type'", text)
        self.assertEqual(pipe[0]["$set"]["updated_at"], T0)


# ── Uniform 429 and account keys ─────────────────────────────────────────────
class UniformResponseTests(unittest.TestCase):
    def test_account_key_normalised_and_existence_independent(self):
        a = rl.account_key("User@Example.COM ")
        self.assertEqual(a, rl.account_key("user@example.com"))
        self.assertRegex(a, r"^[0-9a-f]{64}$")
        self.assertNotEqual(a, rl.account_key("other@example.com"))

    def test_headers_clamped(self):
        self.assertEqual(rl.rate_limit_headers(0), {"Retry-After": "1"})
        self.assertEqual(rl.rate_limit_headers(10**9), {"Retry-After": str(rl.MAX_RETRY_AFTER_SECONDS)})

    def test_detail_is_generic(self):
        low = rl.RATE_LIMIT_DETAIL.lower()
        for word in ("account", "email", "user", "exist", "locked", "password", "code"):
            self.assertNotIn(word, low)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "starlette not installed")
    def test_response_identical_apart_from_retry_after(self):
        a, b = rl.too_many_requests(5), rl.too_many_requests(5)
        self.assertEqual(a.status_code, 429)
        self.assertEqual(a.body, b.body)
        self.assertEqual(a.headers["retry-after"], "5")

    @unittest.skipUnless(importlib.util.find_spec("fastapi"), "fastapi not installed")
    def test_exception_variant(self):
        e = rl.too_many_requests_exception(7)
        self.assertEqual((e.status_code, e.detail, e.headers), (429, rl.RATE_LIMIT_DETAIL, {"Retry-After": "7"}))


# ── manage.py security-indexes (dry run by default; no other writes) ────────
class FakeIndexCollection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    async def index_information(self):
        return copy.deepcopy(self.db.indexes.get(self.name, {}))

    async def create_index(self, keys, name, expireAfterSeconds):
        self.db.writes.append(("create_index", self.name, name))
        self.db.collections.add(self.name)
        self.db.indexes.setdefault(self.name, {"_id_": {"key": [("_id", 1)]}})[name] = {
            "key": list(keys), "expireAfterSeconds": expireAfterSeconds}
        return name


class FakeIndexDB:
    def __init__(self):
        self.collections, self.indexes, self.writes = set(), {}, []

    async def list_collection_names(self, filter=None):
        return [n for n in self.collections if not filter or n == filter.get("name")]

    def __getitem__(self, name):
        return FakeIndexCollection(self, name)


class SecurityIndexesCommandTests(unittest.TestCase):
    def _run(self, db, **kw):
        out = io.StringIO()
        code = run(manage.security_indexes(db, out=out, **kw))
        return code, out.getvalue()

    def test_dry_run_by_default_writes_nothing(self):
        db = FakeIndexDB()
        code, out = self._run(db, apply=False, confirm=False)
        self.assertEqual(code, manage.EXIT_DRY_RUN)
        self.assertEqual(db.writes, [])
        self.assertIn("DRY RUN", out)

    def test_apply_requires_confirm(self):
        db = FakeIndexDB()
        code, _ = self._run(db, apply=True, confirm=False)
        self.assertEqual(code, manage.EXIT_DRY_RUN)
        self.assertEqual(db.writes, [])

    def test_apply_confirm_creates_only_the_ttl_index(self):
        db = FakeIndexDB()
        code, out = self._run(db, apply=True, confirm=True)
        self.assertEqual(code, manage.EXIT_OK)
        self.assertEqual(db.writes, [("create_index", "security_counters", rl.TTL_INDEX_NAME),
                                     ("create_index", "oauth_states", "oauth_states_expires_at_ttl")])
        for col, name in rl.TTL_INDEXES:
            self.assertEqual(db.indexes[col][name], {"key": [("expires_at", 1)], "expireAfterSeconds": 0})

    def test_idempotent_when_present(self):
        db = FakeIndexDB()
        self._run(db, apply=True, confirm=True)
        db.writes.clear()
        code, out = self._run(db, apply=True, confirm=True)
        self.assertEqual(code, manage.EXIT_OK)
        self.assertEqual(db.writes, [])

    def test_conflicting_existing_index_refused(self):
        db = FakeIndexDB()
        db.collections.add("security_counters")
        db.indexes["security_counters"] = {rl.TTL_INDEX_NAME: {"key": [("expires_at", 1)], "expireAfterSeconds": 60}}
        code, out = self._run(db, apply=True, confirm=True)
        self.assertEqual(code, manage.EXIT_REFUSED)
        self.assertEqual(db.writes, [])

    def test_cli_parses_security_indexes(self):
        args = manage._parser().parse_args(["security-indexes"])
        self.assertEqual((args.command, args.apply, args.confirm), ("security-indexes", False, False))
        with self.assertRaises(SystemExit):
            manage._parser().parse_args(["security-indexes", "--app"])  # no abbreviations


class NoStartupIndexCreationTests(unittest.TestCase):
    """R-RL.7: the security_counters TTL index is never created by the API itself."""

    def test_server_never_creates_the_ttl_index(self):
        from tests.unit._support import BACKEND_DIR
        src = (BACKEND_DIR / "server.py").read_text(encoding="utf-8")
        for needle in ("ensure_ttl_index", "security_counters\"].create_index", "COUNTERS).create_index",
                       "_COUNTERS.create_index"):
            self.assertNotIn(needle, src)

    def test_keyed_account_keys(self):
        k1, k2 = b"k" * 32, b"j" * 32
        a = rl.account_key("User@Example.com", k1)
        self.assertEqual(a, rl.account_key("user@example.com", k1))
        self.assertNotEqual(a, rl.account_key("user@example.com", k2))
        self.assertNotEqual(a, rl.account_key("user@example.com"))  # keyed differs from unkeyed
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_clear(self):
        lim = rl.SlidingWindowLimiter(1, 60)
        lim.hit("a")
        self.assertFalse(lim.hit("a").allowed)
        lim.clear()
        self.assertTrue(lim.hit("a").allowed)

if __name__ == "__main__":
    unittest.main()
