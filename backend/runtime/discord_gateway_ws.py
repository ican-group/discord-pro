"""The real Discord gateway protocol adapter (Phase 4F; closes the C1 adapter
gap; verified only against simulated gateways — no live connection).

One ``DiscordGatewayConnection`` = one Enterprise Discord application (D-Q14),
created by ``GatewayManager``'s factory, which also owns the worker lease
(the manager stops the connection when the lease is lost).

Protocol (Discord API v10, JSON encoding, no compression):
* **Hello → Identify/Resume.** op 10 gives ``heartbeat_interval``. With a
  stored session (``session_id``, ``seq``, ``resume_gateway_url``) the adapter
  connects to the resume URL and sends op 6 *Resume*; otherwise op 2
  *Identify* (token, intents, properties). Identify attempts are spaced by a
  per-application limiter (``IDENTIFY_SPACING`` seconds; Discord allows one per
  5 s per ``max_concurrency`` bucket) and the remaining ``session_start_limit``
  is respected.
* **Heartbeats.** op 1 with the last sequence, first one after
  ``interval × jitter``. If the previous heartbeat was not acknowledged (op 11)
  the connection is a **zombie**: it is closed with a non-1000 code and resumed.
  op 1 from the server is answered immediately.
* **Dispatch.** op 0 updates ``seq``; READY stores ``session_id``,
  ``resume_gateway_url`` and the bot user; RESUMED ends a resume; every event
  is handed to ``on_event`` through a bounded queue on a separate task, so a
  slow handler never blocks heartbeats.
* **Reconnect.** op 7 → resume; op 9 *Invalid Session* ``d=true`` → resume
  after 1–5 s, ``false`` → forget the session and identify again. Close codes:
  4004 (authentication failed) → ``unauthorized`` (fatal, no retry);
  4010–4014 (shard/version/intents) → fatal ``intents``/``config``; 4007/4009
  → identify again; others → resume. Reconnect attempts back off (capped).
* **Rate limits.** Outbound commands are limited to ``SEND_LIMIT`` per 60 s
  (Discord closes at 120), heartbeats always fit inside the budget.
* **Intent validation** before connecting (``runtime.discord.validate_intents``).
* **Safe shutdown.** ``stop()`` closes with 1000 (session ends), cancels every
  task and drains nothing further.
* **Redaction.** The token is used only in Identify/Resume payloads; it never
  appears in logs, errors, ``repr`` or status; errors are fixed categories.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import websockets

from runtime.discord import validate_intents

API_VERSION = 10
IDENTIFY_SPACING = 5.5
SEND_LIMIT = 110
SEND_WINDOW = 60.0
MAX_EVENT_QUEUE = 1000
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
RECONNECT_BACKOFF = (1, 2, 5, 10, 30, 60)
FATAL_CLOSE = {4004: "unauthorized", 4010: "config", 4011: "config", 4012: "config", 4013: "intents",
               4014: "intents"}
REIDENTIFY_CLOSE = {4007, 4009}

log = logging.getLogger("bitbotpro.discord.gateway")
for _name in ("websockets", "websockets.client", "websockets.protocol"):
    if logging.getLogger(_name).level < logging.WARNING:
        logging.getLogger(_name).setLevel(logging.WARNING)


class IdentifyLimiter:
    """At most one Identify per ``spacing`` seconds per application, measured at the moment the
    Identify is **sent** (Phase 6C fix): a waiter does not start its own attempt until the
    previous attempt for the same application has sent its Identify or ended (bounded by
    ``max_hold``), so a slow connect can no longer make two Identifies arrive together."""

    def __init__(self, spacing: float = IDENTIFY_SPACING, max_hold: float = 20.0):
        self.spacing = spacing
        self.max_hold = max_hold
        self._last: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._inflight: Dict[str, asyncio.Event] = {}

    async def wait(self, key: str) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            pending = self._inflight.get(key)
            if pending is not None:
                try:
                    await asyncio.wait_for(pending.wait(), timeout=self.max_hold)
                except asyncio.TimeoutError:
                    pass
            delay = self._last.get(key, 0.0) + self.spacing - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[key] = time.monotonic()
            self._inflight[key] = asyncio.Event()

    def sent(self, key: str) -> None:
        """The Identify was sent now: the spacing restarts from this moment."""
        self._last[key] = max(self._last.get(key, 0.0), time.monotonic())
        ev = self._inflight.pop(key, None)
        if ev is not None:
            ev.set()

    def done(self, key: str) -> None:
        """The attempt ended (sent or failed); releases a waiter if Identify was never sent."""
        ev = self._inflight.pop(key, None)
        if ev is not None:
            self._last[key] = max(self._last.get(key, 0.0), time.monotonic())
            ev.set()


GLOBAL_IDENTIFY_LIMITER = IdentifyLimiter()


class _SendBudget:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.stamps: list = []

    async def take(self) -> None:
        while True:
            now = time.monotonic()
            self.stamps = [t for t in self.stamps if now - t < self.window]
            if len(self.stamps) < self.limit:
                self.stamps.append(now)
                return
            await asyncio.sleep(self.window - (now - self.stamps[0]) + 0.01)


class GatewayFatal(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class DiscordGatewayConnection:
    """``runtime.discord_gateway.GatewayConnection`` over a real WebSocket."""

    def _trusted_resume_url(self, url) -> Optional[str]:
        """Phase 6B security review: Resume sends the bot token, so a resume URL (from READY or
        from the stored state) is used only if it is ``wss://`` on a ``discord.gg`` host, or the
        same scheme and host as the configured gateway URL (simulated gateways in tests).
        Anything else falls back to the configured gateway URL."""
        from urllib.parse import urlsplit
        if not isinstance(url, str) or not url:
            return self.gateway_url
        try:
            u, g = urlsplit(url), urlsplit(self.gateway_url)
        except ValueError:
            return self.gateway_url
        host = (u.hostname or "").lower()
        if (u.scheme == "wss" and (host == "discord.gg" or host.endswith(".discord.gg"))) or \
                (u.scheme == g.scheme and host == (g.hostname or "").lower() and u.port == g.port):
            return url
        log.warning("discord gateway: untrusted resume URL ignored (app %s)", self.application_id)
        return self.gateway_url

    def __init__(self, application_id: str, *, gateway_url: str, resume_state: Optional[Dict[str, Any]] = None,
                 on_state: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
                 identify_limiter: IdentifyLimiter = GLOBAL_IDENTIFY_LIMITER, send_limit: int = SEND_LIMIT,
                 heartbeat_jitter: Optional[float] = None, backoff=RECONNECT_BACKOFF, open_timeout: float = 10.0):
        self.application_id = application_id
        self.gateway_url = gateway_url
        self.session_id = (resume_state or {}).get("session_id")
        self.seq = (resume_state or {}).get("seq")
        self.resume_url = self._trusted_resume_url((resume_state or {}).get("resume_gateway_url"))
        self.on_state = on_state
        self.limiter = identify_limiter
        self.budget = _SendBudget(send_limit, SEND_WINDOW)
        self.jitter = heartbeat_jitter
        self.backoff = backoff
        self.open_timeout = open_timeout
        self._token: Optional[str] = None
        self._intents = 0
        self._on_event = None
        self._ws = None
        self._runner: Optional[asyncio.Task] = None
        self._stopping = False
        self._connected = asyncio.Event()
        self._error: Optional[str] = None
        self._fatal: Optional[str] = None
        self._acked = True
        self._last_ack = 0.0
        self._guilds: set = set()
        self.bot_user_id: Optional[str] = None
        self.stats = {"identifies": 0, "resumes": 0, "reconnects": 0, "heartbeats": 0, "zombies": 0,
                      "events": 0, "dropped_events": 0}

    def __repr__(self) -> str:
        return f"DiscordGatewayConnection(app={self.application_id}, token=****)"

    # ── GatewayConnection protocol ──
    async def start(self, token: str, intents: int, on_event) -> None:
        from runtime.discord_gateway import GatewayStartError
        try:
            validate_intents(intents)
        except Exception:
            raise GatewayStartError("intents") from None
        self._token, self._intents, self._on_event = token, intents, on_event
        self._runner = asyncio.create_task(self._run(), name=f"discord-gateway-{self.application_id}")
        # wait for the first READY/RESUMED or a fatal outcome (bounded)
        done = asyncio.create_task(self._connected.wait())
        try:
            await asyncio.wait({done, self._runner}, timeout=self.open_timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            done.cancel()
        if self._fatal:
            await self.stop()
            raise GatewayStartError(self._fatal)
        if not self._connected.is_set():
            await self.stop()
            raise GatewayStartError(self._error or "unavailable")

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            try:
                await self._ws.close(code=1000, reason="shutdown")
            except Exception:
                pass
        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
        self._connected.clear()

    def healthy(self) -> bool:
        return self._connected.is_set() and not self._stopping and self._fatal is None

    def last_error(self) -> Optional[str]:
        return self._fatal or self._error

    def guild_count(self) -> int:
        return len(self._guilds)

    def resume_state(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "seq": self.seq, "resume_gateway_url": self.resume_url}

    # ── internals ──
    async def _send(self, payload: Dict[str, Any], *, priority: bool = False) -> None:
        if not priority:
            await self.budget.take()
        await self._ws.send(json.dumps(payload))

    async def _save_state(self) -> None:
        if self.on_state is not None:
            try:
                await self.on_state(self.resume_state())
            except Exception:
                pass

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            resuming = bool(self.session_id and self.seq is not None)
            url = (self.resume_url if resuming and self.resume_url else self.gateway_url)
            url = f"{url.rstrip('/')}/?v={API_VERSION}&encoding=json"
            if not resuming:
                # take the Identify slot BEFORE connecting: once Hello arrives the read loop
                # must run continuously (heartbeat ACKs), so Identify is sent immediately
                await self.limiter.wait(self.application_id)
            try:
                async with websockets.connect(url, max_size=MAX_MESSAGE_BYTES, compression=None,
                                              open_timeout=self.open_timeout, close_timeout=2) as ws:
                    self._ws = ws
                    attempt = 0 if self._connected.is_set() else attempt
                    await self._session(ws, resuming)
            except GatewayFatal as exc:
                self._fatal = exc.category
                self._connected.clear()
                return
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as exc:
                code = getattr(exc.rcvd, "code", None) if getattr(exc, "rcvd", None) else None
                if code in FATAL_CLOSE:
                    self._fatal = FATAL_CLOSE[code]
                    self._connected.clear()
                    return
                if code in REIDENTIFY_CLOSE:
                    self.session_id, self.seq, self.resume_url = None, None, None
                self._error = f"closed_{code}" if code else "connection_lost"
            except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake, websockets.InvalidURI):
                self._error = "connect_failed"
            except Exception:
                self._error = "protocol_error"
            finally:
                self._ws = None
                if not resuming:
                    self.limiter.done(self.application_id)      # never leave a waiter blocked
            if self._stopping:
                return
            self._connected.clear()
            self.stats["reconnects"] += 1
            delay = self.backoff[min(attempt, len(self.backoff) - 1)]
            attempt += 1
            await asyncio.sleep(delay * (0.8 + 0.4 * random.random()))

    async def _session(self, ws, resuming: bool) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.open_timeout))
        if hello.get("op") != 10:
            raise ConnectionError("no hello")
        interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
        self._acked, self._last_ack = True, time.monotonic()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_EVENT_QUEUE)
        heart = asyncio.create_task(self._heartbeat(ws, interval))
        pump = asyncio.create_task(self._dispatch(queue))
        try:
            if resuming:
                self.stats["resumes"] += 1
                await self._send({"op": 6, "d": {"token": self._token, "session_id": self.session_id,
                                                 "seq": self.seq}})
            else:
                self.stats["identifies"] += 1
                await self._send({"op": 2, "d": {"token": self._token, "intents": self._intents,
                                                 "properties": {"os": "linux", "browser": "bitbotpro",
                                                                "device": "bitbotpro"}}})
                self.limiter.sent(self.application_id)
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    self.seq = msg["s"]
                if op == 0:
                    t, d = msg.get("t"), msg.get("d") or {}
                    if t == "READY":
                        self.session_id = d.get("session_id")
                        self.resume_url = self._trusted_resume_url(d.get("resume_gateway_url"))
                        self.bot_user_id = str((d.get("user") or {}).get("id") or "") or None
                        self._guilds = {str(g.get("id")) for g in d.get("guilds") or [] if isinstance(g, dict)}
                        self._error = None
                        self._connected.set()
                        await self._save_state()
                    elif t == "RESUMED":
                        self._error = None
                        self._connected.set()
                        await self._save_state()
                    else:
                        if t == "GUILD_CREATE" and d.get("id"):
                            self._guilds.add(str(d["id"]))
                        elif t == "GUILD_DELETE" and d.get("id"):
                            self._guilds.discard(str(d["id"]))
                        try:
                            queue.put_nowait({"t": t, "d": d})
                        except asyncio.QueueFull:
                            self.stats["dropped_events"] += 1        # bounded: never blocks heartbeats
                        if self.stats["events"] % 50 == 0:
                            await self._save_state()
                elif op == 1:
                    await self._beat(ws)
                elif op == 7:
                    await ws.close(code=4900, reason="reconnect")    # non-1000: keep the session, resume
                    return
                elif op == 9:
                    if not msg.get("d"):
                        self.session_id, self.seq, self.resume_url = None, None, None
                    await asyncio.sleep(1 + random.random() * 4 if self.backoff[0] >= 1 else 0.01)
                    await ws.close(code=4900, reason="invalid session")
                    return
                elif op == 11:
                    self._acked, self._last_ack = True, time.monotonic()
        finally:
            heart.cancel()
            pump.cancel()
            for task in (heart, pump):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _beat(self, ws) -> None:
        self.stats["heartbeats"] += 1
        await self._send({"op": 1, "d": self.seq}, priority=True)

    async def _heartbeat(self, ws, interval: float) -> None:
        jitter = self.jitter if self.jitter is not None else random.random()
        await asyncio.sleep(interval * jitter)
        while True:
            if not self._acked:
                # zombie: no ACK for the previous heartbeat → close (non-1000) and resume
                self.stats["zombies"] += 1
                self._error = "zombie_connection"
                await ws.close(code=4901, reason="zombie")
                return
            self._acked = False
            await self._beat(ws)
            await asyncio.sleep(interval)

    async def _dispatch(self, queue: asyncio.Queue) -> None:
        while True:
            event = await queue.get()
            self.stats["events"] += 1
            try:
                await self._on_event(event)
            except Exception:
                log.warning("discord gateway event handler failed (app %s, event %s)", self.application_id,
                            event.get("t"))


def connection_factory(*, gateway_url: str, store=None, **kw):
    """Factory for ``GatewayManager``: one adapter per Enterprise application,
    resuming from the sanitised state persisted in ``discord_gateway_status``."""
    def make(app: dict, resume_state: Optional[Dict[str, Any]] = None, on_state=None):
        return DiscordGatewayConnection(app["application_id"], gateway_url=gateway_url, resume_state=resume_state,
                                        on_state=on_state, **kw)
    return make


__all__ = ["DiscordGatewayConnection", "IdentifyLimiter", "connection_factory", "FATAL_CLOSE", "SEND_LIMIT"]
