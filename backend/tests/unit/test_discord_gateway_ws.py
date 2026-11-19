"""Real Discord gateway adapter against a SIMULATED gateway server (Phase 4F).
No connection to Discord is made; tokens are synthetic."""
import asyncio
import json
import logging
import unittest

from tests.unit._api_fixtures import HAVE_API

try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

TOKEN_A = "A" * 24 + ".AAAAAA." + "a" * 27
TOKEN_B = "B" * 24 + ".BBBBBB." + "b" * 27
INTENTS = 1 | 512 | 32768


class FakeGateway:
    """Discord gateway protocol subset (v10, JSON)."""

    def __init__(self, tokens, *, heartbeat_ms=100, allowed_intents=1 | 2 | 512 | 4096 | 32768):
        self.tokens = dict(tokens)                     # token -> bot user id
        self.heartbeat_ms = heartbeat_ms
        self.allowed = allowed_intents
        self.ack = True
        self.sessions = {}                             # session id -> {"events": [...], "token": ...}
        self.conns = []
        self.identifies, self.resumes, self.heartbeats, self.close_codes = [], [], 0, []
        self.commands = []
        self.server = None
        self.url = ""

    async def start(self):
        import websockets
        self.server = await websockets.serve(self.handler, "127.0.0.1", 0)
        port = list(self.server.sockets)[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def handler(self, ws):
        import time
        state = {"session": None, "seq": 0, "commands": []}
        self.conns.append((ws, state))
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": self.heartbeat_ms}}))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                now = time.monotonic()
                if msg.get("op") != 1:                       # per connection; heartbeats are tiny in tests
                    state["commands"] = [t for t in state["commands"] if now - t < 60] + [now]
                if len(state["commands"]) > 120:
                    await ws.close(code=4008, reason="rate limited")
                    return
                op = msg["op"]
                if op == 1:
                    self.heartbeats += 1
                    if self.ack:
                        await ws.send(json.dumps({"op": 11}))
                elif op == 2:
                    d = msg["d"]
                    self.identifies.append({"intents": d["intents"], "at": time.monotonic()})
                    if d["token"] not in self.tokens:
                        await ws.close(code=4004, reason="Authentication failed.")
                        return
                    if d["intents"] & ~self.allowed:
                        await ws.close(code=4014, reason="Disallowed intent(s).")
                        return
                    sid = f"s{len(self.sessions) + 1}"
                    self.sessions[sid] = {"events": [], "token": d["token"]}
                    state["session"] = sid
                    await self.dispatch_to(ws, state, "READY", {
                        "session_id": sid, "resume_gateway_url": self.url,
                        "user": {"id": self.tokens[d["token"]]}, "guilds": [{"id": "111111111111111111"}]})
                elif op == 6:
                    d = msg["d"]
                    sess = self.sessions.get(d["session_id"])
                    self.resumes.append(dict(d, token="redacted"))
                    if not sess or sess["token"] != d["token"]:
                        await ws.send(json.dumps({"op": 9, "d": False}))
                        continue
                    state["session"] = d["session_id"]
                    for seq, t, data in sess["events"]:
                        if seq > (d["seq"] or 0):
                            await ws.send(json.dumps({"op": 0, "s": seq, "t": t, "d": data}))
                    state["seq"] = sess["events"][-1][0] if sess["events"] else d["seq"]
                    await self.dispatch_to(ws, state, "RESUMED", {})
        except Exception:
            pass
        finally:
            if ws.close_code is not None:
                self.close_codes.append(ws.close_code)

    async def dispatch_to(self, ws, state, t, d, record=True):
        sess = self.sessions.get(state["session"])
        seq = (sess["events"][-1][0] if sess and sess["events"] else state["seq"]) + 1
        state["seq"] = seq
        if sess is not None and record and t not in ("READY", "RESUMED"):
            sess["events"].append((seq, t, d))
        await ws.send(json.dumps({"op": 0, "s": seq, "t": t, "d": d}))

    def live(self):
        return [(ws, st) for ws, st in self.conns if ws.close_code is None]

    async def dispatch(self, t, d):
        for ws, st in self.live():
            await self.dispatch_to(ws, st, t, d)

    async def record_only(self, t, d):
        """An event the client misses (it is replayed on resume)."""
        for ws, st in self.live():
            sess = self.sessions[st["session"]]
            seq = (sess["events"][-1][0] if sess["events"] else st["seq"]) + 1
            sess["events"].append((seq, t, d))

    async def send_op(self, op, d=None):
        for ws, _ in self.live():
            await ws.send(json.dumps({"op": op, "d": d}))

    async def drop(self):
        for ws, _ in self.live():
            ws.transport.abort() if hasattr(ws, "transport") else await ws.close(code=1011)


async def until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


@unittest.skipUnless(HAVE_WS and HAVE_API, "websockets/FastAPI not installed (runs in the Docker test target)")
class GatewayProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw = await FakeGateway({TOKEN_A: "930000000000000001", TOKEN_B: "940000000000000002"}).start()
        self.events = []
        self.logs = []

        class H(logging.Handler):
            def emit(h, record):
                self.logs.append(record.getMessage())
        self.h = H()
        logging.getLogger().addHandler(self.h)
        logging.getLogger().setLevel(logging.DEBUG)

    async def asyncTearDown(self):
        logging.getLogger().removeHandler(self.h)
        logging.getLogger().setLevel(logging.WARNING)
        await self.gw.close()
        joined = "\n".join(self.logs)
        self.assertNotIn(TOKEN_A, joined)
        self.assertNotIn(TOKEN_B, joined)

    def conn(self, **kw):
        from runtime.discord_gateway_ws import DiscordGatewayConnection, IdentifyLimiter
        kw.setdefault("identify_limiter", IdentifyLimiter(0.0))
        kw.setdefault("backoff", (0.05,))
        kw.setdefault("heartbeat_jitter", 0.1)
        return DiscordGatewayConnection("910000000000000001", gateway_url=self.gw.url, **kw)

    async def on_event(self, ev):
        self.events.append(ev)

    async def test_identify_ready_heartbeats_and_dispatch(self):
        c = self.conn()
        await c.start(TOKEN_A, INTENTS, self.on_event)
        self.assertTrue(c.healthy())
        self.assertEqual((c.session_id, c.bot_user_id, c.guild_count()), ("s1", "930000000000000001", 1))
        self.assertEqual(self.gw.identifies[0]["intents"], INTENTS)
        await self.gw.dispatch("MESSAGE_CREATE", {"id": "1", "content": "hi"})
        self.assertTrue(await until(lambda: self.events))
        self.assertEqual(self.events[0]["t"], "MESSAGE_CREATE")
        self.assertTrue(await until(lambda: self.gw.heartbeats >= 2))
        self.assertEqual(c.seq, 2)
        self.assertNotIn(TOKEN_A, repr(c))
        await c.stop()

    async def test_zombie_connection_is_detected_and_resumed_with_replay(self):
        c = self.conn()
        await c.start(TOKEN_A, INTENTS, self.on_event)
        self.gw.ack = False
        await self.gw.record_only("MESSAGE_CREATE", {"id": "missed"})
        self.assertTrue(await until(lambda: c.stats["zombies"] >= 1))
        self.gw.ack = True
        self.assertTrue(await until(lambda: self.gw.resumes and c.healthy()))
        self.assertEqual(self.gw.resumes[0]["session_id"], "s1")
        self.assertEqual(len(self.gw.identifies), 1)                       # resumed, not re-identified
        self.assertTrue(await until(lambda: any(e["d"].get("id") == "missed" for e in self.events)))
        await c.stop()

    async def test_reconnect_request_and_invalid_session(self):
        c = self.conn()
        await c.start(TOKEN_A, INTENTS, self.on_event)
        await self.gw.send_op(7)                                           # Reconnect
        self.assertTrue(await until(lambda: len(self.gw.resumes) == 1 and c.healthy()))
        await self.gw.send_op(9, False)                                    # not resumable: identify again
        self.assertTrue(await until(lambda: len(self.gw.identifies) == 2 and c.healthy()))
        self.assertEqual(c.session_id, "s2")
        await c.stop()

    async def test_abrupt_drop_reconnects_and_resumes(self):
        c = self.conn()
        await c.start(TOKEN_A, INTENTS, self.on_event)
        await self.gw.drop()
        self.assertTrue(await until(lambda: len(self.gw.resumes) == 1 and c.healthy()))
        self.assertGreaterEqual(c.stats["reconnects"], 1)
        await c.stop()

    async def test_fatal_closes_and_intent_validation(self):
        from runtime.discord_gateway import GatewayStartError
        with self.assertRaises(GatewayStartError) as ctx:
            await self.conn().start("Z" * 24 + ".ZZZZZZ." + "z" * 27, INTENTS, self.on_event)
        self.assertEqual(ctx.exception.category, "unauthorized")
        with self.assertRaises(GatewayStartError) as ctx:
            await self.conn().start(TOKEN_A, 1, self.on_event)               # required intents missing
        self.assertEqual(ctx.exception.category, "intents")
        self.gw.allowed = 1 | 512                                          # server refuses MESSAGE_CONTENT
        with self.assertRaises(GatewayStartError) as ctx:
            await self.conn().start(TOKEN_A, INTENTS, self.on_event)
        self.assertEqual(ctx.exception.category, "intents")

    async def test_identify_spacing_and_send_budget(self):
        import time
        from runtime.discord_gateway_ws import IdentifyLimiter, _SendBudget
        lim = IdentifyLimiter(0.3)
        c1, c2 = self.conn(identify_limiter=lim), self.conn(identify_limiter=lim)
        await asyncio.gather(c1.start(TOKEN_A, INTENTS, self.on_event), c2.start(TOKEN_A, INTENTS, self.on_event))
        gap = abs(self.gw.identifies[1]["at"] - self.gw.identifies[0]["at"])
        self.assertGreaterEqual(gap, 0.28)
        b = _SendBudget(3, 0.3)
        t0 = time.monotonic()
        for _ in range(6):
            await b.take()
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)
        await c1.stop()
        await c2.stop()

    async def test_safe_shutdown_closes_with_1000_and_never_reconnects(self):
        c = self.conn()
        await c.start(TOKEN_A, INTENTS, self.on_event)
        await c.stop()
        self.assertTrue(await until(lambda: 1000 in self.gw.close_codes))
        await asyncio.sleep(0.2)
        self.assertEqual((len(self.gw.identifies), len(self.gw.resumes)), (1, 0))
        self.assertFalse(c.healthy())


@unittest.skipUnless(HAVE_WS and HAVE_API, "websockets/FastAPI not installed (runs in the Docker test target)")
class GatewayManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The real adapter under the Enterprise-scoped supervisor (D-Q14)."""

    async def asyncSetUp(self):
        from tests.unit._api_fixtures import World
        from tests.unit.test_api_webchat import FakeProvider
        self.w = await World().build()
        self.w.S.providers = {"anthropic": FakeProvider(text="Gateway answer.")}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        await self.w.call("PUT", f"/instances/{self.w.iA_dc.id}/channel/discord/settings", "admin",
                          {"listen_mode": "all_channels"})
        from tests.unit._api_fixtures import DISCORD_TOKEN_A, DISCORD_TOKEN_B
        self.gw = await FakeGateway({DISCORD_TOKEN_A: "930000000000000001",
                                     DISCORD_TOKEN_B: "940000000000000002"}).start()
        for eid in (self.w.eA.id, self.w.eB.id):
            r = await self.w.call("POST", f"/platform/enterprises/{eid}/discord-application/gateway/start", "root")
            assert r.status_code == 200

    async def asyncTearDown(self):
        await self.gw.close()

    def manager(self, worker="w1", clock=None):
        from api.discord import channel_for
        from runtime.discord_gateway import GatewayManager
        from runtime.discord_gateway_ws import IdentifyLimiter, connection_factory
        return GatewayManager(self.w.store, box=self.w.S.box, channel=channel_for(self.w.S),
                              factory=connection_factory(gateway_url=self.gw.url, identify_limiter=IdentifyLimiter(0),
                                                         backoff=(0.05,), heartbeat_jitter=0.1),
                              clock=clock or self.w.S.clock, worker_id=worker)

    async def test_one_connection_per_enterprise_application_and_isolated_routing(self):
        m = self.manager()
        out = await m.reconcile()
        self.assertEqual(out["started"], 2)
        self.assertEqual(len(self.gw.identifies), 2)
        event = {"id": "800000000000000001", "channel_id": "222222222222222221", "guild_id": "111111111111111111",
                 "content": "hello", "author": {"id": "500000000000000001"}, "member": {"roles": []}, "mentions": []}
        ws_a = next(ws for ws, st in self.gw.live() if self.gw.sessions[st["session"]]["token"].startswith("A"))
        st_a = next(st for ws, st in self.gw.live() if ws is ws_a)
        await self.gw.dispatch_to(ws_a, st_a, "MESSAGE_CREATE", event)       # only A's connection receives it
        self.assertTrue(await until(lambda: self.w.discord.messages))
        self.assertEqual([m_["app"] for m_ in self.w.discord.messages], ["910000000000000001"])
        status = (await self.w.call("GET", f"/platform/enterprises/{self.w.eA.id}/discord-application", "root")).json()
        self.assertEqual(status["gateway"]["state"], "connected")
        self.assertNotIn("session_id", json.dumps(status))
        await m.shutdown()

    async def test_tenants_cannot_control_the_gateway(self):
        for who in ("owner", "admin", "org_admin"):
            r = await self.w.call("POST", f"/platform/enterprises/{self.w.eA.id}/discord-application/gateway/restart",
                                  who)
            self.assertEqual(r.status_code, 404)

    async def test_session_recovery_after_worker_restart_and_lease_handover(self):
        from datetime import datetime, timedelta
        m1 = self.manager("w1")
        await m1.reconcile()
        self.assertTrue(await until(lambda: True))
        st = await self.w.store.get("discord_gateway_status", self.w.eA.id)
        self.assertEqual(st["resume"]["session_id"] in ("s1", "s2"), True)
        # worker 1 dies without closing (no 1000): its lease expires; worker 2 resumes the session
        for c in m1.connections.values():
            c._stopping = True
            if c._runner:
                c._runner.cancel()
        later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(seconds=200)).isoformat()
        m2 = self.manager("w2", clock=lambda: later)
        out = await m2.reconcile()
        self.assertEqual(out["started"], 2)
        self.assertGreaterEqual(len(self.gw.resumes), 1)                     # session recovered, not re-identified
        await m2.shutdown()

    async def test_bad_token_is_failed_auth_without_retry_storm(self):
        from tests.unit._api_fixtures import DISCORD_TOKEN_A
        del self.gw.tokens[DISCORD_TOKEN_A]
        m = self.manager()
        await m.reconcile()
        st = await self.w.store.get("discord_gateway_status", self.w.eA.id)
        self.assertEqual((st["state"], st["last_error"]), ("failed_auth", "unauthorized"))
        n = len(self.gw.identifies)
        await m.reconcile()
        self.assertEqual(len(self.gw.identifies), n + 0)                     # A is not retried
        await m.shutdown()




class ResumeUrlTrustTests(unittest.TestCase):
    """Phase 6B security review: Resume carries the bot token, so the resume URL is trusted only
    on discord.gg (wss) or the configured gateway host; anything else falls back."""

    def conn(self, gateway="wss://gateway.discord.gg", resume=None):
        from runtime.discord_gateway_ws import DiscordGatewayConnection
        return DiscordGatewayConnection("910000000000000001", gateway_url=gateway,
                                        resume_state={"session_id": "s", "seq": 3, "resume_gateway_url": resume})

    def test_discord_hosts_are_accepted(self):
        for url in ("wss://gateway-us-east1-b.discord.gg", "wss://gateway.discord.gg/?v=10"):
            self.assertEqual(self.conn(resume=url).resume_url, url)

    def test_untrusted_resume_urls_fall_back_to_the_configured_gateway(self):
        for url in ("wss://evil.example", "ws://gateway.discord.gg", "wss://discord.gg.evil.example",
                    "wss://evil.example/discord.gg", "https://gateway.discord.gg", "", None, "wss://[bad"):
            self.assertEqual(self.conn(resume=url).resume_url, "wss://gateway.discord.gg", url)

    def test_the_configured_simulated_gateway_host_is_accepted(self):
        c = self.conn(gateway="ws://127.0.0.1:8765", resume="ws://127.0.0.1:8765/resume")
        self.assertEqual(c.resume_url, "ws://127.0.0.1:8765/resume")
        self.assertEqual(self.conn(gateway="ws://127.0.0.1:8765", resume="ws://127.0.0.1:9999").resume_url,
                         "ws://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
