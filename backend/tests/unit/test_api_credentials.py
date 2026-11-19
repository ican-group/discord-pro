"""Credentials through the API (spec 012 §6, D-Q9)."""
import json
import unittest

from tests.unit._api_fixtures import ANTHROPIC_KEY, HAVE_API, TELEGRAM_TOKEN_A, TELEGRAM_TOKEN_B, World

SECRET = "not-a-real-openai-key-abcdefghijklmnop"


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class CredentialApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()
        self.O = f"/organisations/{self.w.oA.id}/credentials"

    async def test_secrets_never_leave_the_server(self):
        r = await self.w.call("POST", self.O, "admin", {"provider": "openai", "name": "OpenAI", "secret": SECRET})
        self.assertEqual(r.status_code, 201)
        cid = r.json()["id"]
        responses = [r.text]
        for path in (self.O, f"/credentials/{cid}", f"/bots/{self.w.bA.id}/config",
                     f"/enterprises/{self.w.eA.id}/audit"):
            responses.append((await self.w.call("GET", path, "owner")).text)
        r = await self.w.call("PATCH", f"/credentials/{cid}", "admin", {"secret": SECRET + "-rotated"})
        responses.append(r.text)
        for text in responses:
            for secret in (SECRET, TELEGRAM_TOKEN_A, ANTHROPIC_KEY, "secret_enc"):
                self.assertNotIn(secret, text)
        stored = json.dumps(await self.w.snapshot(), default=str)
        for secret in (SECRET, TELEGRAM_TOKEN_A, ANTHROPIC_KEY):
            self.assertNotIn(secret, stored)                                   # encrypted at rest

    async def test_use_versus_manage(self):
        r = await self.w.call("POST", self.O, "editor", {"provider": "openai", "name": "x", "secret": SECRET})
        self.assertEqual(r.status_code, 403)                                    # editors cannot manage
        r = await self.w.call("GET", self.O, "editor")
        self.assertEqual(r.status_code, 200)
        r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "admin",
                              {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        self.assertEqual(r.status_code, 200)                                    # admins may reference it

    async def test_rotation_keeps_the_id_and_revocation_fails_closed(self):
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "admin",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        before = (await self.w.call("GET", f"/credentials/{self.w.cA_ai.id}", "owner")).json()
        r = await self.w.call("PATCH", f"/credentials/{self.w.cA_ai.id}", "owner", {"secret": ANTHROPIC_KEY + "x"})
        self.assertEqual(r.json()["id"], self.w.cA_ai.id)
        self.assertNotEqual(r.json()["fingerprint"], before["fingerprint"])
        cfg = (await self.w.call("GET", f"/bots/{self.w.bA.id}/config", "owner")).json()
        self.assertNotIn("ai", cfg["blocked"])
        r = await self.w.call("POST", f"/credentials/{self.w.cA_ai.id}/revoke", "owner")
        self.assertEqual(r.json()["status"], "revoked")
        cfg = (await self.w.call("GET", f"/bots/{self.w.bA.id}/config", "owner")).json()
        self.assertEqual(cfg["blocked"]["ai"], "The selected credential has been revoked.")
        r = await self.w.call("PATCH", f"/credentials/{self.w.cA_ai.id}", "owner", {"secret": "again-not-real"})
        self.assertEqual(r.status_code, 422)

    async def test_telegram_token_identity(self):
        r = await self.w.call("POST", self.O, "admin", {"provider": "telegram_bot", "name": "dup",
                                                        "secret": TELEGRAM_TOKEN_A})
        self.assertEqual(r.status_code, 201)                                    # a second credential, same token
        dup = r.json()["id"]
        self.assertEqual(r.json()["metadata"]["telegram_bot_id"], TELEGRAM_TOKEN_A.split(":")[0])
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "admin",
                              {"name": "dup", "channel": {"type": "telegram", "credential_id": dup}})
        self.assertEqual(r.status_code, 409)                                    # same Telegram bot already bound
        r = await self.w.call("PATCH", f"/credentials/{dup}", "admin", {"secret": TELEGRAM_TOKEN_B})
        self.assertEqual(r.status_code, 422)                                    # rotation must keep the bot
        r = await self.w.call("POST", self.O, "admin", {"provider": "telegram_bot", "name": "bad", "secret": "nope"})
        self.assertEqual(r.status_code, 422)
        r = await self.w.call("POST", self.O, "admin", {"provider": "telegram_bot", "name": "spoof",
                                                        "secret": "555555:" + "C" * 35,
                                                        "metadata": {"telegram_bot_id": TELEGRAM_TOKEN_A.split(":")[0]}})
        self.assertEqual(r.json()["metadata"]["telegram_bot_id"], "555555")    # never client-supplied

    async def test_cross_tenant_telegram_reference_is_refused(self):
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                              {"name": "x", "channel": {"type": "telegram", "credential_id": self.w.cB_tg.id}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], "The selected credential is not available here.")

    async def test_enterprise_credentials_policy(self):
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"credentials.allow_enterprise_credentials": False}})
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                              {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        self.assertEqual(r.status_code, 422)

    async def test_platform_credentials(self):
        for who in ("owner", "admin", "owner_b"):
            r = await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", who, {"secret": "x" * 20})
            self.assertEqual(r.status_code, 404)
        r = await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root", {"secret": "zd-secret-xyz-123"})
        self.assertEqual(r.status_code, 422)                       # the client_id is required (Phase 4E)
        r = await self.w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                              {"secret": "zd-secret-xyz-123", "metadata": {"client_id": "bitbotpro_global"}})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("zd-secret-xyz-123", r.text)
        self.assertNotIn("zd-secret-xyz-123", json.dumps(await self.w.snapshot(), default=str))
        # D-Q14: there is no global Discord application any more
        r = await self.w.call("PUT", "/platform/credentials/discord_app", "root", {"secret": "discord-secret-xyz"})
        self.assertEqual(r.status_code, 404)
        r = await self.w.call("PUT", "/platform/credentials/not_a_kind", "root", {"secret": "x"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class CredentialScopeApiTests(unittest.IsolatedAsyncioTestCase):
    """X6 through the API."""

    async def asyncSetUp(self):
        self.w = await World().build()

    async def test_scoped_credential_lifecycle(self):
        w = self.w
        other_bot = (await w.call("POST", f"/organisations/{w.oA.id}/bots", "owner", {"name": "Other"})).json()["id"]
        r = await w.call("POST", f"/enterprises/{w.eA.id}/credentials", "owner",
                         {"provider": "anthropic", "name": "Scoped", "secret": SECRET,
                          "usable_by": [{"level": "bot", "id": w.bA.id}]})
        self.assertEqual(r.status_code, 201, r.text)
        cid = r.json()["id"]
        self.assertEqual(r.json()["usable_by"], [{"level": "bot", "id": w.bA.id}])
        self.assertNotIn(SECRET, r.text)
        ok = await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": cid}})
        self.assertEqual(ok.status_code, 200, ok.text)
        no = await w.call("PATCH", f"/bots/{other_bot}/config", "owner", {"changes": {"ai.credential_id": cid}})
        self.assertEqual((no.status_code, no.json()["detail"]), (422, "The selected credential is not available here."))
        # narrowing the scope later blocks the existing reference at resolution (fail closed)
        r = await w.call("PUT", f"/credentials/{cid}/scope", "owner", {"usable_by": [{"level": "bot", "id": other_bot}]})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = (await w.call("GET", f"/bots/{w.bA.id}/config", "owner")).json()
        self.assertIn("ai", cfg["blocked"])
        rc = (await w.call("GET", f"/instances/{w.iA_web.id}/runtime-config", "owner")).json()
        self.assertIn("ai", rc["blocked"])
        self.assertIsNone(rc["ai"]["credential"])

    async def test_scope_validation_and_permissions(self):
        w = self.w
        cases = [[{"level": "bot", "id": w.bB.id}],                       # other tenant
                 [{"level": "organisation", "id": w.oB.id}],
                 [{"level": "bot_instance", "id": w.iA_web.id}],
                 [{"level": "bot", "id": "00000000-0000-4000-8000-000000000000"}]]
        for usable_by in cases:
            with self.subTest(usable_by=usable_by):
                before = await w.snapshot()
                r = await w.call("POST", f"/enterprises/{w.eA.id}/credentials", "owner",
                                 {"provider": "openai", "name": "x", "secret": SECRET, "usable_by": usable_by})
                self.assertEqual(r.status_code, 422)
                self.assertEqual(await w.snapshot(), before)
        r = await w.call("POST", f"/organisations/{w.oA.id}/credentials", "owner",
                         {"provider": "openai", "name": "x", "secret": SECRET,
                          "usable_by": [{"level": "organisation", "id": w.oA.id}]})
        self.assertEqual(r.status_code, 422)                                # org credentials: bots only
        for who, status in (("editor", 403), ("owner_b", 404)):
            before = await w.snapshot()
            r = await w.call("PUT", f"/credentials/{w.cA_ai.id}/scope", who, {"usable_by": []})
            self.assertEqual(r.status_code, status)
            self.assertEqual(await w.snapshot(), before)
