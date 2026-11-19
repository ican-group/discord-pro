"""Local Spec 012 end-to-end server (Phase 6A). TEST ONLY — never production.

Runs the real Spec 012 FastAPI application (one process, real HTTP, real
session cookies and CSRF) against a disposable standalone MongoDB, with every
external service replaced by the in-process fakes (Discord, Telegram,
Zendesk, AI providers, mail). It also serves the built React admin app from
the same origin, and runs the worker loop (deliveries, deletion jobs).

Safety: refuses unless ``MONGO_URL`` is not the production host and
``DB_NAME`` starts with ``bbp_e2e``; drops and recreates that database on
start. The ``/__e2e/*`` routes (mail outbox, seeded ids) exist only here.

    MONGO_URL=mongodb://bbp-e2e-mongo:27017 DB_NAME=bbp_e2e E2E_ORIGIN=http://bbp-e2e-app:8080 \
      FRONTEND_BUILD=/frontend/build python -m tests.e2e.server
"""
from __future__ import annotations

import asyncio
import os
import sys

PASSWORD = "E2e-correct-horse-battery-7"          # test-only password for every seeded user
USERS = {
    "root": ("root@e2e.test", "Platform Root", "superadmin"),
    "owner": ("owner@e2e.test", "Olivia Owner", "ent_owner"),
    "admin": ("admin@e2e.test", "Adam Admin", "ent_admin"),
    "editor": ("editor@e2e.test", "Edith Editor", "ent_editor"),
    "viewer": ("viewer@e2e.test", "Victor Viewer", "ent_viewer"),
    "orgadmin": ("orgadmin@e2e.test", "Oscar OrgAdmin", "org_admin"),
    "restricted": ("restricted@e2e.test", "Rita Restricted", "other_org_viewer"),
    "ownerb": ("ownerb@e2e.test", "Bea Globex", "ent_b_owner"),
    "newcomer": ("newcomer@e2e.test", "Nina Newcomer", "none"),
}


def _guard():
    url, db = os.environ.get("MONGO_URL", ""), os.environ.get("DB_NAME", "")
    if not url or "bitbotpro-mongo" in url or not db.startswith("bbp_e2e"):
        sys.exit("Refusing: MONGO_URL must be a disposable server and DB_NAME must start with bbp_e2e.")
    return url, db


class Outbox:
    """Mock mail provider: keeps messages in memory for the browser tests."""

    def __init__(self):
        self.messages = []

    async def send_invitation(self, email, *, subject, text, html):
        self.messages.append({"kind": "invitation", "to": email, "subject": subject, "text": text})

    async def send_password_reset(self, email, reset_url):
        self.messages.append({"kind": "reset", "to": email, "subject": "Reset your password", "text": reset_url})

    async def send_discord_invite(self, email, install_url, **kw):
        self.messages.append({"kind": "discord_invite", "to": email, "subject": "Add the bot", "text": install_url})


class FakeAI:
    """Fake AI provider: deterministic answers; key check for credential tests."""
    name = "anthropic"

    async def generate(self, request):
        from runtime.interfaces import GenerationResult
        return GenerationResult(text=f"E2E answer to: {request.user_text[:80]}", input_tokens=12, output_tokens=8)

    async def check(self, api_key):
        return {"ok": api_key.startswith("sk-e2e-"), "category": None if api_key.startswith("sk-e2e-") else "unauthorized"}


async def build():
    from motor.motor_asyncio import AsyncIOMotorClient
    from cryptography.fernet import Fernet
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.staticfiles import StaticFiles
    import secrets as _s
    from api.app import create_app
    from api import passwords
    from api.common import Services, Settings
    from core import memberships as ms
    from core.authz import Role
    from core.credentials import Provider, credential_to_doc, new_credential, telegram_bot_id
    from core.hierarchy import Level, new_id
    from core.identity import USERS as UCOL, create_user, ensure_superadmin_guard, get_user
    from persistence.mongo import MongoStore, prepare_database
    from runtime.discord import discord_cleanup_executors
    from runtime.telegram import telegram_cleanup_executors
    from runtime.zendesk import zendesk_cleanup_executors
    from runtime.worker import Worker
    from tests.unit import _api_fixtures as F

    url, db_name = _guard()
    origin = os.environ.get("E2E_ORIGIN", "http://localhost:8080").rstrip("/")
    client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=10000)
    await client.drop_database(db_name)
    db = client[db_name]
    await prepare_database(db)
    store = MongoStore(db)
    outbox = Outbox()
    settings = Settings(jwt_secret=_s.token_urlsafe(48), encryption_key=Fernet.generate_key().decode(),
                        cookie_secure=False, public_api_origin=origin, oauth_result_origin=origin,
                        reset_url_base=origin + "/app/reset-password#token=",
                        invitation_url_base=origin + "/app/invitations#token=")
    S = Services(store, settings, mailer=outbox)
    tg, dc, zd = F.FakeTelegram(), F.FakeDiscord(), F.FakeZendesk()
    S.telegram_transport, S.discord_transport, S.zendesk_transport = tg.transport(), dc.transport(), zd.transport()
    S.zendesk_resolver = zd.resolve
    S.providers = {"anthropic": FakeAI()}
    dc.codes["e2eoauthcode0001"] = [{"id": F.GUILD_A, "owner": True, "permissions": "0"}]
    now = S.clock()

    # ── seed (test-only tenants) ──
    users = {}
    for key, (email, name, _) in USERS.items():
        u = await create_user(store, email=email, name=name, now=now, email_verified=True)
        await store.update(UCOL, u.id, {"password_hash": passwords.hash_password(PASSWORD)})
        users[key] = u
    await store.update(UCOL, users["root"].id, {"is_platform_superadmin": True})
    await ensure_superadmin_guard(store)
    users = {k: await get_user(store, u.id) for k, u in users.items()}
    root = users["root"].actor()
    H = S.hierarchy
    ent_a = await H.create_enterprise(name="Acme Corp", slug="acme-e2e", actor_id=root.user_id)
    org_support = await H.create_organisation(enterprise_id=ent_a.id, name="Support", actor_id=root.user_id)
    org_sales = await H.create_organisation(enterprise_id=ent_a.id, name="Sales", actor_id=root.user_id)
    bot = await H.create_bot(organisation_id=org_support.id, name="Helpdesk", actor_id=root.user_id)
    web = await H.create_instance(bot_id=bot.id, name="Website chat", channel={"type": "webchat"}, actor_id=root.user_id)
    ent_b = await H.create_enterprise(name="Globex", slug="globex-e2e", actor_id=root.user_id)
    org_b = await H.create_organisation(enterprise_id=ent_b.id, name="Globex Ops", actor_id=root.user_id)
    bot_b = await H.create_bot(organisation_id=org_b.id, name="Globex Bot", actor_id=root.user_id)
    web_b = await H.create_instance(bot_id=bot_b.id, name="Globex chat", channel={"type": "webchat"}, actor_id=root.user_id)
    grants = [(ent_a.id, Level.ENTERPRISE, ent_a.id, "owner", Role.OWNER), (ent_a.id, Level.ENTERPRISE, ent_a.id, "admin", Role.ADMIN),
              (ent_a.id, Level.ENTERPRISE, ent_a.id, "editor", Role.EDITOR), (ent_a.id, Level.ENTERPRISE, ent_a.id, "viewer", Role.VIEWER),
              (ent_a.id, Level.ORGANISATION, org_support.id, "orgadmin", Role.ADMIN),
              (ent_a.id, Level.ORGANISATION, org_sales.id, "restricted", Role.VIEWER),
              (ent_b.id, Level.ENTERPRISE, ent_b.id, "ownerb", Role.OWNER)]
    for eid, level, sid, who, role in grants:
        chain = await H.load_chain(level, sid)
        await ms.mutate(store, eid, ms.op_grant(root, chain, user_id=users[who].id, role=role, now=now,
                                                bypass_authority=True, admit=level is not Level.ENTERPRISE))
    echain = await H.load_chain(Level.ENTERPRISE, ent_a.id)
    ai = new_credential(credential_id=new_id(), chain=echain, provider=Provider.ANTHROPIC, name="Acme AI key",
                        secret="sk-e2e-" + "k" * 40, box=S.box, actor_id=root.user_id, now=now)
    await store.insert("credentials", credential_to_doc(ai, 1))
    ochain = await H.load_chain(Level.ORGANISATION, org_support.id)
    tgc = new_credential(credential_id=new_id(), chain=ochain, provider=Provider.TELEGRAM_BOT, name="Support Telegram",
                         secret=F.TELEGRAM_TOKEN_A, box=S.box, actor_id=root.user_id, now=now,
                         metadata={"telegram_bot_id": telegram_bot_id(F.TELEGRAM_TOKEN_A)})
    await store.insert("credentials", credential_to_doc(tgc, 1))
    ids = {"ent_a": ent_a.id, "ent_b": ent_b.id, "org_support": org_support.id, "org_sales": org_sales.id,
           "org_b": org_b.id, "bot": bot.id, "bot_b": bot_b.id, "web": web.id, "web_b": web_b.id,
           "ai_cred": ai.id, "tg_cred": tgc.id, "users": {k: u.id for k, u in users.items()},
           "discord": {"app_id": F.APP_A, "bot_token": F.DISCORD_TOKEN_A, "client_secret": F.DISCORD_SECRET_A,
                       "guild": F.GUILD_A, "oauth_code": "e2eoauthcode0001", "channel_help": F.CH_A2},
           "password": PASSWORD, "public_id": web.channel.public_id}

    app = create_app(S)

    @app.get("/__e2e/outbox")
    async def e2e_outbox():
        return JSONResponse({"messages": outbox.messages})

    @app.get("/__e2e/ids")
    async def e2e_ids():
        return JSONResponse(ids)

    @app.get("/__e2e/fakes")
    async def e2e_fakes():
        return JSONResponse({"discord_calls": [[c[0], c[1], c[2]] for c in dc.calls[-200:]],
                             "telegram_sent": len(getattr(tg, "sent", []) or []), "zendesk_calls": len(zd.calls)})

    build_dir = os.environ.get("FRONTEND_BUILD")
    if build_dir and os.path.isdir(build_dir):
        index = os.path.join(build_dir, "index.html")
        app.mount("/static", StaticFiles(directory=os.path.join(build_dir, "static")), name="static")

        @app.get("/{path:path}")
        async def spa(path: str):
            full = os.path.realpath(os.path.join(build_dir, path))
            if path and full.startswith(os.path.realpath(build_dir)) and os.path.isfile(full):
                return FileResponse(full)
            return FileResponse(index, headers={"Cache-Control": "no-store"})

    executors = {**telegram_cleanup_executors(S.box, transport=S.telegram_transport),
                 **discord_cleanup_executors(S.box, transport=S.discord_transport),
                 **zendesk_cleanup_executors(S.box, transport=S.zendesk_transport, resolver=S.zendesk_resolver)}

    async def worker_loop():
        from api.telegram import channel_for as tg_channel
        from api.discord import channel_for as dc_channel
        from api.integrations import service_for
        w = Worker(store, clock=S.clock, worker_id="e2e-worker", executors=executors, telegram=tg_channel(S),
                   discord=dc_channel(S), zendesk=service_for(S))
        while True:
            try:
                await w.run_once()
            except Exception as exc:                       # keep running; visible in the (test) server log
                import traceback
                tb = traceback.extract_tb(exc.__traceback__)[-1]
                print("worker error:", type(exc).__name__, str(exc)[:200], f"{tb.filename}:{tb.lineno}", flush=True)
            await asyncio.sleep(2)

    @app.on_event("startup")
    async def start_worker():
        app.state.worker = asyncio.create_task(worker_loop())

    return app


async def serve():
    import uvicorn
    app = await build()                          # one event loop for MongoDB, the API and the worker
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                                           log_level="warning"))
    await server.serve()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
