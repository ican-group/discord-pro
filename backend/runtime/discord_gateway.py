"""Discord gateway supervision for Enterprise applications (Phase 4D, D-Q14).

Runs in the worker process, never in FastAPI. One gateway connection per
*enabled* Enterprise application whose Enterprise is available and whose
superadmin-controlled ``gateway_desired`` is ``running``.

* **Single owner per application.** ``discord_gateway_status`` (id =
  Enterprise id) carries an ``owner`` + ``lease_until`` taken by
  compare-and-swap, renewed by every reconcile. Another worker never opens a
  second connection while the lease is live; if the owner dies, the lease
  expires and another worker takes over (restart-safe).
* **Bounded reconnects.** Failures back off (5 s … 15 min) with a capped
  attempt counter; ``unauthorized`` (bad token) stops retrying until a
  superadmin rotates the token or requests a restart.
* **Restart / stop** are requests recorded on the application by superadmins
  (``restart_requested_at``, ``gateway_desired``); the manager acts on them.
* **Sanitised status only:** state, error category, attempts, timestamps,
  guild count. Never the token, never Discord payloads.
* The connection itself is an injected ``GatewayConnection``. Heartbeats,
  resume, sequence numbers and Discord's gateway rate limits (identify
  concurrency, 120 events / 60 s) belong to the connection implementation;
  in tests it is ``MockGatewayConnection``. No live gateway is started here.
* Event de-duplication across resumes/replays is durable and does not rely on
  the connection: every message is keyed by its Discord message id in
  ``chat_requests`` (``runtime.discord_channel``).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from core.hierarchy import ConcurrentModification, DuplicateKey, HierarchyError, HierarchyService, Level
from runtime.discord import APPS

STATUS = "discord_gateway_status"
LEASE_SECONDS = 90
BACKOFF = (5, 15, 60, 300, 900)
MAX_ATTEMPTS = 20

EventHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


class GatewayConnection(Protocol):
    async def start(self, token: str, intents: int, on_event: EventHandler) -> None: ...
    async def stop(self) -> None: ...
    def healthy(self) -> bool: ...
    def last_error(self) -> Optional[str]: ...
    def guild_count(self) -> int: ...


class GatewayStartError(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class MockGatewayConnection:
    """Test double: records calls, can be told to fail, can inject events."""

    instances: List["MockGatewayConnection"] = []

    def __init__(self, application_id: str, *, fail_with: Optional[str] = None):
        self.application_id = application_id
        self.fail_with = fail_with
        self.started = False
        self.stopped = False
        self.token_seen = False
        self.intents = None
        self.on_event: Optional[EventHandler] = None
        self.error: Optional[str] = None
        self.guilds = 0
        MockGatewayConnection.instances.append(self)

    async def start(self, token: str, intents: int, on_event: EventHandler) -> None:
        if self.fail_with:
            raise GatewayStartError(self.fail_with)
        self.token_seen = bool(token)
        self.intents = intents
        self.on_event = on_event
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.started = False

    def healthy(self) -> bool:
        return self.started and not self.stopped and self.error is None

    def last_error(self) -> Optional[str]:
        return self.error

    def guild_count(self) -> int:
        return self.guilds

    async def emit(self, event: Dict[str, Any]) -> Any:
        assert self.on_event is not None
        return await self.on_event(event)


class GatewayManager:
    def __init__(self, store, *, box, channel, factory: Callable[[dict], GatewayConnection], clock,
                 worker_id: str = "worker"):
        self.store = store
        self.box = box
        self.channel = channel                  # runtime.discord_channel.DiscordChannel
        self.factory = factory
        self.clock = clock
        self.worker_id = worker_id
        self.hierarchy = HierarchyService(store, clock=clock)
        self.connections: Dict[str, GatewayConnection] = {}       # enterprise id -> connection
        self.started_at: Dict[str, str] = {}

    # ── desired state ──
    async def _wanted(self, app: dict) -> bool:
        if app.get("status") != "enabled" or app.get("gateway_desired") != "running" or not app.get("bot_token_enc"):
            return False
        try:
            chain = await self.hierarchy.load_chain(Level.ENTERPRISE, app["enterprise_id"])
        except HierarchyError:
            return False
        return chain.availability().available

    async def _status(self, eid: str) -> dict:
        doc = await self.store.get(STATUS, eid)
        if doc is None:
            try:
                await self.store.insert(STATUS, {"id": eid, "enterprise_id": eid, "state": "stopped", "owner": None,
                                                 "lease_until": None, "attempts": 0, "next_attempt_at": None,
                                                 "last_error": None, "updated_at": self.clock(), "version": 1})
            except DuplicateKey:
                pass
            doc = await self.store.get(STATUS, eid)
        return doc

    async def _save(self, doc: dict, **fields) -> Optional[dict]:
        fields["updated_at"] = self.clock()
        try:
            await self.store.update(STATUS, doc["id"], fields, expect_version=doc["version"])
        except ConcurrentModification:
            return None
        return {**doc, **fields, "version": doc["version"] + 1}

    # ── reconcile ──
    async def reconcile(self) -> Dict[str, int]:
        out = {"running": 0, "started": 0, "stopped": 0, "failed": 0, "not_owner": 0}
        now = self.clock()
        now_dt = datetime.fromisoformat(now)
        apps = {a["enterprise_id"]: a for a in await self.store.find(APPS, {})}
        for eid in list(self.connections):
            app = apps.get(eid)
            if app is None or not await self._wanted(app):
                await self._stop(eid, "stopped")
                out["stopped"] += 1
        for eid, app in apps.items():
            if not await self._wanted(app):
                continue
            st = await self._status(eid)
            lease_live = st.get("lease_until") and now_dt < datetime.fromisoformat(st["lease_until"])
            if st.get("owner") not in (None, self.worker_id) and lease_live:
                out["not_owner"] += 1                     # another worker runs this application's gateway
                continue
            conn = self.connections.get(eid)
            restart = app.get("restart_requested_at") and (eid not in self.started_at or
                                                          app["restart_requested_at"] > self.started_at[eid])
            if conn is not None and restart:
                await self._stop(eid, "restarting")
                conn = None
                st = await self._status(eid)
            if conn is not None and conn.healthy():
                renewed = None
                for _ in range(4):                    # the adapter may have just persisted its resume state
                    renewed = await self._save(st, owner=self.worker_id, state="connected",
                                               guilds=conn.guild_count(), heartbeat_at=now,
                                               lease_until=(now_dt + timedelta(seconds=LEASE_SECONDS)).isoformat())
                    if renewed is not None:
                        break
                    st = await self._status(eid)
                    if st.get("owner") not in (None, self.worker_id):
                        break                         # another worker really owns it now
                st = renewed
                if st is None:                            # lost the lease race: let it go
                    await self._stop(eid, None)
                    out["not_owner"] += 1
                else:
                    out["running"] += 1
                continue
            if conn is not None:                          # unhealthy: drop it and reconnect with back-off
                err = conn.last_error() or "disconnected"
                await self._stop(eid, None)
                st = await self._status(eid)
                st = await self._save(st, state="reconnecting", last_error=err,
                                      attempts=int(st.get("attempts", 0)) + 1,
                                      next_attempt_at=self._backoff(now_dt, int(st.get("attempts", 0)) + 1))
                out["failed"] += 1
                continue
            if not restart and st.get("state") == "failed_auth":
                continue                                  # bad token: wait for a superadmin rotation/restart
            if st.get("next_attempt_at") and now_dt < datetime.fromisoformat(st["next_attempt_at"]) and not restart:
                continue
            if int(st.get("attempts", 0)) >= MAX_ATTEMPTS and not restart:
                continue
            claimed = await self._save(st, owner=self.worker_id, state="connecting",
                                       lease_until=(now_dt + timedelta(seconds=LEASE_SECONDS)).isoformat())
            if claimed is None:
                out["not_owner"] += 1
                continue
            conn = self._make(app, claimed)
            try:
                token = self.box.decrypt(app["bot_token_enc"])
                await conn.start(token, int(app.get("intents") or 0), self._handler(eid))
            except GatewayStartError as exc:
                attempts = int(claimed.get("attempts", 0)) + 1
                await self._save(claimed, state="failed_auth" if exc.category == "unauthorized" else "reconnecting",
                                 last_error=exc.category, attempts=attempts, owner=None, lease_until=None,
                                 next_attempt_at=self._backoff(now_dt, attempts))
                out["failed"] += 1
                continue
            except Exception:
                attempts = int(claimed.get("attempts", 0)) + 1
                await self._save(claimed, state="reconnecting", last_error="start_failed", attempts=attempts,
                                 owner=None, lease_until=None, next_attempt_at=self._backoff(now_dt, attempts))
                out["failed"] += 1
                continue
            self.connections[eid] = conn
            self.started_at[eid] = now
            for _ in range(4):                        # the adapter may have persisted its READY state meanwhile
                if await self._save(claimed, state="connected", attempts=0, last_error=None, next_attempt_at=None,
                                    started_at=now, heartbeat_at=now) is not None:
                    break
                claimed = await self._status(eid)
                if claimed.get("owner") != self.worker_id:
                    break
            out["started"] += 1
        return out

    def _make(self, app: dict, status: dict) -> GatewayConnection:
        """Build the connection; real adapters receive the persisted resume state
        (session recovery after a worker restart) and a callback to persist it."""
        eid = app["enterprise_id"]
        resume = status.get("resume") or None

        async def on_state(state: Dict[str, Any]) -> None:
            for _ in range(4):
                st = await self.store.get(STATUS, eid)
                if st is None or st.get("owner") != self.worker_id:
                    return                                    # lease lost: never write another owner's state
                try:
                    await self.store.update(STATUS, eid, {"resume": {k: state.get(k) for k in
                                                                     ("session_id", "seq", "resume_gateway_url")}},
                                            expect_version=st["version"])
                    return
                except ConcurrentModification:
                    continue
        try:
            return self.factory(app, resume_state=resume, on_state=on_state)
        except TypeError:
            return self.factory(app)                          # simple factories (mock connections)

    def _backoff(self, now_dt: datetime, attempts: int) -> str:
        return (now_dt + timedelta(seconds=BACKOFF[min(attempts - 1, len(BACKOFF) - 1)])).isoformat()

    def _handler(self, eid: str) -> EventHandler:
        async def on_event(event: Dict[str, Any]) -> Any:
            app = await self.store.get(APPS, eid)          # always the current configuration
            if app is None or app.get("status") != "enabled":
                return "application_disabled"
            return await self.channel.handle_event(app, event)
        return on_event

    async def _stop(self, eid: str, state: Optional[str]) -> None:
        conn = self.connections.pop(eid, None)
        self.started_at.pop(eid, None)
        if conn is not None:
            try:
                await conn.stop()
            except Exception:
                pass
        if state is not None:
            st = await self.store.get(STATUS, eid)
            if st is not None and st.get("owner") in (None, self.worker_id):
                await self._save(st, state=state, owner=None, lease_until=None, attempts=0, next_attempt_at=None)

    async def shutdown(self) -> None:
        for eid in list(self.connections):
            await self._stop(eid, "stopped")


def status_view(doc: Optional[dict]) -> Dict[str, Any]:
    if not doc:
        return {"state": "stopped"}
    return {k: doc.get(k) for k in ("state", "last_error", "attempts", "next_attempt_at", "heartbeat_at",
                                    "started_at", "guilds", "updated_at")}


__all__ = ["GatewayConnection", "GatewayManager", "MockGatewayConnection", "GatewayStartError", "status_view",
           "STATUS"]
