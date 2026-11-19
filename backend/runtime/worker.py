"""Background worker foundation (spec 012 §2 ``worker.py``, D-Q4; Phase 4A).

One ``Worker`` loop per process; any number of processes may run. Each pass:

1. **Deletion jobs**: picks up `pending` jobs, jobs `awaiting_external_cleanup`,
   `running` jobs whose lease has expired (a crashed worker), and `failed`
   jobs with attempts left (bounded by ``max_job_attempts``). ``run_job``
   holds the lease, so two workers never run one job at once. External
   actions go only through the injected executors, each call carrying a
   per-(job, action) idempotency key, so a crash between the external effect
   and the job save does not duplicate the effect.
2. **Binding cleanup**: ``BindingRegistry.reconcile`` removes claims (older than
   the grace period) whose instance no longer holds them.
3. **Membership intents**: rolls forward or aborts intents left by crashed
   requests (``core.memberships.resolve_stale``).
4. **Discord connection management (foundation)**: computes the desired set of
   guilds from the live claims and hands it to a ``ConnectionManager``
   (declarative and idempotent). Phase 4D supplies the real gateway manager.

Phase 4A ships only mock adapters; nothing contacts Discord, Telegram or
Zendesk. Every job and claim operation is scoped by the record it acts on, so
tenant isolation is preserved.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Set

from core import audit
from core import deletion as dl
from core import memberships as ms
from core.bindings import COLLECTION as BINDINGS, BindingRegistry, claims_for
from core.hierarchy import COLLECTION, HierarchyService, Level, from_doc
from runtime.usage import StoreUsageTracker

HEARTBEATS = "worker_heartbeats"
RUNNABLE_STATES = ("pending", "awaiting_external_cleanup", "running", "failed")


class ConnectionManager(Protocol):
    async def sync(self, desired: Dict[str, List[str]]) -> None: ...


@dataclass
class WorkerReport:
    jobs_run: List[str] = field(default_factory=list)
    jobs_skipped: List[str] = field(default_factory=list)
    claims_reconciled: int = 0
    intents_resolved: int = 0
    desired_guilds: int = 0
    requests_repaired: Dict[str, int] = field(default_factory=dict)
    telegram_synced: int = 0
    deliveries: Dict[str, int] = field(default_factory=dict)
    gateways: Dict[str, int] = field(default_factory=dict)
    zendesk: Dict[str, int] = field(default_factory=dict)
    knowledge_jobs: int = 0
    usage_reconciled: Dict[str, int] = field(default_factory=dict)


class MockCleanupAdapter:
    """Test double for the external cleanup adapters. Idempotent by key: a
    repeated key never repeats the (recorded) external effect."""

    def __init__(self, fail_times: Optional[Dict[str, int]] = None, not_required: Set[str] = frozenset()):
        self.effects: List[str] = []              # external effects actually "performed"
        self.calls: List[str] = []                # every invocation (including retries)
        self._fail = dict(fail_times or {})
        self._not_required = set(not_required)
        self._done: Set[str] = set()

    async def __call__(self, action: Dict[str, Any], ctx) -> dl.CleanupOutcome:
        key = action["idempotency_key"]
        self.calls.append(key)
        if action["action"] in self._not_required:
            return dl.CleanupOutcome(dl.CleanupStatus.NOT_REQUIRED)
        if self._fail.get(action["action"], 0) > 0:
            self._fail[action["action"]] -= 1
            return dl.CleanupOutcome(dl.CleanupStatus.RETRY, "provider_unavailable")
        if key not in self._done:
            self._done.add(key)
            self.effects.append(key)
        return dl.CleanupOutcome(dl.CleanupStatus.SUCCEEDED)

    def executors(self) -> Dict[str, dl.Executor]:
        return {name: self for name in ("telegram_delete_webhook", "discord_routing_removal", "discord_leave_guild",
                                        "zendesk_token_revocation", "zendesk_webhook_subscription_removal")}


class MockConnectionManager:
    def __init__(self):
        self.desired: Dict[str, List[str]] = {}
        self.syncs = 0

    async def sync(self, desired: Dict[str, List[str]]) -> None:
        self.desired = dict(desired)
        self.syncs += 1


class Worker:
    def __init__(self, store, *, clock: Callable[[], str], worker_id: str,
                 executors: Optional[Mapping[str, dl.Executor]] = None,
                 connection_manager: Optional[ConnectionManager] = None, telegram=None, discord=None,
                 gateway=None, zendesk=None,
                 max_job_attempts: int = 5, batch: int = 50):
        self.store = store
        self.clock = clock
        self.worker_id = worker_id
        self.executors = dict(executors or {})
        self.connections = connection_manager
        self.telegram = telegram                 # runtime.telegram_channel.TelegramChannel (4C)
        self.discord = discord                   # runtime.discord_channel.DiscordChannel (4D)
        self.gateway = gateway                   # runtime.discord_gateway.GatewayManager (4D)
        self.zendesk = zendesk                   # runtime.zendesk_integration.ZendeskService (4E)
        self.max_job_attempts = max_job_attempts
        self.batch = batch

    async def run_once(self) -> WorkerReport:
        report = WorkerReport()
        await self._heartbeat("running")
        await self._deletion_jobs(report)
        report.claims_reconciled = await self._reconcile_bindings()
        report.intents_resolved = await self._resolve_intents()
        report.requests_repaired = await self._repair_requests()
        report.usage_reconciled = await StoreUsageTracker(self.store).reconcile(now=self.clock())
        if self.telegram is not None:
            report.telegram_synced = await self._sync_telegram()
        if self.zendesk is not None:
            report.zendesk = await self.zendesk.maintain(limit=self.batch)
        from runtime.knowledge_store import Ingestor
        report.knowledge_jobs = await Ingestor(self.store, clock=self.clock).maintain(limit=self.batch)
        report.deliveries = await self._deliveries()
        if self.gateway is not None:
            report.gateways = await self.gateway.reconcile()
        if self.connections is not None:
            desired = await discord_desired_state(self.store)
            await self.connections.sync(desired)
            report.desired_guilds = len(desired)
        await self._heartbeat("idle")
        return report

    async def run_forever(self, stop: asyncio.Event, *, interval_seconds: float = 15.0) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:                    # a failed pass never kills the worker; next pass retries
                await self._heartbeat("error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _deletion_jobs(self, report: WorkerReport) -> None:
        await dl.repurge_due(self.store, now=self.clock(), limit=self.batch)
        now = datetime.fromisoformat(self.clock())
        jobs = await self.store.find(dl.JOBS, {"status": list(RUNNABLE_STATES)}, limit=self.batch,
                                     sort="requested_at")
        for job in jobs:
            if job.get("target_level") == "user":
                continue                         # user deletion runs synchronously at confirmation
            if job["status"] == "failed" and int(job.get("attempts", 0)) >= self.max_job_attempts:
                report.jobs_skipped.append(job["id"])        # bounded: operator resume required
                continue
            if job["status"] == "running" and job.get("lease_until") and \
                    now < datetime.fromisoformat(job["lease_until"]):
                report.jobs_skipped.append(job["id"])        # live lease held by another worker
                continue
            before = job["status"]
            after = await dl.run_job(self.store, job["id"], clock=self.clock, worker=self.worker_id,
                                     executors=self.executors)
            report.jobs_run.append(job["id"])
            if after and after["status"] == "failed" and before != "failed":
                await audit.record(self.store, actor=None, action="worker.job_failed",
                                   enterprise_id=after.get("enterprise_id"), target_level="deletion_job",
                                   target_id=job["id"], now=self.clock(),
                                   details={"error": after.get("error"), "attempts": after.get("attempts")})

    async def _reconcile_bindings(self) -> int:
        async def expected(instance_id: str):
            doc = await self.store.get(COLLECTION[Level.BOT_INSTANCE], instance_id)
            if doc is None or (doc.get("lifecycle") or {}).get("state") == "deleted":
                return {}
            inst = from_doc(Level.BOT_INSTANCE, doc)
            return claims_for(inst.channel, telegram_bot_id=inst.telegram_bot_id)
        return await BindingRegistry(self.store, clock=self.clock).reconcile(
            expected, now=datetime.fromisoformat(self.clock()))

    async def _resolve_intents(self) -> int:
        n = 0
        for guard in await self.store.find(ms.GUARDS, {}):
            if guard.get("pending"):
                if await ms.resolve_stale(self.store, guard["id"], now=datetime.fromisoformat(self.clock())):
                    n += 1
        return n

    async def _repair_requests(self) -> Dict[str, int]:
        """Conversation requests whose processing crashed (lease expired):
        * ``generated``: finish them (append + settle + done, idempotent);
        * ``processing`` with nothing reserved: remove the record (safe to retry);
        * ``processing`` with a reservation: ``uncertain`` (never regenerated)."""
        from core import resolver
        from runtime import conversations as conv
        out = {"completed": 0, "removed": 0, "uncertain": 0}
        now = self.clock()
        usage = StoreUsageTracker(self.store)
        for rec in await self.store.find(conv.REQUESTS, {"status": ["processing", "generated"]}, limit=self.batch):
            if datetime.fromisoformat(now) < datetime.fromisoformat(rec.get("lease_until") or now):
                continue
            if rec["status"] == "generated":
                try:
                    chain = await HierarchyService(self.store, clock=self.clock).load_chain(
                        Level.BOT_INSTANCE, rec["instance_id"])
                except Exception:
                    continue                      # instance gone: deletion removes the record
                policy = resolver.effective_policy([n for n in chain.nodes() if n.level in
                                                    (Level.ENTERPRISE, Level.ORGANISATION)],
                                                   usage.platform_policy)[0]
                builder = self._senders().get(rec.get("channel") or "", (None, None))[1]
                await conv.complete_request(self.store, usage, chain, rec, now=now,
                                            retention_days=policy.get("retention.conversation_days"),
                                            delivery_builder=builder)
                out["completed"] += 1
            elif await usage.ledger_state(rec["id"]) in (None, "released", "rejected"):
                await self.store.delete(conv.REQUESTS, rec["id"], expect_version=rec["version"])
                out["removed"] += 1
            else:
                await conv.mark_uncertain(self.store, rec["id"])
                out["uncertain"] += 1
        return out

    def _senders(self):
        out = {}
        if self.telegram is not None:
            out["telegram"] = (self.telegram.send_part, self.telegram.delivery_parts)
        if self.discord is not None:
            out["discord"] = (self.discord.send_part, self.discord.delivery_parts)
        if self.zendesk is not None:
            out["zendesk"] = (self.zendesk.send_part, self.zendesk.delivery_parts)
        return out

    async def _deliveries(self) -> Dict[str, int]:
        """Durable outbound delivery (T-A): recover crashed sends (in-flight parts
        become uncertain), then send what is due. Leases prevent duplicate sends."""
        from runtime import delivery as dlv
        now = self.clock()
        out = dict(await dlv.recover(self.store, now=now))
        senders = self._senders()
        sent = 0
        for doc in await dlv.due(self.store, now=now, limit=self.batch):
            sender = senders.get(doc["channel"])
            if sender is None:
                continue
            after = await dlv.run(self.store, doc["id"], sender[0], now=now, worker=self.worker_id)
            sent += int(bool(after) and after.get("status") == "delivered")
        out["delivered"] = sent
        return out

    async def _sync_telegram(self) -> int:
        """Retry webhook reconciliation for instances whose last sync failed or
        whose desired and registered states differ (e.g. a lifecycle change
        while Telegram was unreachable)."""
        n = 0
        for doc in await self.store.find(COLLECTION[Level.BOT_INSTANCE], {"channel.type": "telegram"},
                                         limit=self.batch * 4):
            state = doc.get("telegram_webhook") or {}
            if not state or (doc.get("lifecycle") or {}).get("state") == "deleted":
                continue
            if state.get("sync_error") or bool(state.get("desired")) != bool(state.get("registered")) \
                    or state.get("desired"):
                try:
                    before = (state.get("registered"), state.get("sync_error"))
                    after = await self.telegram.sync(doc["id"])
                    if (after["registered"], after["sync_error"]) != before:
                        n += 1
                except Exception:
                    continue
        return n

    async def _heartbeat(self, state: str) -> None:
        doc = await self.store.get(HEARTBEATS, self.worker_id)
        fields = {"state": state, "at": self.clock()}
        if doc is None:
            try:
                await self.store.insert(HEARTBEATS, {"id": self.worker_id, **fields, "version": 1})
            except Exception:
                pass
        else:
            try:
                await self.store.update(HEARTBEATS, self.worker_id, fields)
            except Exception:
                pass


async def discord_desired_state(store) -> Dict[str, Dict[str, List[str]]]:
    """application id -> guild id -> instance ids holding it (from the live claims)."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for doc in await store.find(BINDINGS, {}):
        if doc["id"].startswith("discord:"):
            _, app, guild = doc["id"].split(":", 2)
            holders = sorted(h["instance_id"] for h in doc.get("holders") or [])
            if holders:
                out.setdefault(app, {})[guild] = holders
    return out


__all__ = ["Worker", "WorkerReport", "MockCleanupAdapter", "MockConnectionManager", "ConnectionManager",
           "discord_desired_state", "HEARTBEATS"]
