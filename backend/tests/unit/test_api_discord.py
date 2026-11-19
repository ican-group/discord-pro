"""Enterprise Discord (Phase 4D, D-Q14): application ownership, Organisation
connect flow, OAuth security, shared servers/channels, gateway supervision,
routing, DMs (D-Q5), staff handoff, passive mode, delivery, nickname/avatar,
disconnect and deletion, and parity D1–D19. Fake discord.com REST API, mock
gateway connections and a fake AI provider only; nothing live is contacted."""
import asyncio
import json
import logging
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from tests.unit._api_fixtures import (APP_A, APP_B, BOT_USER_A, CH_A1, CH_A2, CH_A3, DISCORD_SECRET_A, DISCORD_TOKEN_A,
                                      DISCORD_TOKEN_A2, DISCORD_TOKEN_B, GUILD_A, HAVE_API, STAFF_ROLE_A, VOICE_A,
                                      World)
from tests.unit.test_api_telegram import CapturedLogs
from tests.unit.test_api_webchat import FakeProvider

USER_1, USER_2 = "500000000000000001", "500000000000000002"
GUILD_X = "666666666666666666"
SECRETS = (DISCORD_TOKEN_A, DISCORD_TOKEN_A2, DISCORD_TOKEN_B, DISCORD_SECRET_A)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class DiscordBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = CapturedLogs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)
        self.w = await World().build()
        self.dc = self.w.discord
        self.provider = FakeProvider(text="Discord answer.")
        self.w.S.providers = {"anthropic": self.provider}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        self.I = self.w.iA_dc.id
        self.now = datetime.fromisoformat(self.w.S.clock())
        self.mid = 800000000000000000

    async def asyncTearDown(self):
        logging.getLogger().removeHandler(self.logs)
        logging.getLogger().setLevel(logging.WARNING)
        joined = "\n".join(self.logs.text)
        for secret in SECRETS:
            self.assertNotIn(secret, joined)                       # never in any log
        dump = json.dumps(await self.w.snapshot(), default=str)
        for secret in SECRETS:
            self.assertNotIn(secret, dump)                          # never stored in clear anywhere

    # helpers
    def clock(self):
        return self.now.isoformat()

    def channel(self):
        from api.discord import channel_for
        return channel_for(self.w.S, clock=self.clock)

    async def app(self, eid=None):
        return await self.w.store.get("discord_applications", eid or self.w.eA.id)

    def event(self, content, *, channel=CH_A1, guild=GUILD_A, author=USER_1, roles=(), mentions=(), bot=False,
              mid=None):
        self.mid += 1
        d = {"id": str(mid or self.mid), "channel_id": channel, "content": content,
             "author": {"id": author, "bot": bot, "username": "u"}, "mentions": [{"id": m} for m in mentions]}
        if guild:
            d["guild_id"] = guild
            d["member"] = {"roles": list(roles)}
        return {"t": "MESSAGE_CREATE", "d": d}

    async def send(self, content, app=None, **kw):
        return await self.channel().handle_event(app or await self.app(), self.event(content, **kw))

    def posted(self):
        return self.dc.messages

    async def settings(self, who="admin", instance=None, **changes):
        r = await self.w.call("PUT", f"/instances/{instance or self.I}/channel/discord/settings", who, changes)
        return r

    async def strict(self):
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"knowledge.minimum_mode": "strict_knowledge_only",
                                           "knowledge.allowed_sources": []}})
        assert r.status_code == 200, r.text
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"knowledge.insufficient_fallback": "Please contact our support team."}})

    async def new_instance(self, bot_id=None, who="root", name="Discord 2"):
        r = await self.w.call("POST", f"/bots/{bot_id or self.w.bA.id}/instances", who,
                              {"name": name, "channel": {"type": "discord"}})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def install(self, instance_id, who="admin", *, guild=GUILD_A, manage=True, code="goodcode12345",
                      in_guild=True, app_id=APP_A):
        r = await self.w.call("POST", f"/instances/{instance_id}/channel/discord/install-url", who)
        assert r.status_code == 200, r.text
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        self.dc.codes[code] = [{"id": guild, "owner": False, "permissions": str(0x20 if manage else 0x400)}]
        if in_guild:
            self.dc.bot_guilds[app_id].add(guild)
        return await self.callback(state, code, guild), state

    async def callback(self, state, code, guild, **extra):
        q = "&".join(f"{k}={v}" for k, v in {"state": state, "code": code, "guild_id": guild, **extra}.items())
        return await self.w.call("GET", f"/hooks/discord/oauth/callback?{q}", None)


# ═════════════════════ D-Q14: application ownership ═════════════════════

class ApplicationOwnershipTests(DiscordBase):
    BASE = "/platform/enterprises/{}/discord-application"

    async def test_tenants_can_never_administer_the_enterprise_application(self):
        base = self.BASE.format(self.w.eA.id)
        calls = [("GET", base, None), ("PUT", base, {"application_id": APP_A, "bot_token": DISCORD_TOKEN_A2}),
                 ("POST", base + "/rotate-token", {"value": DISCORD_TOKEN_A2}),
                 ("POST", base + "/rotate-secret", {"value": "x" * 20}), ("POST", base + "/enable", None),
                 ("POST", base + "/disable", None), ("POST", base + "/gateway/start", None),
                 ("POST", base + "/gateway/restart", None), ("POST", base + "/avatar", {"image": "data:image/png;base64,AA=="})]
        before = await self.w.snapshot()
        for who in ("owner", "admin", "org_admin", "editor", "owner_b"):
            for method, path, body in calls:
                r = await self.w.call(method, path, who, body)
                self.assertEqual(r.status_code, 404, (who, method, path))
        self.assertEqual(await self.w.snapshot(), before)
        # no generic route reaches it either: there is no global Discord platform credential
        r = await self.w.call("PUT", "/platform/credentials/discord_app", "root", {"secret": "x" * 30})
        self.assertEqual(r.status_code, 404)
        r = await self.w.call("PATCH", f"/instances/{self.I}", "admin",
                              {"channel": {"type": "discord", "application_id": APP_B, "guild_id": GUILD_A}})
        self.assertEqual(r.status_code, 422)

    async def test_superadmin_view_and_tenant_view_never_reveal_secrets(self):
        r = await self.w.call("GET", self.BASE.format(self.w.eA.id), "root")
        body = r.json()
        self.assertEqual((body["application_id"], body["configured"], body["status"]), (APP_A, True, "enabled"))
        self.assertTrue(body["token_fingerprint"].startswith("sha256:"))
        self.assertEqual(body["redirect_uri"], "https://api.bitbotpro.io/api/hooks/discord/oauth/callback")
        for s in SECRETS:
            self.assertNotIn(s, r.text)
        self.assertNotIn("bot_token_enc", r.text)
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}/discord", "viewer")
        self.assertEqual(r.json(), {"configured": True, "available": True})
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}/discord", "owner_b")
        self.assertEqual(r.status_code, 404)

    async def test_token_must_belong_to_the_stated_application(self):
        r = await self.w.call("PUT", self.BASE.format(self.w.eA.id), "root",
                              {"application_id": APP_A, "bot_token": DISCORD_TOKEN_B})
        self.assertEqual(r.status_code, 422)
        self.assertIn("different Discord application", r.text)
        r = await self.w.call("POST", self.BASE.format(self.w.eA.id) + "/rotate-token", "root",
                              {"value": DISCORD_TOKEN_B})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.w.S.box.decrypt((await self.app())["bot_token_enc"]), DISCORD_TOKEN_A)

    async def test_one_enterprise_per_application(self):
        org = await self.w.call("POST", "/enterprises", "root", {"name": "C", "slug": "ent-c"})
        eid = org.json()["id"]
        r = await self.w.call("PUT", self.BASE.format(eid), "root",
                              {"application_id": APP_A, "bot_token": DISCORD_TOKEN_A2,
                               "client_secret": "another-secret-1234"})
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(self.w.eA.id, r.text)                       # never says which Enterprise
        self.assertIsNone(await self.app(eid))

    async def test_create_requires_token_and_secret_and_validates_settings(self):
        org = await self.w.call("POST", "/enterprises", "root", {"name": "C", "slug": "ent-c"})
        eid = org.json()["id"]
        self.dc.tokens["C" * 24 + ".CCCCCC." + "c" * 27] = "950000000000000003"
        self.dc.bot_users["950000000000000003"] = "960000000000000003"
        tok = "C" * 24 + ".CCCCCC." + "c" * 27
        r = await self.w.call("PUT", self.BASE.format(eid), "root", {"application_id": "950000000000000003",
                                                                     "bot_token": tok})
        self.assertEqual(r.status_code, 422)
        r = await self.w.call("PUT", self.BASE.format(eid), "root",
                              {"application_id": "950000000000000003", "bot_token": tok,
                               "client_secret": "secret-c-12345678", "permissions": 8})
        self.assertEqual(r.status_code, 422)                         # administrator is never requested
        r = await self.w.call("PUT", self.BASE.format(eid), "root",
                              {"application_id": "950000000000000003", "bot_token": tok,
                               "client_secret": "secret-c-12345678", "intents": 1})
        self.assertEqual(r.status_code, 422)                         # message content intent required
        r = await self.w.call("PUT", self.BASE.format(eid), "root",
                              {"application_id": "950000000000000003", "bot_token": tok,
                               "client_secret": "secret-c-12345678"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["bot_user_id"], r.json()["gateway_desired"]), ("960000000000000003", "stopped"))
        self.assertNotIn(tok, r.text)
        audit = (await self.w.call("GET", "/platform/audit", "root")).text
        self.assertIn("discord_application.create", audit)
        self.assertNotIn(tok, audit)

    async def test_replacing_the_application_requires_disconnected_instances(self):
        tok = "E" * 24 + ".EEEEEE." + "e" * 27
        self.dc.tokens[tok] = "970000000000000004"
        self.dc.bot_users["970000000000000004"] = "980000000000000004"
        body = {"application_id": "970000000000000004", "bot_token": tok, "client_secret": "secret-e-12345678"}
        r = await self.w.call("PUT", self.BASE.format(self.w.eA.id), "root", body)
        self.assertEqual(r.status_code, 409)                          # iA_dc still bound to APP_A
        await self.w.call("POST", f"/instances/{self.I}/channel/disconnect", "admin")
        r = await self.w.call("PUT", self.BASE.format(self.w.eA.id), "root", body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.app())["application_id"], "970000000000000004")

    async def test_rotation_disable_and_gateway_controls(self):
        base = self.BASE.format(self.w.eA.id)
        fp = (await self.app())["bot_token_fp"]
        r = await self.w.call("POST", base + "/rotate-token", "root", {"value": DISCORD_TOKEN_A2})
        self.assertEqual(r.status_code, 200)
        app = await self.app()
        self.assertNotEqual(app["bot_token_fp"], fp)
        self.assertTrue(app["restart_requested_at"])
        self.assertEqual(self.w.S.box.decrypt(app["bot_token_enc"]), DISCORD_TOKEN_A2)
        await self.w.call("POST", base + "/gateway/start", "root")
        self.assertEqual((await self.app())["gateway_desired"], "running")
        await self.w.call("POST", base + "/disable", "root")
        app = await self.app()
        self.assertEqual((app["status"], app["gateway_desired"]), ("disabled", "stopped"))
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}/discord", "viewer")
        self.assertEqual(r.json()["available"], False)
        self.assertEqual(await self.send("hello there"), "application_disabled")
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/install-url", "admin")
        self.assertEqual(r.status_code, 409)

    async def test_avatar_is_superadmin_only_and_validated(self):
        base = self.BASE.format(self.w.eA.id)
        r = await self.w.call("POST", base + "/avatar", "root", {"image": "data:image/png;base64,R0lGODlh"})
        self.assertEqual(r.status_code, 422)                           # GIF bytes labelled as PNG
        r = await self.w.call("POST", base + "/avatar", "root", {"image": "data:image/svg+xml;base64,PHN2Zz4="})
        self.assertEqual(r.status_code, 422)
        import base64
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"1" * 10).decode()
        r = await self.w.call("POST", base + "/avatar", "root", {"image": png})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(c[1] == "PATCH" and c[2] == "/users/@me" for c in self.dc.calls))


# ═════════════════════ Organisation connect flow and OAuth ═════════════════════

class OAuthTests(DiscordBase):
    async def test_install_url_uses_the_enterprise_application_and_a_hashed_single_use_state(self):
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/install-url", "admin")
        q = parse_qs(urlparse(r.json()["url"]).query)
        self.assertEqual(q["client_id"], [APP_A])
        self.assertEqual(q["redirect_uri"], ["https://api.bitbotpro.io/api/hooks/discord/oauth/callback"])
        self.assertEqual(q["scope"], ["bot identify guilds"])
        state = q["state"][0]
        self.assertGreaterEqual(len(state), 40)
        dump = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(state, dump)                                  # stored only as a hash
        for s in SECRETS:
            self.assertNotIn(s, r.text)

    async def test_enterprise_availability_is_visible_to_organisation_members_only_as_two_booleans(self):
        E = self.w.eA.id
        r = await self.w.call("GET", f"/enterprises/{E}/discord", "org_admin")
        self.assertEqual((r.status_code, r.json()), (200, {"available": True, "configured": True}))
        r = await self.w.call("GET", f"/enterprises/{E}/discord", "bot_editor")
        self.assertEqual(r.status_code, 200)
        for who in ("owner_b", "outsider"):
            self.assertEqual((await self.w.call("GET", f"/enterprises/{E}/discord", who)).status_code, 404, who)

    async def test_full_connect_for_a_new_instance(self):
        iid = await self.new_instance()
        await self.settings(instance=iid, listen_mode="all_channels")
        self.dc.bot_guilds[APP_A].add(GUILD_X)
        (r, _) = await self.install(iid, guild=GUILD_X)
        self.assertEqual((r.status_code, r.json()["status"]), (200, "connected"), r.text)
        self.assertNotIn("user-goodcode", r.text)                      # the OAuth access token is never returned
        doc = await self.w.store.get("bot_instances", iid)
        self.assertEqual((doc["channel"]["guild_id"], doc["channel"]["application_id"]), (GUILD_X, APP_A))
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_X}")
        self.assertEqual([h["instance_id"] for h in held["holders"]], [iid])
        token_calls = [c for c in self.dc.calls if c[2] == "/oauth2/token"]
        self.assertEqual(token_calls[-1][0], APP_A)                    # exchanged with the Enterprise client id
        self.assertTrue(await self.w.store.get("discord_installations", f"{APP_A}:{GUILD_X}"))

    async def test_callback_redirects_to_the_admin_result_page_when_configured(self):
        from urllib.parse import parse_qs, urlparse
        self.w.S.settings.oauth_result_origin = "https://bitbotpro.io"
        iid = await self.new_instance()
        await self.settings(instance=iid, listen_mode="all_channels")
        self.dc.bot_guilds[APP_A].add(GUILD_X)
        (r, state) = await self.install(iid, guild=GUILD_X)
        self.assertEqual(r.status_code, 303)
        loc = urlparse(r.headers["location"])
        self.assertEqual(loc.path, "/oauth/discord/result")
        self.assertEqual(parse_qs(loc.query), {"status": ["connected"], "instance_id": [iid], "guild_id": [GUILD_X]})
        self.assertNotIn(state, r.headers["location"])
        self.assertNotIn("goodcode", r.headers["location"])

    async def test_state_replay_expiry_and_tampering_are_rejected(self):
        iid = await self.new_instance()
        (r, state) = await self.install(iid, guild=GUILD_X)
        self.assertEqual(r.json()["status"], "connected")
        r = await self.callback(state, "goodcode12345", GUILD_X)
        self.assertEqual((r.status_code, r.json()["status"]), (400, "invalid_state"))    # single use
        r = await self.callback("x" * 43, "goodcode12345", GUILD_X)
        self.assertEqual(r.status_code, 400)
        r = await self.w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        self.now += timedelta(minutes=11)
        self.w.S.clock = self.clock
        r = await self.callback(state, "goodcode12345", GUILD_X)
        self.assertEqual(r.json()["status"], "invalid_state")                              # expired

    async def test_authority_is_rechecked_at_the_callback(self):
        iid = await self.new_instance()
        r = await self.w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        self.dc.codes["goodcode12345"] = [{"id": GUILD_X, "owner": True, "permissions": "0"}]
        self.dc.bot_guilds[APP_A].add(GUILD_X)
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/members/{self.w.users['admin'].id}", "owner",
                              {"role": "viewer"})
        self.assertIn(r.status_code, (200, 204), r.text)
        before = await self.w.store.get("bot_instances", iid)
        r = await self.callback(state, "goodcode12345", GUILD_X)
        self.assertEqual((r.status_code, r.json()["status"]), (403, "not_authorised"))
        self.assertEqual((await self.w.store.get("bot_instances", iid))["channel"], before["channel"])

    async def test_password_change_or_logout_all_invalidates_pending_states(self):
        iid = await self.new_instance()
        r = await self.w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        self.dc.codes["goodcode12345"] = [{"id": GUILD_X, "owner": True, "permissions": "0"}]
        self.dc.bot_guilds[APP_A].add(GUILD_X)
        uid = self.w.users["admin"].id
        doc = await self.w.store.get("users", uid)
        await self.w.store.update("users", uid, {"token_version": int(doc.get("token_version", 0)) + 1})
        r = await self.callback(state, "goodcode12345", GUILD_X)
        self.assertEqual((r.status_code, r.json()["status"]), (403, "not_authorised"))
        self.assertIsNone((await self.w.store.get("bot_instances", iid))["channel"].get("guild_id"))

    async def test_server_management_authority_comes_from_discord(self):
        iid = await self.new_instance()
        (r, _) = await self.install(iid, guild=GUILD_X, manage=False)
        self.assertEqual((r.status_code, r.json()["status"]), (403, "not_server_manager"))
        self.assertIsNone((await self.w.store.get("bot_instances", iid))["channel"].get("guild_id"))
        # a guild id the user does not belong to (forged query parameter)
        r2 = await self.w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
        state = parse_qs(urlparse(r2.json()["url"]).query)["state"][0]
        self.dc.codes["othercode1234"] = [{"id": GUILD_A, "owner": True, "permissions": "8"}]
        r = await self.callback(state, "othercode1234", GUILD_X)
        self.assertEqual(r.status_code, 403)
        audit = (await self.w.call("GET", f"/enterprises/{self.w.eA.id}/audit", "owner")).text
        self.assertIn("discord.install_denied", audit)

    async def test_bot_must_be_present_and_application_must_be_unchanged(self):
        iid = await self.new_instance()
        (r, _) = await self.install(iid, guild=GUILD_X, in_guild=False)
        self.assertEqual((r.status_code, r.json()["status"]), (409, "bot_not_present"))
        r = await self.w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        await self.w.call("POST", f"/platform/enterprises/{self.w.eA.id}/discord-application/disable", "root")
        r = await self.callback(state, "goodcode12345", GUILD_X)
        self.assertEqual(r.json()["status"], "application_changed")

    async def test_a_state_is_bound_to_its_instance_and_enterprise(self):
        # Enterprise B's admin cannot use (or obtain) a state for A's instance
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/install-url", "owner_b")
        self.assertEqual(r.status_code, 404)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/install-url", "viewer")
        self.assertEqual(r.status_code, 403)
        # a B state binds only B's instance through B's application
        ib = await self.new_instance(bot_id=self.w.bB.id, who="owner_b", name="Discord B")
        (r, _) = await self.install(ib, who="owner_b", guild=GUILD_A, app_id=APP_B)
        self.assertEqual(r.json()["status"], "connected")
        doc = await self.w.store.get("bot_instances", ib)
        self.assertEqual(doc["channel"]["application_id"], APP_B)
        self.assertEqual([c[0] for c in self.dc.calls if c[2] == "/oauth2/token"][-1], APP_B)

    async def test_lifecycle_blocks_install(self):
        from core.hierarchy import Level
        from core.lifecycle import Authority, LifecycleAction
        await self.w.S.hierarchy.change_lifecycle(Level.ORGANISATION, self.w.oA.id, LifecycleAction.SUSPEND,
                                                  authority=Authority.OBJECT, actor_id=self.w.users["owner"].id)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/install-url", "admin")
        self.assertEqual(r.status_code, 409)


# ═════════════════════ shared servers and channels ═════════════════════

class SharedServerTests(DiscordBase):
    async def org2_instance(self):
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner", {"name": "Org 2"})
        o2 = r.json()["id"]
        r = await self.w.call("POST", f"/organisations/{o2}/bots", "owner", {"name": "Bot 2"})
        return await self.new_instance(bot_id=r.json()["id"], who="owner", name="Org2 Discord")

    async def test_two_organisations_share_one_server_in_different_channels(self):
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        i2 = await self.org2_instance()
        (r, _) = await self.install(i2, who="owner")
        self.assertEqual(r.json()["status"], "channel_selection_required")      # whole server not available
        self.assertIsNone((await self.w.store.get("bot_instances", i2))["channel"].get("guild_id"))
        r = await self.w.call("GET", f"/instances/{i2}/channel/discord/channels", "owner")
        states = {c["id"]: c["state"] for c in r.json()["channels"]}
        self.assertEqual(states, {CH_A1: "unavailable", CH_A2: "available", CH_A3: "available"})   # no voice
        self.assertNotIn(self.I, r.text)                                        # holder never revealed
        r = await self.settings("owner", instance=i2, channel_ids=[CH_A1])
        self.assertEqual(r.status_code, 409)                                    # collision prevented
        r = await self.settings("owner", instance=i2, channel_ids=[CH_A2], listen_mode="all_channels")
        self.assertEqual(r.status_code, 200, r.text)
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_A}")
        self.assertEqual(sorted((h["instance_id"], tuple(h["scopes"])) for h in held["holders"]),
                         sorted([(self.I, (CH_A1,)), (i2, (CH_A2,))]))
        # routing: each channel reaches only its own instance
        self.assertIn(await self.send("hi", channel=CH_A2), ("answer", "fallback"))
        self.assertEqual(self.posted()[-1]["channel_id"], CH_A2)
        self.assertEqual(await self.send("hi", channel=CH_A3), "no_route")

    async def test_whole_server_claim_conflicts_with_any_channel_claim(self):
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        i2 = await self.org2_instance()
        await self.install(i2, who="owner")
        await self.settings("owner", instance=i2, channel_ids=[CH_A2])
        r = await self.settings(channel_ids=[], listen_mode="all_channels")  # back to whole server: refused
        self.assertEqual(r.status_code, 409)
        doc = await self.w.store.get("bot_instances", self.I)
        self.assertEqual(doc["channel"]["channel_ids"], [CH_A1])

    async def test_concurrent_claims_for_the_same_channel(self):
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        a, b = await self.org2_instance(), await self.org2_instance()
        await self.install(a, who="owner")
        await self.install(b, who="owner", code="secondcode123")
        ra, rb = await asyncio.gather(self.settings("owner", instance=a, channel_ids=[CH_A2]),
                                      self.settings("owner", instance=b, channel_ids=[CH_A2]))
        self.assertEqual(sorted([ra.status_code, rb.status_code]), [200, 409])

    async def test_channel_selection_is_validated_against_the_server(self):
        for bad in ([VOICE_A], ["777777777777777777"]):
            r = await self.settings(channel_ids=bad)
            self.assertEqual(r.status_code, 422, bad)
            r = await self.w.call("PATCH", f"/instances/{self.I}", "admin",
                                  {"channel": {"type": "discord", "channel_ids": bad, "listen_mode": "specific_channels"}})
            self.assertEqual(r.status_code, 422, bad)                       # generic route validates too
        iid = await self.new_instance()
        r = await self.settings(instance=iid, channel_ids=[CH_A1])
        self.assertEqual(r.status_code, 422)                                # not connected yet

    async def test_cross_enterprise_applications_never_collide_or_cross_route(self):
        ib = await self.new_instance(bot_id=self.w.bB.id, who="owner_b", name="Discord B")
        await self.w.call("PUT", f"/instances/{ib}/channel/discord/settings", "owner_b", {"listen_mode": "all_channels"})
        (r, _) = await self.install(ib, who="owner_b", guild=GUILD_A, app_id=APP_B)
        self.assertEqual(r.json()["status"], "connected")                    # A's whole-server claim is A's app only
        await self.settings(listen_mode="all_channels")
        self.assertEqual(await self.send("hi"), "answer")
        a_msg = self.posted()[-1]
        self.assertEqual(a_msg["app"], APP_A)
        appb = await self.app(self.w.eB.id)
        self.assertIn(await self.send("hi", app=appb), ("answer", "fallback"))      # B has no AI credential
        self.assertEqual(self.posted()[-1]["app"], APP_B)
        # B cannot read A's channels or deliveries
        for path in (f"/instances/{self.I}/channel/discord/channels", f"/instances/{self.I}/deliveries"):
            self.assertEqual((await self.w.call("GET", path, "owner_b")).status_code, 404)

    async def test_disconnect_removes_only_this_instance_and_leaves_only_when_unused(self):
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        i2 = await self.org2_instance()
        await self.install(i2, who="owner")
        await self.settings("owner", instance=i2, channel_ids=[CH_A2])
        r = await self.w.call("POST", f"/instances/{i2}/channel/disconnect", "owner")
        self.assertEqual(r.json(), {"connected": False, "left_server": False})   # still used by iA_dc
        self.assertIn(GUILD_A, self.dc.bot_guilds[APP_A])
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_A}")
        self.assertEqual([h["instance_id"] for h in held["holders"]], [self.I])
        r = await self.w.call("POST", f"/instances/{self.I}/channel/disconnect", "admin")
        self.assertEqual(r.json(), {"connected": False, "left_server": True})
        self.assertNotIn(GUILD_A, self.dc.bot_guilds[APP_A])
        self.assertIsNone(await self.w.store.get("discord_installations", f"{APP_A}:{GUILD_A}"))
        r = await self.w.call("POST", f"/instances/{self.I}/channel/disconnect", "viewer")
        self.assertEqual(r.status_code, 403)


# ═════════════════════ nickname / status / test (D15, D17) ═════════════════════

class NicknameStatusTests(DiscordBase):
    async def test_nickname_rules(self):
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/nickname", "admin", {"nickname": "Helper"})
        self.assertEqual(r.status_code, 200)
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/nickname", "admin",
                              {"nickname": "@everyone"})
        self.assertEqual(r.status_code, 422)
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        i2 = await SharedServerTests.org2_instance(self)
        await self.install(i2, who="owner")
        await self.settings("owner", instance=i2, channel_ids=[CH_A2])
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/nickname", "admin", {"nickname": "Mine"})
        self.assertEqual(r.status_code, 403)                                 # shared server
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/nickname", "root", {"nickname": "Shared"})
        self.assertEqual(r.status_code, 200)

    async def test_status_and_test(self):
        r = await self.w.call("GET", f"/instances/{self.I}/channel/status", "viewer")
        body = r.json()
        self.assertEqual((body["type"], body["connected"], body["guild_id"]), ("discord", True, GUILD_A))
        self.assertEqual(body["application"], {"configured": True, "available": True})
        for s in SECRETS:
            self.assertNotIn(s, r.text)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/test", "admin")
        self.assertEqual(r.json(), {"ok": True})
        self.dc.bot_guilds[APP_A].discard(GUILD_A)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/test", "admin")
        self.assertEqual(r.status_code, 422)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/rotate-secret", "admin")
        self.assertEqual(r.status_code, 404)                                  # Telegram-only operation


# ═════════════════════ gateway supervision ═════════════════════

class GatewayTests(DiscordBase):
    def manager(self, worker="w1", fail=None):
        from runtime.discord_gateway import GatewayManager, MockGatewayConnection
        MockGatewayConnection.instances = []
        return GatewayManager(self.w.store, box=self.w.S.box, channel=self.channel(),
                              factory=lambda app: MockGatewayConnection(app["application_id"], fail_with=fail),
                              clock=self.clock, worker_id=worker)

    async def start(self, eid=None):
        r = await self.w.call("POST", f"/platform/enterprises/{eid or self.w.eA.id}/discord-application/gateway/start",
                              "root")
        assert r.status_code == 200

    async def test_one_connection_per_enabled_running_application(self):
        m = self.manager()
        self.assertEqual((await m.reconcile())["started"], 0)               # desired = stopped
        await self.start()
        await self.start(self.w.eB.id)
        out = await m.reconcile()
        self.assertEqual(out["started"], 2)
        conn = m.connections[self.w.eA.id]
        self.assertTrue(conn.token_seen and conn.intents & 32768)
        st = (await self.w.call("GET", f"/platform/enterprises/{self.w.eA.id}/discord-application", "root")).json()
        self.assertEqual(st["gateway"]["state"], "connected")
        for s in SECRETS:
            self.assertNotIn(s, json.dumps(st))
        self.assertEqual((await m.reconcile())["running"], 2)

    async def test_events_route_through_the_application_that_received_them(self):
        await self.settings(listen_mode="all_channels")
        await self.start()
        m = self.manager()
        await m.reconcile()
        result = await m.connections[self.w.eA.id].emit(self.event("hello"))
        self.assertEqual(result, "answer")
        self.assertEqual(self.posted()[-1]["app"], APP_A)

    async def test_two_workers_never_run_the_same_application(self):
        await self.start()
        m1, m2 = self.manager("w1"), self.manager("w2")
        await m1.reconcile()
        out = await m2.reconcile()
        self.assertEqual((out["started"], out["not_owner"]), (0, 1))
        # owner dies: its lease expires and the other worker takes over
        self.now += timedelta(seconds=200)
        out = await m2.reconcile()
        self.assertEqual(out["started"], 1)

    async def test_restart_stop_disable_and_lifecycle(self):
        await self.start()
        m = self.manager()
        await m.reconcile()
        first = m.connections[self.w.eA.id]
        self.now += timedelta(seconds=5)
        await self.w.call("POST", f"/platform/enterprises/{self.w.eA.id}/discord-application/gateway/restart", "root")
        self.now += timedelta(seconds=5)
        await m.reconcile()
        self.assertTrue(first.stopped)
        self.assertIsNot(m.connections[self.w.eA.id], first)
        from core.hierarchy import Level
        from core.lifecycle import Authority, LifecycleAction
        await self.w.S.hierarchy.change_lifecycle(Level.ENTERPRISE, self.w.eA.id, LifecycleAction.SUSPEND,
                                                  authority=Authority.PLATFORM, actor_id=self.w.users["root"].id)
        out = await m.reconcile()
        self.assertEqual(out["stopped"], 1)
        self.assertNotIn(self.w.eA.id, m.connections)

    async def test_bad_token_stops_retrying_until_rotated_and_failures_back_off(self):
        await self.start()
        m = self.manager(fail="unauthorized")
        out = await m.reconcile()
        self.assertEqual(out["failed"], 1)
        st = await self.w.store.get("discord_gateway_status", self.w.eA.id)
        self.assertEqual((st["state"], st["last_error"]), ("failed_auth", "unauthorized"))
        self.now += timedelta(hours=1)
        self.assertEqual((await m.reconcile())["failed"], 0)                # no retry storm
        await self.w.call("POST", f"/platform/enterprises/{self.w.eA.id}/discord-application/rotate-token", "root",
                          {"value": DISCORD_TOKEN_A2})
        m2 = self.manager()
        self.assertEqual((await m2.reconcile())["started"], 1)
        # transient failures back off
        m3 = self.manager("w3", fail="unavailable")
        await self.start(self.w.eB.id)
        await m3.reconcile()
        st = await self.w.store.get("discord_gateway_status", self.w.eB.id)
        self.assertEqual((st["state"], st["attempts"]), ("reconnecting", 1))
        self.assertEqual((await m3.reconcile())["failed"], 0)                # inside the back-off window

    async def test_unhealthy_connection_reconnects(self):
        await self.start()
        m = self.manager()
        await m.reconcile()
        m.connections[self.w.eA.id].error = "heartbeat_timeout"
        out = await m.reconcile()
        self.assertEqual(out["failed"], 1)
        st = await self.w.store.get("discord_gateway_status", self.w.eA.id)
        self.assertEqual((st["state"], st["last_error"]), ("reconnecting", "heartbeat_timeout"))
        self.now += timedelta(seconds=10)
        self.assertEqual((await m.reconcile())["started"], 1)


# ═════════════════════ message handling, parity D4–D12 ═════════════════════

class MessageTests(DiscordBase):
    async def test_d6_listen_modes(self):
        self.assertEqual(await self.send("hello"), "not_addressed")                      # mention_only default
        self.assertEqual(await self.send(f"<@{BOT_USER_A}> hello", mentions=[BOT_USER_A]), "answer")
        self.assertEqual(self.provider.calls[-1].user_text, "hello")                     # mention stripped
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        self.assertEqual(await self.send("hello"), "answer")
        self.assertEqual(await self.send("hello", channel=CH_A2), "no_route")             # outside the claim
        await self.settings(listen_mode="all_channels", channel_ids=[])
        self.assertEqual(await self.send("hello", channel=CH_A3), "answer")

    async def test_d10_reply_formatting_and_safe_mentions(self):
        await self.settings(listen_mode="all_channels", reply_style="with_mention")
        self.provider.text = "@everyone look: " + ("word " * 500) + "\n```\n" + ("code\n" * 300) + "```"
        self.assertEqual(await self.send("hello"), "answer")
        msgs = self.posted()
        self.assertGreater(len(msgs), 1)
        for m in msgs:
            self.assertLessEqual(len(m["content"]), 2000)
            self.assertEqual(m["allowed_mentions"]["parse"], [])
            self.assertTrue(m["enforce_nonce"])
            self.assertEqual(m["content"].count("```") % 2, 0)                            # no open code fence
        self.assertEqual(msgs[0]["allowed_mentions"].get("users"), [USER_1])
        self.assertTrue(msgs[0]["content"].startswith(f"<@{USER_1}>"))
        self.assertEqual(msgs[0]["message_reference"]["message_id"], str(self.mid))
        self.assertNotIn("message_reference", msgs[1])

    async def test_bots_and_duplicates_are_ignored(self):
        await self.settings(listen_mode="all_channels")
        self.assertEqual(await self.send("hello", bot=True), "ignored_bot")
        self.assertEqual(await self.send("hello", author=BOT_USER_A), "ignored_bot")
        ev = self.event("hello")
        app = await self.app()
        results = await asyncio.gather(*[self.channel().handle_event(app, ev) for _ in range(4)])
        self.assertEqual(sum(r == "answer" for r in results), 1)
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(len(self.provider.calls), 1)                                     # billed once

    async def test_unknown_inactive_and_foreign_events_fail_closed(self):
        await self.settings(listen_mode="all_channels")
        self.assertEqual(await self.send("hello", guild=GUILD_X), "no_route")
        appb = await self.app(self.w.eB.id)
        self.assertEqual(await self.send("hello", app=appb), "no_route")                 # B's app, A's server
        forged = {**(await self.app()), "enterprise_id": self.w.eB.id}
        self.assertEqual(await self.send("hello", app=forged), "foreign_enterprise")
        from core.hierarchy import Level
        from core.lifecycle import Authority, LifecycleAction
        await self.w.S.hierarchy.change_lifecycle(Level.BOT, self.w.bA.id, LifecycleAction.SUSPEND,
                                                  authority=Authority.OBJECT, actor_id=self.w.users["owner"].id)
        self.assertEqual(await self.send("hello"), "unavailable")
        self.assertEqual(self.posted(), [])

    async def test_strict_mode_fails_closed_on_discord(self):
        await self.settings(listen_mode="all_channels")
        await self.strict()
        self.assertIn(await self.send("what is the refund policy?"), ("refusal", "fallback"))
        self.assertEqual(self.provider.calls, [])                                         # no general-knowledge call
        self.assertEqual(self.posted()[-1]["content"], "Please contact our support team.")

    async def test_quota_and_rate_limits(self):
        await self.settings(listen_mode="all_channels")
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 1}})
        self.assertEqual(await self.send("one"), "answer")
        self.assertEqual(await self.send("two"), "quota_exceeded")
        self.assertIn("usage limit", self.posted()[-1]["content"])
        self.assertEqual(len(self.provider.calls), 1)

    async def test_provider_failure_posts_a_safe_apology(self):
        from runtime.errors import ProviderUnavailable
        await self.settings(listen_mode="all_channels")
        self.provider.error = ProviderUnavailable()
        self.assertEqual(await self.send("hello"), "provider_failure")
        self.assertEqual(self.posted()[-1]["content"], "Sorry, something went wrong. Please try again.")
        ledger = await self.w.store.find("usage_ledger", {})
        self.assertTrue(all(l["state"] in ("released", "rejected") for l in ledger))


class DMTests(DiscordBase):
    async def dm(self, author=USER_1, app=None):
        return await self.channel().handle_event(app or await self.app(), self.event("hi there", guild=None,
                                                                                       author=author, channel="99"))

    async def test_dms_are_off_by_default(self):
        self.assertEqual(await self.dm(), "no_route")
        self.assertEqual(self.posted(), [])

    async def test_one_unambiguous_destination_routes(self):
        await self.settings(dm_enabled=True)
        self.assertEqual(await self.dm(), "answer")
        self.assertEqual(await self.dm(author="500000000000000099"), "no_route")        # shares no server

    async def test_multiple_destinations_are_refused(self):
        await self.settings(dm_enabled=True)
        iid = await self.new_instance()
        r = await self.settings(instance=iid, dm_enabled=True)
        self.assertEqual(r.status_code, 422)                                         # DMs need a server first
        self.dc.members[GUILD_X] = {USER_1}
        await self.install(iid, guild=GUILD_X)
        self.assertEqual((await self.settings(instance=iid, dm_enabled=True)).status_code, 200)
        self.assertEqual(await self.dm(), "ambiguous_binding")
        self.assertEqual(self.posted(), [])
        self.assertEqual(await self.dm(author=USER_2), "answer")                          # only in GUILD_A

    async def test_other_enterprises_are_never_candidates(self):
        await self.settings(dm_enabled=True)
        ib = await self.new_instance(bot_id=self.w.bB.id, who="owner_b", name="Discord B")
        await self.install(ib, who="owner_b", guild=GUILD_A, app_id=APP_B)
        r = await self.w.call("PUT", f"/instances/{ib}/channel/discord/settings", "owner_b", {"dm_enabled": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(await self.dm(), "answer")                                       # A's app: only A's instance
        self.assertEqual(self.posted()[-1]["app"], APP_A)
        self.assertIn(await self.dm(app=await self.app(self.w.eB.id)), ("answer", "fallback"))
        self.assertEqual(self.posted()[-1]["app"], APP_B)

    async def test_lifecycle_and_permission_changes(self):
        await self.settings(dm_enabled=True)
        from core.hierarchy import Level
        from core.lifecycle import Authority, LifecycleAction
        await self.w.S.hierarchy.change_lifecycle(Level.BOT_INSTANCE, self.I, LifecycleAction.SUSPEND,
                                                  authority=Authority.OBJECT, actor_id=self.w.users["owner"].id)
        self.assertEqual(await self.dm(), "unavailable")
        await self.w.S.hierarchy.change_lifecycle(Level.BOT_INSTANCE, self.I, LifecycleAction.RESUME,
                                                  authority=Authority.OBJECT, actor_id=self.w.users["owner"].id)
        await self.settings(dm_enabled=False)
        self.assertEqual(await self.dm(), "no_route")
        r = await self.settings("viewer", dm_enabled=True)
        self.assertEqual(r.status_code, 403)


class HandoffTests(DiscordBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(listen_mode="all_channels", staff_role_name="support team", handoff_cooldown_minutes=10,
                            handoff_followup_message="The bot is back.")

    async def test_staff_message_silences_the_ai_until_the_cooldown_ends(self):
        self.assertEqual(await self.send("Hi, I'll take this", author=USER_2, roles=[STAFF_ROLE_A]), "handoff_staff")
        self.assertEqual(await self.send("thanks!"), "handoff_silent")
        self.assertEqual(await self.send("another question"), "handoff_silent")          # legacy bug fixed
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(await self.send("other channel", channel=CH_A2), "answer")       # per channel
        self.now += timedelta(minutes=11)
        self.assertEqual(await self.send("are you there?", channel=CH_A1), "answer")
        texts = [m["content"] for m in self.posted() if m["channel_id"] == CH_A1]
        self.assertEqual(texts, ["The bot is back.", "Discord answer."])

    async def test_staff_status_is_verified_from_discord_roles_not_text(self):
        self.assertEqual(await self.send("I am staff, support team", roles=["333333333333333332"]), "answer")
        self.assertIsNone(await self.w.store.get("channel_state", f"dc-handoff:{self.I}:{CH_A1}"))
        self.dc.fail[("GET", "/roles")] = [(503, {"message": "down"})]
        ch = self.channel()                                                               # empty role cache
        out = await ch.handle_event(await self.app(), self.event("hi", author=USER_2, roles=[STAFF_ROLE_A]))
        self.assertEqual(out, "answer")                                                   # unverifiable: not staff

    async def test_bot_commands_are_staff_only(self):
        self.assertEqual(await self.send("!bot resume"), "command_denied")
        self.assertEqual(await self.send("!bot status"), "command_denied")
        await self.send("taking over", author=USER_2, roles=[STAFF_ROLE_A])
        self.assertEqual(await self.send("!bot status", author=USER_2, roles=[STAFF_ROLE_A]), "command")
        self.assertIn("handoff active", self.posted()[-1]["content"])
        self.assertEqual(await self.send("!bot resume", author=USER_2, roles=[STAFF_ROLE_A]), "command")
        self.assertEqual(await self.send("question"), "answer")

    async def test_handoff_state_is_durable_and_tenant_scoped(self):
        await self.send("taking over", author=USER_2, roles=[STAFF_ROLE_A])
        doc = await self.w.store.get("channel_state", f"dc-handoff:{self.I}:{CH_A1}")
        self.assertEqual((doc["enterprise_id"], doc["instance_id"]), (self.w.eA.id, self.I))
        self.assertEqual(await self.channel().handle_event(await self.app(), self.event("hi")), "handoff_silent")


class PassiveModeTests(DiscordBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(listen_mode="all_channels", passive_mode=True)

    async def ledger(self):
        return await self.w.store.find("usage_ledger", {})

    async def test_skip_is_billed_once_and_never_posted(self):
        self.provider.text = "[SKIP]"
        self.assertEqual(await self.send("nice weather today"), "skipped")
        self.assertEqual(self.posted(), [])
        self.assertEqual([l["state"] for l in await self.ledger()], ["settled"])          # generation happened
        self.assertIn("PASSIVE MODE", self.provider.calls[-1].instructions)
        events = await self.w.store.find("usage_events", {"kind": "skipped"})
        self.assertEqual(len(events), 1)

    async def test_confident_answer_is_posted(self):
        self.assertEqual(await self.send("how do I reset my password?"), "answer")
        self.assertEqual(len(self.posted()), 1)

    async def test_mentions_bypass_passive_mode(self):
        self.provider.text = "Sure."
        await self.send(f"<@{BOT_USER_A}> hi", mentions=[BOT_USER_A])
        self.assertNotIn("PASSIVE MODE", self.provider.calls[-1].instructions)

    async def test_no_charge_and_no_post_when_nothing_was_generated(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 0}})
        self.assertEqual(await self.send("question?"), "skipped")
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.provider.calls, [])
        self.assertFalse([l for l in await self.ledger() if l["state"] == "settled"])

    async def test_strict_mode_is_never_bypassed_by_passive_mode(self):
        await self.strict()
        self.assertEqual(await self.send("refund policy?"), "skipped")
        self.assertEqual((self.posted(), self.provider.calls), ([], []))


class DeliveryTests(DiscordBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(listen_mode="all_channels")

    async def worker(self, seconds):
        from runtime.worker import Worker
        self.now += timedelta(seconds=seconds)
        return await Worker(self.w.store, clock=self.clock, worker_id="w", discord=self.channel()).run_once()

    async def test_timeout_inside_the_nonce_window_is_retried_safely(self):
        import httpx
        state = {"n": 1}

        def timeout_after_send(request):
            if request.url.path.endswith("/messages") and state["n"]:
                state["n"] -= 1
                self.dc.handle(request)                                  # Discord DID create the message
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.dc.intercept = timeout_after_send
        await self.send("hello")
        [d] = await self.w.store.find("outbound_deliveries", {"instance_id": self.I})
        self.assertEqual(d["status"], "retry")
        await self.worker(10)
        [d] = await self.w.store.find("outbound_deliveries", {"instance_id": self.I})
        self.assertEqual(d["status"], "delivered")
        self.assertEqual(len(self.posted()), 1)                           # nonce de-duplicated: one message

    async def test_timeout_outside_the_window_is_uncertain(self):
        import httpx

        def always(request):
            if request.url.path.endswith("/messages"):
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.dc.intercept = always
        await self.send("hello")
        await self.worker(200)
        [d] = await self.w.store.find("outbound_deliveries", {"instance_id": self.I})
        self.assertEqual(d["status"], "uncertain")
        self.dc.intercept = None
        await self.worker(3600)
        self.assertEqual(self.posted(), [])                               # never resent blindly

    async def test_missing_permission_is_final_and_visible(self):
        self.dc.fail[("POST", "/messages")] = [(403, {"message": "Missing Permissions"})]
        await self.send("hello")
        r = await self.w.call("GET", f"/instances/{self.I}/deliveries?status=failed_before_send", "admin")
        [d] = r.json()["deliveries"]
        self.assertEqual(d["last_error"], "forbidden")
        self.assertNotIn("Discord answer", r.text)


class DeletionTests(DiscordBase):
    async def test_enterprise_deletion_leaves_servers_then_removes_the_application(self):
        from core import deletion
        from core.hierarchy import Level
        from runtime.discord import discord_cleanup_executors
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/deletion-preview", "root")
        self.assertEqual(r.status_code, 200, r.text)
        acts = {a["action"]: a for a in r.json()["external_actions"]}
        self.assertEqual(acts["discord_leave_guild"]["target"]["application_id"], APP_A)
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/delete", "root",
                              {"preview_token": r.json()["preview_token"], "confirm_name": self.w.eA.name})
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()["id"]
        self.assertIsNotNone(await self.app())                            # token kept for cleanup
        from runtime.telegram import telegram_cleanup_executors
        executors = {**discord_cleanup_executors(self.w.S.box, transport=self.w.S.discord_transport),
                     **telegram_cleanup_executors(self.w.S.box, transport=self.w.S.telegram_transport)}
        done = await deletion.run_job(self.w.store, job, clock=self.w.S.clock, executors=executors)
        self.assertEqual(done["status"], "completed", done)
        self.assertNotIn(GUILD_A, self.dc.bot_guilds[APP_A])
        self.assertIsNone(await self.app())
        self.assertIsNotNone(await self.app(self.w.eB.id))                # other tenants untouched

    async def test_leave_guild_is_skipped_while_another_instance_needs_the_server(self):
        from runtime.discord import discord_cleanup_executors

        class Ctx:
            store = self.w.store
        execs = discord_cleanup_executors(self.w.S.box, transport=self.w.S.discord_transport)
        action = {"target": {"guild_id": GUILD_A, "application_id": APP_A, "application_enterprise_id": self.w.eA.id}}
        out = await execs["discord_leave_guild"](action, Ctx())
        self.assertEqual(out.status.value if hasattr(out.status, "value") else out.status, "not_required")
        self.assertIn(GUILD_A, self.dc.bot_guilds[APP_A])



# ═════════════════════ Phase 4D corrections ═════════════════════

class RecordingMailer:
    def __init__(self):
        self.sent = []

    async def send_password_reset(self, email, reset_url):
        return None

    async def send_discord_invite(self, email, install_url, *, organisation_name, instance_name, expires_at):
        self.sent.append({"email": email, "url": install_url, "org": organisation_name, "instance": instance_name})


class ShareLinkTests(DiscordBase):
    """C2: D3 share link and invitation email, re-designed securely."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mailer = RecordingMailer()
        self.w.S.mailer = self.mailer
        self.iid = await self.new_instance()

    async def create(self, who="admin", instance=None, **body):
        return await self.w.call("POST", f"/instances/{instance or self.iid}/channel/discord/share-links", who, body)

    async def open_link(self, url):
        path = url.split("https://api.bitbotpro.io", 1)[1]
        return await self.w.call("GET", path[len("/api"):], None)

    def state_of(self, r):
        self.assertEqual(r.status_code, 302, r.text)
        loc = r.headers["location"]
        self.assertTrue(loc.startswith("https://discord.com/oauth2/authorize?"))
        return parse_qs(urlparse(loc).query)

    async def complete(self, q, guild=GUILD_X, code="thirdparty1234", manage=True):
        self.dc.codes[code] = [{"id": guild, "owner": False, "permissions": str(0x20 if manage else 0x400)}]
        self.dc.bot_guilds[APP_A].add(guild)
        return await self.callback(q["state"][0], code, guild)

    async def test_link_contains_no_oauth_state_or_secret_and_is_stored_hashed(self):
        r = await self.create()
        self.assertEqual(r.status_code, 201, r.text)
        url = r.json()["url"]
        token = url.rsplit("/", 1)[1]
        self.assertTrue(url.startswith("https://api.bitbotpro.io/api/public/discord/install/"))
        self.assertNotIn("state=", url)
        self.assertNotIn(token, json.dumps(await self.w.snapshot(), default=str))
        for sec in SECRETS:
            self.assertNotIn(sec, r.text)
        q = self.state_of(await self.open_link(url))
        self.assertEqual(q["client_id"], [APP_A])                  # the Enterprise application, never the client's
        self.assertNotEqual(q["state"][0], token)                   # fresh state per opening

    async def test_a_failing_mail_provider_does_not_fail_the_share_link(self):
        """Phase 6B security review: delivery failure is reported, never a 500 after the link exists."""
        class Failing:
            async def send_discord_invite(self, *a, **k):
                raise RuntimeError("provider down")
        self.w.S.mailer = Failing()
        r = await self.create(email="owner@server.example")
        self.assertIn(r.status_code, (200, 201), r.text)
        self.assertIs(r.json()["email_sent"], False)
        self.w.S.mailer = self.mailer
        r = await self.create(email="owner@server.example")
        self.assertIs(r.json()["email_sent"], True)
        self.assertNotIn("email_sent", (await self.create()).json())          # no e-mail requested

    async def test_invited_server_admin_completes_installation_once(self):
        r = await self.create(email="owner@server.example")
        url = r.json()["url"]
        self.assertEqual([m["email"] for m in self.mailer.sent], ["owner@server.example"])
        self.assertEqual(self.mailer.sent[0]["url"], url)
        q1 = self.state_of(await self.open_link(url))
        q2 = self.state_of(await self.open_link(url))                # opened twice (e.g. two tabs)
        r = await self.complete(q1)
        self.assertEqual(r.json()["status"], "connected", r.text)
        doc = await self.w.store.get("bot_instances", self.iid)
        self.assertEqual((doc["channel"]["guild_id"], doc["channel"]["application_id"]), (GUILD_X, APP_A))
        r = await self.complete(q2, code="thirdparty5678")
        self.assertEqual(r.json()["status"], "share_link_inactive")  # single successful use
        self.assertEqual((await self.open_link(url)).status_code, 410)
        links = (await self.w.call("GET", f"/instances/{self.iid}/channel/discord/share-links", "admin")).json()
        self.assertEqual(links["share_links"][0]["status"], "used")

    async def test_concurrent_acceptances_of_one_invitation_succeed_once(self):
        url = (await self.create()).json()["url"]
        q1 = self.state_of(await self.open_link(url))
        q2 = self.state_of(await self.open_link(url))
        self.dc.codes["concurrent1111"] = [{"id": GUILD_X, "owner": True, "permissions": "0"}]
        self.dc.codes["concurrent2222"] = [{"id": GUILD_X, "owner": True, "permissions": "0"}]
        self.dc.bot_guilds[APP_A].add(GUILD_X)
        r1, r2 = await asyncio.gather(self.callback(q1["state"][0], "concurrent1111", GUILD_X),
                                      self.callback(q2["state"][0], "concurrent2222", GUILD_X))
        self.assertEqual(sorted([r1.json()["status"], r2.json()["status"]]), ["connected", "share_link_used"])

    async def test_server_authority_and_expected_server_are_enforced(self):
        r = await self.create(expected_guild_id=GUILD_X)
        q = self.state_of(await self.open_link(r.json()["url"]))
        self.assertEqual((q["guild_id"], q["disable_guild_select"]), ([GUILD_X], ["true"]))
        r2 = await self.complete(q, guild=GUILD_A)
        self.assertEqual(r2.json()["status"], "wrong_server")
        q = self.state_of(await self.open_link(r.json()["url"]))
        r2 = await self.complete(q, manage=False, code="nomanage12345")
        self.assertEqual((r2.status_code, r2.json()["status"]), (403, "not_server_manager"))
        self.assertIsNone((await self.w.store.get("bot_instances", self.iid))["channel"].get("guild_id"))

    async def test_revocation_and_creator_permission_loss(self):
        r = await self.create()
        sid, url = r.json()["id"], r.json()["url"]
        q = self.state_of(await self.open_link(url))
        await self.w.call("DELETE", f"/instances/{self.iid}/channel/discord/share-links/{sid}", "admin")
        self.assertEqual((await self.complete(q)).json()["status"], "share_link_inactive")
        self.assertEqual((await self.open_link(url)).status_code, 410)
        r = await self.create()
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/members/{self.w.users['admin'].id}", "owner",
                          {"role": "viewer"})
        self.assertEqual((await self.open_link(r.json()["url"])).status_code, 410)

    async def test_invitations_are_tenant_and_instance_scoped(self):
        r = await self.create()
        sid = r.json()["id"]
        other = await self.new_instance(name="Other")
        for who, code in (("owner_b", 404), ("viewer", 403)):
            self.assertEqual((await self.create(who=who)).status_code, code)
            r = await self.w.call("GET", f"/instances/{self.iid}/channel/discord/share-links", who)
            self.assertEqual(r.status_code, code)
        r = await self.w.call("DELETE", f"/instances/{other}/channel/discord/share-links/{sid}", "admin")
        self.assertEqual(r.status_code, 404)                          # wrong instance
        q = self.state_of(await self.open_link((await self.create()).json()["url"]))
        await self.complete(q)
        self.assertIsNone((await self.w.store.get("bot_instances", other))["channel"].get("guild_id"))

    async def test_invitation_email_validation_and_rate_limit(self):
        for bad in ("not-an-email", "a@b\r\nBcc: x@y.z", "<x@y.z>"):
            self.assertEqual((await self.create(email=bad)).status_code, 422)
        await self.w.call("PATCH", f"/organisations/{self.w.oA.id}", "owner", {"name": "Org\r\nBcc: evil"})
        await self.create(email="ok@server.example")
        self.assertNotIn("\n", self.mailer.sent[-1]["org"])
        codes = [(await self.create(email="ok@server.example")).status_code for _ in range(25)]
        self.assertIn(429, codes)


class HandoffAdminTests(DiscordBase):
    """C3: tenant-scoped handoff administration; chat commands stay staff-only."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(listen_mode="all_channels", staff_role_name="support team", handoff_cooldown_minutes=10)

    async def test_view_resume_and_set(self):
        await self.send("on it", author=USER_2, roles=[STAFF_ROLE_A])
        r = await self.w.call("GET", f"/instances/{self.I}/channel/discord/handoffs", "viewer")
        [h] = r.json()["handoffs"]
        self.assertEqual((h["channel_id"], h["active"], h["source"]), (CH_A1, True, "staff_message"))
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/handoffs/{CH_A1}/resume", "viewer")
        self.assertEqual(r.status_code, 403)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/discord/handoffs/{CH_A1}/resume", "admin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(await self.send("question"), "answer")
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/handoffs/{CH_A2}", "admin", {"minutes": 30})
        self.assertEqual((r.status_code, r.json()["source"]), (200, "admin"), r.text)
        self.assertEqual(await self.send("hi", channel=CH_A2), "handoff_silent")
        self.now += timedelta(minutes=31)
        self.assertEqual(await self.send("hi", channel=CH_A2), "answer")

    async def test_controls_are_validated_and_scoped(self):
        for bad in ("abc", VOICE_A, "777777777777777777"):
            r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/handoffs/{bad}", "admin", {"minutes": 5})
            self.assertEqual(r.status_code, 422, bad)
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/handoffs/{CH_A1}", "admin", {"minutes": 0})
        self.assertEqual(r.status_code, 422)
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        r = await self.w.call("PUT", f"/instances/{self.I}/channel/discord/handoffs/{CH_A2}", "admin", {"minutes": 5})
        self.assertEqual(r.status_code, 422)                          # outside this instance's claim
        # another Organisation's admin and another Enterprise cannot touch it
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner", {"name": "Org 2"})
        b = await self.w.call("POST", f"/organisations/{r.json()['id']}/bots", "owner", {"name": "Bot 2"})
        i2 = await self.new_instance(bot_id=b.json()["id"], who="owner", name="Org2 Discord")
        for who, code in (("org_admin", 404), ("owner_b", 404)):
            r = await self.w.call("POST", f"/instances/{i2}/channel/discord/handoffs/{CH_A1}/resume", who)
            self.assertEqual(r.status_code, code, who)
        r = await self.w.call("GET", f"/instances/{self.I}/channel/discord/handoffs", "owner_b")
        self.assertEqual(r.status_code, 404)

    async def test_staff_commands_still_staff_only(self):
        self.assertEqual(await self.send("!bot resume"), "command_denied")
        self.assertEqual(await self.send("!bot status", author=USER_2, roles=[STAFF_ROLE_A]), "command")


class PassiveUsageTests(DiscordBase):
    """C4: generation ⇒ charge (once); no generation ⇒ no charge; attribution."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(listen_mode="all_channels", passive_mode=True)

    async def test_generated_skip_is_attributed_and_billed_once_even_when_duplicated(self):
        self.provider.text = "[SKIP]"
        ev = self.event("nice weather")
        app = await self.app()
        results = await asyncio.gather(*[self.channel().handle_event(app, ev) for _ in range(3)])
        self.assertEqual(sum(r == "skipped" for r in results), 1)
        self.assertEqual(len(self.provider.calls), 1)
        ledger = await self.w.store.find("usage_ledger", {})
        self.assertEqual([l["state"] for l in ledger], ["settled"])
        events = await self.w.store.find("usage_events", {})
        self.assertTrue(events)
        for e in events:
            self.assertEqual((e["enterprise_id"], e["organisation_id"], e["bot_id"], e["instance_id"]),
                             (self.w.eA.id, self.w.oA.id, self.w.bA.id, self.I))
        self.assertEqual(len([e for e in events if e["kind"] == "skipped"]), 1)
        self.assertEqual(self.posted(), [])

    async def test_provider_failure_in_passive_mode_is_silent_and_unbilled(self):
        from runtime.errors import ProviderUnavailable
        self.provider.error = ProviderUnavailable()
        out = await self.send("how do I reset my password?")
        self.assertIn(out, ("skipped", "provider_failure"))
        self.assertEqual(self.posted(), [])                           # no apology in passive mode
        self.assertFalse([l for l in await self.w.store.find("usage_ledger", {}) if l["state"] == "settled"])

    async def test_strict_mode_generation_failure_is_never_posted(self):
        await self.strict()
        self.provider.text = "General knowledge answer."
        self.assertEqual(await self.send("anything?"), "skipped")
        self.assertEqual((self.posted(), self.provider.calls), ([], []))


class SharedCleanupTests(DiscordBase):
    """C6: disconnect/deletion never disturb another Organisation, the shared
    gateway or the Enterprise application token."""

    async def two_orgs(self):
        await self.settings(listen_mode="specific_channels", channel_ids=[CH_A1])
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner", {"name": "Org 2"})
        self.o2 = r.json()["id"]
        b = await self.w.call("POST", f"/organisations/{self.o2}/bots", "owner", {"name": "Bot 2"})
        self.i2 = await self.new_instance(bot_id=b.json()["id"], who="owner", name="Org2 Discord")
        await self.install(self.i2, who="owner")
        r = await self.settings("owner", instance=self.i2, channel_ids=[CH_A2])
        assert r.status_code == 200, r.text

    async def gateway(self):
        from runtime.discord_gateway import GatewayManager, MockGatewayConnection
        await self.w.call("POST", f"/platform/enterprises/{self.w.eA.id}/discord-application/gateway/start", "root")
        m = GatewayManager(self.w.store, box=self.w.S.box, channel=self.channel(),
                           factory=lambda app: MockGatewayConnection(app["application_id"]), clock=self.clock)
        await m.reconcile()
        return m

    async def test_disconnecting_one_organisation_leaves_everything_else_intact(self):
        await self.two_orgs()
        m = await self.gateway()
        app_before = await self.app()
        r = await self.w.call("POST", f"/instances/{self.i2}/channel/disconnect", "owner")
        self.assertEqual(r.json()["left_server"], False)
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_A}")
        self.assertEqual([(h["instance_id"], h["scopes"]) for h in held["holders"]], [(self.I, [CH_A1])])
        self.assertIn(GUILD_A, self.dc.bot_guilds[APP_A])
        app_after = await self.app()
        self.assertEqual((app_after["bot_token_enc"], app_after["status"], app_after["gateway_desired"]),
                         (app_before["bot_token_enc"], "enabled", "running"))
        self.assertEqual((await m.reconcile())["running"], 1)        # the shared gateway keeps running
        self.assertEqual(await self.send("hi", channel=CH_A1), "answer")

    async def test_concurrent_disconnects_leave_once_and_remove_only_their_bindings(self):
        await self.two_orgs()
        m = await self.gateway()
        r1, r2 = await asyncio.gather(self.w.call("POST", f"/instances/{self.I}/channel/disconnect", "admin"),
                                      self.w.call("POST", f"/instances/{self.i2}/channel/disconnect", "owner"))
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertTrue(r1.json()["left_server"] or r2.json()["left_server"])
        self.assertNotIn(GUILD_A, self.dc.bot_guilds[APP_A])
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_A}")
        self.assertFalse(held and held.get("holders"))
        self.assertTrue((await self.app())["bot_token_enc"])
        self.assertEqual((await m.reconcile())["running"], 1)

    async def test_disconnect_racing_a_channel_change_keeps_the_other_binding(self):
        await self.two_orgs()
        await asyncio.gather(self.w.call("POST", f"/instances/{self.i2}/channel/disconnect", "owner"),
                             self.settings(channel_ids=[CH_A1, CH_A3]))
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_A}")
        self.assertEqual([h["instance_id"] for h in held["holders"]], [self.I])
        self.assertIn(GUILD_A, self.dc.bot_guilds[APP_A])            # leave re-checked: server still needed

    async def test_deleting_another_organisation_does_not_leave_a_shared_server(self):
        from core import deletion
        from runtime.discord import discord_cleanup_executors
        await self.two_orgs()
        m = await self.gateway()
        token_before = (await self.app())["bot_token_enc"]
        pv = (await self.w.call("POST", f"/organisations/{self.o2}/deletion-preview", "owner")).json()
        r = await self.w.call("POST", f"/organisations/{self.o2}/delete", "owner",
                              {"preview_token": pv["preview_token"], "confirm_name": "Org 2"})
        self.assertEqual(r.status_code, 202, r.text)
        done = await deletion.run_job(self.w.store, r.json()["id"], clock=self.w.S.clock,
                                      executors=discord_cleanup_executors(self.w.S.box,
                                                                          transport=self.w.S.discord_transport))
        self.assertEqual(done["status"], "completed", done)
        leave = [a for a in done["external_actions"] if a["action"] == "discord_leave_guild"]
        self.assertEqual([a["state"] for a in leave], ["not_required"])
        self.assertIn(GUILD_A, self.dc.bot_guilds[APP_A])
        held = await self.w.store.get("channel_bindings", f"discord:{APP_A}:{GUILD_A}")
        self.assertEqual([h["instance_id"] for h in held["holders"]], [self.I])
        self.assertEqual((await self.app())["bot_token_enc"], token_before)
        self.assertEqual((await m.reconcile())["running"], 1)


if __name__ == "__main__":
    unittest.main()
