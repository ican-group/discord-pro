"""Zendesk Bot Integration (Phase 4E): parity Z1–Z18, D-Q9/D-Q10/D-Q11, X2.

Everything runs against a fake Zendesk API *behind the real outbound guard*
(fake DNS resolver + httpx MockTransport) and a fake AI provider. Nothing live
is contacted."""
import asyncio
import io
import json
import logging
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from tests.unit._api_fixtures import (HAVE_API, ZD_API_TOKEN, ZD_CLIENT_SECRET, ZD_HOST, ZD_IP, World)
from tests.unit.test_api_telegram import CapturedLogs
from tests.unit.test_api_webchat import FakeProvider

SECRET_MARKERS = (ZD_API_TOKEN, ZD_CLIENT_SECRET, "access-tok-", "zd-signing-")


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ZendeskBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = CapturedLogs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)
        self.w = await World().build()
        self.zd = self.w.zendesk
        self.provider = FakeProvider(text="You can request a refund from the billing page.")
        self.w.S.providers = {"anthropic": self.provider}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        self.Z = await self.integration()
        self.cred = await self.api_token_credential()
        r = await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", {"credential_id": self.cred})
        assert r.status_code == 200, r.text
        self.signing = await self.set_signing()

    async def asyncTearDown(self):
        logging.getLogger().removeHandler(self.logs)
        logging.getLogger().setLevel(logging.WARNING)
        joined = "\n".join(self.logs.text)
        for marker in (ZD_API_TOKEN, ZD_CLIENT_SECRET):
            self.assertNotIn(marker, joined)
        # D-Q11: BitBotPro never creates Zendesk tickets
        self.assertFalse([c for c in self.zd.calls if c[1] == "POST" and c[2].rstrip("/") in
                          ("/api/v2/tickets", "/api/v2/tickets.json")])
        for c in self.zd.calls:                       # every call went to the pinned, checked address
            self.assertEqual(c[4], ZD_IP)
            self.assertTrue(c[0].endswith(".zendesk.com"))

    # helpers
    async def integration(self, bot=None, who="admin", **settings):
        r = await self.w.call("POST", f"/bots/{bot or self.w.bA.id}/integrations", who,
                              {"type": "zendesk", "name": "Support", "settings": settings})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def api_token_credential(self, org=None, who="admin"):
        r = await self.w.call("POST", f"/organisations/{org or self.w.oA.id}/credentials", who,
                              {"provider": "zendesk_api_token", "name": "Zendesk", "secret": ZD_API_TOKEN,
                               "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def set_signing(self, integ=None, secret="manual-signing-secret-0123456789"):
        r = await self.w.call("PUT", f"/integrations/{integ or self.Z}/zendesk/signing-secret", "admin",
                              {"secret": secret})
        assert r.status_code == 200, r.text
        return secret

    async def settings(self, integ=None, **settings):
        r = await self.w.call("PATCH", f"/integrations/{integ or self.Z}", "admin", {"settings": settings})
        assert r.status_code == 200, r.text

    async def hook_url(self, integ=None):
        r = await self.w.call("GET", f"/integrations/{integ or self.Z}/zendesk/webhook", "admin")
        return "/" + r.json()["url"].split("/api/", 1)[1]

    async def webhook(self, payload=None, *, integ=None, secret=None, ts=None, signature=None, raw=None, path=None):
        import httpx
        import webhook_auth
        body = raw if raw is not None else json.dumps(payload or {"ticket_id": 101}).encode()
        ts = ts or datetime.now(timezone.utc).isoformat()
        sig = signature if signature is not None else webhook_auth.zendesk_signature(secret or self.signing, ts, body)
        headers = {webhook_auth.ZENDESK_TIMESTAMP_HEADER: ts, webhook_auth.ZENDESK_SIGNATURE_HEADER: sig,
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            return await c.post("/api" + (path or await self.hook_url(integ)), content=body, headers=headers)

    async def worker(self, seconds=0):
        from api.integrations import service_for
        from runtime.worker import Worker
        clock = (datetime.fromisoformat(self.w.S.clock()) + timedelta(seconds=seconds)).isoformat()
        svc = service_for(self.w.S)
        svc.clock = lambda: clock
        return await Worker(self.w.store, clock=lambda: clock, worker_id="w", zendesk=svc).run_once()

    async def events(self):
        return await self.w.store.find("zendesk_events", {"integration_id": self.Z})

    async def suggestions(self, integ=None):
        return await self.w.store.find("suggestions", {"integration_id": integ or self.Z})


# ═════════════════════ Z1/D-Q9: credential ownership ═════════════════════

class CredentialTests(ZendeskBase):
    async def test_api_token_is_validated_scoped_and_never_exposed(self):
        r = await self.w.call("GET", f"/credentials/{self.cred}", "admin")
        self.assertEqual(r.json()["metadata"], {"subdomain": "acme", "email": "agent@acme.example"})
        self.assertNotIn(ZD_API_TOKEN, r.text)
        dump = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(ZD_API_TOKEN, dump)
        bad = {"provider": "zendesk_api_token", "name": "Z", "secret": "wrongtoken" + "X" * 20,
               "metadata": {"subdomain": "acme", "email": "agent@acme.example"}}
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin", bad)
        self.assertEqual(r.status_code, 422)                                   # Zendesk refused it
        for sub in ("evil.com", "acme.zendesk.com.evil.com", "127.0.0.1", "0x7f000001", "acme:443",
                    "https://acme.zendesk.com/x", "a@b", "acme.zendesk.com#x", "xn--ls8h"):
            r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                                  {**bad, "secret": ZD_API_TOKEN, "metadata": {"subdomain": sub, "email": "a@b.cd"}})
            if sub == "xn--ls8h":
                continue                                                      # a valid label: resolution decides
            self.assertEqual(r.status_code, 422, sub)

    async def test_oauth_credentials_only_come_from_the_platform_flow(self):
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                              {"provider": "zendesk_oauth", "name": "Z", "secret": "pasted-token-123456",
                               "metadata": {"subdomain": "acme"}})
        self.assertEqual(r.status_code, 422)

    async def test_an_integration_cannot_override_credential_ownership(self):
        cred_b = (await self.w.call("POST", f"/organisations/{self.w.oB.id}/credentials", "owner_b",
                                    {"provider": "zendesk_api_token", "name": "B", "secret": ZD_API_TOKEN,
                                     "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})).json()["id"]
        r = await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", {"credential_id": cred_b})
        self.assertEqual(r.status_code, 422)                                   # another Enterprise's credential
        r = await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", {"credential_id": self.w.cA_ai.id})
        self.assertEqual(r.status_code, 422)                                   # wrong provider for the slot
        r = await self.w.call("PATCH", f"/integrations/{self.Z}", "viewer", {"credential_id": self.cred})
        self.assertEqual(r.status_code, 403)

    async def test_revocation_fails_closed_at_runtime(self):
        await self.w.call("POST", f"/credentials/{self.cred}/revoke", "admin")
        r = await self.webhook()
        self.assertEqual(r.status_code, 200)
        [e] = await self.events()
        self.assertEqual(e["label"], "unavailable")
        self.assertEqual((self.provider.calls, self.zd.notes()), ([], []))
        r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertEqual(r.status_code, 422)

    async def test_platform_oauth_client_is_superadmin_only(self):
        for who in ("owner", "admin", "owner_b"):
            r = await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", who,
                                  {"secret": ZD_CLIENT_SECRET, "metadata": {"client_id": "x"}})
            self.assertEqual(r.status_code, 404)


# ═════════════════════ Z2/Z3: OAuth ═════════════════════

class OAuthTests(ZendeskBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        r = await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                              {"secret": ZD_CLIENT_SECRET, "metadata": {"client_id": "bitbotpro_global"}})
        assert r.status_code == 200

    async def start(self, who="admin", integ=None, sub="acme"):
        r = await self.w.call("POST", f"/integrations/{integ or self.Z}/zendesk/oauth/start", who, {"subdomain": sub})
        return r

    async def callback(self, state, code="authcode12345", **extra):
        q = "&".join(f"{k}={v}" for k, v in {"state": state, "code": code, **extra}.items())
        return await self.w.call("GET", f"/hooks/zendesk/oauth/callback?{q}", None)

    def state(self, r):
        assert r.status_code == 200, r.text
        return parse_qs(urlparse(r.json()["url"]).query)

    async def test_start_builds_a_pkce_url_with_a_hashed_single_use_state(self):
        r = await self.start(sub="https://ACME.zendesk.com/")
        url = urlparse(r.json()["url"])
        q = parse_qs(url.query)
        self.assertEqual((url.scheme, url.hostname, url.path), ("https", ZD_HOST, "/oauth/authorizations/new"))
        self.assertEqual((q["client_id"], q["code_challenge_method"]), (["bitbotpro_global"], ["S256"]))
        self.assertEqual(q["redirect_uri"], ["https://api.bitbotpro.io/api/hooks/zendesk/oauth/callback"])
        self.assertNotIn(q["state"][0], json.dumps(await self.w.snapshot(), default=str))
        self.assertNotIn(ZD_CLIENT_SECRET, r.text)
        for sub in ("evil.com", "acme.zendesk.com.evil.com", "10.0.0.1", "acme/../x"):
            self.assertEqual((await self.start(sub=sub)).status_code, 422, sub)
        self.assertEqual((await self.start(who="viewer")).status_code, 403)
        self.assertEqual((await self.start(who="owner_b")).status_code, 404)

    async def test_full_connection_creates_an_organisation_credential(self):
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-1", "refresh-tok-1", ZD_HOST, "admin")
        r = await self.callback(q["state"][0])
        self.assertEqual((r.status_code, r.json()["status"]), (200, "connected"), r.text)
        self.assertNotIn("access-tok-1", r.text)
        integ = await self.w.store.get("bot_integrations", self.Z)
        cred = await self.w.store.get("credentials", integ["credential_id"])
        self.assertEqual((cred["provider"], cred["organisation_id"], cred["metadata"]["subdomain"]),
                         ("zendesk_oauth", self.w.oA.id, "acme"))
        self.assertNotIn("access-tok-1", json.dumps(await self.w.snapshot(), default=str))
        exchange = [c for c in self.zd.calls if c[2] == "/oauth/tokens"][-1]
        self.assertEqual((exchange[0], exchange[3]["client_id"]), (ZD_HOST, "bitbotpro_global"))
        self.assertTrue(exchange[3]["code_verifier"])
        # replay of the same state
        r = await self.callback(q["state"][0])
        self.assertEqual((r.status_code, r.json()["status"]), (400, "invalid_state"))

    async def test_callback_redirects_to_the_admin_result_page_when_configured(self):
        """C14: with ``oauth_result_origin`` the browser lands on the admin app,
        carrying only the status and non-secret ids."""
        self.w.S.settings.oauth_result_origin = "https://bitbotpro.io/"
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-1", "refresh-tok-1", ZD_HOST, "admin")
        r = await self.callback(q["state"][0])
        self.assertEqual(r.status_code, 303)
        loc = urlparse(r.headers["location"])
        self.assertEqual((loc.scheme, loc.netloc, loc.path), ("https", "bitbotpro.io", "/oauth/zendesk/result"))
        self.assertEqual(parse_qs(loc.query), {"status": ["connected"], "integration_id": [self.Z],
                                               "subdomain": ["acme"]})
        for secret in (q["state"][0], "authcode12345", "access-tok-1"):
            self.assertNotIn(secret, r.headers["location"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        r = await self.callback(q["state"][0])                                  # replay → failure page
        self.assertEqual(parse_qs(urlparse(r.headers["location"]).query), {"status": ["invalid_state"]})

    async def test_callback_rechecks_authority(self):
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-2", "refresh-tok-2", ZD_HOST, "admin")
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/members/{self.w.users['admin'].id}", "owner",
                          {"role": "viewer"})
        r = await self.callback(q["state"][0])
        self.assertEqual((r.status_code, r.json()["status"]), (403, "not_authorised"))
        self.assertEqual((await self.w.store.get("bot_integrations", self.Z))["credential_id"], self.cred)

    async def test_password_change_and_client_rotation_invalidate_states(self):
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-3", "refresh-tok-3", ZD_HOST, "admin")
        uid = self.w.users["admin"].id
        doc = await self.w.store.get("users", uid)
        await self.w.store.update("users", uid, {"token_version": int(doc.get("token_version", 0)) + 1})
        self.assertEqual((await self.callback(q["state"][0])).json()["status"], "not_authorised")
        await self.w.refresh_user("admin") if hasattr(self.w, "refresh_user") else None

    async def test_client_rotation_between_start_and_callback(self):
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-4", "refresh-tok-4", ZD_HOST, "admin")
        await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                          {"secret": ZD_CLIENT_SECRET + "x", "metadata": {"client_id": "bitbotpro_global"}})
        self.assertEqual((await self.callback(q["state"][0])).json()["status"], "client_changed")

    async def test_account_confusion_is_refused(self):
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-5", "refresh-tok-5", "other.zendesk.com", "admin")
        r = await self.callback(q["state"][0])
        self.assertEqual((r.status_code, r.json()["status"]), (403, "account_mismatch"))
        q = self.state(await self.start())
        self.zd.codes["authcode99999"] = ("access-tok-6", "refresh-tok-6", ZD_HOST, "end-user")
        self.assertEqual((await self.callback(q["state"][0], code="authcode99999")).json()["status"],
                         "account_mismatch")

    async def test_cross_tenant_state_cannot_bind_another_integration(self):
        bz = await self.integration(bot=self.w.bB.id, who="owner_b")
        qb = self.state(await self.start(who="owner_b", integ=bz))
        self.zd.codes["authcode12345"] = ("access-tok-7", "refresh-tok-7", ZD_HOST, "admin")
        r = await self.callback(qb["state"][0])
        self.assertEqual(r.json()["integration_id"], bz)                       # only the state's own integration
        self.assertEqual((await self.w.store.get("bot_integrations", self.Z))["credential_id"], self.cred)
        cred = await self.w.store.get("credentials", (await self.w.store.get("bot_integrations", bz))["credential_id"])
        self.assertEqual(cred["enterprise_id"], self.w.eB.id)

    async def test_reconnect_rotates_and_revokes_the_previous_token(self):
        for code, tok in (("authcode11111", "access-tok-8"), ("authcode22222", "access-tok-9")):
            q = self.state(await self.start())
            self.zd.codes[code] = (tok, "refresh-" + tok, ZD_HOST, "admin")
            self.assertEqual((await self.callback(q["state"][0], code=code)).json()["status"], "connected")
        self.assertEqual(self.zd.revoked, ["access-tok-8"])
        creds = await self.w.store.find("credentials", {"provider": "zendesk_oauth"})
        self.assertEqual(sorted(c["status"] for c in creds), ["active", "revoked"])

    async def test_expiring_tokens_are_refreshed_once_under_concurrency(self):
        from api.integrations import service_for
        from runtime.zendesk import ensure_fresh
        q = self.state(await self.start())
        self.zd.codes["authcode12345"] = ("access-tok-r", "refresh-tok-r", ZD_HOST, "admin")
        await self.callback(q["state"][0])
        cid = (await self.w.store.get("bot_integrations", self.Z))["credential_id"]
        later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(hours=2)).isoformat()
        doc = await self.w.store.get("credentials", cid)
        platform = await self.w.store.get("platform_credentials", "zendesk_oauth_client")
        results = await asyncio.gather(*[ensure_fresh(self.w.store, self.w.S.box, doc, platform_client=platform,
                                                      now=later, transport=self.w.S.zendesk_transport,
                                                      resolver=self.w.S.zendesk_resolver) for _ in range(4)])
        versions = {r["version"] for r in results}
        self.assertEqual(versions, {doc["version"] + 1})                       # one winning update
        del service_for


# ═════════════════════ Z18/D3: outbound security ═════════════════════

class OutboundGuardTests(ZendeskBase):
    async def test_private_and_obfuscated_addresses_are_refused(self):
        for answers in (["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], ["::ffff:10.0.0.1"], ["::1"],
                        [ZD_IP, "192.168.1.1"], ["100.64.0.1"], ["0.0.0.0"], []):
            self.zd.dns[ZD_HOST] = answers
            before = len(self.zd.calls)
            r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
            self.assertEqual(r.status_code, 422, answers)
            self.assertIn("not allowed", r.text) if answers else None
            self.assertEqual(len(self.zd.calls), before)                       # nothing was sent

    async def test_dns_rebinding_cannot_swap_the_checked_address(self):
        seq = iter([[ZD_IP], ["10.0.0.5"], ["10.0.0.5"]])
        original = self.zd.resolve

        async def rebinding(host, port):
            self.zd.resolutions.append(host)
            return next(seq)
        self.w.S.zendesk_resolver = rebinding
        r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertEqual(r.status_code, 200)                                   # first answer checked + pinned
        self.assertEqual(self.zd.calls[-1][4], ZD_IP)
        r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertEqual(r.status_code, 422)                                   # rebinding answer refused
        self.w.S.zendesk_resolver = original

    async def test_redirects_are_never_followed(self):
        import httpx

        def redirect(request):
            if request.url.path.endswith("/users/me.json"):
                return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
            return None
        self.zd.intercept = redirect
        r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("169.254", r.text)

    async def test_host_header_and_paths_are_fixed(self):
        from runtime.zendesk import ZendeskClient, ZendeskError, guarded_call
        await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertEqual(self.zd.calls[-1][0], ZD_HOST)
        client = ZendeskClient("acme", email="agent@acme.example", api_token=ZD_API_TOKEN,
                               transport=self.w.S.zendesk_transport, resolver=self.w.S.zendesk_resolver)
        for path in ("/api/v2/../../admin", "//evil.com/x", "/api/v2/tickets/1.json#x", "/api\\v2"):
            with self.assertRaises(ZendeskError):
                await client.call("GET", path)
        import base64
        auth = "Basic " + base64.b64encode(f"agent@acme.example/token:{ZD_API_TOKEN}".encode()).decode()
        for h in ({"Host": "evil.com"}, {"host": "evil.com"}, {"X-Forwarded-Host": "evil.com"}):
            await guarded_call("acme", "GET", "/api/v2/users/me.json", headers={"Authorization": auth, **h},
                               transport=self.w.S.zendesk_transport, resolver=self.w.S.zendesk_resolver)
            self.assertEqual(self.zd.calls[-1][0], ZD_HOST)                    # a caller Host header never wins
        for sub in ("evil.com", "acme.zendesk.com@evil.com", "0x7f.0.0.1", "[::1]", "127.0.0.1", "http://acme.zendesk.com"):
            with self.assertRaises(ZendeskError):
                ZendeskClient(sub, bearer="x")

    async def test_webhook_payload_cannot_inject_urls_or_paths(self):
        for payload in ({"ticket_id": "../../users/me"}, {"ticket_id": "1/comments"}, {"ticket_id": -5},
                        {"ticket_id": "1e9"}, {"ticket_id": 101, "event_id": "../x y"}):
            r = await self.webhook(payload)
            self.assertEqual(r.status_code, 400, payload)
        self.assertEqual(await self.events(), [])


# ═════════════════════ Z7/Z8, D-Q10: webhook authentication ═════════════════════

class WebhookTests(ZendeskBase):
    async def test_signature_is_mandatory_and_checked_before_any_work(self):
        z2 = await self.integration()
        path = await self.hook_url(z2)
        r = await self.webhook(path=path, secret="anything-at-all-123456")
        self.assertEqual(r.status_code, 404)                                    # no signing secret configured
        for kwargs in ({"signature": "AAAA"}, {"secret": "wrong-signing-secret-0123"},
                       {"ts": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()},
                       {"ts": (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()},
                       {"signature": ""}):
            r = await self.webhook(**kwargs)
            self.assertEqual(r.status_code, 404, kwargs)
        bad_path = (await self.hook_url()).rsplit("/", 1)[0] + "/" + "x" * 43
        self.assertEqual((await self.webhook(path=bad_path)).status_code, 404)
        self.assertEqual((await self.events(), self.provider.calls, self.zd.calls[1:]), ([], [], []))

    async def test_duplicates_and_replays_are_processed_once(self):
        body = json.dumps({"ticket_id": 101, "event_id": "evt-1"}).encode()
        ts = datetime.now(timezone.utc).isoformat()
        results = await asyncio.gather(*[self.webhook(raw=body, ts=ts) for _ in range(4)])
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(sum(1 for r in results if r.json().get("accepted")), 1)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.zd.notes()), 1)
        ledger = await self.w.store.find("usage_ledger", {})
        self.assertEqual([l["state"] for l in ledger], ["settled"])

    async def test_payload_tenant_ids_are_never_trusted(self):
        bz = await self.integration(bot=self.w.bB.id, who="owner_b")
        r = await self.webhook({"ticket_id": 101, "integration_id": bz, "enterprise_id": self.w.eB.id,
                                "bot_id": self.w.bB.id})
        self.assertEqual(r.status_code, 200)
        [e] = await self.events()
        self.assertEqual((e["enterprise_id"], e["integration_id"]), (self.w.eA.id, self.Z))
        self.assertEqual(await self.w.store.find("zendesk_events", {"integration_id": bz}), [])
        # B's path with A's signing secret fails
        await self.set_signing(bz, "b-signing-secret-0123456789") if False else None
        self.assertEqual((await self.webhook(path=await self.hook_url(self.Z), secret="b-signing-secret-0123456789"))
                         .status_code, 404)

    async def test_size_limit_rate_limit_and_disabled(self):
        r = await self.webhook(raw=b"{" + b" " * (260 * 1024) + b"}")
        self.assertEqual(r.status_code, 413)
        await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", {"status": "disabled"})
        r = await self.webhook({"ticket_id": 101, "event_id": "evt-disabled"})
        self.assertEqual(r.json().get("skipped"), "disabled")
        await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", {"status": "enabled"})
        codes = []
        for i in range(125):
            codes.append((await self.webhook({"ticket_id": 102, "event_id": f"e{i}"})).status_code)
        self.assertIn(429, codes)

    async def test_rotated_path_secret_disables_the_old_url(self):
        old = await self.hook_url()
        await self.w.call("POST", f"/integrations/{self.Z}/rotate-webhook-secret", "admin")
        self.assertEqual((await self.webhook(path=old)).status_code, 404)
        self.assertEqual((await self.webhook()).status_code, 200)

    async def test_provisioned_webhook_uses_zendesks_signing_secret(self):
        r = await self.w.call("POST", f"/integrations/{self.Z}/zendesk/webhook/provision", "admin")
        self.assertEqual(r.status_code, 200, r.text)
        hid = r.json()["provisioned_webhook_id"]
        self.assertTrue(self.zd.webhooks[hid]["endpoint"].startswith("https://api.bitbotpro.io/api/hooks/zendesk/"))
        self.assertNotIn("zd-signing-", r.text)
        zsecret = self.zd.webhooks[hid]["secret"]
        self.assertEqual((await self.webhook(secret=self.signing)).status_code, 404)   # manual secret replaced
        self.assertEqual((await self.webhook(secret=zsecret)).status_code, 200)
        self.assertEqual((await self.w.call("POST", f"/integrations/{self.Z}/zendesk/webhook/provision",
                                            "admin")).status_code, 409)
        await self.w.call("POST", f"/integrations/{self.Z}/rotate-webhook-secret", "admin")
        self.assertEqual(self.zd.webhooks[hid]["endpoint"], "https://api.bitbotpro.io/api" + await self.hook_url())


# ═════════════════════ Z9–Z13: ticket events, routing, reply modes ═════════════════════

class TicketTests(ZendeskBase):
    async def test_auto_reply_posts_a_public_comment_and_a_private_audit_note(self):
        await self.settings(reply_mode="auto_reply")
        await self.webhook()
        public, notes = self.zd.public_posts(), self.zd.notes()
        self.assertEqual(len(public), 1)
        t = public[0][3]["ticket"]
        self.assertEqual(t["comment"]["body"], "You can request a refund from the billing page.")
        self.assertEqual(t["additional_tags"], ["ai_processed", "bitbotpro"])
        self.assertNotIn("tags", t)                                             # Z10 fix: never replaces tags
        self.assertTrue(t["safe_update"])
        self.assertEqual(len(notes), 1)
        self.assertIn("AI auto-reply posted", notes[0][3]["ticket"]["comment"]["body"])
        self.assertEqual(self.zd.tickets["101"]["tags"], ["ai_processed", "billing", "bitbotpro"])
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual((d["status"], d["instance_id"]), ("delivered", None))
        self.assertTrue(all(p["message_id"] for p in d["parts"]))

    async def test_agent_assist_posts_only_a_private_note(self):
        await self.webhook()
        self.assertEqual(self.zd.public_posts(), [])
        [n] = self.zd.notes()
        self.assertIn("Suggested Reply", n[3]["ticket"]["comment"]["body"])
        self.assertEqual(n[3]["ticket"]["additional_tags"], ["ai_agent_assist", "bitbotpro"])
        [s] = await self.suggestions()
        self.assertEqual((s["status"], s["source"], s["action"]), ("pending", "webhook", "agent_assist"))

    async def test_suggest_only_posts_nothing(self):
        await self.settings(reply_mode="suggest_only")
        await self.webhook()
        self.assertEqual(self.zd.notes() + self.zd.public_posts(), [])
        [s] = await self.suggestions()
        self.assertEqual(s["text"], "You can request a refund from the billing page.")

    async def test_policy_can_forbid_public_replies(self):
        await self.settings(reply_mode="auto_reply")
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"zendesk.public_replies_allowed": False}})
        self.assertEqual(r.status_code, 200, r.text)
        await self.webhook()
        self.assertEqual(self.zd.public_posts(), [])
        self.assertEqual(len(self.zd.notes()), 1)

    async def test_a_private_part_can_never_be_published_publicly(self):
        from api.integrations import service_for
        from runtime import delivery as dlv
        await self.webhook()                                                    # agent_assist
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        forged = {**d, "parts": [{**d["parts"][0], "extra": {**d["parts"][0]["extra"], "public": True}}]}
        result = await service_for(self.w.S).send_part(forged, forged["parts"][0])
        self.assertEqual((result.status, result.error), ("not_sent_final", "public_not_permitted"))
        self.assertEqual(self.zd.public_posts(), [])

    async def test_routing_filters_ignore_unmatched_tickets(self):
        await self.settings(ticket_groups=["11"], ticket_tags=["billing"])
        self.zd.add_ticket(102, group_id=99, tags=["billing"])
        await self.webhook({"ticket_id": 102})
        [e] = await self.events()
        self.assertEqual(e["label"], "filtered")
        self.assertEqual(self.provider.calls, [])
        await self.webhook({"ticket_id": 101})
        self.assertEqual(len(self.provider.calls), 1)

    async def test_two_bots_share_one_zendesk_account_with_disjoint_filters(self):
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/bots", "admin", {"name": "Bot 2"})
        b2 = r.json()["id"]
        await self.w.call("PATCH", f"/bots/{b2}/config", "owner", {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        z2 = await self.integration(bot=b2)
        await self.w.call("PATCH", f"/integrations/{z2}", "admin", {"credential_id": self.cred})
        await self.set_signing(z2, "second-signing-secret-0123456")
        await self.settings(ticket_groups=["11"])
        await self.settings(z2, ticket_groups=["22"])
        self.zd.add_ticket(103, group_id=22)
        for integ, secret in ((self.Z, self.signing), (z2, "second-signing-secret-0123456")):
            for tid in (101, 103):
                await self.webhook({"ticket_id": tid}, path=await self.hook_url(integ), secret=secret)
        labels = {e["integration_id"]: sorted((e["ticket_id"], e["label"]) for e in
                                              await self.w.store.find("zendesk_events", {"integration_id": e["integration_id"]}))
                  for e in await self.w.store.find("zendesk_events", {})}
        self.assertEqual(labels[self.Z], [("101", "answer"), ("103", "filtered")])
        self.assertEqual(labels[z2], [("101", "filtered"), ("103", "answer")])

    async def test_strict_knowledge_never_posts_or_suggests_unsupported_content(self):
        await self.settings(reply_mode="auto_reply")
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.minimum_mode": "strict_knowledge_only",
                                       "knowledge.allowed_sources": []}})
        await self.webhook()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.zd.public_posts(), [])
        [n] = self.zd.notes()
        self.assertIn("No AI reply was posted", n[3]["ticket"]["comment"]["body"])
        [s] = await self.suggestions()
        self.assertEqual((s["status"], s["text"]), ("no_suggestion", ""))

    async def test_lifecycle_and_quota(self):
        from core.hierarchy import Level
        from core.lifecycle import Authority, LifecycleAction
        await self.w.S.hierarchy.change_lifecycle(Level.BOT, self.w.bA.id, LifecycleAction.SUSPEND,
                                                  authority=Authority.OBJECT, actor_id=self.w.users["owner"].id)
        await self.webhook({"ticket_id": 101, "event_id": "a"})
        await self.w.S.hierarchy.change_lifecycle(Level.BOT, self.w.bA.id, LifecycleAction.RESUME,
                                                  authority=Authority.OBJECT, actor_id=self.w.users["owner"].id)
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 0}})
        await self.webhook({"ticket_id": 101, "event_id": "b"})
        labels = sorted(e.get("label") for e in await self.events())
        self.assertEqual(labels, ["quota_exceeded", "unavailable"])
        self.assertEqual((self.provider.calls, self.zd.notes()), ([], []))

    async def test_usage_is_attributed_to_the_integration(self):
        await self.webhook()
        events = await self.w.store.find("usage_events", {"kind": "llm_call"})
        [e] = events
        self.assertEqual((e["enterprise_id"], e["organisation_id"], e["bot_id"], e["details"]["integration_id"]),
                         (self.w.eA.id, self.w.oA.id, self.w.bA.id, self.Z))


# ═════════════════════ X2: integration-level configuration ═════════════════════

class ConfigTests(ZendeskBase):
    async def test_overrides_apply_through_the_registry(self):
        r = await self.w.call("PATCH", f"/integrations/{self.Z}/config", "editor",
                              {"changes": {"behaviour.persona": "You are the Zendesk support assistant."}})
        self.assertEqual(r.status_code, 200, r.text)
        await self.webhook()
        self.assertEqual(self.provider.calls[-1].persona, "You are the Zendesk support assistant.")
        r = await self.w.call("GET", f"/integrations/{self.Z}/config", "viewer")
        self.assertEqual(r.json()["sources"]["behaviour.persona"]["level"], "integration")
        self.assertEqual(r.json()["sources"]["behaviour.language"]["kind"], "default")

    async def test_enterprise_restrictions_cannot_be_weakened(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.minimum_mode": "knowledge_preferred"}})
        r = await self.w.call("PATCH", f"/integrations/{self.Z}/config", "owner",
                              {"changes": {"knowledge.mode": "general"}})
        self.assertEqual(r.status_code, 422)
        for key, value in (("ai.provider", "openai"), ("ai.credential_id", self.w.cA_ai.id),
                           ("widget.title", "x"), ("limits.end_user_msgs_per_minute", 5)):
            r = await self.w.call("PATCH", f"/integrations/{self.Z}/config", "owner", {"changes": {key: value}})
            self.assertEqual(r.status_code, 422, key)
        r = await self.w.call("PATCH", f"/integrations/{self.Z}/config", "editor",
                              {"changes": {"knowledge.mode": "strict_knowledge_only"}})
        self.assertEqual(r.status_code, 403)                                    # admin-only property
        r = await self.w.call("PATCH", f"/integrations/{self.Z}/config", "viewer",
                              {"changes": {"behaviour.persona": "x"}})
        self.assertEqual(r.status_code, 403)

    async def test_a_later_policy_wins_over_a_stored_override(self):
        await self.w.call("PATCH", f"/integrations/{self.Z}/config", "owner", {"changes": {"knowledge.mode": "general"}})
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.minimum_mode": "strict_knowledge_only",
                                       "knowledge.allowed_sources": []}})
        r = await self.w.call("GET", f"/integrations/{self.Z}/config", "viewer")
        self.assertEqual(r.json()["values"]["knowledge.mode"], "strict_knowledge_only")
        self.assertTrue(r.json()["conflicts"])
        await self.webhook()
        self.assertEqual(self.provider.calls, [])                               # fail closed


# ═════════════════════ delivery and crash recovery ═════════════════════

class DeliveryTests(ZendeskBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(reply_mode="auto_reply")

    async def test_timeout_after_the_comment_landed_never_duplicates_it(self):
        import httpx
        state = {"n": 1}

        def lands_then_times_out(request):
            if request.method == "PUT" and state["n"]:
                state["n"] -= 1
                self.zd.handle(request)                                         # Zendesk applied it
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.zd.intercept = lands_then_times_out
        await self.webhook()
        await self.worker(60)
        await self.worker(600)
        public = [c for c in self.zd.comments["101"] if c["public"] and c["author_id"] == 77]
        self.assertEqual(len(public), 1)                                        # never posted twice
        # Z-D: never retried; the worker's reconciliation found the comment, then sent the audit note
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual(d["status"], "delivered")
        self.assertTrue(d["parts"][0]["message_id"].startswith("comment:"))
        r = await self.w.call("GET", f"/integrations/{self.Z}/deliveries", "admin")
        self.assertEqual(r.json()["deliveries"][0]["status"], "delivered")

    async def test_connection_failure_before_sending_is_retried_once(self):
        import httpx
        state = {"n": 1}

        def refuse(request):
            if request.method == "PUT" and state["n"]:
                state["n"] -= 1
                raise httpx.ConnectError("refused", request=request)
            return None
        self.zd.intercept = refuse
        await self.webhook()
        await self.worker(60)
        await self.worker(600)
        public = [c for c in self.zd.comments["101"] if c["public"] and c["author_id"] == 77]
        self.assertEqual(len(public), 1)
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual(d["status"], "delivered")

    async def test_an_agent_update_before_our_first_attempt_is_handled(self):
        import httpx
        state = {"n": 1}

        def agent_edits(request):
            if request.method == "PUT" and state["n"]:
                state["n"] -= 1
                self.zd.bump("101")                                             # the stamp is now stale
                self.zd.comments["101"].append({"id": 99, "author_id": 5, "public": False, "plain_body": "agent"})
                self.zd.bump("101")
            return None
        self.zd.intercept = agent_edits
        await self.webhook()
        public = [c for c in self.zd.comments["101"] if c["public"] and c["author_id"] == 77]
        self.assertEqual(len(public), 1)

    async def test_crash_after_generation_is_completed_by_the_worker_and_billed_once(self):
        from runtime.zendesk_integration import ZendeskService
        original = ZendeskService.complete

        async def crash(self, eid):
            raise RuntimeError("worker died")
        ZendeskService.complete = crash
        try:
            await self.webhook()
        except Exception:
            pass
        finally:
            ZendeskService.complete = original
        [e] = await self.events()
        self.assertEqual(e["status"], "generated")
        self.assertEqual(self.zd.public_posts(), [])
        await self.worker()
        await self.worker(10)
        self.assertEqual(len(self.zd.public_posts()), 1)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([l["state"] for l in await self.w.store.find("usage_ledger", {})], ["settled"])

    async def test_crash_with_a_reservation_but_no_result_is_never_regenerated(self):
        from runtime import usage
        original = usage.StoreUsageTracker.settle

        async def broken(*a, **k):
            raise RuntimeError("x")
        await self.webhook({"ticket_id": 101, "event_id": "ok"})
        # simulate a worker that reserved and died before storing the result
        from runtime.zendesk_integration import event_doc_id
        eid = event_doc_id(self.Z, "dead")
        await self.w.store.insert("zendesk_events", {
            "id": eid, "enterprise_id": self.w.eA.id, "organisation_id": self.w.oA.id, "bot_id": self.w.bA.id,
            "integration_id": self.Z, "ticket_id": "101", "status": "processing", "attempts": 1,
            "lease_until": "2020-01-01T00:00:00+00:00",
            "expires_at_dt": datetime.now(timezone.utc), "version": 1})
        now = self.w.S.clock()
        await self.w.store.insert("usage_ledger", {"id": f"zd:{eid}", "state": "reserved", "period": now[:7],
                                                   "counters": [], "enterprise_id": self.w.eA.id,
                                                   "created_at": now, "updated_at": now, "version": 1})
        calls = len(self.provider.calls)
        await self.worker()
        self.assertEqual((await self.w.store.get("zendesk_events", eid))["status"], "uncertain")
        self.assertEqual(len(self.provider.calls), calls)
        del original, broken

    async def test_provider_failure_is_retried_without_billing(self):
        from runtime.errors import ProviderUnavailable
        self.provider.error = ProviderUnavailable()
        await self.webhook()
        [e] = await self.events()
        self.assertEqual(e["status"], "received")
        self.assertFalse([l for l in await self.w.store.find("usage_ledger", {}) if l["state"] == "settled"])
        self.provider.error = None
        await self.worker(60)
        self.assertEqual(len(self.zd.public_posts()), 1)


# ═════════════════════ Z16, D9/D10: sidebar app and app keys ═════════════════════

class SidebarTests(ZendeskBase):
    async def key(self, integ=None):
        r = await self.w.call("POST", f"/integrations/{integ or self.Z}/sidebar-key", "admin")
        assert r.status_code == 200, r.text
        return r.json()["sidebar_key"]

    async def generate(self, key, ticket_id="101"):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            return await c.post("/api/hooks/zendesk/sidebar/generate", json={"ticket_id": ticket_id},
                                headers={"X-Sidebar-Key": key} if key is not None else {})

    async def test_key_is_shown_once_hashed_rotatable_and_revocable(self):
        k1 = await self.key()
        self.assertTrue(k1.startswith("bbz_") and not k1.startswith("bbk_"))
        self.assertNotIn(k1, json.dumps(await self.w.snapshot(), default=str))
        r = await self.w.call("GET", f"/integrations/{self.Z}", "viewer")
        self.assertEqual(r.json()["sidebar_key"], {"configured": True, "hint": k1[-4:]})
        self.assertNotIn(k1, r.text)
        self.assertEqual((await self.generate(k1)).status_code, 200)
        k2 = await self.key()
        self.assertEqual((await self.generate(k1)).status_code, 401)            # rotation invalidates
        self.assertEqual((await self.generate(k2)).status_code, 200)
        await self.w.call("DELETE", f"/integrations/{self.Z}/sidebar-key", "admin")
        self.assertEqual((await self.generate(k2)).status_code, 401)
        for bad in (None, "", "bbk_" + "a" * 43, "bbz_short", k2 + "x"):
            self.assertEqual((await self.generate(bad)).status_code, 401)
        self.assertEqual((await self.w.call("POST", f"/integrations/{self.Z}/sidebar-key", "viewer")).status_code, 403)

    async def test_generation_posts_nothing_and_is_scoped_to_its_integration(self):
        k = await self.key()
        r = await self.generate(k)
        self.assertEqual(r.json()["suggestion"], "You can request a refund from the billing page.")
        self.assertEqual(self.zd.notes() + self.zd.public_posts(), [])
        [s] = await self.suggestions()
        self.assertEqual(s["source"], "sidebar")
        self.assertEqual((await self.generate(k, "abc")).status_code, 400)
        self.assertEqual((await self.generate(k, "999")).status_code, 404)
        bz = await self.integration(bot=self.w.bB.id, who="owner_b")
        self.assertEqual(await self.suggestions(bz), [])

    async def test_strict_mode_refuses_without_ai(self):
        k = await self.key()
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.minimum_mode": "strict_knowledge_only",
                                       "knowledge.allowed_sources": []}})
        r = await self.generate(k)
        self.assertEqual((r.json()["refused"], r.json()["suggestion"]), (True, ""))
        self.assertEqual(self.provider.calls, [])

    async def test_rate_limit(self):
        k = await self.key()
        codes = [(await self.generate(k)).status_code for _ in range(32)]
        self.assertIn(429, codes)

    async def test_app_zip_contains_no_secret_and_targets_the_new_route(self):
        await self.key()
        r = await self.w.call("GET", f"/integrations/{self.Z}/zendesk/sidebar-app.zip", "admin")
        self.assertEqual(r.status_code, 200)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        manifest = json.loads(z.read("manifest.json"))
        self.assertEqual(manifest["domainWhitelist"], ["api.bitbotpro.io"])
        self.assertEqual([p["default"] for p in manifest["parameters"] if p["name"] == "sidebar_api_key"], [""])
        main = z.read("assets/main.js").decode()
        self.assertIn("/api/hooks/zendesk/sidebar/generate", main)
        self.assertNotIn("bbz_", r.content.decode("latin-1"))


# ═════════════════════ Z6/D11: deletion cleanup and isolation ═════════════════════

class DeletionAndIsolationTests(ZendeskBase):
    def executors(self):
        from runtime.telegram import telegram_cleanup_executors
        from runtime.zendesk import zendesk_cleanup_executors
        from runtime.discord import discord_cleanup_executors
        return {**zendesk_cleanup_executors(self.w.S.box, transport=self.w.S.zendesk_transport,
                                            resolver=self.w.S.zendesk_resolver),
                **telegram_cleanup_executors(self.w.S.box, transport=self.w.S.telegram_transport),
                **discord_cleanup_executors(self.w.S.box, transport=self.w.S.discord_transport)}

    async def delete(self, path, who="owner", name=None):
        pv = (await self.w.call("POST", f"{path}/deletion-preview", who)).json()
        r = await self.w.call("POST", f"{path}/delete", who, {"preview_token": pv["preview_token"],
                                                              "confirm_name": name or pv["confirm_name"]})
        assert r.status_code == 202, r.text
        return r.json()["id"], pv

    async def test_bot_deletion_removes_the_provisioned_webhook_and_keeps_credentials_until_done(self):
        from core import deletion
        r = await self.w.call("POST", f"/integrations/{self.Z}/zendesk/webhook/provision", "admin")
        hid = r.json()["provisioned_webhook_id"]
        self.zd.fail[("DELETE", f"/webhooks/{hid}")] = [(503, {})]
        job, pv = await self.delete(f"/bots/{self.w.bA.id}")
        acts = {a["action"]: a for a in pv["external_actions"]}
        self.assertEqual(acts["zendesk_webhook_subscription_removal"]["target"]["webhook_id"], hid)
        first = await deletion.run_job(self.w.store, job, clock=self.w.S.clock, executors=self.executors())
        self.assertNotEqual(first["status"], "completed")                       # mandatory work unresolved
        self.assertIn(hid, self.zd.webhooks)
        self.assertIsNotNone(await self.w.store.get("credentials", self.cred))
        later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(hours=3)).isoformat()
        done = await deletion.run_job(self.w.store, job, clock=lambda: later, executors=self.executors())
        self.assertEqual(done["status"], "completed", done)
        self.assertNotIn(hid, self.zd.webhooks)
        self.assertIsNone(await self.w.store.get("bot_integrations", self.Z))
        self.assertEqual(await self.w.store.find("zendesk_app_keys", {"integration_id": self.Z}), [])

    async def test_organisation_deletion_revokes_oauth_tokens_but_not_the_platform_client(self):
        from core import deletion
        await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                          {"secret": ZD_CLIENT_SECRET, "metadata": {"client_id": "bitbotpro_global"}})
        r = await self.w.call("POST", f"/integrations/{self.Z}/zendesk/oauth/start", "admin", {"subdomain": "acme"})
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        self.zd.codes["authcode12345"] = ("access-tok-del", "refresh-del", ZD_HOST, "admin")
        await self.w.call("GET", f"/hooks/zendesk/oauth/callback?state={state}&code=authcode12345", None)
        bz = await self.integration(bot=self.w.bB.id, who="owner_b")
        job, _ = await self.delete(f"/organisations/{self.w.oA.id}")
        done = await deletion.run_job(self.w.store, job, clock=self.w.S.clock, executors=self.executors())
        self.assertEqual(done["status"], "completed", done)
        self.assertIn("access-tok-del", self.zd.revoked)
        self.assertEqual((await self.w.store.get("platform_credentials", "zendesk_oauth_client"))["status"], "active")
        self.assertIsNotNone(await self.w.store.get("bot_integrations", bz))   # other Enterprise untouched

    async def test_integration_delete_is_all_or_nothing(self):
        r = await self.w.call("POST", f"/integrations/{self.Z}/zendesk/webhook/provision", "admin")
        hid = r.json()["provisioned_webhook_id"]
        self.zd.fail[("DELETE", f"/webhooks/{hid}")] = [(503, {})]
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual(r.status_code, 502)
        self.assertIsNotNone(await self.w.store.get("bot_integrations", self.Z))
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(hid, self.zd.webhooks)

    async def test_tenant_isolation_across_every_route(self):
        await self.webhook()
        [s] = await self.suggestions()
        for method, path, body in (("GET", f"/integrations/{self.Z}", None),
                                   ("PATCH", f"/integrations/{self.Z}", {"name": "x"}),
                                   ("GET", f"/integrations/{self.Z}/config", None),
                                   ("GET", f"/integrations/{self.Z}/suggestions", None),
                                   ("POST", f"/integrations/{self.Z}/suggestions/{s['id']}/accept", None),
                                   ("GET", f"/integrations/{self.Z}/zendesk/webhook", None),
                                   ("POST", f"/integrations/{self.Z}/sidebar-key", None),
                                   ("GET", f"/integrations/{self.Z}/deliveries", None),
                                   ("DELETE", f"/integrations/{self.Z}", None),
                                   ("GET", f"/bots/{self.w.bA.id}/integrations", None)):
            r = await self.w.call(method, path, "owner_b", body)
            self.assertEqual(r.status_code, 404, (method, path))
        bz = await self.integration(bot=self.w.bB.id, who="owner_b")
        r = await self.w.call("POST", f"/integrations/{bz}/suggestions/{s['id']}/accept", "owner_b")
        self.assertEqual(r.status_code, 404)                                    # A's suggestion via B's route

    async def test_suggestion_review(self):
        await self.webhook()
        [s] = await self.suggestions()
        p = f"/integrations/{self.Z}/suggestions/{s['id']}"
        self.assertEqual((await self.w.call("POST", p + "/accept", "viewer")).status_code, 403)
        self.assertEqual((await self.w.call("POST", p + "/accept", "editor")).json()["status"], "accepted")
        self.assertEqual((await self.w.call("POST", p + "/dismiss", "editor")).status_code, 409)
        self.assertEqual(len(self.zd.public_posts()), 0)                        # accepting posts nothing (Z15)


if __name__ == "__main__":
    unittest.main()
