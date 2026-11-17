"""Rate limiting and attempt counters (spec 008, R-RL.1–R-RL.5).

Two layers:

* ``SlidingWindowLimiter`` — in-process, per-IP sliding-window log. Valid because
  the API runs exactly one worker (spec 004 F3). If workers or replicas are ever
  added, this must move to a shared store.
* ``MongoCounter`` — per-account / per-code failure counters in the MongoDB
  collection ``security_counters``. Each update is a single atomic
  ``find_one_and_update`` with an aggregation-pipeline update, and expiry is
  checked **explicitly** against ``expires_at`` on every read and update. The
  TTL index only garbage-collects old documents; correctness never depends on
  when (or whether) the TTL monitor deletes them.

Account-keyed limits use ``account_key()``: a one-way hash of the normalised
identifier that is computed identically whether or not the account exists, so
limits and 429 responses never reveal account existence (R-RL.3).

Nothing in this module is wired into a route yet (spec 008 Stage 1), and the
TTL index is created only by the explicit ``manage.py security-indexes`` command,
never at startup.

Standard library only at import time; Motor/pymongo and Starlette are imported
lazily where needed.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, Optional

# ── Uniform 429 ─────────────────────────────────────────────────────────────
RATE_LIMIT_STATUS = 429
RATE_LIMIT_DETAIL = "Too many requests. Please try again later."
MAX_RETRY_AFTER_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0  # whole seconds, >= 1 when not allowed
    count: int = 0


def _clamp_retry_after(seconds: float) -> int:
    return max(1, min(MAX_RETRY_AFTER_SECONDS, int(math.ceil(seconds))))


def rate_limit_headers(retry_after: int) -> Dict[str, str]:
    return {"Retry-After": str(_clamp_retry_after(retry_after))}


def too_many_requests(retry_after: int):
    """The single uniform 429 response: same body for every route and every
    account state; only ``Retry-After`` varies with the counter's window."""
    from starlette.responses import JSONResponse  # lazy: keeps the module stdlib-only
    return JSONResponse(status_code=RATE_LIMIT_STATUS, content={"detail": RATE_LIMIT_DETAIL},
                        headers=rate_limit_headers(retry_after))


def too_many_requests_exception(retry_after: int):
    from fastapi import HTTPException  # lazy
    return HTTPException(status_code=RATE_LIMIT_STATUS, detail=RATE_LIMIT_DETAIL,
                         headers=rate_limit_headers(retry_after))


# ── Keys ────────────────────────────────────────────────────────────────────
_KEY_NAMESPACE = b"bitbotpro:security-counter:v1:"


def normalise_identifier(value: str) -> str:
    return (value or "").strip().lower()


def account_key(identifier: str, key: Optional[bytes] = None) -> str:
    """One-way, existence-independent key for an account identifier (e.g. email).

    With ``key`` (the code HMAC key, spec 008 A3): HMAC-SHA256, so someone with
    database read access cannot test guessed emails against stored counters.
    Without a key: namespaced SHA-256 (Stage 1 behaviour, kept for tests/tools).
    The raw identifier is never stored in ``security_counters``.
    """
    data = _KEY_NAMESPACE + normalise_identifier(identifier).encode("utf-8")
    if key:
        import hmac as _hmac
        return _hmac.new(key, data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


# ── In-memory sliding-window limiter (per IP) ───────────────────────────────
class SlidingWindowLimiter:
    """At most ``limit`` hits per ``window_seconds`` per key (sliding log).

    Thread-safe (a lock guards each check-and-record), so concurrent callers can
    never exceed the limit. Memory is bounded by ``max_keys``: expired keys are
    purged first, then the least-recently-used key is evicted.
    """

    def __init__(self, limit: int, window_seconds: float, *,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("limit, window_seconds and max_keys must be positive")
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._clock = clock
        self._max_keys = int(max_keys)
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, dq: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def _make_room(self, now: float) -> None:
        if len(self._hits) < self._max_keys:
            return
        for key in [k for k, dq in self._hits.items() if not dq or dq[-1] <= now - self.window]:
            del self._hits[key]
        while len(self._hits) >= self._max_keys:
            self._hits.popitem(last=False)

    def hit(self, key: str) -> Decision:
        """Record one attempt for ``key`` if allowed; never records a denied attempt."""
        with self._lock:
            now = self._clock()
            dq = self._hits.get(key)
            if dq is None:
                self._make_room(now)
                dq = self._hits[key] = deque()
            else:
                self._hits.move_to_end(key)
            self._prune(dq, now)
            if len(dq) >= self.limit:
                return Decision(False, _clamp_retry_after(dq[0] + self.window - now), len(dq))
            dq.append(now)
            return Decision(True, 0, len(dq))

    def peek(self, key: str) -> Decision:
        with self._lock:
            now = self._clock()
            dq = self._hits.get(key)
            if dq is None:
                return Decision(True, 0, 0)
            self._prune(dq, now)
            if len(dq) >= self.limit:
                return Decision(False, _clamp_retry_after(dq[0] + self.window - now), len(dq))
            return Decision(True, 0, len(dq))

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        """Forget every key (tests and administrative resets only)."""
        with self._lock:
            self._hits.clear()

    def __len__(self) -> int:
        return len(self._hits)


# ── MongoDB-backed counters (per account / per code) ────────────────────────
COUNTERS_COLLECTION = "security_counters"
TTL_INDEX_NAME = "security_counters_expires_at_ttl"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Motor returns naive UTC datetimes unless tz_aware=True; normalise both."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _truncate_ms(dt: datetime) -> datetime:
    # BSON dates have millisecond precision; keep local comparisons consistent.
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def increment_pipeline(scope: str, now: datetime, window_seconds: float):
    """Aggregation-pipeline update for one atomic, expiry-aware increment.

    If the stored window is still live (``expires_at > now``) the count is
    incremented and the window kept; otherwise (missing, expired, or malformed)
    a fresh window starts at count 1. Evaluated server-side in a single
    document update, so concurrent increments cannot be lost.
    """
    expires = now + timedelta(seconds=window_seconds)
    live = {"$and": [{"$eq": [{"$type": "$expires_at"}, "date"]}, {"$gt": ["$expires_at", now]}]}
    return [{"$set": {
        "scope": scope,
        "count": {"$cond": [live, {"$add": [{"$ifNull": ["$count", 0]}, 1]}, 1]},
        "window_start": {"$cond": [live, "$window_start", now]},
        "expires_at": {"$cond": [live, "$expires_at", expires]},
        "updated_at": now,
    }}]


class MongoCounter:
    """Failure counter in ``security_counters`` for one scope (e.g. ``login``).

    ``limit`` failures are allowed within ``window_seconds``; once the count
    reaches ``limit`` the key is blocked until the window's ``expires_at``.
    """

    def __init__(self, collection, scope: str, limit: int, window_seconds: float, *,
                 now_fn: Callable[[], datetime] = _utcnow, duplicate_key_errors: tuple = ()):
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        if not scope or ":" in scope:
            raise ValueError("scope must be non-empty and must not contain ':'")
        self._col = collection
        self.scope = scope
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._now = now_fn
        self._dup_errors = duplicate_key_errors or _pymongo_duplicate_key_error()

    def _doc_id(self, key: str) -> str:
        return f"{self.scope}:{key}"

    def _decision(self, doc: Optional[Dict[str, Any]], now: datetime) -> Decision:
        expires_at = _as_utc((doc or {}).get("expires_at"))
        if not doc or expires_at is None or expires_at <= now:
            return Decision(True, 0, 0)  # no live window (TTL may not have deleted it yet)
        count = int(doc.get("count") or 0)
        if count >= self.limit:
            return Decision(False, _clamp_retry_after((expires_at - now).total_seconds()), count)
        return Decision(True, 0, count)

    async def check(self, key: str) -> Decision:
        """Read-only: is ``key`` currently blocked?"""
        now = _truncate_ms(self._now())
        doc = await self._col.find_one({"_id": self._doc_id(key)})
        return self._decision(doc, now)

    async def record_failure(self, key: str) -> Decision:
        """Atomically count one failure; returns the state *after* counting."""
        now = _truncate_ms(self._now())
        pipeline = increment_pipeline(self.scope, now, self.window)
        for attempt in range(2):
            try:
                doc = await self._col.find_one_and_update(
                    {"_id": self._doc_id(key)}, pipeline, upsert=True, return_document=True,
                )
                break
            except self._dup_errors:
                # Two concurrent upserts of a new key: one inserts, the other
                # gets a duplicate-key error and retries as a plain update.
                if attempt:
                    raise
        return self._decision(doc, now)

    async def reset(self, key: str) -> None:
        await self._col.delete_one({"_id": self._doc_id(key)})


def _pymongo_duplicate_key_error() -> tuple:
    try:
        from pymongo.errors import DuplicateKeyError
    except ImportError:  # offline tests without pymongo
        return (_NeverRaised,)
    return (DuplicateKeyError,)


class _NeverRaised(Exception):
    pass


# ── TTL indexes (explicit, approval-gated; never called at startup) ─────────
TTL_INDEX_SPEC = {"keys": [("expires_at", 1)], "name": TTL_INDEX_NAME, "expireAfterSeconds": 0}
# Every expiring security collection: (collection, index name). Garbage
# collection only; code always checks expires_at explicitly (R-RL.1, R-RL.7).
TTL_INDEXES = (
    (COUNTERS_COLLECTION, TTL_INDEX_NAME),
    ("oauth_states", "oauth_states_expires_at_ttl"),
)


async def describe_ttl_index(db, collection: str = COUNTERS_COLLECTION, name: str = TTL_INDEX_NAME) -> Dict[str, Any]:
    """Read-only: does the TTL index exist on `collection`, and is it correct?"""
    names = await db.list_collection_names(filter={"name": collection})
    if collection not in names:
        return {"collection_exists": False, "ttl_index": None}
    info = await db[collection].index_information()
    idx = info.get(name)
    return {
        "collection_exists": True,
        "ttl_index": None if idx is None else {
            "key": idx.get("key"), "expireAfterSeconds": idx.get("expireAfterSeconds"),
        },
        "other_indexes": sorted(n for n in info if n not in ("_id_", name)),
    }


async def ensure_ttl_index(db, collection: str = COUNTERS_COLLECTION, name: str = TTL_INDEX_NAME) -> str:
    """Create the TTL index on `collection`.expires_at (idempotent). Invoked only
    through ``manage.py security-indexes --apply --confirm``."""
    return await db[collection].create_index(
        TTL_INDEX_SPEC["keys"], name=name, expireAfterSeconds=TTL_INDEX_SPEC["expireAfterSeconds"],
    )
