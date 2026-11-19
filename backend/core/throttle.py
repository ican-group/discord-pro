"""Attempt throttling for authentication (Phase 4A), on the Store contract
(atomic compare-and-swap; works on standalone MongoDB and in memory).

Sliding-window estimate over fixed windows aligned to the window length (one
document per window, ``<key>@<index>``), counted with one atomic increment
(F-A). ``hit`` counts an attempt and refuses once the estimate would exceed
``limit``; ``blocked`` only reads. Keys are hashed so no email address or IP is stored in clear.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
import math
from datetime import datetime, timedelta
from typing import Optional

from core.hierarchy import ConcurrentModification, DuplicateKey, NotFound

COLLECTION = "auth_throttle"
_MAX_ATTEMPTS = 32


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    retry_after: int = 0
    count: int = 0


def throttle_key(scope: str, identifier: str) -> str:
    digest = hashlib.sha256(f"bitbotpro:throttle:v1:{scope}:{identifier}".encode()).hexdigest()
    return f"{scope}:{digest[:40]}"


def _decision(doc: Optional[dict], limit: int, window: int, now: datetime) -> ThrottleDecision:
    if not doc:
        return ThrottleDecision(True, 0, 0)
    start = datetime.fromisoformat(doc["window_start"])
    if now >= start + timedelta(seconds=window):
        return ThrottleDecision(True, 0, 0)
    count = int(doc.get("count", 0))
    if count >= limit:
        return ThrottleDecision(False, max(1, int((start + timedelta(seconds=window) - now).total_seconds())), count)
    return ThrottleDecision(True, 0, count)


def _window(key: str, window_seconds: int, now: datetime):
    idx = int(now.timestamp() // window_seconds)
    start = datetime.fromtimestamp(idx * window_seconds, tz=now.tzinfo)
    return idx, start, start + timedelta(seconds=window_seconds)


async def _estimate(store, key: str, idx: int, start: datetime, window_seconds: int, now: datetime,
                    current: int) -> float:
    """Sliding-window estimate: the current window's count plus the previous
    window's count weighted by the share of the sliding window still covering
    it. Closes the 2× burst a plain fixed window allows across its boundary."""
    prev = await store.get(COLLECTION, f"{key}@{idx - 1}")
    weight = max(0.0, 1.0 - (now - start).total_seconds() / window_seconds)
    # Rounded UP (Phase 6A): just after a boundary the previous window counts in full, so a burst that
    # straddles the boundary can never squeeze in an extra attempt (errs on the strict side by < 1).
    return current + math.ceil(int((prev or {}).get("count", 0)) * weight)


async def blocked(store, key: str, *, limit: int, window_seconds: int, now: str) -> ThrottleDecision:
    t = datetime.fromisoformat(now)
    idx, start, end = _window(key, window_seconds, t)
    cur = int((await store.get(COLLECTION, f"{key}@{idx}") or {}).get("count", 0))
    estimate = await _estimate(store, key, idx, start, window_seconds, t, cur)
    if estimate >= limit:
        return ThrottleDecision(False, max(1, int((end - t).total_seconds())), int(estimate))
    return ThrottleDecision(True, 0, int(estimate))


async def hit(store, key: str, *, limit: int, window_seconds: int, now: str) -> ThrottleDecision:
    """Count one attempt with a single atomic increment and decide on the
    sliding-window estimate (F-A, approved form). Refused attempts are not
    counted (the increment is undone). Windows are aligned to the server clock
    (workers must use synchronised clocks, e.g. NTP)."""
    t = datetime.fromisoformat(now)
    idx, start, end = _window(key, window_seconds, t)
    wid = f"{key}@{idx}"
    initial = {"key": key, "window_start": start.isoformat(),
               "expires_at_dt": end + timedelta(seconds=window_seconds)}    # kept one extra window (previous count)
    count = await store.increment(COLLECTION, wid, "count", 1, initial=initial)
    estimate = await _estimate(store, key, idx, start, window_seconds, t, count)
    if estimate > limit:
        await store.increment(COLLECTION, wid, "count", -1, initial=initial)
        return ThrottleDecision(False, max(1, int((end - t).total_seconds())), limit)
    return ThrottleDecision(True, 0, count)


async def reset(store, key: str) -> None:
    """Forget every window of one key (e.g. after a successful login)."""
    await store.delete_many(COLLECTION, {"key": key})


__all__ = ["COLLECTION", "ThrottleDecision", "throttle_key", "blocked", "hit", "reset"]
