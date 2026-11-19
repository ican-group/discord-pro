"""Webchat channel (Phase 4B): parity rows W1–W12 plus session, origin,
knowledge-mode, streaming, quota, deletion and error-safety requirements.
Fake/mocked AI providers only."""
import asyncio
import json
import unittest

from tests.unit._api_fixtures import ANTHROPIC_KEY, HAVE_API, World

ORIGIN = "https://shop.example"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MARKER = "UNVALIDATED-MODEL-TEXT-MARKER"



_B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def tampered_token(tok: str) -> str:
    """A token that is guaranteed to differ from ``tok`` in exactly one position.

    Phase 6A gate run 2 failed because the old tamper (replace the last character
    with "A") returned the real token whenever that token already ended in "A"
    (about 1 run in 64). The replacement is chosen from the token alphabet and is
    never the original character, so the result can never be the valid token."""
    if not tok:
        raise ValueError("empty token")
    last = tok[-1]
    repl = next(c for c in _B64URL if c != last)
    out = tok[:-1] + repl
    assert out != tok and len(out) == len(tok)
    return out


class TamperedTokenRegressionTests(unittest.TestCase):
    """Phase 6B regression for the Phase 6A webchat flake: the tampered token must
    differ for every possible final character, deterministically."""

    def test_every_possible_final_character_produces_a_different_token(self):
        for c in _B64URL:
            tok = "x" * 42 + c
            t = tampered_token(tok)
            self.assertNotEqual(t, tok, c)
            self.assertEqual(len(t), len(tok))
            self.assertEqual(t[:-1], tok[:-1])

    def test_the_old_tamper_was_the_flake(self):
        tok = "x" * 42 + "A"
        self.assertEqual(tok[:-1] + "A", tok)          # the Phase 4B tamper: identical token, test accepted it
        self.assertNotEqual(tampered_token(tok), tok)

class FakeProvider:
    name = "anthropic"

    def __init__(self, text="Hello from the bot.", claims=(), error=None, delay=0.0):
        self.text, self.claims, self.error, self.delay = text, claims, error, delay
        self.calls = []

    async def generate(self, request):
        from runtime.interfaces import GenerationResult
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return GenerationResult(claims=tuple(self.claims), text=self.text, input_tokens=10, output_tokens=5)


def sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev = next((l[7:] for l in block.split("\n") if l.startswith("event: ")), None)
        data = next((l[6:] for l in block.split("\n") if l.startswith("data: ")), "{}")
        if ev:
            out.append((ev, json.loads(data)))
    return out


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class WebchatBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()
        self.provider = FakeProvider()
        self.w.S.providers = {"anthropic": self.provider}
        r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                              {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        assert r.status_code == 200, r.text
        self.pid = self.w.iA_web.channel.public_id
        self.P = f"/public/instances/{self.pid}"

    async def pub(self, method, path, body=None, headers=None, origin=ORIGIN):
        import httpx
        h = dict(headers or {})
        if origin:
            h["Origin"] = origin
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            return await c.request(method, "/api" + path, json=body, headers=h)

    async def chat(self, message="Hi", session=None, cmid=None, stream=False, origin=ORIGIN, path=None):
        body = {"message": message}
        headers = {}
        if session:
            body["session_id"] = session[0]
            headers["X-Chat-Session-Token"] = session[1]
        if cmid:
            body["client_message_id"] = cmid
        return await self.pub("POST", (path or self.P) + ("/chat/stream" if stream else "/chat"), body, headers, origin)

    async def new_session(self, path=None):
        r = await self.pub("POST", (path or self.P) + "/sessions")
        assert r.status_code == 201, r.text
        return r.json()["session_id"], r.json()["session_token"]

    async def strict(self, allowed_sources=()):
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"knowledge.minimum_mode": "strict_knowledge_only",
                                           "knowledge.allowed_sources": list(allowed_sources)}})
        assert r.status_code == 200, r.text
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"knowledge.insufficient_fallback": "Please contact our support team."}})


class W1EmbedLoaderTests(WebchatBase):
    async def test_embed_js(self):
        r = await self.pub("GET", "/embed.js", origin=None)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/javascript"))
        js = r.text
        self.assertIn('var API = "https://api.bitbotpro.io";', js)
        for attr in ("accent", "accentLight", "bg", "surface", "panel", "text", "textSecondary", "border",
                     "userBubble", "botBubble", "inputBg", "font", "radius"):
            self.assertIn(f"{attr}:", js.replace(" ", "")[:5000] + js)          # all 13 legacy attributes
        for mode in ('"popup"', '"inline"', '"fullpage"'):
            self.assertIn(mode, js)
        self.assertNotIn("XMLHttpRequest", js)                                  # async config fetch only
        self.assertNotIn("innerHTML", js)
        self.assertIn("encodeURIComponent", js)
        self.assertIn('credentials: "omit"', js)

    async def test_api_origin_is_emitted_as_a_json_literal(self):
        self.w.S.settings.public_api_origin = 'https://evil"</script><script>alert(1)//'
        r = await self.pub("GET", "/embed.js", origin=None)
        self.assertNotIn("</script>", r.text)
        self.assertIn('\\"', r.text)


class W2WidgetPageTests(WebchatBase):
    async def test_widget_page_and_assets(self):
        r = await self.pub("GET", f"/public/widget/{self.pid}", origin=None)
        self.assertEqual(r.status_code, 200)
        csp = r.headers["content-security-policy"]
        for part in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "frame-ancestors *"):
            self.assertIn(part, csp)
        self.assertNotIn("<script>", r.text)                                     # no inline script
        self.assertIn(f'data-public-id="{self.pid}"', r.text)
        for asset, ctype in (("/public/widget.js", "application/javascript"), ("/public/widget.css", "text/css")):
            a = await self.pub("GET", asset, origin=None)
            self.assertEqual(a.status_code, 200)
            self.assertTrue(a.headers["content-type"].startswith(ctype))
        js = (await self.pub("GET", "/public/widget.js", origin=None)).text
        self.assertNotIn("innerHTML", js)
        self.assertIn("textContent", js)
        self.assertIn("sessionStorage", js)                                     # widget session restore
        self.assertIn("localStorage", js)                                       # full-page chat (/chat parity)
        bad = await self.pub("GET", "/public/widget/pk_" + "a" * 30, origin=None)
        self.assertEqual(bad.status_code, 404)
        bad = await self.pub("GET", "/public/widget/<script>", origin=None)
        self.assertEqual(bad.status_code, 404)


class W3BrandingTests(WebchatBase):
    async def test_public_config_has_public_values_only(self):
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner", {"changes": {
            "widget.title": "Support", "widget.welcome_message": "Welcome!", "widget.theme": {"accent": "#112233"},
            "widget.default_open": True, "widget.custom_css_url": "https://cdn.shop.example/w.css",
            "behaviour.instructions": "SECRET-INTERNAL-INSTRUCTIONS"}})
        await self.w.call("PATCH", f"/instances/{self.w.iA_web.id}/config", "owner",
                          {"changes": {"widget.title": "Shop help"}})               # instance override wins
        r = await self.pub("GET", self.P)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["title"], body["welcome_message"], body["theme"], body["default_open"]),
                         ("Shop help", "Welcome!", {"accent": "#112233"}, True))
        self.assertEqual(body["custom_css_url"], "https://cdn.shop.example/w.css")
        text = r.text
        for private in ("SECRET-INTERNAL-INSTRUCTIONS", self.w.eA.id, self.w.oA.id, self.w.bA.id, self.w.iA_web.id,
                        self.w.cA_ai.id, "credential", "enterprise", "organisation"):
            self.assertNotIn(private, text)
        page = await self.pub("GET", f"/public/widget/{self.pid}", origin=None)
        self.assertIn("style-src 'self' https://cdn.shop.example", page.headers["content-security-policy"])
        self.assertIn('href="https://cdn.shop.example/w.css"', page.text)

    async def test_invalid_branding_is_rejected_and_title_defaults_to_bot_name(self):
        for change in ({"widget.custom_css_url": "http://insecure.example/x.css"},
                       {"widget.theme": {"accent": "red;background:url(x)"}}, {"widget.theme": {"unknown": "#fff"}}):
            r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner", {"changes": change})
            self.assertEqual(r.status_code, 422)
        self.assertEqual((await self.pub("GET", self.P)).json()["title"], self.w.bA.name)


class W4LogoTests(WebchatBase):
    async def test_logo_upload_serve_and_delete(self):
        import httpx
        async def put(data, who="editor"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
                return await c.put(f"/api/bots/{self.w.bA.id}/logo", content=data,
                                   headers={**self.w.headers(who), "Content-Type": "image/png"})
        self.assertEqual((await put(PNG, "viewer")).status_code, 403)
        for bad in (b"<svg onload=alert(1)>", b"%PDF-1.7", PNG + b"0" * 300_000):
            self.assertEqual((await put(bad)).status_code, 422)
        r = await put(PNG)
        self.assertEqual((r.status_code, r.json()["content_type"]), (200, "image/png"))
        cfg = (await self.pub("GET", self.P)).json()
        self.assertEqual(cfg["logo_url"], f"/api/public/instances/{self.pid}/logo")
        logo = await self.pub("GET", f"{self.P}/logo")
        self.assertEqual((logo.status_code, logo.content, logo.headers["content-type"]), (200, PNG, "image/png"))
        self.assertEqual(logo.headers["content-security-policy"], "default-src 'none'")
        r = await self.w.call("DELETE", f"/bots/{self.w.bA.id}/logo", "editor")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await self.pub("GET", f"{self.P}/logo")).status_code, 404)
        self.assertIsNone((await self.pub("GET", self.P)).json()["logo_url"])


class W5ChatAndStreamingTests(WebchatBase):
    async def test_chat_json(self):
        r = await self.chat("Hello")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["reply"], "Hello from the bot.")
        self.assertTrue(body["session_token"])                                   # new session, token once
        req = self.provider.calls[0]
        self.assertEqual((req.user_text, req.knowledge_mode, req.api_key.reveal()), ("Hello", "general", ANTHROPIC_KEY))
        self.assertNotIn(ANTHROPIC_KEY, r.text)
        r2 = await self.chat("And?", session=(body["session_id"], body["session_token"]))
        self.assertNotIn("session_token", r2.json())
        self.assertEqual(self.provider.calls[1].history, (("user", "Hello"), ("assistant", "Hello from the bot.")))

    async def test_stream_protocol(self):
        r = await self.chat("Hello", stream=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        events = sse(r.text)
        self.assertEqual(events[0][0], "session")
        self.assertIn("session_token", events[0][1])
        self.assertEqual(events[-1][0], "done")
        self.assertEqual("".join(d["token"] for e, d in events if e == "token"), "Hello from the bot.")
        self.assertEqual(events[-1][1]["full_response"], "Hello from the bot.")


class W6SessionSecurityTests(WebchatBase):
    async def test_history_requires_the_session_token(self):
        sid, tok = await self.new_session()
        await self.chat("first", session=(sid, tok))
        H = f"{self.P}/sessions/{sid}/history"
        ok = await self.pub("GET", H, headers={"X-Chat-Session-Token": tok})
        self.assertEqual([m["role"] for m in ok.json()["messages"]], ["user", "assistant"])
        tampered = tampered_token(tok)            # always a different, invalid token (Phase 6A flake, 1 in 64)
        for headers in ({}, {"X-Chat-Session-Token": "x" * 43}, {"X-Chat-Session-Token": tampered}):
            self.assertEqual((await self.pub("GET", H, headers=headers)).status_code, 404)
        guessed = await self.pub("GET", f"{self.P}/sessions/00000000-0000-4000-8000-000000000000/history",
                                 headers={"X-Chat-Session-Token": tok})
        self.assertEqual(guessed.status_code, 404)

    async def test_sessions_cannot_be_swapped_between_instances_or_tenants(self):
        sid, tok = await self.new_session()
        other_inst = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                                       {"name": "Web 2", "channel": {"type": "webchat"}})
        P2 = f"/public/instances/{other_inst.json()['channel']['public_id']}"
        PB = f"/public/instances/{self.w.iB_web.channel.public_id}"
        for path in (P2, PB):
            r = await self.chat("x", session=(sid, tok), path=path)
            self.assertEqual(r.status_code, 404)
            r = await self.pub("GET", f"{path}/sessions/{sid}/history", headers={"X-Chat-Session-Token": tok})
            self.assertEqual(r.status_code, 404)
        sid2, tok2 = await self.new_session()
        self.assertEqual((await self.chat("x", session=(sid, tok2))).status_code, 404)   # token of another session

    async def test_expired_sessions_are_rejected(self):
        sid, tok = await self.new_session()
        await self.w.store.update("chat_sessions", sid, {"expires_at": "2020-01-01T00:00:00+00:00"})
        self.assertEqual((await self.chat("x", session=(sid, tok))).status_code, 404)

    async def test_session_secrets_are_not_stored_in_clear(self):
        sid, tok = await self.new_session()
        self.assertNotIn(tok, json.dumps(await self.w.snapshot(), default=str))


class W7LimitTests(WebchatBase):
    async def test_message_length_and_per_session_limit(self):
        self.assertEqual((await self.chat("")).status_code, 422)
        self.assertEqual((await self.chat("x" * 4001)).status_code, 422)
        sid, tok = await self.new_session()
        from api.webchat import LIMIT_PER_SESSION
        statuses = [(await self.chat(f"m{i}", session=(sid, tok))).status_code for i in range(LIMIT_PER_SESSION[0] + 1)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(statuses.count(200), LIMIT_PER_SESSION[0])

    async def test_per_ip_chat_limit(self):
        from api.webchat import LIMIT_CHAT_IP
        statuses = [(await self.chat(f"m{i}")).status_code for i in range(LIMIT_CHAT_IP[0] + 1)]
        self.assertEqual(statuses[-1], 429)

    async def test_daily_instance_cap(self):
        self.w.S.settings.webchat_daily_limit_per_instance = 2
        results = [(await self.chat(f"m{i}")).status_code for i in range(3)]
        self.assertEqual(results, [200, 200, 429])

    async def test_monthly_quota_is_never_exceeded_under_concurrency(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 3}})
        self.provider.delay = 0.01
        sessions = [await self.new_session() for _ in range(8)]
        results = await asyncio.gather(*[self.chat("hi", session=s) for s in sessions])
        self.assertEqual(sorted(r.status_code for r in results), [200] * 3 + [429] * 5)
        self.assertEqual(len(self.provider.calls), 3)
        counter = await self.w.store.get("usage_counters", f"{self.w.eA.id}:" + self.w.S.clock()[:7])
        self.assertEqual(counter["count"], 3)

    async def test_end_user_limit_from_configuration(self):
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"limits.end_user_msgs_per_minute": 2}})
        sid, tok = await self.new_session()
        statuses = [(await self.chat(f"m{i}", session=(sid, tok))).status_code for i in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


class W8ConversationTests(WebchatBase):
    async def test_conversations_are_stored_listed_and_approvable(self):
        r = (await self.chat("What are your hours?")).json()
        sid = r["session_id"]
        doc = await self.w.store.get("conversations", sid)
        self.assertEqual((doc["enterprise_id"], doc["organisation_id"], doc["bot_id"], doc["instance_id"]),
                         (self.w.eA.id, self.w.oA.id, self.w.bA.id, self.w.iA_web.id))
        self.assertIn("expires_at_dt", doc)
        self.assertNotIn(ANTHROPIC_KEY, json.dumps(doc, default=str))
        lst = await self.w.call("GET", f"/instances/{self.w.iA_web.id}/conversations", "viewer")
        self.assertEqual([c["session_id"] for c in lst.json()["conversations"]], [sid])
        self.assertEqual((await self.w.call("GET", f"/instances/{self.w.iA_web.id}/conversations",
                                            "owner_b")).status_code, 404)
        detail = await self.w.call("GET", f"/instances/{self.w.iA_web.id}/conversations/{sid}", "viewer")
        reply_id = detail.json()["messages"][1]["id"]
        self.assertEqual((await self.w.call("POST", f"/instances/{self.w.iA_web.id}/conversations/{sid}/approve",
                                            "viewer", {"message_id": reply_id})).status_code, 403)
        ok = await self.w.call("POST", f"/instances/{self.w.iA_web.id}/conversations/{sid}/approve", "editor",
                               {"message_id": reply_id})
        self.assertEqual(ok.status_code, 200, ok.text)
        cfg = (await self.w.call("GET", f"/bots/{self.w.bA.id}/config", "owner")).json()
        self.assertEqual(cfg["values"]["behaviour.tone_examples"],
                         [{"user": "What are your hours?", "bot": "Hello from the bot."}])

    async def test_retention_policy_sets_expiry(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"retention.conversation_days": 30}})
        sid = (await self.chat("x")).json()["session_id"]
        doc = await self.w.store.get("conversations", sid)
        from datetime import datetime
        days = (doc["expires_at_dt"] - datetime.fromisoformat(doc["updated_at"])).days
        self.assertEqual(days, 30)


class W9EmbedSnippetTests(WebchatBase):
    async def test_embed_snippet(self):
        r = await self.w.call("GET", f"/instances/{self.w.iA_web.id}/embed-snippet", "viewer")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'data-instance="{self.pid}"', r.json()["script"])
        self.assertEqual((await self.w.call("GET", f"/instances/{self.w.iA_tg.id}/embed-snippet",
                                            "viewer")).status_code, 404)


class W10AvailabilityTests(WebchatBase):
    async def check_all_404(self):
        sid, tok = ("00000000-0000-4000-8000-000000000000", "x" * 43)
        checks = [await self.pub("GET", self.P), await self.pub("GET", f"{self.P}/logo"),
                  await self.pub("POST", f"{self.P}/sessions"), await self.chat("hi"), await self.chat("hi", stream=True),
                  await self.pub("GET", f"{self.P}/sessions/{sid}/history", headers={"X-Chat-Session-Token": tok}),
                  await self.pub("GET", f"/public/widget/{self.pid}", origin=None),
                  await self.pub("OPTIONS", f"{self.P}/chat")]
        for r in checks:
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "Not found."}))
        self.assertEqual(self.provider.calls, [])

    async def test_unknown_instance(self):
        self.P = "/public/instances/pk_" + "Z" * 30
        self.pid = "pk_" + "Z" * 30
        await self.check_all_404()

    async def test_suspended_archived_deleted_or_policy_blocked(self):
        for setup in ("org_suspended", "bot_archived", "instance_suspended", "public_off", "channel_off", "deleted"):
            with self.subTest(setup=setup):
                await self.asyncSetUp()
                if setup == "org_suspended":
                    await self.w.call("POST", f"/organisations/{self.w.oA.id}/suspend", "owner", {})
                elif setup == "bot_archived":
                    await self.w.call("POST", f"/bots/{self.w.bA.id}/archive", "owner", {})
                elif setup == "instance_suspended":
                    await self.w.call("POST", f"/instances/{self.w.iA_web.id}/suspend", "owner", {})
                elif setup == "public_off":
                    await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                                      {"changes": {"webchat.public_access_allowed": False}})
                elif setup == "channel_off":
                    await self.w.call("PATCH", f"/organisations/{self.w.oA.id}/policy", "owner",
                                      {"changes": {"channels.allowed": ["telegram"]}})
                else:
                    pv = (await self.w.call("POST", f"/instances/{self.w.iA_web.id}/deletion-preview", "owner")).json()
                    await self.w.call("POST", f"/instances/{self.w.iA_web.id}/delete", "owner",
                                      {"preview_token": pv["preview_token"], "confirm_name": self.w.iA_web.name})
                await self.check_all_404()


class W11OriginTests(WebchatBase):
    async def restrict(self, instance_origins, policy_origins=None):
        await self.w.call("PATCH", f"/instances/{self.w.iA_web.id}", "owner",
                          {"channel": {"allowed_origins": instance_origins}})
        if policy_origins is not None:
            await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"webchat.allowed_embed_origins": policy_origins}})

    async def test_unrestricted_instance_echoes_the_origin_without_credentials(self):
        r = await self.pub("GET", self.P, origin="https://any.example")
        self.assertEqual(r.headers["access-control-allow-origin"], "https://any.example")
        self.assertNotIn("access-control-allow-credentials", r.headers)
        self.assertEqual(r.headers["vary"], "Origin")
        self.assertNotEqual(r.headers["access-control-allow-origin"], "*")

    async def test_allowed_and_disallowed_origins(self):
        await self.restrict(["https://shop.example"])
        ok = await self.chat("hi", origin="https://shop.example")
        self.assertEqual((ok.status_code, ok.headers["access-control-allow-origin"]), (200, "https://shop.example"))
        for bad in ("https://evil.example", "http://shop.example", "null", "https://shop.example.evil.com"):
            r = await self.chat("hi", origin=bad)
            self.assertEqual(r.status_code, 404, bad)
            self.assertNotIn("access-control-allow-origin", r.headers)
        self.assertEqual((await self.chat("hi", origin=None)).status_code, 404)      # unsafe without Origin
        self.assertEqual((await self.pub("GET", self.P, origin=None)).status_code, 200)   # plain GET allowed
        own = await self.chat("hi", origin="https://api.bitbotpro.io")                 # the widget itself
        self.assertEqual(own.status_code, 200)
        page = await self.pub("GET", f"/public/widget/{self.pid}", origin=None)
        self.assertIn("frame-ancestors 'self' https://shop.example", page.headers["content-security-policy"])

    async def test_policy_intersection_limits_instance_origins(self):
        await self.restrict(["https://shop.example", "https://blog.example"], ["https://shop.example"])
        self.assertEqual((await self.chat("hi", origin="https://shop.example")).status_code, 200)
        self.assertEqual((await self.chat("hi", origin="https://blog.example")).status_code, 404)
        await self.restrict([], ["https://shop.example"])                              # instance unrestricted
        self.assertEqual((await self.chat("hi", origin="https://other.example")).status_code, 404)

    async def test_preflight(self):
        await self.restrict(["https://shop.example"])
        ok = await self.pub("OPTIONS", f"{self.P}/chat/stream", headers={"Access-Control-Request-Method": "POST"},
                            origin="https://shop.example")
        self.assertEqual(ok.status_code, 204)
        self.assertEqual(ok.headers["access-control-allow-origin"], "https://shop.example")
        self.assertIn("x-chat-session-token", ok.headers["access-control-allow-headers"])
        bad = await self.pub("OPTIONS", f"{self.P}/chat", origin="https://evil.example")
        self.assertEqual(bad.status_code, 404)


class KnowledgeModeTests(WebchatBase):
    async def test_general_mode_without_a_knowledge_base(self):
        r = await self.chat("hi")
        self.assertEqual(r.json()["reply"], "Hello from the bot.")

    async def test_strict_without_approved_evidence_or_retrieval_fails_closed(self):
        self.provider.text = MARKER
        await self.strict(allowed_sources=[])
        for stream in (False, True):
            r = await self.chat("What is the refund policy?", stream=stream)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(MARKER, r.text)
            self.assertIn("Please contact our support team.", r.text)
        self.assertEqual(self.provider.calls, [])                               # no AI call at all
        # an approved source without any matching document: no evidence, no AI call
        await self.w.store.insert("knowledge_sources", {
            "id": "11111111-1111-4111-8111-111111111111", "enterprise_id": self.w.eA.id, "organisation_id": None,
            "name": "Empty", "kind": "document", "status": "active", "acl_groups": [], "version": 1})
        await self.strict(allowed_sources=["11111111-1111-4111-8111-111111111111"])
        r = await self.chat("x", stream=True)
        self.assertNotIn(MARKER, r.text)
        self.assertEqual(self.provider.calls, [])

    async def test_strict_streaming_never_emits_unvalidated_output(self):
        from core.knowledge import Evidence, KnowledgeSource, RetrievalResult, RetrievalStatus, SourceKind
        src = KnowledgeSource("22222222-2222-4222-8222-222222222222", self.w.eA.id, self.w.oA.id, None,
                              SourceKind.DOCUMENT)
        await self.w.store.insert("knowledge_sources", {"id": src.id, "enterprise_id": self.w.eA.id,
                                                        "organisation_id": self.w.oA.id, "name": "S",
                                                        "kind": "document", "status": "active", "acl_groups": [],
                                                        "version": 1})
        await self.strict(allowed_sources=[src.id])
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"knowledge.approved_sources": [src.id]}})

        class Catalog:
            async def sources(self, eid):
                return [src]

        class Retriever:
            async def retrieve(self, q, *, chain, plan, authorised):
                return RetrievalResult(RetrievalStatus.OK, (Evidence("ev-1", src.id, src.enterprise_id, 0.95,
                                                                     "Refunds take 14 days."),))
        self.w.S.catalog = Catalog()
        self.w.S.retriever = Retriever()
        if True:
            self.provider.text = MARKER                 # free text from the model: not deliverable in strict mode
            r = await self.chat("refunds?", stream=True)
            self.assertNotIn(MARKER, r.text)
            self.assertIn("Please contact our support team.", r.text)
            from core.knowledge import Claim
            self.provider.text, self.provider.claims = "", (Claim("Refunds take 14 days.", frozenset({"ev-1"}),
                                                                  quote="Refunds take 14 days"),)
            r = await self.chat("refunds?", stream=True)
            events = sse(r.text)
            self.assertEqual(events[-1][1]["full_response"], "Refunds take 14 days.")
            self.assertEqual(events[-1][1]["citations"], [src.id])


class ProviderAndCredentialTests(WebchatBase):
    async def test_provider_failures_are_safe(self):
        from runtime.errors import ProviderAuthError, ProviderRateLimited, ProviderUnavailable
        # definite provider failures: nothing billed, retryable 503; auth failure: admin issue + fallback;
        # an unexpected failure cannot prove nothing was generated: uncertain (409, never regenerated)
        for err, status in ((ProviderUnavailable(), 503), (ProviderAuthError(), 200), (ProviderRateLimited(), 503),
                            (RuntimeError(f"500 from upstream key={ANTHROPIC_KEY}"), 409)):
            self.provider.error = err
            for stream in (False, True):
                r = await self.chat("hi", stream=stream)
                self.assertEqual(r.status_code, status if not stream else 200)
                self.assertNotIn(ANTHROPIC_KEY, r.text)
                self.assertNotIn("upstream", r.text)
                self.assertNotIn("Traceback", r.text)

    async def test_real_adapter_end_to_end_with_mocked_http(self):
        import httpx
        from runtime.providers import AnthropicProvider
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Mocked Anthropic reply."}],
                                             "usage": {"input_tokens": 3, "output_tokens": 4}})
        self.w.S.providers = {"anthropic": AnthropicProvider(transport=httpx.MockTransport(handler))}
        r = await self.chat("hi")
        self.assertEqual(r.json()["reply"], "Mocked Anthropic reply.")
        self.assertEqual(seen[0].headers["x-api-key"], ANTHROPIC_KEY)
        events = await self.w.store.find("usage_events", {"kind": "llm_call"})
        self.assertEqual((events[0]["details"]["input_tokens"], events[0]["details"]["output_tokens"]), (3, 4))

    async def test_credential_scope_and_revocation_fail_closed(self):
        other_bot = (await self.w.call("POST", f"/organisations/{self.w.oA.id}/bots", "owner", {"name": "X"})).json()
        await self.w.call("PUT", f"/credentials/{self.w.cA_ai.id}/scope", "owner",
                          {"usable_by": [{"level": "bot", "id": other_bot["id"]}]})
        r = await self.chat("hi")
        self.assertEqual(r.json()["reply"], "Sorry, I can't help with that right now.")
        self.assertEqual(self.provider.calls, [])
        await self.w.call("PUT", f"/credentials/{self.w.cA_ai.id}/scope", "owner", {"usable_by": []})
        await self.w.call("POST", f"/credentials/{self.w.cA_ai.id}/revoke", "owner")
        r = await self.chat("hi")
        self.assertEqual(r.json()["reply"], "Sorry, I can't help with that right now.")
        self.assertEqual(self.provider.calls, [])


class IdempotencyAndDisconnectTests(WebchatBase):
    async def test_retries_replay_without_double_billing(self):
        sid, tok = await self.new_session()
        cmid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        first = await self.chat("hi", session=(sid, tok), cmid=cmid)
        again = await self.chat("hi", session=(sid, tok), cmid=cmid, stream=True)
        self.assertEqual(first.json()["message_id"], sse(again.text)[-1][1]["message_id"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len((await self.w.store.find("usage_events", {"kind": "llm_call"}))), 1)
        hist = await self.w.store.get("conversations", sid)
        self.assertEqual(len(hist["messages"]), 2)

    async def test_concurrent_duplicates_run_once(self):
        sid, tok = await self.new_session()
        cmid = "9b2a5d7e-1c3f-4e8a-9d6b-7f0e1a2b3c4d"
        self.provider.delay = 0.05
        results = await asyncio.gather(*[self.chat("hi", session=(sid, tok), cmid=cmid) for _ in range(4)])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(all(r.status_code in (200, 409) for r in results))
        self.assertGreaterEqual(sum(1 for r in results if r.status_code == 200), 1)

    async def test_client_disconnect_does_not_lose_the_answer(self):
        import httpx
        sid, tok = await self.new_session()
        cmid = "5c0e7b8a-2d4f-4b6e-8a1c-3e5f7a9b0c2d"
        self.provider.delay = 0.05
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            async with c.stream("POST", f"/api{self.P}/chat/stream", headers={"Origin": ORIGIN, "X-Chat-Session-Token": tok},
                                json={"message": "hi", "session_id": sid, "client_message_id": cmid}) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("event: session"):
                        break                                                   # client goes away
        for _ in range(50):
            rec = await self.w.store.get("chat_requests", f"{sid}:{cmid}")
            if rec and rec["status"] == "done":
                break
            await asyncio.sleep(0.02)
        r = await self.chat("hi", session=(sid, tok), cmid=cmid)               # reconnect: replay
        self.assertEqual((r.status_code, r.json()["replayed"]), (200, True))
        self.assertEqual(len(self.provider.calls), 1)


class DeletionTests(WebchatBase):
    async def test_instance_deletion_removes_conversations_and_sessions(self):
        sid = (await self.chat("hi")).json()["session_id"]
        other = (await self.chat("hi", path=f"/public/instances/{self.w.iB_web.channel.public_id}"))
        pv = (await self.w.call("POST", f"/instances/{self.w.iA_web.id}/deletion-preview", "owner")).json()
        self.assertEqual(pv["counts"]["conversations"], 1)
        self.assertEqual(pv["counts"]["chat_sessions"], 1)
        r = await self.w.call("POST", f"/instances/{self.w.iA_web.id}/delete", "owner",
                              {"preview_token": pv["preview_token"], "confirm_name": self.w.iA_web.name})
        self.assertEqual(r.status_code, 202)
        for col in ("conversations", "chat_sessions", "chat_requests"):
            self.assertEqual(await self.w.store.count(col, {"instance_id": self.w.iA_web.id}), 0, col)
        self.assertIsNone(await self.w.store.get("conversations", sid))
        self.assertEqual(await self.w.store.count("conversations", {"enterprise_id": self.w.eB.id}),
                         1 if other.status_code == 200 else 0)


if __name__ == "__main__":
    unittest.main()
