"""Restoration R3: API integrations in the Spec 012 hierarchy.

The external API is a fake behind the real outbound guard (fake DNS + httpx
MockTransport); the AI provider is a test double that answers the routing call
with a JSON selection and echoes whether live data reached the answer call."""
import json
import unittest

from tests.unit._api_fixtures import HAVE_API
from tests.unit.test_website import PROD, FakeWeb

OPENAPI = {
    "openapi": "3.0.0", "info": {"title": "Shop", "version": "1"},
    "paths": {
        "/orders/{order_id}": {"get": {"summary": "Order status", "tags": ["orders"],
                                       "parameters": [{"name": "order_id", "in": "path", "required": True,
                                                       "schema": {"type": "string"}}],
                                       "responses": {"200": {"description": "ok", "content": {"application/json": {
                                           "schema": {"type": "object", "properties": {"status": {"type": "string"}}}}}}}}},
        "/health": {"get": {"summary": "Health", "responses": {"200": {"description": "ok"}}}},
        "/orders": {"post": {"summary": "Create order", "responses": {"201": {"description": "created"}}}},
    }}


class RoutingProvider:
    name = "anthropic"

    def __init__(self):
        self.calls = []
        self.pick = None                   # (endpoint_id, parameters) for the routing call

    async def generate(self, request):
        from runtime.interfaces import GenerationResult
        self.calls.append(request)
        if request.knowledge_mode == "api_routing":
            calls = [{"endpoint_id": self.pick[0], "parameters": self.pick[1]}] if self.pick else []
            return GenerationResult(text=json.dumps({"calls": calls}), input_tokens=7, output_tokens=3)
        if "<api_data>" in request.instructions:
            data = request.instructions.split("<api_data>", 1)[1]
            return GenerationResult(text="Live: " + ("shipped" if '"shipped"' in data else "no data"),
                                    input_tokens=5, output_tokens=5)
        return GenerationResult(text="General answer.", input_tokens=5, output_tokens=5)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from runtime import api_calls as ac
        from tests.unit._api_fixtures import World
        self.w = await World().build()
        self.web = FakeWeb()
        self._saved = dict(ac.NETWORK)
        ac.NETWORK.update(resolver=self.web.resolve, client_factory=self.web.client_factory, env=PROD)
        self.web.page("https://docs.example.com/openapi.json", json.dumps(OPENAPI), ctype="application/json")
        self.web.page("https://docs.example.com/health", '{"ok": true}', ctype="application/json")
        self.web.page("https://docs.example.com/orders/A%2F1", '{"status": "shipped"}', ctype="application/json")
        self.provider = RoutingProvider()
        self.w.S.providers = {"anthropic": self.provider}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})

    async def asyncTearDown(self):
        from runtime import api_calls as ac
        ac.NETWORK.clear()
        ac.NETWORK.update(self._saved)

    async def create(self, who="admin", **extra):
        body = {"name": "Shop API", "base_url": "https://docs.example.com", "auth_type": "bearer",
                "secret": "shop-token-123", "doc_source_url": "https://docs.example.com/openapi.json", **extra}
        return await self.w.call("POST", f"/bots/{self.w.bA.id}/api-integrations", who, body)

    async def integration(self):
        r = await self.create()
        self.assertEqual(r.status_code, 201, r.text)
        iid = r.json()["id"]
        r = await self.w.call("POST", f"/api-integrations/{iid}/discover", "admin", {})
        self.assertEqual((r.status_code, r.json()["new"]), (200, 3), r.text)
        eps = (await self.w.call("GET", f"/api-integrations/{iid}/endpoints", "admin")).json()["endpoints"]
        return iid, {f"{e['method']} {e['path']}": e for e in eps}

    async def chat(self, text):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            r = await c.post(f"/api/public/instances/{self.w.iA_web.channel.public_id}/chat", json={"message": text},
                             headers={"Origin": "https://shop.example"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["reply"]

    async def test_create_with_a_secret_stores_an_encrypted_credential_never_returned(self):
        r = await self.create()
        self.assertEqual(r.status_code, 201, r.text)
        view = r.json()
        self.assertNotIn("shop-token-123", json.dumps(view))
        cred = await self.w.store.get("credentials", view["credential_id"])
        self.assertEqual((cred["provider"], cred["organisation_id"]), ("api_integration", self.w.oA.id))
        self.assertNotIn("shop-token-123", json.dumps(cred))
        listing = await self.w.call("GET", f"/bots/{self.w.bA.id}/api-integrations", "viewer")
        self.assertEqual([i["id"] for i in listing.json()["integrations"]], [view["id"]])

    async def test_validation_permissions_and_tenancy(self):
        for url in ("http://docs.example.com", "https://10.0.0.1", "https://localhost", "https://evil.example.org/x?y=1"):
            self.assertEqual((await self.create(base_url=url)).status_code, 422, url)
        self.assertEqual((await self.create(auth_type="magic")).status_code, 422)
        self.assertEqual((await self.create(auth_type="api_key_header", auth_header_name="Host")).status_code, 422)
        self.assertEqual((await self.create(who="viewer")).status_code, 403)
        self.assertIn((await self.create(who="owner_b")).status_code, (403, 404))
        iid = (await self.create()).json()["id"]
        for method, path in (("GET", ""), ("GET", "/endpoints"), ("POST", "/test-connection"), ("DELETE", "")):
            r = await self.w.call(method, f"/api-integrations/{iid}{path}", "owner_b")
            self.assertEqual(r.status_code, 404, (method, path))
        self.assertEqual((await self.w.call("POST", f"/api-integrations/{iid}/test-connection", "viewer")).status_code,
                         403)

    async def test_discovery_endpoint_selection_and_refresh_keeps_choices(self):
        iid, eps = await self.integration()
        self.assertTrue(eps["GET /orders/{order_id}"]["enabled"])
        self.assertFalse(eps["POST /orders"]["executable"])
        self.assertFalse(eps["POST /orders"]["enabled"])
        r = await self.w.call("PATCH", f"/api-integrations/{iid}/endpoints/{eps['POST /orders']['id']}", "admin",
                              {"enabled": True})
        self.assertEqual(r.status_code, 422)                      # only GET endpoints can run
        r = await self.w.call("PATCH", f"/api-integrations/{iid}/endpoints/{eps['GET /health']['id']}", "admin",
                              {"enabled": False})
        self.assertFalse(r.json()["enabled"])
        r = await self.w.call("POST", f"/api-integrations/{iid}/discover", "admin", {})
        self.assertEqual((r.json()["new"], r.json()["updated"]), (0, 3))
        again = (await self.w.call("GET", f"/api-integrations/{iid}/endpoints/{eps['GET /health']['id']}",
                                   "admin")).json()
        self.assertFalse(again["enabled"])                        # the admin's choice survives a refresh
        r = await self.w.call("POST", f"/api-integrations/{iid}/endpoints/bulk", "admin", {"enabled": True})
        self.assertEqual(r.json()["updated"], 2)
        view = (await self.w.call("GET", f"/api-integrations/{iid}", "admin")).json()
        self.assertEqual((view["endpoint_count"], view["enabled_endpoint_count"]), (3, 2))

    async def test_openapi_upload_and_manual_endpoints(self):
        iid = (await self.create(doc_source_url="", doc_source_type="openapi")).json()["id"]
        r = await self.w.call("POST", f"/api-integrations/{iid}/discover", "admin", {})
        self.assertEqual(r.status_code, 422)
        r = await self.w.call("POST", f"/api-integrations/{iid}/discover", "admin", {"document": OPENAPI})
        self.assertEqual(r.json()["new"], 3)
        r = await self.w.call("POST", f"/api-integrations/{iid}/endpoints", "admin",
                              {"path": "/stock/{sku}", "summary": "Stock level", "parameters": [{"name": "sku"}]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["parameters"][0]["in"], "path")
        self.assertEqual((await self.w.call("POST", f"/api-integrations/{iid}/discover", "admin",
                                            {"document": {"nothing": 1}})).status_code, 422)

    async def test_connection_and_endpoint_tests_send_the_credential_only_to_the_api(self):
        iid, eps = await self.integration()
        r = await self.w.call("POST", f"/api-integrations/{iid}/test-connection", "admin")
        self.assertTrue(r.json()["success"], r.text)
        ep = eps["GET /orders/{order_id}"]
        r = await self.w.call("POST", f"/api-integrations/{iid}/endpoints/{ep['id']}/test", "admin",
                              {"parameters": {"order_id": "A/1"}})
        self.assertEqual((r.json()["success"], r.json()["data"]), (True, {"status": "shipped"}), r.text)
        self.assertEqual(self.web.requests[-1][1], "/orders/A%2F1")       # path values cannot add segments
        r = await self.w.call("POST", f"/api-integrations/{iid}/endpoints/{ep['id']}/test", "admin", {"parameters": {}})
        self.assertIn("Missing required parameter", r.json()["error"])
        # a documentation URL on another host never receives the secret
        from runtime import api_calls as ac
        doc = await self.w.store.get("api_integrations", iid)
        headers, params = ac.auth_for(doc, "https://other.example.net/openapi.json", "s3cret")
        self.assertNotIn("Authorization", headers)
        headers, _ = ac.auth_for(doc, "https://docs.example.com/x", "s3cret")
        self.assertEqual(headers["Authorization"], "Bearer s3cret")
        activity = (await self.w.call("GET", f"/api-integrations/{iid}/activity", "viewer")).json()["activity"]
        self.assertEqual({a["source"] for a in activity}, {"connection_test", "test"})
        stats = (await self.w.call("GET", f"/api-integrations/{iid}/stats", "viewer")).json()
        self.assertEqual((stats["calls"], stats["succeeded"], stats["failed"]), (3, 2, 1))

    async def test_conversation_uses_live_api_data_when_enabled(self):
        iid, eps = await self.integration()
        self.provider.pick = (eps["GET /orders/{order_id}"]["id"], {"order_id": "A/1"})
        self.assertEqual(await self.chat("Where is order A/1?"), "General answer.")    # off by default
        self.assertFalse(any(c.knowledge_mode == "api_routing" for c in self.provider.calls))
        r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                              {"changes": {"tools.api_calls_enabled": True}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(await self.chat("Where is order A/1?"), "Live: shipped")
        routing = [c for c in self.provider.calls if c.knowledge_mode == "api_routing"][-1]
        self.assertIn("GET /orders/{order_id}", routing.user_text)
        self.assertNotIn("POST /orders", routing.user_text)
        answer = self.provider.calls[-1]
        self.assertIn("untrusted data", answer.instructions)
        self.assertNotIn("shop-token-123", answer.instructions)
        activity = (await self.w.call("GET", f"/api-integrations/{iid}/activity", "admin")).json()["activity"]
        self.assertEqual(activity[0]["source"], "conversation")
        # the routing call is counted as an AI message
        ledger = await self.w.store.find("usage_ledger", {"enterprise_id": self.w.eA.id})
        self.assertTrue(any(entry.get("purpose") == "api_routing" for entry in ledger))

    async def test_unknown_or_disabled_endpoints_are_never_called(self):
        iid, eps = await self.integration()
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner", {"changes": {"tools.api_calls_enabled": True}})
        await self.w.call("PATCH", f"/api-integrations/{iid}/endpoints/{eps['GET /orders/{order_id}']['id']}", "admin",
                          {"enabled": False})
        before = len(self.web.requests)
        self.provider.pick = (eps["GET /orders/{order_id}"]["id"], {"order_id": "A/1"})
        self.assertEqual(await self.chat("Where is order A/1?"), "General answer.")
        self.provider.pick = ("not-an-endpoint", {})
        self.assertEqual(await self.chat("Where is order A/1?"), "General answer.")
        self.assertEqual(len(self.web.requests), before)

    async def test_strict_mode_never_calls_apis(self):
        iid, eps = await self.integration()
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"tools.api_calls_enabled": True}})
        self.provider.pick = (eps["GET /orders/{order_id}"]["id"], {"order_id": "A/1"})
        from tests.unit.test_knowledge import KnowledgeBase
        src = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/knowledge/sources", "admin",
                                 {"name": "Docs"})).json()["id"]
        await KnowledgeBase.approve(self, [src])                 # strict_knowledge_only
        before = len(self.web.requests)
        await self.chat("Where is order A/1?")
        self.assertFalse(any(c.knowledge_mode == "api_routing" for c in self.provider.calls))
        self.assertEqual(len(self.web.requests), before)

    async def test_enterprise_policy_can_forbid_api_calls(self):
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"tools.external_api_calls_allowed": False}})
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                              {"changes": {"tools.api_calls_enabled": True}})
        self.assertEqual(r.status_code, 422)

    async def test_revoked_credential_fails_safely(self):
        iid, eps = await self.integration()
        doc = await self.w.store.get("api_integrations", iid)
        r = await self.w.call("POST", f"/credentials/{doc['credential_id']}/revoke", "owner")
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.w.call("POST", f"/api-integrations/{iid}/test-connection", "admin")
        self.assertEqual(r.status_code, 422)
        self.assertIn("credential is not available", r.json()["detail"])

    async def test_delete_removes_endpoints_and_activity(self):
        iid, _ = await self.integration()
        await self.w.call("POST", f"/api-integrations/{iid}/test-connection", "admin")
        self.assertEqual((await self.w.call("DELETE", f"/api-integrations/{iid}", "admin")).status_code, 200)
        for col in ("api_integrations", "api_endpoints", "api_activity"):
            self.assertEqual(await self.w.store.count(col, {"enterprise_id": self.w.eA.id}), 0, col)
        events = [e["action"] for e in await self.w.store.find("audit_events", {"enterprise_id": self.w.eA.id})]
        for action in ("api_integration.create", "api_integration.discover", "api_integration.test_connection",
                       "api_integration.delete"):
            self.assertIn(action, events)


if __name__ == "__main__":
    unittest.main()
