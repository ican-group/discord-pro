"""Exclusive channel bindings (spec 012 §3.1, §5).

A channel binding is *claimed* in the ``channel_bindings`` collection before an
instance may use it. One claim document exists per binding key:

* ``discord:<guild_id>``          — scopes are channel ids, or ``*`` for the whole guild
* ``telegram:credential:<id>``    — the credential holding the bot token
* ``telegram:bot:<telegram_bot_id>`` — the Telegram bot identity (two credentials
  holding the same token can never serve two instances)
* ``webchat:<public_id>``

``holders`` is a list of ``{instance_id, enterprise_id, scopes, at}`` (indexed
on ``holders.instance_id``). A claim succeeds
only if no *other* holder's scopes overlap (``*`` overlaps everything), and it is
written with a compare-and-swap on the claim document's ``version``. Two
concurrent claims on the same key therefore serialise on one document: at most
one of two overlapping claims can ever be stored. This needs only single-document
atomicity (standalone MongoDB; no transactions).

Ordering with the instance document (the binding is claimed *before* the
instance stores it, and released *after* the instance stops using it) means a
crash can only leave a claim that is too broad — never two instances on one
binding. :meth:`BindingRegistry.reconcile` removes stale claims after a grace
period. Conflict messages never disclose which tenant holds a binding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.hierarchy import (ChannelBinding, ConcurrentModification, DiscordBinding, DuplicateKey,
                            HierarchyError, NotFound, TelegramBinding, WebchatBinding)

COLLECTION = "channel_bindings"
WHOLE = "*"
RECONCILE_GRACE_SECONDS = 600
_MAX_ATTEMPTS = 64


class BindingConflict(HierarchyError):
    def __init__(self):
        super().__init__("This channel binding is already in use.")


Claims = Dict[str, Tuple[str, ...]]


def claims_for(binding: ChannelBinding, *, telegram_bot_id: Optional[str] = None) -> Claims:
    """Binding keys and scopes an instance with ``binding`` must hold."""
    if isinstance(binding, DiscordBinding):
        if not binding.guild_id or not binding.application_id:
            return {}
        # D-Q14: scoped to the Enterprise application, so two Enterprises' applications in
        # one server never collide; within one application, channel scopes are exclusive.
        return {f"discord:{binding.application_id}:{binding.guild_id}": tuple(binding.channel_ids) or (WHOLE,)}
    if isinstance(binding, TelegramBinding):
        if not binding.credential_id:
            return {}
        out = {f"telegram:credential:{binding.credential_id}": (WHOLE,)}
        if telegram_bot_id:
            out[f"telegram:bot:{telegram_bot_id}"] = (WHOLE,)
        return out
    if isinstance(binding, WebchatBinding):
        return {f"webchat:{binding.public_id}": (WHOLE,)}
    raise HierarchyError("Unknown channel type.")


def overlaps(a: Sequence[str], b: Sequence[str]) -> bool:
    return WHOLE in a or WHOLE in b or bool(set(a) & set(b))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BindingRegistry:
    def __init__(self, store, *, clock: Callable[[], str] = _now):
        self.store = store
        self.clock = clock

    # ── claim / release ──
    async def claim(self, instance_id: str, enterprise_id: str, claims: Claims) -> Dict[str, Optional[Tuple[str, ...]]]:
        """Claim every key (all-or-nothing). Returns the instance's previous
        scopes per key (None if it held nothing) so a caller can :meth:`restore`."""
        acquired: Dict[str, Optional[Tuple[str, ...]]] = {}
        try:
            for key in sorted(claims):
                acquired[key] = await self._set_holder(key, instance_id, enterprise_id, tuple(claims[key]))
        except BaseException:
            await self.restore(instance_id, enterprise_id, acquired)
            raise
        return acquired

    async def restore(self, instance_id: str, enterprise_id: str,
                      previous: Mapping[str, Optional[Tuple[str, ...]]]) -> None:
        """Undo :meth:`claim` (best effort; anything left over is only too broad)."""
        for key, prev in previous.items():
            try:
                if prev is None:
                    await self._remove_holder(key, instance_id)
                else:
                    await self._set_holder(key, instance_id, enterprise_id, prev, check=False)
            except HierarchyError:
                pass

    async def release(self, instance_id: str, keys: Iterable[str]) -> None:
        for key in sorted(set(keys)):
            await self._remove_holder(key, instance_id)

    async def holders(self, key: str) -> Dict[str, Dict[str, Any]]:
        doc = await self.store.get(COLLECTION, key)
        return {h["instance_id"]: h for h in (doc or {}).get("holders") or []}

    async def keys_held_by(self, instance_id: str) -> List[str]:
        docs = await self.store.find(COLLECTION, {"holders.instance_id": instance_id})
        return sorted(d["id"] for d in docs)

    # ── primitives (compare-and-swap loops) ──
    async def _set_holder(self, key: str, instance_id: str, enterprise_id: str, scopes: Tuple[str, ...], *,
                          check: bool = True) -> Optional[Tuple[str, ...]]:
        entry = {"instance_id": instance_id, "enterprise_id": enterprise_id, "scopes": list(scopes),
                 "at": self.clock()}
        for _ in range(_MAX_ATTEMPTS):
            doc = await self.store.get(COLLECTION, key)
            if doc is None:
                try:
                    await self.store.insert(COLLECTION, {"id": key, "holders": [entry], "version": 1})
                    return None
                except DuplicateKey:
                    continue
            holders = list(doc.get("holders") or [])
            if check:
                for h in holders:
                    if h.get("instance_id") != instance_id and overlaps(h.get("scopes") or [WHOLE], scopes):
                        raise BindingConflict()
            prev = next((h for h in holders if h.get("instance_id") == instance_id), None)
            holders = [h for h in holders if h.get("instance_id") != instance_id] + [entry]
            try:
                await self.store.update(COLLECTION, key, {"holders": holders}, expect_version=doc["version"])
                return tuple(prev["scopes"]) if prev else None
            except (ConcurrentModification, NotFound):
                continue
        raise ConcurrentModification()

    async def _remove_holder(self, key: str, instance_id: str) -> None:
        for _ in range(_MAX_ATTEMPTS):
            doc = await self.store.get(COLLECTION, key)
            holders = list((doc or {}).get("holders") or [])
            if doc is None or not any(h.get("instance_id") == instance_id for h in holders):
                return
            holders = [h for h in holders if h.get("instance_id") != instance_id]
            if not holders:
                if await self.store.delete(COLLECTION, key, expect_version=doc["version"]):
                    return
                continue
            try:
                await self.store.update(COLLECTION, key, {"holders": holders}, expect_version=doc["version"])
                return
            except (ConcurrentModification, NotFound):
                continue
        raise ConcurrentModification()

    # ── repair ──
    async def reconcile(self, expected: Callable[[str], "Any"], *, now: Optional[datetime] = None,
                        grace_seconds: int = RECONCILE_GRACE_SECONDS) -> int:
        """Remove claim entries older than the grace period whose instance no
        longer holds that key. ``expected(instance_id)`` is an async callable
        returning the instance's current :data:`Claims` (``{}`` if it is missing
        or deleted). Returns the number of entries removed."""
        now = now or datetime.now(timezone.utc)
        removed = 0
        for doc in await self.store.find(COLLECTION, {}):
            for entry in list(doc.get("holders") or []):
                inst_id = entry.get("instance_id")
                try:
                    at = datetime.fromisoformat(entry.get("at", ""))
                except ValueError:
                    at = now - timedelta(seconds=grace_seconds + 1)
                if now - at < timedelta(seconds=grace_seconds):
                    continue
                want = await expected(inst_id)
                if doc["id"] not in want:
                    await self._remove_holder(doc["id"], inst_id)
                    removed += 1
        return removed


__all__ = ["COLLECTION", "WHOLE", "BindingConflict", "Claims", "claims_for", "overlaps", "BindingRegistry"]
