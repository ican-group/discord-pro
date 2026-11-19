"""Shared fixtures for Spec 012 API tests (in-memory store by default; the
real-MongoDB integration tests reuse :class:`World` with a ``MongoStore``)."""
import copy
import importlib.util

from tests.unit._support import BACKEND_DIR  # noqa: F401  (adds backend/ to sys.path)

HAVE_API = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "httpx", "pydantic", "jwt"))

TELEGRAM_TOKEN_A = "123456789:" + "A" * 35
TELEGRAM_TOKEN_B = "987654321:" + "B" * 35
ANTHROPIC_KEY = "not-a-real-anthropic-key-" + "k" * 30
GUILD_A = "111111111111111111"
FERNET_KEY = "c3BlYzAxMi10ZXN0LW9ubHktZmVybmV0LWtleS0zMmI="   # test-only (base64 of a fixed 32-byte string)
JWT_SECRET = "test-only-jwt-secret-" + "0" * 24


class FakeTelegram:
    """Fake Telegram Bot API (httpx MockTransport handler). Records method calls
    by bot id (never the token). ``fail`` maps method -> list of responses to
    return first (status, json)."""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.next_message_id = 1000
        self.intercept = None                 # optional callable(request) -> response | raises | None

    def transport(self):
        import httpx
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        import httpx
        import json as _json
        if self.intercept is not None:
            hooked = self.intercept(request)
            if hooked is not None:
                return hooked
        path = request.url.path                                  # /bot<token>/<method>
        _, token, method = path.split("/", 2)
        bot_id = token[3:].split(":", 1)[0]
        payload = _json.loads(request.content or b"{}")
        self.calls.append((bot_id, method, payload))
        queued = self.fail.get(method)
        if queued:
            status, body = queued.pop(0)
            return httpx.Response(status, json=body)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": int(bot_id), "is_bot": True,
                                                                     "username": f"bot{bot_id}"}})
        if method == "sendMessage":
            self.next_message_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def methods(self, name=None):
        return [c for c in self.calls if name is None or c[1] == name]


DISCORD_TOKEN_A = "A" * 24 + ".AAAAAA." + "a" * 27
DISCORD_TOKEN_A2 = "D" * 24 + ".DDDDDD." + "d" * 27       # rotated token of application A
DISCORD_TOKEN_B = "B" * 24 + ".BBBBBB." + "b" * 27
DISCORD_SECRET_A = "client-secret-a-" + "s" * 16
DISCORD_SECRET_B = "client-secret-b-" + "t" * 16
APP_A, APP_B = "910000000000000001", "920000000000000002"
BOT_USER_A, BOT_USER_B = "930000000000000001", "940000000000000002"
CH_A1, CH_A2, CH_A3, VOICE_A = "222222222222222221", "222222222222222222", "222222222222222223", "222222222222222229"
STAFF_ROLE_A = "333333333333333331"


class FakeDiscord:
    """Fake discord.com REST API (httpx MockTransport). Records calls by
    application id (never the token). Enforced nonces are de-duplicated like
    Discord does."""

    def __init__(self):
        self.tokens = {DISCORD_TOKEN_A: APP_A, DISCORD_TOKEN_A2: APP_A, DISCORD_TOKEN_B: APP_B}
        self.bot_users = {APP_A: BOT_USER_A, APP_B: BOT_USER_B}
        self.bot_guilds = {APP_A: {GUILD_A}, APP_B: set()}
        self.channels = {GUILD_A: [{"id": CH_A1, "name": "general", "type": 0}, {"id": CH_A2, "name": "help", "type": 0},
                                   {"id": CH_A3, "name": "news", "type": 5}, {"id": VOICE_A, "name": "voice", "type": 2}]}
        self.roles = {GUILD_A: [{"id": STAFF_ROLE_A, "name": "Support Team"}, {"id": "333333333333333332", "name": "Member"}]}
        self.members = {GUILD_A: {"500000000000000001", "500000000000000002"}}
        self.codes = {}                  # oauth code -> list of guild entries for that user
        self.calls = []
        self.messages = []
        self.nonces = {}
        self.fail = {}                   # (method, path-suffix) -> list of (status, json)
        self.intercept = None
        self.next_id = 700000000000000000

    def transport(self):
        import httpx
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        import httpx
        import json as _json
        if self.intercept is not None:
            hooked = self.intercept(request)
            if hooked is not None:
                return hooked
        path = request.url.path.split("/api/v10", 1)[1]
        auth = request.headers.get("authorization", "")
        method = request.method
        body = request.content or b""
        if path == "/oauth2/token":
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            self.calls.append((form.get("client_id"), method, path, {"code": form.get("code")}))
            if form.get("code") not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "user-" + form["code"], "token_type": "Bearer"})
        if auth.startswith("Bearer "):
            code = auth[len("Bearer user-"):]
            self.calls.append(("user", method, path, None))
            if path == "/users/@me/guilds" and code in self.codes:
                return httpx.Response(200, json=self.codes[code])
            return httpx.Response(401, json={"message": "401"})
        app = self.tokens.get(auth[len("Bot "):]) if auth.startswith("Bot ") else None
        payload = _json.loads(body) if body else None
        self.calls.append((app, method, path, payload))
        if app is None:
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        for (m, suffix), queued in list(self.fail.items()):
            if m == method and path.endswith(suffix) and queued:
                status, data = queued.pop(0)
                return httpx.Response(status, json=data)
        parts = path.strip("/").split("/")
        if path == "/oauth2/applications/@me":
            return httpx.Response(200, json={"id": app, "name": f"App {app}"})
        if path == "/users/@me" and method == "GET":
            return httpx.Response(200, json={"id": self.bot_users[app], "username": f"bot{app[-1]}"})
        if path == "/users/@me" and method == "PATCH":
            return httpx.Response(200, json={"id": self.bot_users[app]})
        if parts[0] == "guilds":
            g = parts[1]
            if g not in self.bot_guilds.get(app, set()):
                return httpx.Response(404, json={"message": "Unknown Guild"})
            if len(parts) == 2:
                return httpx.Response(200, json={"id": g, "name": "Guild"})
            if parts[2] == "channels":
                return httpx.Response(200, json=self.channels.get(g, []))
            if parts[2] == "roles":
                return httpx.Response(200, json=self.roles.get(g, []))
            if parts[2] == "members" and parts[3] == "@me":
                return httpx.Response(200, json={"nick": (payload or {}).get("nick")})
            if parts[2] == "members":
                ok = parts[3] in self.members.get(g, set())
                return httpx.Response(200 if ok else 404, json={"user": {"id": parts[3]}} if ok else {"message": "x"})
        if path.startswith("/users/@me/guilds/") and method == "DELETE":
            g = parts[-1]
            if g not in self.bot_guilds.get(app, set()):
                return httpx.Response(404, json={"message": "Unknown Guild"})
            self.bot_guilds[app].discard(g)
            return httpx.Response(204)
        if parts[0] == "channels" and parts[-1] == "typing":
            return httpx.Response(204)
        if parts[0] == "channels" and parts[-1] == "messages":
            nonce = (payload or {}).get("nonce")
            if (payload or {}).get("enforce_nonce") and (app, nonce) in self.nonces:
                return httpx.Response(200, json={"id": self.nonces[(app, nonce)]})
            self.next_id += 1
            mid = str(self.next_id)
            self.nonces[(app, nonce)] = mid
            self.messages.append({"app": app, "channel_id": parts[1], "id": mid, **(payload or {})})
            return httpx.Response(200, json={"id": mid})
        return httpx.Response(404, json={"message": "Unknown"})


ZD_SUB = "acme"
ZD_HOST = "acme.zendesk.com"
ZD_IP = "104.16.0.10"                        # a public address (the outbound guard refuses private ones)
ZD_API_TOKEN = "zdtoken" + "Z" * 33
ZD_CLIENT_SECRET = "zendesk-client-secret-" + "q" * 20


class FakeZendesk:
    """Fake Zendesk API behind the real outbound guard. Requests arrive at the
    *pinned IP* with the real host in the Host header; the fake records
    (host, method, path, body) and never the credentials."""

    def __init__(self):
        self.dns = {ZD_HOST: [ZD_IP], "other.zendesk.com": [ZD_IP]}
        self.resolutions = []
        self.calls = []
        self.fail = {}                      # (method, path suffix) -> [(status, json)]
        self.intercept = None
        self.bearer = set()                 # valid OAuth access tokens
        self.basic = {("agent@acme.example", ZD_API_TOKEN)}
        self.codes = {}                     # oauth code -> (access, refresh, me_host, role)
        self.revoked = []
        self.webhooks = {}
        self.audit_id = 5000
        self.tickets = {}
        self.comments = {}
        self.add_ticket(101, subject="Refund question", description="How do I get a refund?", group_id=11,
                        tags=["billing"])

    def add_ticket(self, tid, **fields):
        self.tickets[str(tid)] = {"id": int(tid), "subject": fields.get("subject", "Help"),
                                  "description": fields.get("description", "Please help"),
                                  "requester_id": 900, "group_id": fields.get("group_id"),
                                  "tags": fields.get("tags", []), "updated_at": "2026-11-18T10:00:00Z"}
        self.comments[str(tid)] = [{"id": 1, "author_id": 900, "public": True,
                                    "plain_body": fields.get("description", "Please help")}]

    def bump(self, tid):
        n = len(self.comments[str(tid)])
        self.tickets[str(tid)]["updated_at"] = f"2026-11-18T10:{n:02d}:00Z"

    async def resolve(self, host, port):
        self.resolutions.append(host)
        return list(self.dns.get(host, []))

    def transport(self):
        import httpx
        return httpx.MockTransport(self.handle)

    def public_posts(self):
        return [c for c in self.calls if c[1] == "PUT" and (c[3] or {}).get("ticket", {}).get("comment", {}).get("public")]

    def notes(self):
        return [c for c in self.calls if c[1] == "PUT" and not (c[3] or {}).get("ticket", {}).get("comment", {}).get("public")]

    def handle(self, request):
        import base64 as _b64
        import json as _json
        import httpx
        if self.intercept is not None:
            hooked = self.intercept(request)
            if hooked is not None:
                return hooked
        host = request.headers.get("host")
        path = request.url.path
        method = request.method
        try:
            body = _json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        self.calls.append((host, method, path, body, request.url.host))
        for (m, suffix), queued in list(self.fail.items()):
            if m == method and path.endswith(suffix) and queued:
                status, data = queued.pop(0)
                return httpx.Response(status, json=data)
        if path == "/oauth/tokens":
            if body.get("grant_type") == "authorization_code":
                entry = self.codes.get(body.get("code"))
                if not entry or not body.get("code_verifier"):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                access, refresh = entry[0], entry[1]
            else:
                access, refresh = "refreshed-" + body.get("refresh_token", "")[:20], "r2-" + body.get("refresh_token", "")[:20]
            self.bearer.add(access)
            return httpx.Response(200, json={"access_token": access, "refresh_token": refresh, "expires_in": 7200})
        auth = request.headers.get("authorization", "")
        ok = False
        who = None
        if auth.startswith("Bearer "):
            ok = auth[7:] in self.bearer
            who = auth[7:]
        elif auth.startswith("Basic "):
            email, _, token = _b64.b64decode(auth[6:]).decode().partition("/token:")
            ok = (email, token) in self.basic
        if not ok:
            return httpx.Response(401, json={"error": "Couldn't authenticate you"})
        if path == "/api/v2/users/me.json":
            entry = next((v for v in self.codes.values() if v[0] == who), None)
            me_host, role = (entry[2], entry[3]) if entry else (host, "admin")
            return httpx.Response(200, json={"user": {"id": 77, "role": role,
                                                      "url": f"https://{me_host}/api/v2/users/77.json"}})
        if path == "/api/v2/oauth/tokens/current.json" and method == "DELETE":
            self.bearer.discard(who)
            self.revoked.append(who)
            return httpx.Response(204)
        if path.startswith("/api/v2/webhooks"):
            parts = path.strip("/").split("/")
            if method == "POST" and len(parts) == 3:
                hid = f"01HW{len(self.webhooks) + 1:04d}"
                self.webhooks[hid] = {"endpoint": body["webhook"]["endpoint"], "secret": "zd-signing-" + "s" * 30}
                return httpx.Response(201, json={"webhook": {"id": hid}})
            hid = parts[3]
            if hid not in self.webhooks:
                return httpx.Response(404, json={"error": "NotFound"})
            if len(parts) == 5 and parts[4] == "signing_secret":
                return httpx.Response(200, json={"signing_secret": {"secret": self.webhooks[hid]["secret"],
                                                                    "algorithm": "SHA256"}})
            if method == "GET" and len(parts) == 4:
                return httpx.Response(200, json={"webhook": {"id": hid, "endpoint": self.webhooks[hid]["endpoint"]}})
            if method == "DELETE":
                del self.webhooks[hid]
                return httpx.Response(204)
            if method == "PATCH":
                self.webhooks[hid]["endpoint"] = body["webhook"]["endpoint"]
                return httpx.Response(200, json={"webhook": {"id": hid}})
        import re as _re
        m = _re.fullmatch(r"/api/v2/tickets/(\d+)(/comments)?\.json", path)
        if m:
            tid = m.group(1)
            if tid not in self.tickets:
                return httpx.Response(404, json={"error": "RecordNotFound"})
            if m.group(2):
                return httpx.Response(200, json={"comments": self.comments[tid]})
            if method == "GET":
                return httpx.Response(200, json={"ticket": self.tickets[tid]})
            if method == "PUT":
                t = body["ticket"]
                if t.get("safe_update") and t.get("updated_stamp") != self.tickets[tid]["updated_at"]:
                    return httpx.Response(409, json={"error": "UpdateConflict"})
                c = t["comment"]
                self.comments[tid].append({"id": len(self.comments[tid]) + 1, "author_id": 77, "public": c["public"],
                                           "plain_body": c["body"]})
                self.tickets[tid]["tags"] = sorted(set(self.tickets[tid]["tags"]) | set(t.get("additional_tags") or []))
                self.bump(tid)
                self.audit_id += 1
                return httpx.Response(200, json={"ticket": self.tickets[tid], "audit": {"id": self.audit_id}})
        return httpx.Response(404, json={"error": "Unknown"})


class World:
    """Two Enterprises (A and B) with every role, objects and credentials."""

    ROLES = ("viewer", "editor", "admin", "owner")

    def __init__(self, store=None):
        from core.hierarchy import InMemoryStore
        self.store = store if store is not None else InMemoryStore()

    async def build(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 10     # quiet asyncio debug noise in tests
        from api.app import create_app
        from api.common import Services, Settings
        from core import memberships as ms
        from core.authz import Actor, Role
        from core.credentials import (Provider, credential_to_doc, new_credential, telegram_bot_id)
        from core.hierarchy import Level, new_id
        from core.identity import USERS, create_user, ensure_superadmin_guard, get_user

        S = self.S = Services(self.store, Settings(jwt_secret=JWT_SECRET, encryption_key=FERNET_KEY))
        self.app = create_app(S)
        self.telegram = FakeTelegram()
        S.telegram_transport = self.telegram.transport()     # never the live Telegram API in tests
        self.discord = FakeDiscord()
        S.discord_transport = self.discord.transport()       # never the live Discord API in tests
        self.zendesk = FakeZendesk()
        S.zendesk_transport = self.zendesk.transport()       # never live Zendesk in tests
        S.zendesk_resolver = self.zendesk.resolve             # fake DNS behind the real outbound guard
        now = S.clock()
        self.users = {}
        for name in ("root", "owner", "admin", "editor", "viewer", "member2", "org_admin", "bot_editor",
                     "owner_b", "outsider"):
            u = await create_user(self.store, email=f"{name}@example.test", name=name, now=now, email_verified=True)
            self.users[name] = u
        await self.store.update(USERS, self.users["root"].id, {"is_platform_superadmin": True})
        await ensure_superadmin_guard(self.store)
        for k, u in list(self.users.items()):
            self.users[k] = await get_user(self.store, u.id)
        self._tokens = {}
        root = self.users["root"].actor()
        H = S.hierarchy

        async def tree(prefix, owner_key):
            ent = await H.create_enterprise(name=f"Ent {prefix}", slug=f"ent-{prefix.lower()}-{new_id()[:6]}",
                                            actor_id=root.user_id)
            org = await H.create_organisation(enterprise_id=ent.id, name=f"Org {prefix}", actor_id=root.user_id)
            bot = await H.create_bot(organisation_id=org.id, name=f"Bot {prefix}", actor_id=root.user_id)
            echain = await H.load_chain(Level.ENTERPRISE, ent.id)
            await ms.mutate(self.store, ent.id, ms.op_grant(root, echain, user_id=self.users[owner_key].id,
                                                            role=Role.OWNER, now=now, bypass_authority=True))
            return ent, org, bot, echain

        self.eA, self.oA, self.bA, echainA = await tree("A", "owner")
        self.eB, self.oB, self.bB, echainB = await tree("B", "owner_b")
        for key, role in (("admin", Role.ADMIN), ("editor", Role.EDITOR), ("viewer", Role.VIEWER),
                          ("member2", Role.VIEWER)):
            await ms.mutate(self.store, self.eA.id, ms.op_grant(root, echainA, user_id=self.users[key].id,
                                                                role=role, now=now, bypass_authority=True))
        ochain = await H.load_chain(Level.ORGANISATION, self.oA.id)
        bchain = await H.load_chain(Level.BOT, self.bA.id)
        await ms.mutate(self.store, self.eA.id, ms.op_grant(root, ochain, user_id=self.users["org_admin"].id,
                                                            role=Role.ADMIN, now=now, bypass_authority=True,
                                                            admit=True))
        await ms.mutate(self.store, self.eA.id, ms.op_grant(root, bchain, user_id=self.users["bot_editor"].id,
                                                            role=Role.EDITOR, now=now, bypass_authority=True,
                                                            admit=True))

        async def cred(chain, provider, secret, name):
            meta = {"telegram_bot_id": telegram_bot_id(secret)} if provider is Provider.TELEGRAM_BOT else {}
            c = new_credential(credential_id=new_id(), chain=chain, provider=provider, name=name, secret=secret,
                               box=S.box, actor_id=root.user_id, now=now, metadata=meta)
            await self.store.insert("credentials", credential_to_doc(c, 1))
            return c

        self.cA_tg = await cred(ochain, Provider.TELEGRAM_BOT, TELEGRAM_TOKEN_A, "Telegram A")
        self.cA_ai = await cred(echainA, Provider.ANTHROPIC, ANTHROPIC_KEY, "Anthropic A")
        self.cB_tg = await cred(await H.load_chain(Level.ORGANISATION, self.oB.id), Provider.TELEGRAM_BOT,
                                TELEGRAM_TOKEN_B, "Telegram B")
        self.iA_web = await H.create_instance(bot_id=self.bA.id, name="Web A", channel={"type": "webchat"},
                                              actor_id=root.user_id)
        self.iA_tg = await H.create_instance(bot_id=self.bA.id, name="TG A",
                                             channel={"type": "telegram", "credential_id": self.cA_tg.id},
                                             actor_id=root.user_id,
                                             telegram_bot_id=self.cA_tg.metadata["telegram_bot_id"])
        self.iA_dc = await H.create_instance(bot_id=self.bA.id, name="Discord A", channel={"type": "discord"},
                                             actor_id=root.user_id)
        for ent, app_id, tok, sec in ((self.eA, APP_A, DISCORD_TOKEN_A, DISCORD_SECRET_A),
                                      (self.eB, APP_B, DISCORD_TOKEN_B, DISCORD_SECRET_B)):
            from runtime.discord import DEFAULT_INTENTS, DEFAULT_PERMISSIONS, fingerprint
            await self.store.insert("discord_applications", {
                "id": ent.id, "enterprise_id": ent.id, "application_id": app_id, "bot_token_enc": S.box.encrypt(tok),
                "bot_token_fp": fingerprint(tok), "client_secret_enc": S.box.encrypt(sec),
                "client_secret_fp": fingerprint(sec), "bot_user_id": self.discord.bot_users[app_id],
                "bot_username": "bot", "permissions": DEFAULT_PERMISSIONS, "intents": DEFAULT_INTENTS,
                "status": "enabled", "gateway_desired": "stopped", "restart_requested_at": None,
                "configured_at": now, "updated_at": now, "version": 1})
        self.iA_dc = await H.bind_discord_guild(self.iA_dc.id, GUILD_A, application_id=APP_A, actor_id=root.user_id)
        self.iB_web = await H.create_instance(bot_id=self.bB.id, name="Web B", channel={"type": "webchat"},
                                              actor_id=root.user_id)
        from api.common import issue_session
        for k, u in self.users.items():
            self._tokens[k] = await issue_session(S, u)          # real server-side sessions
        return self

    # ── helpers ──
    def headers(self, who):
        if who is None:
            return {}
        return {"Authorization": "Bearer " + self._tokens[who]}

    async def call(self, method, path, who=None, json=None, headers=None):
        import httpx
        h = {**self.headers(who), **(headers or {})}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as c:
            return await c.request(method, "/api" + path, json=json, headers=h)

    async def snapshot(self):
        if hasattr(self.store, "collections"):
            return copy.deepcopy(self.store.collections)
        out = {}
        for name in sorted(await self.store.db.list_collection_names()):
            out[name] = [d async for d in self.store.db[name].find({}).sort("_id", 1)]
        return out

    async def refresh_user(self, key):
        """Reload the user and open a new session (after a token-version change)."""
        from api.common import issue_session
        from core.identity import get_user
        self.users[key] = await get_user(self.store, self.users[key].id)
        self._tokens[key] = await issue_session(self.S, self.users[key])
        return self.users[key]
