"""Spec 009 — v2 tenant isolation regression tests (in-process FastAPI).

Two tenants (workspace/org A and workspace/org B) share one in-memory Motor
stand-in. Every org-scoped v2 route is attacked from tenant A against tenant B:

  * mismatched ``X-Org-ID`` header vs URL org                → 400
  * URL org B without membership (with or without header)   → 403
  * own URL org A but a nested resource id from org B       → 404

After every rejected request the whole database must be byte-for-byte
unchanged. Legitimate access (members, workspace admins, platform
superadmins) keeps working. Needs the backend dependencies; skipped otherwise.
Never touches a real database.
"""
import copy
import importlib.util
import os
import sys
import unittest
from unittest import mock

from tests.unit._support import BACKEND_DIR  # noqa: F401  (adds backend/ to sys.path)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "motor", "httpx", "bcrypt", "jwt", "jose", "bson"))

TEST_ENV = {
    "APP_MODE": "api",
    "APP_ENV": "production",
    "CORS_ORIGINS": "https://bitbotpro.io",
    "MONGO_URL": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=300&connectTimeoutMS=300",
    "DB_NAME": "unit_test_never_used",
    "JWT_SECRET": "unit-test-jwt-secret-not-real-0123456789",
    "CODE_HMAC_KEY": "unit-test-code-hmac-key-not-real-abcdefghijklmnop",
    "PUBLIC_APP_URL": "https://bitbotpro.io",
    "PUBLIC_API_URL": "https://api.bitbotpro.io",
    "AUTH_RESPONSE_FLOOR_MS": "0",
    "ENCRYPTION_KEY": "dW5pdC10ZXN0LWZlcm5ldC1rZXktbm90LXJlYWwtMDA=",  # synthetic Fernet key
}

# Synthetic, obviously fake secret values: used to prove they never leak.
FAKE_ANTHROPIC_KEY = "sk-ant-UNITTEST-not-a-real-key-000000000000"
FAKE_DISCORD_TOKEN = "UNITTEST.fake-discord-bot-token.000000000000"
SUGGESTION_OID = "65a1b2c3d4e5f60718293a4b"

ORG_A, ORG_B = "org-a", "org-b"
WS_A, WS_B = "ws-a", "ws-b"

server = None
client = None


def setUpModule():
    global server, client
    if not HAVE_DEPS:
        return
    for k in ("CLAUDE_API_KEY", "EMERGENT_LLM_KEY", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
    sys.modules.pop("server", None)
    import server as _server  # noqa: E402
    from fastapi.testclient import TestClient
    server = _server
    client = TestClient(server.app)  # startup does not run: no real database


def tearDownModule():
    global server, client
    mongo_client = getattr(server, "mongo_client", None)
    if mongo_client is not None:
        mongo_client.close()
    server = None
    client = None


def _seed(store):
    from bson import ObjectId
    users = [
        ("alice", "user"),        # org A admin
        ("bob", "user"),          # org B admin
        ("carol", "user"),        # plain member of org A
        ("wsadmin-a", "user"),    # workspace A admin, not a direct org member
        ("wsmember-a", "user"),   # workspace A plain member
        ("root", "superadmin"),   # platform superadmin
    ]
    for uid, role in users:
        store.col("users").append({
            "id": uid, "email": f"{uid}@example.org", "username": uid, "role": role,
            "is_verified": True, "token_version": 0, "hashed_password": "x",
        })
    store.col("workspaces").extend([
        {"id": WS_A, "name": "A", "slug": "a", "owner_id": "alice",
         "billing": {"limits": {"max_organisations": 3}}},
        {"id": WS_B, "name": "B", "slug": "b", "owner_id": "bob",
         "billing": {"limits": {"max_organisations": 3}}},
    ])
    store.col("workspace_members").extend([
        {"workspace_id": WS_A, "user_id": "wsadmin-a", "role": "admin"},
        {"workspace_id": WS_A, "user_id": "wsmember-a", "role": "member"},
        {"workspace_id": WS_B, "user_id": "bob", "role": "owner"},
    ])
    store.col("organisations").extend([
        {"id": ORG_A, "name": "Org A", "workspace_id": WS_A, "owner_id": "alice",
         "member_ids": ["alice", "carol"], "member_roles": {"alice": "admin", "carol": "member"}},
        {"id": ORG_B, "name": "Org B", "workspace_id": WS_B, "owner_id": "bob",
         "member_ids": ["bob"], "member_roles": {"bob": "admin"}},
    ])
    for org, bot in ((ORG_A, "bot-a"), (ORG_B, "bot-b")):
        store.col("bots").append({
            "id": bot, "org_id": org, "name": bot, "persona": "p",
            "llm_config": {"provider": "anthropic", "model": None, "anthropic_api_key": FAKE_ANTHROPIC_KEY},
        })
        store.col("bot_templates").append({
            "id": f"tpl-{bot}", "org_id": org, "name": f"T {bot}",
            "llm_config": {"provider": "anthropic", "anthropic_api_key": FAKE_ANTHROPIC_KEY},
        })
        store.col("connected_accounts").append({
            "id": f"acct-{bot}", "org_id": org, "provider": "discord", "name": f"A {bot}",
            "status": "connected", "credentials": {"bot_token": FAKE_DISCORD_TOKEN},
        })
        store.col("connections").append({
            "id": f"conn-{bot}", "org_id": org, "bot_id": bot, "account_id": f"acct-{bot}",
            "provider": "discord", "name": f"C {bot}", "enabled": False,
            "destination": {"channel_id": f"chan-{bot}"},
        })
    store.col("v2_suggestions").append({
        "_id": ObjectId(SUGGESTION_OID), "bot_id": "bot-b", "connection_id": "conn-bot-b",
        "status": "pending",
    })


def _routes(o, bot, tpl, acct, conn, member):
    """Every org-scoped v2 route, with a body valid enough to pass validation."""
    p = f"/api/v2/orgs/{o}"
    b = f"{p}/bots/{bot}"
    return [
        ("GET", p, None),
        ("PUT", p, {"name": "pwned"}),
        ("DELETE", p, None),
        ("GET", f"{p}/members", None),
        ("POST", f"{p}/members", {"user_id": "alice", "role": "admin"}),
        ("DELETE", f"{p}/members/{member}", None),
        ("PATCH", f"{p}/members/{member}/role", {"user_id": member, "role": "member"}),
        ("GET", f"{p}/overview", None),
        ("GET", f"{p}/bots", None),
        ("POST", f"{p}/bots", {"name": "planted"}),
        ("POST", f"{b}/clone", None),
        ("GET", b, None),
        ("PUT", b, {"name": "pwned"}),
        ("DELETE", b, None),
        ("GET", f"{b}/settings", None),
        ("PUT", f"{b}/settings", {"persona": "pwned"}),
        ("POST", f"{b}/save-as-template", {"name": "stolen"}),
        ("GET", f"{p}/templates", None),
        ("POST", f"{p}/templates/{tpl}/create-bot", {"name": "planted"}),
        ("DELETE", f"{p}/templates/{tpl}", None),
        ("GET", f"{b}/llm-config", None),
        ("PUT", f"{b}/llm-config", {"provider": "openai", "openai_api_key": "sk-UNITTEST"}),
        ("POST", f"{b}/llm-config/test", None),
        ("GET", f"{p}/accounts", None),
        ("POST", f"{p}/accounts", {"provider": "discord", "name": "planted", "credentials": {"bot_token": "x"}}),
        ("GET", f"{p}/accounts/{acct}", None),
        ("PUT", f"{p}/accounts/{acct}", {"name": "pwned", "credentials": {"bot_token": "y"}}),
        ("POST", f"{p}/accounts/{acct}/disconnect", None),
        ("POST", f"{p}/accounts/{acct}/reconnect", None),
        ("POST", f"{p}/accounts/{acct}/test", None),
        ("DELETE", f"{p}/accounts/{acct}", None),
        ("GET", f"{b}/connections", None),
        ("POST", f"{b}/connections", {"account_id": acct, "name": "planted", "destination": {"channel_id": "c9"}}),
        ("GET", f"{b}/connections/{conn}", None),
        ("PUT", f"{b}/connections/{conn}", {"name": "pwned", "enabled": True}),
        ("PATCH", f"{b}/connections/{conn}/toggle", None),
        ("GET", f"{b}/connections/{conn}/activity", None),
        ("GET", f"{b}/suggestions", None),
        ("PATCH", f"{b}/suggestions/{SUGGESTION_OID}", {"status": "accepted"}),
    ]


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        from tests.unit._fake_motor import patch_motor
        self.store = patch_motor(self)
        _seed(self.store)
        # No outbound calls may ever happen from these tests.
        p = mock.patch("aiohttp.ClientSession", side_effect=AssertionError("network call attempted"))
        p.start()
        self.addCleanup(p.stop)

    # helpers
    def auth(self, uid, org_header=None):
        role = "superadmin" if uid == "root" else "user"
        h = {"Authorization": f"Bearer {server.create_access_token(uid, f'{uid}@example.org', role, 0)}"}
        if org_header:
            h["X-Org-ID"] = org_header
        return h

    def snapshot(self):
        return copy.deepcopy({k: v for k, v in self.store.cols.items()})

    def call(self, method, path, body, headers):
        return client.request(method, path, json=body, headers=headers)

    def assert_rejected_without_change(self, routes, headers, expected):
        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                before = self.snapshot()
                r = self.call(method, path, body, headers)
                self.assertEqual(r.status_code, expected, f"{method} {path} -> {r.status_code} {r.text}")
                self.assertEqual(self.snapshot(), before, f"{method} {path} modified the database")
                self.assertNotIn(FAKE_ANTHROPIC_KEY, r.text)
                self.assertNotIn(FAKE_DISCORD_TOKEN, r.text)

    def b_routes(self):
        return _routes(ORG_B, "bot-b", "tpl-bot-b", "acct-bot-b", "conn-bot-b", "bob")

    # ── R1.2: header/URL mismatch is rejected before anything else
    def test_mismatched_org_header_is_rejected_for_every_route(self):
        self.assert_rejected_without_change(self.b_routes(), self.auth("alice", ORG_A), 400)

    def test_mismatched_org_header_is_rejected_even_for_superadmin(self):
        self.assert_rejected_without_change(self.b_routes(), self.auth("root", ORG_A), 400)

    # ── R1.1: authorisation is against the org in the URL
    def test_non_member_cannot_touch_other_org_without_header(self):
        self.assert_rejected_without_change(self.b_routes(), self.auth("alice"), 403)

    def test_non_member_cannot_touch_other_org_with_matching_header(self):
        self.assert_rejected_without_change(self.b_routes(), self.auth("alice", ORG_B), 403)

    def test_workspace_admin_has_no_access_to_other_workspace_orgs(self):
        self.assert_rejected_without_change(self.b_routes(), self.auth("wsadmin-a", ORG_B), 403)

    def test_unknown_org_is_404_and_changes_nothing(self):
        routes = _routes("no-such-org", "bot-b", "tpl-bot-b", "acct-bot-b", "conn-bot-b", "bob")
        self.assert_rejected_without_change(routes, self.auth("alice"), 404)

    # ── R1.4: nested resources must belong to the verified org
    def test_nested_resources_from_other_org_are_404_under_own_org(self):
        p = f"/api/v2/orgs/{ORG_A}"
        own_bot = f"{p}/bots/bot-a"
        routes = [
            r for r in _routes(ORG_A, "bot-b", "tpl-bot-b", "acct-bot-b", "conn-bot-b", "bob")
            if "/bots/bot-b" in r[1] or "/templates/tpl-bot-b" in r[1]
            or "/accounts/acct-bot-b" in r[1] or "/members/bob" in r[1]
        ]
        # Own bot, but another org's connection / account / suggestion.
        routes += [
            ("GET", f"{own_bot}/connections/conn-bot-b", None),
            ("PUT", f"{own_bot}/connections/conn-bot-b", {"name": "pwned", "enabled": True}),
            ("PATCH", f"{own_bot}/connections/conn-bot-b/toggle", None),
            ("GET", f"{own_bot}/connections/conn-bot-b/activity", None),
            ("POST", f"{own_bot}/connections", {"account_id": "acct-bot-b", "name": "x", "destination": {"channel_id": "c9"}}),
            ("PATCH", f"{own_bot}/suggestions/{SUGGESTION_OID}", {"status": "accepted"}),
            ("PATCH", f"{own_bot}/suggestions/not-an-object-id", {"status": "accepted"}),
        ]
        self.assertGreater(len(routes), 25)
        self.assert_rejected_without_change(routes, self.auth("alice", ORG_A), 404)

    def test_every_org_scoped_route_is_covered(self):
        """Fail when a new /v2/orgs/{org_id}/… route is added without a test here."""
        covered = set()
        for method, path, _ in self.b_routes():
            norm = (path.replace(ORG_B, "{org_id}").replace("bot-b", "{bot_id}")
                    .replace("tpl-{bot_id}", "{template_id}").replace("acct-{bot_id}", "{account_id}")
                    .replace("conn-{bot_id}", "{conn_id}").replace(SUGGESTION_OID, "{suggestion_id}")
                    .replace("/members/bob", "/members/{member_id}"))
            covered.add((method, norm))
        registered = set()
        for route in server.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/v2/orgs/{org_id}"):
                for m in route.methods - {"HEAD", "OPTIONS"}:
                    registered.add((m, path))
        self.assertTrue(registered)
        self.assertEqual(registered - covered, set())

    # ── R1.6: legitimate access is preserved
    def test_member_can_use_own_org_with_or_without_header(self):
        for headers in (self.auth("alice", ORG_A), self.auth("alice")):
            r = client.get(f"/api/v2/orgs/{ORG_A}/bots", headers=headers)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([b["id"] for b in r.json()["bots"]], ["bot-a"])

    def test_plain_org_member_can_read_own_org(self):
        r = client.get(f"/api/v2/orgs/{ORG_A}/bots/bot-a", headers=self.auth("carol", ORG_A))
        self.assertEqual(r.status_code, 200, r.text)

    def test_workspace_admin_inherits_access_to_own_workspace_orgs(self):
        r = client.get(f"/api/v2/orgs/{ORG_A}/bots", headers=self.auth("wsadmin-a", ORG_A))
        self.assertEqual(r.status_code, 200, r.text)

    def test_superadmin_keeps_access_to_any_org(self):
        r = client.get(f"/api/v2/orgs/{ORG_B}/bots", headers=self.auth("root", ORG_B))
        self.assertEqual(r.status_code, 200, r.text)
        r = client.post(f"/api/v2/orgs/{ORG_B}/bots", json={"name": "Support"}, headers=self.auth("root"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["org_id"], ORG_B)

    def test_create_bot_writes_into_verified_org(self):
        r = client.post(f"/api/v2/orgs/{ORG_A}/bots", json={"name": " Support "}, headers=self.auth("alice", ORG_A))
        self.assertEqual(r.status_code, 200, r.text)
        created = [b for b in self.store.col("bots") if b["name"] == "Support"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["org_id"], ORG_A)

    def test_clone_and_template_bots_stay_in_verified_org(self):
        h = self.auth("alice", ORG_A)
        r = client.post(f"/api/v2/orgs/{ORG_A}/bots/bot-a/clone", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["org_id"], ORG_A)
        r = client.post(f"/api/v2/orgs/{ORG_A}/bots/bot-a/save-as-template", json={"name": "Tpl"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["org_id"], ORG_A)
        r = client.post(f"/api/v2/orgs/{ORG_A}/templates/tpl-bot-a/create-bot", json={"name": "From tpl"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["org_id"], ORG_A)
        self.assertEqual({b["org_id"] for b in self.store.col("bots") if b["id"] != "bot-b"}, {ORG_A})

    def test_connection_lifecycle_in_own_org(self):
        h = self.auth("alice", ORG_A)
        base = f"/api/v2/orgs/{ORG_A}/bots/bot-a/connections"
        r = client.post(base, json={"account_id": "acct-bot-a", "name": "Main", "destination": {"channel_id": "c1"}}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        cid = r.json()["id"]
        self.assertEqual(client.patch(f"{base}/{cid}/toggle", headers=h).json()["enabled"], False)
        self.assertEqual(client.put(f"{base}/{cid}", json={"name": "Renamed"}, headers=h).status_code, 200)
        conn = next(c for c in self.store.col("connections") if c["id"] == cid)
        self.assertEqual((conn["org_id"], conn["name"], conn["enabled"]), (ORG_A, "Renamed", False))

    # ── R1.5: membership management
    def test_member_roles_are_validated(self):
        h = self.auth("alice", ORG_A)
        for role in ("owner", "superadmin", "", 5):
            with self.subTest(role=role):
                before = self.snapshot()
                r = client.post(f"/api/v2/orgs/{ORG_A}/members", json={"user_id": "bob", "role": role}, headers=h)
                self.assertIn(r.status_code, (400, 422), r.text)
                self.assertEqual(self.snapshot(), before)
        r = client.patch(f"/api/v2/orgs/{ORG_A}/members/carol/role", json={"user_id": "carol", "role": "owner"}, headers=h)
        self.assertEqual(r.status_code, 400, r.text)

    def test_member_management_in_own_org(self):
        h = self.auth("alice", ORG_A)
        self.assertEqual(client.post(f"/api/v2/orgs/{ORG_A}/members", json={"user_id": "wsmember-a"}, headers=h).status_code, 200)
        self.assertEqual(client.patch(f"/api/v2/orgs/{ORG_A}/members/wsmember-a/role",
                                      json={"user_id": "wsmember-a", "role": "admin"}, headers=h).status_code, 200)
        self.assertEqual(client.delete(f"/api/v2/orgs/{ORG_A}/members/wsmember-a", headers=h).status_code, 200)
        org_a = next(o for o in self.store.col("organisations") if o["id"] == ORG_A)
        self.assertNotIn("wsmember-a", org_a["member_ids"])
        self.assertNotIn("wsmember-a", org_a["member_roles"])
        org_b = next(o for o in self.store.col("organisations") if o["id"] == ORG_B)
        self.assertEqual(org_b["member_ids"], ["bob"])

    def test_plain_member_cannot_manage_members(self):
        before = self.snapshot()
        r = client.post(f"/api/v2/orgs/{ORG_A}/members", json={"user_id": "bob"}, headers=self.auth("carol", ORG_A))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.snapshot(), before)

    def test_create_org_auto_assign_requires_workspace_admin(self):
        r = client.post("/api/v2/orgs", json={"name": "Side org"}, headers=self.auth("wsmember-a"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(r.json()["workspace_id"], WS_A)
        r = client.post("/api/v2/orgs", json={"name": "Real org"}, headers=self.auth("wsadmin-a"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["workspace_id"], WS_A)

    def test_create_org_auto_assign_enforces_workspace_limit(self):
        self.store.col("organisations").extend([
            {"id": f"extra-{i}", "name": f"x{i}", "workspace_id": WS_A, "member_ids": []} for i in range(2)
        ])  # WS_A now holds 3 orgs (limit 3)
        before = self.snapshot()
        r = client.post("/api/v2/orgs", json={"name": "Over limit"}, headers=self.auth("wsadmin-a"))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.snapshot(), before)

    def test_create_org_in_other_workspace_is_forbidden(self):
        before = self.snapshot()
        r = client.post("/api/v2/orgs", json={"name": "Plant", "workspace_id": WS_B}, headers=self.auth("wsadmin-a"))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.snapshot(), before)

    def test_create_org_requires_name(self):
        r = client.post("/api/v2/orgs", json={"name": "  ", "workspace_id": WS_A}, headers=self.auth("wsadmin-a"))
        self.assertEqual(r.status_code, 400, r.text)

    def test_only_owner_or_superadmin_deletes_org(self):
        before = self.snapshot()
        self.assertEqual(client.delete(f"/api/v2/orgs/{ORG_A}", headers=self.auth("carol", ORG_A)).status_code, 403)
        self.assertEqual(self.snapshot(), before)
        r = client.delete(f"/api/v2/orgs/{ORG_B}", headers=self.auth("root", ORG_B))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual({b["org_id"] for b in self.store.col("bots")}, {ORG_A})
        self.assertEqual({a["org_id"] for a in self.store.col("connected_accounts")}, {ORG_A})


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class SecretExposureAndProviderDefaultTests(unittest.TestCase):
    def setUp(self):
        from tests.unit._fake_motor import patch_motor
        self.store = patch_motor(self)
        _seed(self.store)
        self.h = {"Authorization": f"Bearer {server.create_access_token('alice', 'alice@example.org', 'user', 0)}",
                  "X-Org-ID": ORG_A}

    def test_bot_and_template_responses_never_include_api_keys(self):
        for path in (f"/api/v2/orgs/{ORG_A}/bots", f"/api/v2/orgs/{ORG_A}/bots/bot-a",
                     f"/api/v2/orgs/{ORG_A}/bots/bot-a/settings", f"/api/v2/orgs/{ORG_A}/templates",
                     f"/api/v2/orgs/{ORG_A}/bots/bot-a/llm-config", f"/api/v2/orgs/{ORG_A}/accounts",
                     f"/api/v2/orgs/{ORG_A}/accounts/acct-bot-a"):
            with self.subTest(path=path):
                r = client.get(path, headers=self.h)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotIn(FAKE_ANTHROPIC_KEY, r.text)
                self.assertNotIn(FAKE_DISCORD_TOKEN, r.text)
        for path in (f"/api/v2/orgs/{ORG_A}/bots/bot-a/clone", f"/api/v2/orgs/{ORG_A}/bots/bot-a/save-as-template"):
            with self.subTest(path=path):
                r = client.post(path, json={}, headers=self.h)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotIn(FAKE_ANTHROPIC_KEY, r.text)
                self.assertTrue(r.json()["llm_config"]["anthropic_api_key_set"])

    def test_stored_keys_are_kept_when_cloning(self):
        r = client.post(f"/api/v2/orgs/{ORG_A}/bots/bot-a/clone", headers=self.h)
        clone = next(b for b in self.store.col("bots") if b["id"] == r.json()["id"])
        self.assertEqual(clone["llm_config"]["anthropic_api_key"], FAKE_ANTHROPIC_KEY)
        self.assertTrue(r.json()["llm_configured"])

    def test_new_bot_defaults_to_anthropic_without_key(self):
        r = client.post(f"/api/v2/orgs/{ORG_A}/bots", json={"name": "Fresh"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["llm_config"]["provider"], "anthropic")
        self.assertFalse(body["llm_configured"])
        stored = next(b for b in self.store.col("bots") if b["id"] == body["id"])
        self.assertEqual(stored["llm_config"], {"provider": "anthropic", "model": None})
        self.assertNotEqual(stored["llm_config"]["provider"], "emergent")
        r = client.get(f"/api/v2/orgs/{ORG_A}/bots/{body['id']}/llm-config", headers=self.h)
        self.assertFalse(r.json()["llm_configured"])
        self.assertFalse(r.json()["emergent_available"])

    def test_template_without_config_creates_anthropic_bot(self):
        self.store.col("bot_templates").append({"id": "tpl-bare", "org_id": ORG_A, "name": "Bare"})
        r = client.post(f"/api/v2/orgs/{ORG_A}/templates/tpl-bare/create-bot", json={"name": "B"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["llm_config"]["provider"], "anthropic")

    def test_existing_emergent_bot_is_not_migrated(self):
        self.store.col("bots").append({"id": "legacy", "org_id": ORG_A, "name": "Old",
                                       "llm_config": {"provider": "emergent", "model": None}})
        r = client.get(f"/api/v2/orgs/{ORG_A}/bots/legacy", headers=self.h)
        self.assertEqual(r.json()["llm_config"]["provider"], "emergent")
        self.assertFalse(r.json()["llm_configured"])  # no Emergent key on this deployment
        stored = next(b for b in self.store.col("bots") if b["id"] == "legacy")
        self.assertEqual(stored["llm_config"], {"provider": "emergent", "model": None})

    def test_settings_update_cannot_overwrite_llm_config(self):
        r = client.put(f"/api/v2/orgs/{ORG_A}/bots/bot-a/settings",
                       json={"persona": "new", "llm_config": {"provider": "emergent"}}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        stored = next(b for b in self.store.col("bots") if b["id"] == "bot-a")
        self.assertEqual(stored["persona"], "new")
        self.assertEqual(stored["llm_config"]["anthropic_api_key"], FAKE_ANTHROPIC_KEY)

    def test_new_bot_requires_a_name(self):
        r = client.post(f"/api/v2/orgs/{ORG_A}/bots", json={"name": "   "}, headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class MissingKeyMessageTests(unittest.TestCase):
    def test_anthropic_stream_without_key_fails_clearly_before_any_client(self):
        import asyncio
        from fastapi import HTTPException
        with mock.patch("anthropic.AsyncAnthropic", side_effect=AssertionError("client created")):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(server._stream_anthropic("s", "hi", "sys", "bot", "claude-sonnet-4-6", ""))
        self.assertIn("Anthropic API key", ctx.exception.detail)
        self.assertIn("Bot Settings", ctx.exception.detail)

    def test_anthropic_call_without_key_fails_clearly(self):
        import asyncio
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(server._call_anthropic("s", "hi", "sys", "bot", "claude-sonnet-4-6", ""))
        self.assertIn("Anthropic API key", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
