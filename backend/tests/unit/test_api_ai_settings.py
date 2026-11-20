"""Stage 0 (owner requirement 2026-11-20): customer AI keys are a core feature.

A normal authorised administrator picks a provider, a model and a saved credential by
name, adds credentials, and runs a real connection test; superadmins own platform AI
keys granted per Enterprise. Permissions and tenant isolation are enforced server-side;
secrets never leave the server. All provider traffic goes to a MockTransport.
"""
import json
import unittest

from tests.unit._api_fixtures import ANTHROPIC_KEY, HAVE_API, World

OPENAI_KEY = "not-a-real-openai-key-" + "o" * 30
PLATFORM_KEY = "not-a-real-platform-anthropic-key-" + "p" * 20


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class AiSettingsBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from runtime.providers import build_providers
        self.w = await World().build()
        self.calls = []
        self.reply = {"status": 200, "text": "OK"}

        def handler(request):
            body = json.loads(request.content or b"{}") if request.method == "POST" else {}
            self.calls.append((request.method, str(request.url), dict(request.headers), body))
            st = self.reply["status"]
            if st != 200:
                return httpx.Response(st, json={"error": {"type": "x", "message": "model: nope " + ANTHROPIC_KEY}})
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            if "anthropic" in str(request.url):
                return httpx.Response(200, json={"content": [{"type": "text", "text": self.reply["text"]}],
                                                  "usage": {"input_tokens": 12, "output_tokens": 2}})
            return httpx.Response(200, json={"choices": [{"message": {"content": self.reply["text"]}}],
                                              "usage": {"prompt_tokens": 11, "completion_tokens": 1}})
        self.w.S.providers = build_providers(transport=httpx.MockTransport(handler))
        self.B = f"/bots/{self.w.bA.id}"

    async def org_openai(self, who="admin"):
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", who,
                              {"provider": "openai", "name": "Team OpenAI", "secret": OPENAI_KEY})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()


class OptionsTests(AiSettingsBase):
    async def test_options_list_providers_models_and_usable_credentials_by_name(self):
        await self.org_openai()
        r = await self.w.call("GET", self.B + "/ai", "admin")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual([p["id"] for p in d["providers"]], ["anthropic", "openai"])
        anth = d["providers"][0]
        self.assertTrue(anth["allowed"])
        self.assertIn("claude-sonnet-4-6", [m["id"] for m in anth["models"]])
        self.assertTrue(all(m["label"] for m in anth["models"]))
        names = {(c["name"], c["provider"], c["owner"], c["owner_name"]) for c in d["credentials"]}
        self.assertEqual(names, {("Anthropic A", "anthropic", "enterprise", "Ent A"),
                                 ("Team OpenAI", "openai", "organisation", "Org A")})
        self.assertNotIn("Telegram A", r.text)                                      # not an AI credential
        self.assertTrue(d["can_edit"])
        self.assertEqual({t["type"] for t in d["can_add_credential_to"]}, {"enterprises", "organisations"})
        self.assertEqual(d["current"]["provider"], "anthropic")
        self.assertIsNone(d["current"]["credential_id"])
        self.assertIn("no AI credential", d["current"]["blocked"])
        for secret in (ANTHROPIC_KEY, OPENAI_KEY):
            self.assertNotIn(secret, r.text)

    async def test_roles_and_isolation(self):
        d = (await self.w.call("GET", self.B + "/ai", "viewer")).json()
        self.assertFalse(d["can_edit"])
        self.assertEqual(d["can_add_credential_to"], [])
        d = (await self.w.call("GET", self.B + "/ai", "bot_editor")).json()
        self.assertTrue(d["can_edit"])                                                # editors select (Stage 0)
        self.assertEqual(d["can_add_credential_to"], [])                             # but cannot manage keys
        for who in ("owner_b", "outsider"):
            self.assertEqual((await self.w.call("GET", self.B + "/ai", who)).status_code, 404)
        # Enterprise B never sees Enterprise A's credentials
        d = (await self.w.call("GET", f"/bots/{self.w.bB.id}/ai", "owner_b")).json()
        self.assertEqual(d["credentials"], [])
        self.assertEqual((await self.w.call("GET", f"/instances/{self.w.iA_web.id}/ai", "owner")).status_code, 404)


class OrganisationIsolationTests(AiSettingsBase):
    async def test_an_organisation_key_is_invisible_and_unusable_in_a_sibling_organisation(self):
        oa = await self.org_openai()                                         # owned by Org A
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner", {"name": "Org A2"})
        org2 = r.json()["id"]
        bot2 = (await self.w.call("POST", f"/organisations/{org2}/bots", "owner", {"name": "Bot A2"})).json()["id"]
        d = (await self.w.call("GET", f"/bots/{bot2}/ai", "owner")).json()
        self.assertEqual([c["name"] for c in d["credentials"]], ["Anthropic A"])  # Enterprise key only
        before = json.dumps(await self.w.snapshot(), default=str)
        for body in ({"provider": "openai", "credential_id": oa["id"]}, {"credential_id": oa["id"]}):
            r = await self.w.call("PUT", f"/bots/{bot2}/ai", "owner", body)
            self.assertEqual(r.status_code, 422, r.text)
        r = await self.w.call("POST", f"/bots/{bot2}/ai/test", "owner", {"provider": "openai", "credential_id": oa["id"]})
        self.assertEqual(r.status_code, 422)
        r = await self.w.call("PUT", f"/organisations/{org2}/ai", "owner", {"provider": "openai", "credential_id": oa["id"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(json.dumps(await self.w.snapshot(), default=str), before)
        self.assertEqual(self.calls, [])
        # the owning Organisation's bot can use it
        r = await self.w.call("PUT", self.B + "/ai", "owner", {"provider": "openai", "credential_id": oa["id"]})
        self.assertEqual(r.status_code, 200, r.text)


class SelectionTests(AiSettingsBase):
    async def test_select_provider_model_and_credential_in_one_step(self):
        r = await self.w.call("PUT", self.B + "/ai", "admin", {"provider": "anthropic", "model": "claude-haiku-4-5",
                                                                "credential_id": self.w.cA_ai.id})
        self.assertEqual(r.status_code, 200, r.text)
        cur = r.json()["current"]
        self.assertEqual((cur["provider"], cur["model"], cur["credential_id"], cur["blocked"]),
                         ("anthropic", "claude-haiku-4-5", self.w.cA_ai.id, None))
        self.assertEqual(cur["credential"]["name"], "Anthropic A")
        # switching provider + credential together works (validated in order)
        oa = await self.org_openai()
        r = await self.w.call("PUT", self.B + "/ai", "bot_editor", {"provider": "openai", "credential_id": oa["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        cur = r.json()["current"]
        self.assertEqual((cur["provider"], cur["model"], cur["credential_id"]), ("openai", "gpt-4o", oa["id"]))
        rt = (await self.w.call("GET", f"/instances/{self.w.iA_web.id}/runtime-config", "admin")).json()
        self.assertNotIn(OPENAI_KEY, json.dumps(rt))
        # the generic config route accepts the same combined change now
        r = await self.w.call("PATCH", self.B + "/config", "admin",
                              {"changes": {"ai.provider": "anthropic", "ai.credential_id": self.w.cA_ai.id,
                                           "ai.model": "claude-sonnet-4-6"}})
        self.assertEqual(r.status_code, 200, r.text)
        audit = await self.w.store.find("audit_events", {"action": "config.update"})
        self.assertTrue(any((a.get("details") or {}).get("via") == "ai_settings" for a in audit))

    async def test_rejected_selections_change_nothing(self):
        before = json.dumps(await self.w.snapshot(), default=str)
        cases = [("viewer", {"credential_id": self.w.cA_ai.id}, 403),
                 ("owner_b", {"credential_id": self.w.cA_ai.id}, 404),
                 ("admin", {"credential_id": self.w.cA_tg.id}, 422),                 # not an AI credential
                 ("admin", {"provider": "anthropic", "model": "gpt-4o"}, 422),       # model of another provider
                 ("admin", {"provider": "ollama"}, 422),                             # not allowed by platform policy
                 ("admin", {"credential_id": "not-an-id"}, 422),
                 ("admin", {}, 422)]
        for who, body, code in cases:
            r = await self.w.call("PUT", self.B + "/ai", who, body)
            self.assertEqual(r.status_code, code, (who, body, r.text))
        # an Enterprise B credential cannot be used in Enterprise A
        rb = await self.w.call("POST", f"/enterprises/{self.w.eB.id}/credentials", "owner_b",
                               {"provider": "anthropic", "name": "B key", "secret": "b" * 40})
        before = json.dumps(await self.w.snapshot(), default=str)
        r = await self.w.call("PUT", self.B + "/ai", "admin", {"credential_id": rb.json()["id"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(json.dumps(await self.w.snapshot(), default=str), before)

    async def test_enterprise_policy_bounds_the_choice(self):
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"ai.allowed_models": ["claude-haiku-4-5"]}})
        self.assertEqual(r.status_code, 200, r.text)
        d = (await self.w.call("GET", self.B + "/ai", "admin")).json()
        allowed = [m["id"] for m in d["providers"][0]["models"] if m["allowed"]]
        self.assertEqual(allowed, ["claude-haiku-4-5"])
        r = await self.w.call("PUT", self.B + "/ai", "admin", {"model": "claude-opus-4-8"})
        self.assertEqual(r.status_code, 422)


class ConnectionTestTests(AiSettingsBase):
    async def select(self):
        r = await self.w.call("PUT", self.B + "/ai", "admin", {"model": "claude-haiku-4-5",
                                                                "credential_id": self.w.cA_ai.id})
        self.assertEqual(r.status_code, 200, r.text)

    async def test_real_generation_probe_with_the_selected_key(self):
        await self.select()
        r = await self.w.call("POST", self.B + "/ai/test", "admin", {})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["ok"], d["model"], d["reply"], d["credential"]["name"]),
                         (True, "claude-haiku-4-5", "OK", "Anthropic A"))
        self.assertIn("Claude Haiku 4.5 answered", d["message"])
        [(method, url, headers, body)] = self.calls
        self.assertEqual((method, url), ("POST", "https://api.anthropic.com/v1/messages"))
        self.assertEqual(headers["x-api-key"], ANTHROPIC_KEY)
        self.assertEqual(body["model"], "claude-haiku-4-5")
        self.assertLessEqual(body["max_tokens"], 32)                                   # a few tokens only
        self.assertNotIn(ANTHROPIC_KEY, r.text)
        ev = await self.w.store.find("usage_events", {"kind": "ai_test"})
        self.assertEqual([(e["details"]["input_tokens"], e["details"]["output_tokens"]) for e in ev], [(12, 2)])
        audit = await self.w.store.find("audit_events", {"action": "ai.test"})
        self.assertEqual(len(audit), 1)
        self.assertNotIn(ANTHROPIC_KEY, json.dumps(audit))

    async def test_unsaved_choice_can_be_tested_and_failures_are_explained(self):
        oa = await self.org_openai()
        r = await self.w.call("POST", self.B + "/ai/test", "bot_editor",
                              {"provider": "openai", "model": "gpt-4.1-mini", "credential_id": oa["id"]})
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True), r.text)
        self.assertEqual(self.calls[-1][1], "https://api.openai.com/v1/chat/completions")
        await self.select()
        for status, category in ((401, "unauthorized"), (404, "model_unavailable"), (429, "rate_limited"),
                                 (503, "unavailable")):
            self.reply["status"] = status
            d = (await self.w.call("POST", self.B + "/ai/test", "admin", {})).json()
            self.assertEqual((d["ok"], d["category"]), (False, category))
            self.assertNotIn(ANTHROPIC_KEY, json.dumps(d))
            self.assertNotIn("nope", json.dumps(d))                                     # no provider text

    async def test_permissions_and_limits(self):
        await self.select()
        for who, code in (("viewer", 403), ("owner_b", 404), ("outsider", 404)):
            self.assertEqual((await self.w.call("POST", self.B + "/ai/test", who, {})).status_code, code)
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/ai/test", "admin", {"credential_id": self.w.cA_tg.id})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [])
        codes = [(await self.w.call("POST", self.B + "/ai/test", "admin", {})).status_code for _ in range(22)]
        self.assertEqual(codes.count(429), 2)

    async def test_nothing_selected_is_a_clear_error(self):
        r = await self.w.call("POST", self.B + "/ai/test", "admin", {})
        self.assertEqual(r.status_code, 422)
        self.assertIn("credential", r.json()["detail"].lower())


class PlatformAiCredentialTests(AiSettingsBase):
    async def create(self, **extra):
        body = {"provider": "anthropic", "name": "BitBotPro shared Claude", "secret": PLATFORM_KEY, **extra}
        r = await self.w.call("POST", "/platform/ai-credentials", "root", body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    async def test_superadmin_grants_a_platform_key_to_one_enterprise(self):
        c = await self.create(enterprise_ids=[self.w.eA.id])
        self.assertEqual((c["owner"], [e["id"] for e in c["enterprises"]]), ("platform", [self.w.eA.id]))
        self.assertNotIn(PLATFORM_KEY, json.dumps(c))
        d = (await self.w.call("GET", self.B + "/ai", "admin")).json()
        shared = [x for x in d["credentials"] if x["owner"] == "platform"]
        self.assertEqual([(x["id"], x["owner_name"]) for x in shared], [(c["id"], "Platform")])
        self.assertEqual((await self.w.call("GET", f"/bots/{self.w.bB.id}/ai", "owner_b")).json()["credentials"], [])
        r = await self.w.call("PUT", self.B + "/ai", "admin", {"credential_id": c["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        d = (await self.w.call("POST", self.B + "/ai/test", "admin", {})).json()
        self.assertTrue(d["ok"])
        self.assertEqual(self.calls[-1][2]["x-api-key"], PLATFORM_KEY)
        # Enterprise B cannot reference it
        r = await self.w.call("PUT", f"/bots/{self.w.bB.id}/ai", "owner_b", {"credential_id": c["id"]})
        self.assertEqual(r.status_code, 422)
        # tenants cannot read or manage it through tenant routes
        for path in (f"/credentials/{c['id']}", "/platform/ai-credentials"):
            self.assertEqual((await self.w.call("GET", path, "owner")).status_code, 404)
        self.assertEqual((await self.w.call("POST", f"/credentials/{c['id']}/revoke", "owner")).status_code, 404)
        # revocation fails closed at runtime and in the options
        self.assertEqual((await self.w.call("POST", f"/platform/ai-credentials/{c['id']}/revoke", "root")).status_code, 200)
        rt = (await self.w.call("GET", f"/instances/{self.w.iA_web.id}/runtime-config", "admin")).json()
        self.assertFalse(rt.get("runnable", True))
        d = (await self.w.call("GET", self.B + "/ai", "admin")).json()
        self.assertEqual([x for x in d["credentials"] if x["owner"] == "platform"], [])

    async def test_grant_changes_and_superadmin_only(self):
        c = await self.create(all_enterprises=True)
        d = (await self.w.call("GET", f"/bots/{self.w.bB.id}/ai", "owner_b")).json()
        self.assertEqual([x["id"] for x in d["credentials"]], [c["id"]])
        r = await self.w.call("PATCH", f"/platform/ai-credentials/{c['id']}", "root",
                              {"all_enterprises": False, "enterprise_ids": [self.w.eA.id], "name": "Shared"})
        self.assertEqual((r.status_code, r.json()["name"], r.json()["all_enterprises"]), (200, "Shared", False))
        self.assertEqual((await self.w.call("GET", f"/bots/{self.w.bB.id}/ai", "owner_b")).json()["credentials"], [])
        for who in ("owner", "admin", "owner_b"):
            for method, path, body in (("GET", "/platform/ai-credentials", None),
                                       ("POST", "/platform/ai-credentials", {"provider": "openai", "name": "x",
                                                                              "secret": OPENAI_KEY}),
                                       ("PATCH", f"/platform/ai-credentials/{c['id']}", {"name": "y"}),
                                       ("DELETE", f"/platform/ai-credentials/{c['id']}", None)):
                self.assertEqual((await self.w.call(method, path, who, body)).status_code, 404, (who, method, path))
        r = await self.w.call("POST", f"/platform/ai-credentials/{c['id']}/test", "root")
        self.assertEqual(r.json(), {"ok": True, "category": None})
        self.assertEqual(self.calls[-1][:2], ("GET", "https://api.anthropic.com/v1/models"))
        bad = await self.w.call("POST", "/platform/ai-credentials", "root",
                                {"provider": "telegram_bot", "name": "x", "secret": PLATFORM_KEY})
        self.assertEqual(bad.status_code, 422)
        bad = await self.w.call("POST", "/platform/ai-credentials", "root",
                                {"provider": "anthropic", "name": "x", "secret": "short", "enterprise_ids": []})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual((await self.w.call("DELETE", f"/platform/ai-credentials/{c['id']}", "root")).status_code, 200)
        snapshot = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(PLATFORM_KEY, snapshot)
        audit = [a["action"] for a in await self.w.store.find("audit_events", {}) if a["action"].startswith("platform_ai")]
        self.assertEqual(audit[:2], ["platform_ai_credential.create", "platform_ai_credential.update"])


if __name__ == "__main__":
    unittest.main()
