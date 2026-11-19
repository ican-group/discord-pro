"""Usage tracking and quotas (spec 012 §4.2 `limits.*`, §10), on the Store
contract (atomic compare-and-swap; standalone MongoDB safe).

* Per end-user rate: ``limits.end_user_msgs_per_minute`` (effective value,
  already capped by policy) per (instance, end user), via ``core.throttle``.
* Monthly AI-message quota: ``limits.max_monthly_ai_messages`` is counted
  separately at Enterprise and Organisation level (an Organisation may have a
  tighter sub-quota). A reservation increments both counters only while they
  are below their limits; if the second fails the first is released, so
  concurrent requests can never exceed either quota.
* Usage events carry the full ancestry, so permanent deletion removes them.
* **Billing idempotency (4C Part C):** every reservation has a ledger entry
  (``usage_ledger``) whose id is the request's billing key. The entry moves
  ``reserving → reserved → settled`` (or ``released`` / ``rejected`` /
  ``uncertain``) by compare-and-swap. A key can be reserved and settled at
  most once, the billable usage event id is derived from the key, and stale
  entries are repaired by ``reconcile``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core import resolver, throttle
from core.config_registry import PLATFORM_POLICY
from core.hierarchy import Chain, ConcurrentModification, DuplicateKey, NotFound, new_id

COUNTERS = "usage_counters"
EVENTS = "usage_events"
_MAX_ATTEMPTS = 64


LEDGER = "usage_ledger"
LEDGER_STALE_SECONDS = 600           # far longer than any pipeline run (provider timeouts + one retry)
COUNTED_STATES = ("reserving", "reserved", "settled", "uncertain")


class AlreadyReserved(Exception):
    """A ledger entry for this billing key exists: never generate (bill) twice."""


@dataclass(frozen=True)
class Reservation:
    key: str
    counter_ids: Tuple[str, ...]


# Server-set attribution labels on ledger entries (never from client input):
# ``integration_id`` (Bot Integration usage) and ``preview`` (F5: admin preview traffic,
# counted and billed exactly like live traffic, only reported separately).
LABELS = ("integration_id", "preview", "purpose")


def _labels(labels: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for k, v in (labels or {}).items():
        if k not in LABELS:
            raise ValueError(f"unknown usage label {k}")
        out[k] = v
    return out


def usage_event_id(key: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bitbotpro:usage:" + key))


def period_of(now: str) -> str:
    return datetime.fromisoformat(now).strftime("%Y-%m")


class StoreUsageTracker:
    def __init__(self, store, *, platform_policy=PLATFORM_POLICY):
        self.store = store
        self.platform_policy = platform_policy

    async def allow_end_user(self, chain: Chain, end_user_id: str, per_minute: int, *, now: str) -> bool:
        key = throttle.throttle_key("enduser", f"{chain.target.id}:{end_user_id}")
        d = await throttle.hit(self.store, key, limit=max(1, int(per_minute)), window_seconds=60, now=now)
        return d.allowed

    def _limits(self, chain: Chain) -> List[Tuple[str, Dict[str, Any], Optional[int]]]:
        ent_policy = resolver.effective_policy([chain.enterprise], self.platform_policy)[0]
        out = [(chain.enterprise.id, {"enterprise_id": chain.enterprise.id, "organisation_id": None},
                ent_policy.get("limits.max_monthly_ai_messages"))]
        if chain.organisation is not None:
            org_policy = resolver.effective_policy([chain.enterprise, chain.organisation], self.platform_policy)[0]
            out.append((chain.organisation.id, {"enterprise_id": chain.enterprise.id,
                                                "organisation_id": chain.organisation.id},
                        org_policy.get("limits.max_monthly_ai_messages")))
        return out

    async def reserve_ai_message(self, chain: Chain, *, now: str, key: Optional[str] = None,
                                 labels: Optional[Dict[str, Any]] = None) -> Optional[Reservation]:
        """Reserve one AI message against every applicable quota.

        Durable and idempotent per ``key``: a ledger entry with that unique id
        is written *before* the counters move, so one key can be reserved at
        most once (``AlreadyReserved``) even across crashes and concurrent
        duplicates. Returns None if a quota is exhausted (nothing billed)."""
        key = key or new_id()
        period = period_of(now)
        limits = self._limits(chain)
        counters = [f"{scope_id}:{period}" for scope_id, _, _ in limits]
        inst = chain.instance
        try:
            await self.store.insert(LEDGER, {
                "id": key, "state": "reserving", "period": period, "counters": counters,
                "enterprise_id": chain.enterprise.id,
                "organisation_id": chain.organisation.id if chain.organisation else None,
                "bot_id": chain.bot.id if chain.bot else None, "instance_id": inst.id if inst else None,
                **_labels(labels), "created_at": now, "updated_at": now, "version": 1})
        except DuplicateKey:
            # A stable billing identity may be reserved again only when its earlier
            # reservation *definitely* billed nothing (released after a confirmed
            # pre-generation failure, or rejected by a quota). reserving / reserved /
            # uncertain / settled keys are never reused (no regeneration, no double charge).
            old = await self.store.get(LEDGER, key)
            if not old or old.get("state") not in ("released", "rejected") or \
                    old.get("enterprise_id") != chain.enterprise.id:
                raise AlreadyReserved()
            try:
                await self.store.update(LEDGER, key, {"state": "reserving", "period": period, "counters": counters,
                                                      "updated_at": now, "attempts": int(old.get("attempts", 1)) + 1},
                                        expect_version=old["version"])
            except ConcurrentModification:
                raise AlreadyReserved()
        taken: List[str] = []
        for (scope_id, ancestry, limit), cid in zip(limits, counters):
            if not await self._increment(cid, ancestry, period, limit):
                for c in taken:
                    await self._decrement(c)
                await self._transition(key, ("reserving",), "rejected", now)
                return None
            taken.append(cid)
        await self._transition(key, ("reserving",), "reserved", now)
        return Reservation(key, tuple(counters))

    async def release(self, reservation: Optional[Reservation], *, now: Optional[str] = None) -> None:
        """Nothing was billed (provider failed): undo the reservation once."""
        if reservation is None:
            return
        if await self._transition(reservation.key, ("reserving", "reserved"), "released", now or ""):
            for cid in reservation.counter_ids:
                await self._decrement(cid)

    async def settle(self, reservation: Reservation, chain: Chain, *, now: str,
                     details: Optional[Dict[str, Any]] = None) -> None:
        """The billable event, exactly once per key (idempotent)."""
        await self._transition(reservation.key, ("reserved", "uncertain"), "settled", now)
        doc = await self.store.get(LEDGER, reservation.key)
        if doc is None or doc.get("state") != "settled":
            return
        labels = {k: doc[k] for k in LABELS if doc.get(k) is not None}           # attribution stays with the event
        try:
            await self.record(chain, "llm_call", now=now, details={**(details or {}), **labels},
                              event_id=usage_event_id(reservation.key))
        except DuplicateKey:
            pass

    async def ledger_state(self, key: str) -> Optional[str]:
        doc = await self.store.get(LEDGER, key)
        return doc.get("state") if doc else None

    async def waive_uncertain(self, key: str, *, now: str) -> bool:
        """Operator decision for an uncertain reservation: not charged. The quota
        counters it held are returned once (the transition happens once)."""
        doc = await self.store.get(LEDGER, key)
        if doc is None or not await self._transition(key, ("uncertain",), "released", now):
            return False
        for cid in doc.get("counters") or ():
            await self._decrement(cid)
        return True

    async def mark_uncertain(self, key: str, *, now: str) -> None:
        await self._transition(key, ("reserving", "reserved"), "uncertain", now)

    async def _transition(self, key: str, from_states, to_state: str, now: str) -> bool:
        for _ in range(_MAX_ATTEMPTS):
            doc = await self.store.get(LEDGER, key)
            if doc is None or doc.get("state") not in from_states:
                return False
            try:
                await self.store.update(LEDGER, key, {"state": to_state, "updated_at": now},
                                        expect_version=doc["version"])
                return True
            except (ConcurrentModification, NotFound):
                continue
        return False

    async def reconcile(self, *, now: str, stale_seconds: int = LEDGER_STALE_SECONDS) -> Dict[str, int]:
        """Worker repair after crashes:

        * ``reserving`` older than the stale window becomes ``released``: the
          provider is only called after ``reserved``, so nothing was billed;
        * ``reserved`` older than the stale window becomes ``uncertain``: the
          provider may have run. It stays counted (conservative) and is never
          regenerated automatically;
        * counters above the ledger's count of counted entries are lowered with
          a compare-and-swap. Counters are never raised here, so a hard quota
          can never be exceeded through repair."""
        from datetime import timedelta
        cutoff = datetime.fromisoformat(now) - timedelta(seconds=stale_seconds)
        out = {"released": 0, "uncertain": 0, "counters_lowered": 0}
        touched = set()
        for state, target in (("reserving", "released"), ("reserved", "uncertain")):
            for doc in await self.store.find(LEDGER, {"state": state}):
                if datetime.fromisoformat(doc["updated_at"]) <= cutoff:
                    if await self._transition(doc["id"], (state,), target, now):
                        out[target] += 1
                        touched.update(doc.get("counters") or [])
        for doc in await self.store.find(COUNTERS, {"period": [period_of(now)]}):
            touched.add(doc["id"])
        for cid in touched:
            for _ in range(_MAX_ATTEMPTS):
                counter = await self.store.get(COUNTERS, cid)
                if counter is None:
                    break
                actual = await self.store.count(LEDGER, {"counters": cid, "state": list(COUNTED_STATES)})
                if int(counter.get("count", 0)) <= actual:
                    break
                try:
                    await self.store.update(COUNTERS, cid, {"count": actual}, expect_version=counter["version"])
                    out["counters_lowered"] += 1
                    break
                except (ConcurrentModification, NotFound):
                    continue
        return out

    async def _increment(self, cid: str, ancestry: Dict[str, Any], period: str, limit: Optional[int]) -> bool:
        for _ in range(_MAX_ATTEMPTS):
            doc = await self.store.get(COUNTERS, cid)
            count = int((doc or {}).get("count", 0))
            if limit is not None and count >= limit:
                return False
            try:
                if doc is None:
                    await self.store.insert(COUNTERS, {"id": cid, **ancestry, "period": period, "count": 1,
                                                       "version": 1})
                else:
                    await self.store.update(COUNTERS, cid, {"count": count + 1}, expect_version=doc["version"])
                return True
            except (DuplicateKey, ConcurrentModification, NotFound):
                continue
        return False

    async def _decrement(self, cid: str) -> None:
        for _ in range(_MAX_ATTEMPTS):
            doc = await self.store.get(COUNTERS, cid)
            if doc is None or int(doc.get("count", 0)) <= 0:
                return
            try:
                await self.store.update(COUNTERS, cid, {"count": int(doc["count"]) - 1},
                                        expect_version=doc["version"])
                return
            except (ConcurrentModification, NotFound):
                continue

    async def record(self, chain: Chain, kind: str, *, now: str, details: Optional[Dict[str, Any]] = None,
                     event_id: Optional[str] = None) -> None:
        inst = chain.instance
        await self.store.insert(EVENTS, {
            "id": event_id or new_id(), "at": now, "kind": kind, "enterprise_id": chain.enterprise.id,
            "organisation_id": chain.organisation.id if chain.organisation else None,
            "bot_id": chain.bot.id if chain.bot else None, "instance_id": inst.id if inst else None,
            "details": {k: v for k, v in (details or {}).items() if isinstance(v, (str, int, float, bool))},
            "version": 1})


__all__ = ["COUNTERS", "EVENTS", "LEDGER", "Reservation", "AlreadyReserved", "StoreUsageTracker", "period_of",
           "usage_event_id", "COUNTED_STATES"]
