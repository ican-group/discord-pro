"""Phase 6A, Parts C and E: journeys over the real HTTP boundary.

Runs against the running local server (``tests/e2e/server.py``) with real
session cookies and the CSRF header, exactly like the browser. Skipped unless
``E2E_BASE_URL`` is set. Tests run in name order and share one server.
"""
import json
import os
import re
import time
import unittest
from urllib.parse import parse_qs, urlparse

BASE = os.environ.get("E2E_BASE_URL", "").rstrip("/")
CSRF = {"X-Requested-With": "BitBotPro"}


def client():
    import httpx
    return httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)


@unittest.skipUnless(BASE, "E2E_BASE_URL not set (needs the running local e2e environment)")
class Journeys(unittest.TestCase):
    ids = None
    sessions = {}

    @classmethod
    def setUpClass(cls):
        with client() as c:
            cls.ids = c.get("/__e2e/ids").json()
        cls.pw = cls.ids["password"]

    def as_(self, who):
        """A logged-in client (cookie session) for a seeded user."""
        if who not in self.sessions:
            c = client()
            r = c.post("/api/auth/login", json={"email": f"{who}@e2e.test", "password": self.pw}, headers=CSRF)
            assert r.status_code == 200, (who, r.text)
            self.sessions[who] = c
        return self.sessions[who]

    def api(self, who, method, path, body=None, headers=None, **kw):
        h = dict(CSRF if method not in ("GET", "HEAD") else {})
        h.update(headers or {})
        return self.as_(who).request(method, "/api" + path, json=body, headers=h, **kw)

    def outbox(self):
        with client() as c:
            return c.get("/__e2e/outbox").json()["messages"]

    # ── C1–C7: authentication and sessions ──
    def test_01_login_cookie_flags_csrf_and_logout(self):
        c = client()
        r = c.post("/api/auth/login", json={"email": "viewer@e2e.test", "password": self.pw})
        self.assertEqual(r.status_code, 403)                                      # CSRF header required, even for login
        r = c.post("/api/auth/login", json={"email": "viewer@e2e.test", "password": "wrong-password-x"}, headers=CSRF)
        self.assertEqual(r.status_code, 401)
        r = c.post("/api/auth/login", json={"email": "viewer@e2e.test", "password": self.pw}, headers=CSRF)
        self.assertEqual(r.status_code, 200)
        cookie = r.headers["set-cookie"]
        for flag in ("HttpOnly", "Path=/api", "SameSite=strict"):
            self.assertIn(flag.lower(), cookie.lower())
        self.assertNotIn("bbp_session", r.text)                                   # token only in the cookie
        self.assertEqual(c.get("/api/auth/session").json()["user"]["email"], "viewer@e2e.test")
        r = c.patch(f"/api/organisations/{self.ids['org_support']}", json={"name": "x"})
        self.assertEqual(r.status_code, 403)                                      # cookie session without CSRF header
        self.assertEqual(c.post("/api/auth/logout", headers=CSRF).status_code, 200)
        self.assertEqual(c.get("/api/auth/session").status_code, 401)

    def test_02_password_reset_and_change(self):
        with client() as c:
            self.assertEqual(c.post("/api/auth/forgot-password", json={"email": "newcomer@e2e.test"}, headers=CSRF).status_code, 202)
        [mail] = [m for m in self.outbox() if m["kind"] == "reset" and m["to"] == "newcomer@e2e.test"]
        self.assertTrue(mail["text"].startswith(BASE + "/app/reset-password#token="))
        token = mail["text"].split("#token=")[1]
        new = "Violet-Lantern-Quarry-2026"
        with client() as c:
            self.assertEqual(c.post("/api/auth/reset-password", json={"token": token, "new_password": new}, headers=CSRF).status_code, 200)
            self.assertEqual(c.post("/api/auth/reset-password", json={"token": token, "new_password": new + "x"}, headers=CSRF).status_code, 400)
            self.assertEqual(c.post("/api/auth/login", json={"email": "newcomer@e2e.test", "password": new}, headers=CSRF).status_code, 200)
            r = c.post("/api/auth/change-password", json={"current_password": new, "new_password": self.pw}, headers=CSRF)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(c.get("/api/auth/session").status_code, 200)        # this session stays signed in

    def test_03_superadmin_only_platform(self):
        self.assertEqual(self.api("root", "GET", "/platform/users?q=own").status_code, 200)
        for who in ("owner", "admin", "orgadmin", "ownerb"):
            for path in ("/platform/users", "/platform/credentials", "/platform/workers",
                         f"/platform/enterprises/{self.ids['ent_a']}/discord-application"):
                self.assertEqual(self.api(who, "GET", path).status_code, 404, (who, path))

    # ── C8–C12: creation through the hierarchy ──
    def test_04_create_hierarchy_end_to_end(self):
        r = self.api("root", "POST", "/enterprises", {"name": "Initech", "slug": "initech-e2e",
                                                      "owner_user_id": self.ids["users"]["newcomer"]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.api("owner", "POST", "/enterprises", {"name": "X", "slug": "x-e2e"}).status_code, 404)
        org = self.api("owner", "POST", f"/enterprises/{self.ids['ent_a']}/organisations", {"name": "Marketing"})
        self.assertEqual(org.status_code, 201, org.text)
        bot = self.api("admin", "POST", f"/organisations/{org.json()['id']}/bots", {"name": "Promo bot"})
        self.assertEqual(bot.status_code, 201, bot.text)
        b = bot.json()["id"]
        for ch in ({"type": "webchat"}, {"type": "discord"}):
            r = self.api("admin", "POST", f"/bots/{b}/instances", {"name": ch["type"], "channel": ch})
            self.assertEqual(r.status_code, 201, r.text)
        r = self.api("orgadmin", "POST", f"/bots/{self.ids['bot']}/instances",
                     {"name": "Support Telegram", "channel": {"type": "telegram", "credential_id": self.ids["tg_cred"]}})
        self.assertEqual(r.status_code, 201, r.text)
        r = self.api("orgadmin", "POST", f"/bots/{self.ids['bot']}/integrations", {"type": "zendesk", "name": "Desk"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["type"], "zendesk")
        self.assertNotIn("instance", json.dumps(r.json()).lower().replace("instances", ""))

    # ── C13–C14: memberships and invitations ──
    def test_05_memberships_invitations_and_last_owner(self):
        E = self.ids["ent_a"]
        r = self.api("owner", "POST", f"/enterprises/{E}/invitations", {"email": "newcomer@e2e.test", "role": "viewer"})
        self.assertEqual((r.status_code, r.json().get("email_sent")), (201, True), r.text)
        self.assertNotIn("token", r.json())
        mail = [m for m in self.outbox() if m["kind"] == "invitation" and m["to"] == "newcomer@e2e.test"][-1]
        token = mail["text"].split("#token=")[1].split()[0]
        self.assertEqual(self.api("viewer", "POST", "/invitations/accept", {"token": token}).status_code, 404)
        self.assertEqual(self.api("newcomer", "POST", "/invitations/accept", {"token": token}).status_code, 200)
        self.assertEqual(self.api("newcomer", "POST", "/invitations/accept", {"token": token}).status_code, 404)
        cands = self.api("orgadmin", "GET", f"/organisations/{self.ids['org_support']}/member-candidates?q=newc").json()
        self.assertEqual([c["email"] for c in cands["candidates"]], ["newcomer@e2e.test"])
        self.assertEqual(self.api("orgadmin", "GET", f"/organisations/{self.ids['org_support']}/member-candidates?q=owne").json()
                         ["candidates"][0]["email"], "owner@e2e.test")
        self.assertNotIn("ownerb@e2e.test", json.dumps(self.api("orgadmin", "GET",
                         f"/organisations/{self.ids['org_support']}/member-candidates?q=own").json()))
        uid = self.ids["users"]["owner"]
        r = self.api("owner", "PATCH", f"/enterprises/{E}/members/{uid}", {"role": "admin"})
        self.assertIn(r.status_code, (403, 409, 422))                              # last owner protected
        r = self.api("owner", "DELETE", f"/enterprises/{E}/members/{uid}")
        self.assertIn(r.status_code, (403, 409, 422))

    # ── C15–C18: credentials, knowledge, inheritance ──
    def test_06_credentials_knowledge_and_inheritance(self):
        E, B = self.ids["ent_a"], self.ids["bot"]
        r = self.api("owner", "POST", f"/credentials/{self.ids['ai_cred']}/test")
        self.assertEqual(r.json(), {"ok": True, "category": None})
        r = self.api("owner", "GET", f"/enterprises/{E}/credentials")
        self.assertNotIn("sk-e2e-", r.text)
        self.assertEqual(self.api("admin", "PATCH", f"/bots/{B}/config", {"changes": {"ai.credential_id": self.ids["ai_cred"]}}).status_code, 200)
        src = self.api("admin", "POST", f"/enterprises/{E}/knowledge/sources", {"name": "Handbook"}).json()["id"]
        r = self.as_("admin").put(f"/api/knowledge/sources/{src}/documents/refunds.txt",
                                  content=b"Refunds are accepted within 14 days of delivery.",
                                  headers={**CSRF, "Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 200, r.text)
        for _ in range(30):
            docs = self.api("admin", "GET", f"/knowledge/sources/{src}/documents").json()["documents"]
            if docs and docs[0]["status"] == "ready":
                break
            time.sleep(1)
        self.assertEqual(docs[0]["status"], "ready")
        r = self.api("owner", "PATCH", f"/enterprises/{E}/policy", {"changes": {"knowledge.allowed_sources": [src]}})
        self.assertEqual(r.status_code, 200, r.text)
        # inheritance: Enterprise default → Bot inherits → Bot overrides → reset
        self.api("owner", "PATCH", f"/enterprises/{E}/config", {"changes": {"behaviour.language": "de"}})
        cfg = self.api("viewer", "GET", f"/bots/{B}/config").json()
        self.assertEqual((cfg["values"]["behaviour.language"], cfg["sources"]["behaviour.language"]["level"]), ("de", "enterprise"))
        self.api("editor", "PATCH", f"/bots/{B}/config", {"changes": {"behaviour.language": "fr"}})
        self.assertEqual(self.api("viewer", "GET", f"/bots/{B}/config").json()["values"]["behaviour.language"], "fr")
        self.api("editor", "PATCH", f"/bots/{B}/config", {"changes": {"behaviour.language": None}})
        self.assertEqual(self.api("viewer", "GET", f"/bots/{B}/config").json()["values"]["behaviour.language"], "de")
        self.assertEqual(self.api("viewer", "PATCH", f"/bots/{B}/config", {"changes": {"behaviour.language": "it"}}).status_code, 403)

    # ── C19: lifecycle ──
    def test_07_lifecycle_blocks_activity_and_resume_restores(self):
        pid = self.ids["public_id"]
        with client() as anon:
            h = {"Origin": "https://shop.example"}
            self.assertEqual(anon.post(f"/api/public/instances/{pid}/chat", json={"message": "hi"}, headers=h).status_code, 200)
            self.assertEqual(self.api("owner", "POST", f"/organisations/{self.ids['org_support']}/suspend", {"reason": "e2e"}).status_code, 200)
            self.assertEqual(anon.post(f"/api/public/instances/{pid}/chat", json={"message": "hi"}, headers=h).status_code, 404)
            inst = self.api("owner", "GET", f"/instances/{self.ids['web']}").json()
            self.assertFalse(inst["available"])
            self.assertEqual(self.api("owner", "POST", f"/organisations/{self.ids['org_support']}/resume", {"reason": "e2e"}).status_code, 200)
            self.assertEqual(anon.post(f"/api/public/instances/{pid}/chat", json={"message": "hi"}, headers=h).status_code, 200)
        u = self.api("owner", "GET", f"/enterprises/{self.ids['ent_a']}/usage").json()
        self.assertGreaterEqual(u["ai_messages"]["settled"], 2)

    # ── C20: deletion ──
    def test_08_bot_deletion_workflow(self):
        b = self.api("admin", "POST", f"/organisations/{self.ids['org_sales']}/bots", {"name": "Doomed"}).json()["id"]
        self.api("admin", "POST", f"/bots/{b}/instances", {"name": "w", "channel": {"type": "webchat"}})
        pv = self.api("admin", "POST", f"/bots/{b}/deletion-preview").json()
        r = self.api("admin", "POST", f"/bots/{b}/delete", {"preview_token": pv["preview_token"], "confirm_name": "doomed"})
        self.assertEqual(r.status_code, 409)                                       # exact typed name
        pv = self.api("admin", "POST", f"/bots/{b}/deletion-preview").json()
        r = self.api("admin", "POST", f"/bots/{b}/delete", {"preview_token": pv["preview_token"], "confirm_name": "Doomed"})
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()["id"]
        for _ in range(30):
            st = self.api("admin", "GET", f"/deletion-jobs/{job}").json()["status"]
            if st == "completed":
                break
            time.sleep(1)
        self.assertEqual(st, "completed")
        self.assertEqual(self.api("admin", "GET", f"/bots/{b}").status_code, 404)

    # ── E: roles, tenants, headers, secrets ──
    def test_09_role_and_tenant_matrix_over_http(self):
        E, O, B, I = self.ids["ent_a"], self.ids["org_support"], self.ids["bot"], self.ids["web"]
        expect = [  # (method, path, body, {who: status})
            ("GET", f"/enterprises/{E}", None, {"owner": 200, "admin": 200, "editor": 200, "viewer": 200, "orgadmin": 404,
                                                 "restricted": 404, "ownerb": 404}),       # Org-level roles: navigation only
            ("GET", f"/enterprises/{E}/organisations", None, {"orgadmin": 200, "restricted": 200, "ownerb": 404}),
            ("PATCH", f"/organisations/{O}", {"name": "Support"}, {"admin": 200, "editor": 200, "viewer": 403,
                                                                    "orgadmin": 200, "restricted": 404, "ownerb": 404}),
            ("GET", f"/bots/{B}", None, {"viewer": 200, "orgadmin": 200, "restricted": 404, "ownerb": 404}),
            ("GET", f"/instances/{I}/conversations", None, {"viewer": 200, "orgadmin": 200, "restricted": 404, "ownerb": 404}),
            ("PATCH", f"/enterprises/{E}/policy", {"changes": {"retention.conversation_days": 365}},
                {"owner": 200, "admin": 200, "editor": 403, "orgadmin": 404, "ownerb": 404}),
            ("GET", f"/organisations/{self.ids['org_sales']}", None, {"restricted": 200, "orgadmin": 404}),
        ]
        for m, path, body, want in expect:
            for who, code in want.items():
                self.assertEqual(self.api(who, m, path, body).status_code, code, (who, m, path))
        # tenant headers never grant authority
        for h in ({"X-Instance-ID": I}, {"X-Enterprise-ID": E}, {"X-Org-ID": O}, {"X-Workspace-ID": E}):
            self.assertEqual(self.api("ownerb", "GET", f"/bots/{B}", headers=h).status_code, 404)
        # non-disclosing 404: unknown id and foreign id look the same
        a = self.api("ownerb", "GET", f"/bots/{B}")
        b = self.api("ownerb", "GET", "/bots/00000000-0000-4000-8000-000000000000")
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        # restricted member sees only their Organisation
        orgs = self.api("restricted", "GET", f"/enterprises/{E}/organisations").json()["organisations"]
        self.assertEqual([o["name"] for o in orgs], ["Sales"])

    def test_10_no_secret_leaks_in_any_response_seen(self):
        secrets_ = ["sk-e2e-" + "k" * 40, self.ids["discord"]["bot_token"], self.ids["discord"]["client_secret"], self.pw]
        for who, path in (("owner", f"/enterprises/{self.ids['ent_a']}/credentials"), ("orgadmin", f"/organisations/{self.ids['org_support']}/credentials"),
                          ("root", f"/platform/enterprises/{self.ids['ent_a']}/discord-application"), ("root", "/platform/audit"),
                          ("owner", f"/enterprises/{self.ids['ent_a']}/audit"), ("owner", f"/bots/{self.ids['bot']}/config")):
            text = self.api(who, "GET", path).text
            for s in secrets_:
                self.assertNotIn(s, text, path)

    # ── E: Discord invariant (D-Q14), UI-independent ──
    def test_11_discord_application_is_superadmin_only(self):
        E, d = self.ids["ent_a"], self.ids["discord"]
        app_path = f"/platform/enterprises/{E}/discord-application"
        r = self.api("root", "PUT", app_path, {"application_id": d["app_id"], "bot_token": d["bot_token"], "client_secret": d["client_secret"]})
        self.assertIn(r.status_code, (200, 201), r.text)
        self.assertNotIn(d["bot_token"], r.text)
        for who in ("owner", "admin", "orgadmin"):
            for m, p, body in (("GET", app_path, None), ("PUT", app_path, {"application_id": d["app_id"]}),
                               ("POST", app_path + "/rotate-token", {"value": d["bot_token"]}),
                               ("POST", app_path + "/rotate-secret", {"value": "x" * 32}), ("POST", app_path + "/disable", None),
                               ("POST", app_path + "/gateway/restart", None), ("POST", app_path + "/avatar", {"image": "data:image/png;base64,AA=="})):
                self.assertEqual(self.api(who, m, p, body).status_code, 404, (who, m, p))
        self.assertEqual(self.api("orgadmin", "GET", f"/enterprises/{E}/discord").json(), {"available": True, "configured": True})
        # Organisation admin connects their own instance with the Enterprise application
        inst = self.api("orgadmin", "POST", f"/bots/{self.ids['bot']}/instances", {"name": "Support Discord", "channel": {"type": "discord"}}).json()
        url = self.api("orgadmin", "POST", f"/instances/{inst['id']}/channel/discord/install-url").json()["url"]
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["client_id"], [d["app_id"]])
        with client() as browserless:
            r = browserless.get(f"/api/hooks/discord/oauth/callback?state={q['state'][0]}&code={d['oauth_code']}&guild_id={d['guild']}")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(urlparse(r.headers["location"]).path, "/oauth/discord/result")
        st = self.api("orgadmin", "GET", f"/instances/{inst['id']}/channel/status").json()
        self.assertTrue(st["connected"])
        r = self.api("orgadmin", "PUT", f"/instances/{inst['id']}/channel/discord/settings",
                     {"listen_mode": "specific_channels", "channel_ids": [d["channel_help"]]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.api("viewer", "POST", f"/instances/{inst['id']}/channel/disconnect").status_code, 403)
        self.assertEqual(self.api("ownerb", "POST", f"/instances/{inst['id']}/channel/disconnect").status_code, 404)
        self.assertEqual(self.api("orgadmin", "POST", f"/instances/{inst['id']}/channel/disconnect").status_code, 200)


    # ── 6C B-ACCT: a new person joins through an e-mailed invitation ──
    def test_12_new_user_registers_from_an_emailed_invitation(self):
        E, email = self.ids["ent_a"], "joiner.12@e2e.test"
        r = self.api("owner", "POST", f"/enterprises/{E}/invitations", {"email": email, "role": "viewer"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertNotIn("token", r.json())
        mail = [m for m in self.outbox() if m["kind"] == "invitation" and m["to"] == email][-1]
        token = mail["text"].split("#token=")[1].split()[0]
        with client() as c:
            body = {"token": token, "name": "Joiner Twelve", "password": "Copper-Meadow-Lantern-2026"}
            self.assertEqual(c.post("/api/invitations/register", json=body).status_code, 403)       # CSRF header required
            r = c.post("/api/invitations/register", json=body, headers=CSRF)
            self.assertEqual(r.status_code, 201, r.text)
            cookie = r.headers.get("set-cookie", "").lower()
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=strict", cookie)
            self.assertEqual(c.get("/api/auth/session").status_code, 200)                         # signed in
            self.assertEqual(c.get(f"/api/enterprises/{E}").status_code, 200)
            self.assertEqual(c.get(f"/api/enterprises/{self.ids['ent_b']}").status_code, 404)        # no other tenant
            self.assertEqual(c.patch(f"/api/enterprises/{E}", json={"name": "x"}, headers=CSRF).status_code, 403)  # viewer
        with client() as c:
            self.assertEqual(c.post("/api/invitations/register", json=body, headers=CSRF).status_code, 404)  # single use
            r = c.post("/api/auth/login", json={"email": email, "password": body["password"]}, headers=CSRF)
            self.assertEqual(r.status_code, 200, r.text)
        # an existing account cannot register again; it signs in and accepts instead
        r = self.api("owner", "POST", f"/enterprises/{E}/invitations", {"email": "viewer@e2e.test", "role": "editor",
                                                                     "scope_type": "organisations",
                                                                     "scope_id": self.ids["org_sales"]})
        self.assertEqual(r.status_code, 201, r.text)
        mail = [m for m in self.outbox() if m["kind"] == "invitation" and m["to"] == "viewer@e2e.test"][-1]
        token2 = mail["text"].split("#token=")[1].split()[0]
        with client() as c:
            r = c.post("/api/invitations/register", json={"token": token2, "name": "V", "password": "Copper-Meadow-Lantern-2026"},
                       headers=CSRF)
            self.assertEqual(r.status_code, 409)
        self.assertEqual(self.api("viewer", "POST", "/invitations/accept", {"token": token2}).status_code, 200)

if __name__ == "__main__":
    unittest.main()
