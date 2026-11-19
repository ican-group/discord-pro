"""Generated route × role × tenant permission matrix (spec 012 §17, Phase 3).

For every route of the new API and every actor (anonymous, Enterprise viewer,
editor, admin, owner, the owner of another Enterprise, a platform superadmin)
the request is made against Enterprise A on a fresh world. Expected outcome:

* anonymous → 401;
* the other Enterprise's owner → 404 on every Enterprise-A route (no existence
  oracle) and on platform routes;
* a member below the route's minimum role → 403 (404 where the route hides
  the object, e.g. platform routes);
* allowed actors → 2xx.

Every rejected request must leave the database byte-for-byte unchanged
(including the audit log). A coverage test fails if a route is added without
a matrix entry.
"""
import asyncio
import unittest

import base64

from tests.unit._api_fixtures import APP_A, CH_A1, DISCORD_TOKEN_A2, HAVE_API, World

DAPP = "/platform/enterprises/{enterprise_id}/discord-application"
ZI = "/integrations/{integration_id}"
PNG_URI = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()

ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3, "owner": 4}
ACTORS = ("anon", "viewer", "editor", "admin", "owner", "foreign", "root")
WHO = {"anon": None, "foreign": "owner_b", "root": "root"}


class Case:
    def __init__(self, method, route, path, min_role, body=None, prepare=None, member_deny=403):
        self.method, self.route, self.path, self.min_role = method, route, path, min_role
        self.body, self.prepare, self.member_deny = body, prepare, member_deny

    def expected(self, actor):
        if actor == "anon":
            return 401
        if actor == "root":
            return 200
        if self.min_role == "any":
            return 200
        if actor == "foreign":
            return 404
        if self.min_role == "superadmin":
            return 404
        if self.min_role == "root_only":
            return self.member_deny
        return 200 if ROLE_RANK[actor] >= ROLE_RANK[self.min_role] else self.member_deny


async def _suspend(w, level, oid):
    from core.lifecycle import Authority, LifecycleAction
    await w.S.hierarchy.change_lifecycle(level, oid, LifecycleAction.SUSPEND, authority=Authority.OBJECT,
                                         actor_id=w.users["owner"].id)


async def _archive(w, level, oid):
    from core.lifecycle import Authority, LifecycleAction
    await w.S.hierarchy.change_lifecycle(level, oid, LifecycleAction.ARCHIVE, authority=Authority.OBJECT,
                                         actor_id=w.users["owner"].id)


def _token_for(level_value, attr):
    async def prep(w, who):
        from core import deletion as dl
        from core.hierarchy import Level
        obj = getattr(w, attr)
        pv = await dl.preview(w.store, Level(level_value), obj.id, w.eA.id)
        user = w.users.get(WHO.get(who, who)) if who != "anon" else None
        token, _ = await dl.issue_token(w.store, actor_id=user.id if user else "anon", level=level_value,
                                        target_id=obj.id, digest=pv["digest"], now=w.S.clock())
        return {"preview_token": token, "confirm_name": obj.name}
    return prep


def _user_token(target):
    async def prep(w, who):
        from core import deletion as dl
        user = w.users.get(WHO.get(who, who)) if who != "anon" else None
        pv = await dl.preview_user(w.store, w.users[target].id)
        token, _ = await dl.issue_token(w.store, actor_id=user.id if user else "anon", level="user",
                                        target_id=w.users[target].id, digest=pv["digest"], now=w.S.clock())
        return {"preview_token": token, "confirm_email": w.users[target].email}
    return prep


async def _job(w, who):
    from core import deletion as dl
    from core.authz import Actor
    action = dl._action("telegram_delete_webhook", "x", {"instance_id": "x"}, mandatory=True)
    await dl.create_job(w.store, level="bot", target_id=w.bA.id, enterprise_id=w.eA.id, target_name="x",
                        actor=Actor(w.users["owner"].id), now=w.S.clock(), external=[action], counts={})


async def _invitation(w, who):
    from core.hierarchy import new_id
    w.inv_id = new_id()
    await w.store.insert("invitations", {
        "id": w.inv_id, "token_hash": "0" * 64, "email": "invitee@example.test", "enterprise_id": w.eA.id,
        "scope_level": "enterprise", "scope_id": w.eA.id, "role": "viewer", "status": "pending",
        "invited_by": w.users["owner"].id, "created_at": w.S.clock(), "expires_at": "2999-01-01T00:00:00+00:00",
        "version": 1})


async def _conversation(w, who):
    from runtime import conversations as conv
    chain = await w.S.hierarchy.load_chain(__import__("core.hierarchy").hierarchy.Level.BOT_INSTANCE, w.iA_web.id)
    session, _ = await conv.create_session(w.store, chain, now=w.S.clock())
    w.conv_id, w.msg_id = session["id"], "6d1f5c3a-8b2e-4f7a-9c1d-2e3f4a5b6c7d"
    await conv.append_turn(w.store, chain, session["id"], [
        {"id": "1d1f5c3a-8b2e-4f7a-9c1d-2e3f4a5b6c7d", "role": "user", "text": "q", "at": w.S.clock()},
        {"id": w.msg_id, "role": "assistant", "text": "a", "at": w.S.clock()}], now=w.S.clock(), retention_days=30)


async def _tg_connected(w, who):
    from api.telegram import channel_for
    await channel_for(w.S).connect(w.iA_tg.id)


async def _share_link(w, who):
    r = await w.call("POST", f"/instances/{w.iA_dc.id}/channel/discord/share-links", "root", {})
    w.share_id = r.json()["id"]


async def _uncertain_delivery(w, who):
    from runtime import delivery as dlv
    chain = await w.S.hierarchy.load_chain(__import__("core.hierarchy").hierarchy.Level.BOT_INSTANCE, w.iA_tg.id)
    doc = await dlv.ensure(w.store, chain, "matrix-req", channel="telegram", target={"chat_id": 1},
                           parts=[{"text": "x"}], now=w.S.clock())
    await w.store.update("outbound_deliveries", doc["id"], {"status": "uncertain"})
    w.delivery_id = doc["id"]


async def _zendesk(w, who):
    """A Zendesk integration with a verified API-token credential, the platform
    OAuth client and one pending suggestion (created as root, before the snapshot)."""
    from tests.unit._api_fixtures import ZD_API_TOKEN
    r = await w.call("POST", f"/bots/{w.bA.id}/integrations", "root", {"type": "zendesk", "name": "Support"})
    w.integ_id = r.json()["id"]
    r = await w.call("POST", f"/organisations/{w.oA.id}/credentials", "root",
                     {"provider": "zendesk_api_token", "name": "ZD", "secret": ZD_API_TOKEN,
                      "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})
    await w.call("PATCH", f"/integrations/{w.integ_id}", "root", {"credential_id": r.json()["id"]})
    await w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                 {"secret": "zendesk-client-secret-tests", "metadata": {"client_id": "bitbotpro"}})
    w.sugg_id = "8f7e6d5c-4b3a-4291-8a7b-6c5d4e3f2a1c"
    await w.store.insert("suggestions", {"id": w.sugg_id, "enterprise_id": w.eA.id, "organisation_id": w.oA.id,
                                         "bot_id": w.bA.id, "integration_id": w.integ_id, "ticket_id": "101",
                                         "text": "x", "status": "pending", "source": "webhook",
                                         "created_at": w.S.clock(), "version": 1})


async def _zendesk_provisioned(w, who):
    await _zendesk(w, who)
    await w.call("POST", f"/integrations/{w.integ_id}/zendesk/webhook/provision", "root")
    integ = await w.store.get("bot_integrations", w.integ_id)
    w.zcred = integ["credential_id"]


async def _zendesk_uncertain(w, who):
    await _zendesk(w, who)
    from runtime.zendesk_integration import event_doc_id
    w.event_id = event_doc_id(w.integ_id, "matrix")
    now = w.S.clock()
    await w.store.insert("zendesk_events", {
        "id": w.event_id, "enterprise_id": w.eA.id, "organisation_id": w.oA.id, "bot_id": w.bA.id,
        "integration_id": w.integ_id, "ticket_id": "101", "status": "uncertain", "attempts": 1,
        "generation": {"key": f"zd:{w.event_id}", "state": "uncertain"},
        "expires_at_dt": __import__("datetime").datetime.fromisoformat(now), "version": 1})
    await w.store.insert("usage_ledger", {"id": f"zd:{w.event_id}", "state": "uncertain", "period": now[:7],
                                          "counters": [], "enterprise_id": w.eA.id, "created_at": now,
                                          "updated_at": now, "version": 1})


async def _knowledge(w, who):
    from runtime.knowledge_store import Ingestor
    r = await w.call("POST", f"/enterprises/{w.eA.id}/knowledge/sources", "root", {"name": "Policies"})
    w.ksrc = r.json()["id"]
    src = await w.store.get("knowledge_sources", w.ksrc)
    doc = await Ingestor(w.store, clock=w.S.clock).submit(src, name="refunds.txt", content_type="text/plain",
                                                          data=b"Refunds take 14 days.", actor_id=w.users["root"].id)
    w.kdoc = doc["id"]


async def _platform_cred(w, who):
    await w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root", {"secret": "zendesk-client-secret-tests",
                                                                                "metadata": {"client_id": "bitbotpro"}})


def cases():
    L = "{type_:objtype}"
    out = [
        Case("GET", "/enterprises", "/enterprises", "any"),
        Case("POST", "/enterprises", "/enterprises", "superadmin", {"name": "New", "slug": "new-ent"}),
        Case("GET", "/enterprises/{enterprise_id}", "/enterprises/{E}", "viewer"),
        Case("PATCH", "/enterprises/{enterprise_id}", "/enterprises/{E}", "admin", {"name": "Renamed"}),
        Case("GET", "/enterprises/{parent_id}/organisations", "/enterprises/{E}/organisations", "viewer"),
        Case("POST", "/enterprises/{parent_id}/organisations", "/enterprises/{E}/organisations", "admin",
             {"name": "Org 2"}),
        Case("GET", "/organisations/{object_id}", "/organisations/{O}", "viewer"),
        Case("PATCH", "/organisations/{object_id}", "/organisations/{O}", "editor", {"description": "d"}),
        Case("GET", "/organisations/{parent_id}/bots", "/organisations/{O}/bots", "viewer"),
        Case("POST", "/organisations/{parent_id}/bots", "/organisations/{O}/bots", "admin", {"name": "Bot 2"}),
        Case("GET", "/bots/{object_id}", "/bots/{B}", "viewer"),
        Case("PATCH", "/bots/{object_id}", "/bots/{B}", "editor", {"name": "Bot renamed"}),
        Case("GET", "/bots/{bot_id}/instances", "/bots/{B}/instances", "viewer"),
        Case("POST", "/bots/{bot_id}/instances", "/bots/{B}/instances", "admin",
             {"name": "Web 2", "channel": {"type": "webchat"}}),
        Case("GET", "/instances/{instance_id}", "/instances/{I}", "viewer"),
        Case("PATCH", "/instances/{instance_id}", "/instances/{I}", "admin",
             {"channel": {"allowed_origins": ["https://shop.example"]}}),
        Case("GET", f"/{L}/{{object_id}}/config", "/instances/{I}/config", "viewer"),
        Case("PATCH", f"/{L}/{{object_id}}/config", "/bots/{B}/config", "editor",
             {"changes": {"behaviour.language": "de"}}),
        Case("PATCH", f"/{L}/{{object_id}}/config", "/enterprises/{E}/config", "admin",
             {"changes": {"knowledge.mode": "knowledge_preferred"}}),
        Case("GET", "/{type_:policytype}/{object_id}/policy", "/organisations/{O}/policy", "viewer"),
        Case("PATCH", "/{type_:policytype}/{object_id}/policy", "/enterprises/{E}/policy", "admin",
             {"changes": {"limits.max_end_user_msgs_per_minute": 30}}),
        Case("POST", f"/{L}/{{object_id}}/suspend", "/instances/{I}/suspend", "admin", {"reason": "test"}),
        Case("POST", f"/{L}/{{object_id}}/suspend", "/enterprises/{E}/suspend", "admin", {}),
        Case("POST", f"/{L}/{{object_id}}/resume", "/bots/{B}/resume", "admin", {},
             prepare=lambda w, who: _suspend(w, __import__("core.hierarchy").hierarchy.Level.BOT, w.bA.id)),
        Case("POST", f"/{L}/{{object_id}}/archive", "/organisations/{O}/archive", "admin", {}),
        Case("POST", f"/{L}/{{object_id}}/unarchive", "/bots/{B}/unarchive", "admin", {},
             prepare=lambda w, who: _archive(w, __import__("core.hierarchy").hierarchy.Level.BOT, w.bA.id)),
        Case("GET", "/{scope_type:scopetype}/{scope_id}/members", "/enterprises/{E}/members", "viewer"),
        Case("POST", "/{scope_type:scopetype}/{scope_id}/members", "/organisations/{O}/members", "admin",
             {"user_id": "{member2}", "role": "editor"}),
        Case("POST", "/{scope_type:scopetype}/{scope_id}/members", "/enterprises/{E}/members", "root_only",
             {"user_id": "{outsider}", "role": "viewer"}),
        Case("PATCH", "/{scope_type:scopetype}/{scope_id}/members/{member_id}", "/enterprises/{E}/members/{member2}",
             "admin", {"role": "editor"}),
        Case("DELETE", "/{scope_type:scopetype}/{scope_id}/members/{member_id}", "/enterprises/{E}/members/{member2}",
             "admin"),
        Case("POST", "/enterprises/{enterprise_id}/invitations", "/enterprises/{E}/invitations", "admin",
             {"email": "new.person@example.test", "role": "viewer"}),
        Case("GET", "/enterprises/{enterprise_id}/invitations", "/enterprises/{E}/invitations", "viewer"),
        Case("DELETE", "/invitations/{invitation_id}", "/invitations/{INV}", "admin", prepare=_invitation),
        Case("GET", "/{owner_type:ownertype}/{owner_id}/credentials", "/organisations/{O}/credentials", "viewer"),
        Case("POST", "/{owner_type:ownertype}/{owner_id}/credentials", "/organisations/{O}/credentials", "admin",
             {"provider": "openai", "name": "OpenAI", "secret": "not-a-real-openai-key-0123456789"}),
        Case("GET", "/credentials/{credential_id}", "/credentials/{C}", "viewer"),
        Case("PATCH", "/credentials/{credential_id}", "/credentials/{C}", "admin", {"name": "Renamed"}),
        Case("POST", "/credentials/{credential_id}/revoke", "/credentials/{C}/revoke", "admin"),
        Case("DELETE", "/credentials/{credential_id}", "/credentials/{C}", "admin"),
        Case("GET", "/platform/credentials", "/platform/credentials", "superadmin"),
        Case("PUT", "/platform/credentials/{kind}", "/platform/credentials/zendesk_oauth_client", "superadmin",
             {"secret": "zendesk-client-secret-for-tests", "metadata": {"client_id": "bitbotpro"}}),
        Case("POST", "/platform/credentials/{kind}/revoke", "/platform/credentials/zendesk_oauth_client/revoke", "superadmin",
             prepare=_platform_cred),
        Case("POST", f"/{L}/{{object_id}}/deletion-preview", "/bots/{B}/deletion-preview", "admin"),
        Case("POST", f"/{L}/{{object_id}}/deletion-preview", "/enterprises/{E}/deletion-preview", "owner"),
        Case("POST", f"/{L}/{{object_id}}/deletion-preview", "/organisations/{O}/deletion-preview", "owner"),  # X7
        Case("POST", f"/{L}/{{object_id}}/delete", "/bots/{B}/delete", "admin", prepare=_token_for("bot", "bA")),
        Case("POST", f"/{L}/{{object_id}}/delete", "/enterprises/{E}/delete", "owner",
             prepare=_token_for("enterprise", "eA")),
        Case("GET", "/deletion-jobs/{job_id}", "/deletion-jobs/bot:{B}", "owner", prepare=_job, member_deny=404),
        Case("POST", "/deletion-jobs/{job_id}/resume", "/deletion-jobs/bot:{B}/resume", "superadmin", prepare=_job),
        Case("POST", "/deletion-jobs/{job_id}/actions/{action_id}/resolve",
             "/deletion-jobs/bot:{B}/actions/telegram_delete_webhook:x/resolve", "superadmin",
             {"reason": "operator check"}, prepare=_job),
        Case("GET", "/users/me", "/users/me", "any"),
        Case("POST", "/users/me/deletion-preview", "/users/me/deletion-preview", "any"),
        Case("POST", "/platform/users/{user_id}/deletion-preview", "/platform/users/{member2}/deletion-preview",
             "superadmin"),
        Case("POST", "/platform/users/{user_id}/delete", "/platform/users/{member2}/delete", "superadmin",
             prepare=_user_token("member2")),
        Case("POST", "/platform/users/{user_id}/superadmin", "/platform/users/{member2}/superadmin", "superadmin",
             {"grant": True}),
        Case("PUT", "/credentials/{credential_id}/scope", "/credentials/{C}/scope", "admin",
             {"usable_by": [{"level": "bot", "id": "{B}"}]}),
        Case("GET", "/instances/{instance_id}/runtime-config", "/instances/{I}/runtime-config", "viewer"),
        Case("GET", "/platform/workers", "/platform/workers", "superadmin"),
        # Phase 6A (F1, F2, F5)
        Case("GET", f"/{L}/{{object_id}}/usage", "/organisations/{O}/usage", "viewer"),
        Case("GET", ZI + "/usage", "/integrations/{ZI}/usage", "viewer", prepare=_zendesk),
        Case("GET", "/platform/users", "/platform/users", "superadmin"),
        Case("GET", "/{scope_type:scopetype}/{scope_id}/member-candidates", "/enterprises/{E}/member-candidates?q=mem",
             "admin"),
        Case("POST", "/instances/{instance_id}/preview-session", "/instances/{I}/preview-session", "editor"),
        Case("GET", "/auth/session", "/auth/session", "any"),
        Case("GET", "/instances/{instance_id}/conversations", "/instances/{I}/conversations", "viewer"),
        Case("GET", "/instances/{instance_id}/conversations/{session_id}", "/instances/{I}/conversations/{CONV}",
             "viewer", prepare=_conversation),
        Case("POST", "/instances/{instance_id}/conversations/{session_id}/approve",
             "/instances/{I}/conversations/{CONV}/approve", "editor", {"message_id": "{MSG}"}, prepare=_conversation),
        Case("DELETE", "/bots/{bot_id}/logo", "/bots/{B}/logo", "editor"),
        Case("GET", "/instances/{instance_id}/embed-snippet", "/instances/{I}/embed-snippet", "viewer"),
        Case("POST", "/instances/{instance_id}/channel/connect", "/instances/{T}/channel/connect", "admin"),
        Case("POST", "/instances/{instance_id}/channel/test", "/instances/{T}/channel/test", "admin"),
        Case("GET", "/instances/{instance_id}/channel/status", "/instances/{T}/channel/status", "viewer"),
        Case("POST", "/instances/{instance_id}/channel/rotate-secret", "/instances/{T}/channel/rotate-secret", "admin",
             prepare=_tg_connected),
        Case("POST", "/instances/{instance_id}/channel/disconnect", "/instances/{T}/channel/disconnect", "admin",
             prepare=_tg_connected),
        Case("POST", "/credentials/{credential_id}/test", "/credentials/{C}/test", "admin"),
        Case("GET", "/enterprises/{enterprise_id}/audit", "/enterprises/{E}/audit", "admin"),
        Case("GET", "/platform/audit", "/platform/audit", "superadmin"),
        # Phase 4D: T-A delivery visibility and the Enterprise Discord application (D-Q14)
        Case("GET", "/instances/{instance_id}/deliveries", "/instances/{T}/deliveries", "admin"),
        Case("GET", "/enterprises/{enterprise_id}/discord", "/enterprises/{E}/discord", "viewer"),
        Case("GET", DAPP, "/platform/enterprises/{E}/discord-application", "superadmin"),
        Case("PUT", DAPP, "/platform/enterprises/{E}/discord-application", "superadmin",
             {"application_id": APP_A, "bot_token": DISCORD_TOKEN_A2}),
        Case("POST", DAPP + "/rotate-token", "/platform/enterprises/{E}/discord-application/rotate-token",
             "superadmin", {"value": DISCORD_TOKEN_A2}),
        Case("POST", DAPP + "/rotate-secret", "/platform/enterprises/{E}/discord-application/rotate-secret",
             "superadmin", {"value": "rotated-client-secret-xyz"}),
        Case("POST", DAPP + "/enable", "/platform/enterprises/{E}/discord-application/enable", "superadmin"),
        Case("POST", DAPP + "/disable", "/platform/enterprises/{E}/discord-application/disable", "superadmin"),
        Case("POST", DAPP + "/gateway/{op:gateway_op}", "/platform/enterprises/{E}/discord-application/gateway/restart",
             "superadmin"),
        Case("POST", DAPP + "/avatar", "/platform/enterprises/{E}/discord-application/avatar", "superadmin",
             {"image": PNG_URI}),
        Case("POST", "/instances/{instance_id}/channel/discord/install-url",
             "/instances/{D}/channel/discord/install-url", "admin"),
        Case("GET", "/instances/{instance_id}/channel/discord/channels", "/instances/{D}/channel/discord/channels",
             "admin"),
        Case("PUT", "/instances/{instance_id}/channel/discord/settings", "/instances/{D}/channel/discord/settings",
             "admin", {"listen_mode": "specific_channels", "channel_ids": [CH_A1]}),
        Case("PUT", "/instances/{instance_id}/channel/discord/nickname", "/instances/{D}/channel/discord/nickname",
             "admin", {"nickname": "Helper"}),
        # Phase 4D corrections (C2 share links, C3 handoff administration, C5 delivery resolution)
        Case("POST", "/instances/{instance_id}/channel/discord/share-links",
             "/instances/{D}/channel/discord/share-links", "admin", {}),
        Case("GET", "/instances/{instance_id}/channel/discord/share-links",
             "/instances/{D}/channel/discord/share-links", "admin"),
        Case("DELETE", "/instances/{instance_id}/channel/discord/share-links/{share_id}",
             "/instances/{D}/channel/discord/share-links/{SHARE}", "admin", prepare=_share_link),
        Case("GET", "/instances/{instance_id}/channel/discord/handoffs", "/instances/{D}/channel/discord/handoffs",
             "viewer"),
        Case("PUT", "/instances/{instance_id}/channel/discord/handoffs/{channel_id}",
             "/instances/{D}/channel/discord/handoffs/" + CH_A1, "admin", {"minutes": 5}),
        Case("POST", "/instances/{instance_id}/channel/discord/handoffs/{channel_id}/resume",
             "/instances/{D}/channel/discord/handoffs/" + CH_A1 + "/resume", "admin"),
        Case("POST", "/instances/{instance_id}/deliveries/{delivery_id}/resolve",
             "/instances/{T}/deliveries/{DLV}/resolve", "admin", {"note": "checked with the customer"},
             prepare=_uncertain_delivery),
        # Phase 4E: Zendesk Bot Integrations
        Case("POST", "/bots/{bot_id}/integrations", "/bots/{B}/integrations", "admin",
             {"type": "zendesk", "name": "Support"}),
        Case("GET", "/bots/{bot_id}/integrations", "/bots/{B}/integrations", "viewer"),
        Case("GET", ZI, "/integrations/{ZI}", "viewer", prepare=_zendesk),
        Case("PATCH", ZI, "/integrations/{ZI}", "admin", {"name": "Renamed"}, prepare=_zendesk),
        Case("DELETE", ZI, "/integrations/{ZI}", "admin", prepare=_zendesk),
        Case("GET", ZI + "/config", "/integrations/{ZI}/config", "viewer", prepare=_zendesk),
        Case("PATCH", ZI + "/config", "/integrations/{ZI}/config", "editor",
             {"changes": {"behaviour.persona": "Zendesk persona"}}, prepare=_zendesk),
        Case("POST", ZI + "/zendesk/oauth/start", "/integrations/{ZI}/zendesk/oauth/start", "admin",
             {"subdomain": "acme"}, prepare=_zendesk),
        Case("POST", ZI + "/test", "/integrations/{ZI}/test", "admin", prepare=_zendesk),
        Case("GET", ZI + "/zendesk/webhook", "/integrations/{ZI}/zendesk/webhook", "admin", prepare=_zendesk),
        Case("PUT", ZI + "/zendesk/signing-secret", "/integrations/{ZI}/zendesk/signing-secret", "admin",
             {"secret": "manual-signing-secret-123456"}, prepare=_zendesk),
        Case("POST", ZI + "/rotate-webhook-secret", "/integrations/{ZI}/rotate-webhook-secret", "admin",
             prepare=_zendesk),
        Case("POST", ZI + "/zendesk/webhook/provision", "/integrations/{ZI}/zendesk/webhook/provision", "admin",
             prepare=_zendesk),
        Case("POST", ZI + "/sidebar-key", "/integrations/{ZI}/sidebar-key", "admin", prepare=_zendesk),
        Case("DELETE", ZI + "/sidebar-key", "/integrations/{ZI}/sidebar-key", "admin", prepare=_zendesk),
        Case("GET", ZI + "/zendesk/sidebar-app.zip", "/integrations/{ZI}/zendesk/sidebar-app.zip", "admin",
             prepare=_zendesk),
        Case("GET", ZI + "/suggestions", "/integrations/{ZI}/suggestions", "viewer", prepare=_zendesk),
        Case("POST", ZI + "/suggestions/{suggestion_id}/{decision}", "/integrations/{ZI}/suggestions/{SUG}/accept",
             "editor", prepare=_zendesk),
        Case("GET", ZI + "/deliveries", "/integrations/{ZI}/deliveries", "admin", prepare=_zendesk),
        # Phase 4K review: Zendesk corrections (Z-C, Part C)
        Case("POST", ZI + "/zendesk/webhook/deprovision", "/integrations/{ZI}/zendesk/webhook/deprovision", "admin",
             prepare=_zendesk_provisioned),
        Case("PUT", ZI + "/zendesk/webhook/owner-credential", "/integrations/{ZI}/zendesk/webhook/owner-credential",
             "admin", {"credential_id": "{ZC}"}, prepare=_zendesk_provisioned),
        Case("GET", ZI + "/events", "/integrations/{ZI}/events", "admin", prepare=_zendesk),
        Case("POST", ZI + "/events/{event_id}/resolve", "/integrations/{ZI}/events/{EV}/resolve", "admin",
             {"decision": "waive", "note": "reviewed"}, prepare=_zendesk_uncertain),
        # Phase 4K: knowledge sources and documents
        Case("POST", "/{owner_type:ownertype}/{owner_id}/knowledge/sources", "/enterprises/{E}/knowledge/sources", "admin",
             {"name": "Policies"}),
        Case("GET", "/{owner_type:ownertype}/{owner_id}/knowledge/sources", "/enterprises/{E}/knowledge/sources", "viewer"),
        Case("GET", "/knowledge/sources/{source_id}", "/knowledge/sources/{KS}", "viewer", prepare=_knowledge),
        Case("PATCH", "/knowledge/sources/{source_id}", "/knowledge/sources/{KS}", "admin", {"name": "Renamed"},
             prepare=_knowledge),
        Case("DELETE", "/knowledge/sources/{source_id}", "/knowledge/sources/{KS}", "admin", prepare=_knowledge),
        Case("PUT", "/knowledge/sources/{source_id}/documents/{name}", "/knowledge/sources/{KS}/documents/faq.json",
             "admin", {"q": "Warranty?", "a": "Two years."}, prepare=_knowledge),
        Case("GET", "/knowledge/sources/{source_id}/documents", "/knowledge/sources/{KS}/documents", "viewer",
             prepare=_knowledge),
        Case("GET", "/knowledge/documents/{document_id}", "/knowledge/documents/{KD}", "viewer", prepare=_knowledge),
        Case("PUT", "/knowledge/documents/{document_id}/acl", "/knowledge/documents/{KD}/acl", "admin",
             {"organisations": [], "bots": []}, prepare=_knowledge),
        Case("PUT", "/knowledge/documents/{document_id}/validity", "/knowledge/documents/{KD}/validity", "admin",
             {"valid_until": "2030-01-01T00:00:00+00:00"}, prepare=_knowledge),
        Case("POST", "/knowledge/documents/{document_id}/reprocess", "/knowledge/documents/{KD}/reprocess", "admin",
             prepare=_knowledge),
        Case("DELETE", "/knowledge/documents/{document_id}", "/knowledge/documents/{KD}", "admin",
             prepare=_knowledge),
    ]
    return out


# Routes with dedicated tests instead of a matrix row (they act on the caller's own account / token).
COVERED_ELSEWHERE = {("POST", "/invitations/lookup"), ("POST", "/invitations/accept"), ("POST", "/users/me/delete"),
                     # 6C B-ACCT: public, the e-mailed invitation token is the credential (test_api_registration.py)
                     ("POST", "/invitations/register"),
                     # authentication flows act on the caller's own credentials/session (test_api_auth.py)
                     ("POST", "/auth/login"), ("POST", "/auth/logout"), ("POST", "/auth/logout-all"),
                     ("POST", "/auth/change-password"), ("POST", "/auth/forgot-password"),
                     ("POST", "/auth/reset-password"),
                     # public Webchat routes and assets: no account session; test_api_webchat.py covers them
                     ("GET", "/public/instances/{pid}"), ("GET", "/public/instances/{pid}/logo"),
                     ("POST", "/public/instances/{pid}/sessions"), ("POST", "/public/instances/{pid}/chat"),
                     ("POST", "/public/instances/{pid}/chat/stream"),
                     ("GET", "/public/instances/{pid}/sessions/{sid}/history"),
                     ("OPTIONS", "/public/instances/{pid}"), ("OPTIONS", "/public/instances/{pid}/{rest:path}"),
                     ("GET", "/embed.js"), ("GET", "/public/widget.js"), ("GET", "/public/widget.css"),
                     ("GET", "/public/widget/{pid}"),
                     # Telegram webhook (Telegram-authenticated, no account session): test_api_telegram.py
                     ("POST", "/hooks/telegram/{instance_id}/{secret}"),
                     # raw image upload with role checks: test_api_webchat.W4LogoTests
                     ("PUT", "/bots/{bot_id}/logo"),
                     # Discord OAuth redirect: no session (SameSite=Strict); authority from the single-use
                     # state + re-authorisation (test_api_discord.OAuthTests)
                     ("GET", "/hooks/discord/oauth/callback"),
                     # Discord share link opened by an invited server admin (no account; C2):
                     # test_api_discord.ShareLinkTests
                     ("GET", "/public/discord/install/{token}"),
                     # Zendesk public endpoints (signed webhook, OAuth redirect, sidebar app key):
                     # test_api_zendesk.py
                     ("POST", "/hooks/zendesk/{integration_id}/{secret}"), ("GET", "/hooks/zendesk/oauth/callback"),
                     ("POST", "/hooks/zendesk/sidebar/generate")}


def _fill(template, w):
    ids = {"E": w.eA.id, "O": w.oA.id, "B": w.bA.id, "I": w.iA_web.id, "C": w.cA_tg.id,
           "INV": getattr(w, "inv_id", "x"), "T": w.iA_tg.id, "D": w.iA_dc.id, "CONV": getattr(w, "conv_id", "x"), "SHARE": getattr(w, "share_id", "x"),
           "DLV": getattr(w, "delivery_id", "x"), "ZI": getattr(w, "integ_id", "x"),
           "SUG": getattr(w, "sugg_id", "x"), "ZC": getattr(w, "zcred", "x"), "EV": getattr(w, "event_id", "x"),
           "KS": getattr(w, "ksrc", "x"), "KD": getattr(w, "kdoc", "x"), "MSG": getattr(w, "msg_id", "x"), **{k: u.id for k, u in w.users.items()}}
    if isinstance(template, str):
        for k, v in ids.items():
            template = template.replace("{" + k + "}", v)
        return template
    if isinstance(template, dict):
        return {k: _fill(v, w) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, w) for v in template]
    return template


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class PermissionMatrixTests(unittest.TestCase):
    def test_route_actor_matrix(self):
        failures = []
        seen = {}

        async def one(case, actor):
            w = await World().build()
            body = case.body
            if case.prepare is not None:
                prepared = await case.prepare(w, actor)
                if isinstance(prepared, dict):
                    body = prepared
            before = await w.snapshot()
            who = WHO.get(actor, actor)
            r = await w.call(case.method, _fill(case.path, w), who, json=_fill(body, w) if body is not None else None)
            want = case.expected(actor)
            seen[r.status_code // 100 * 100 if r.status_code < 400 else r.status_code] = \
                seen.get(r.status_code // 100 * 100 if r.status_code < 400 else r.status_code, 0) + 1
            ok = (200 <= r.status_code < 300) if want == 200 else r.status_code == want
            if not ok:
                failures.append(f"{case.method} {case.path} as {actor}: got {r.status_code} want {want} "
                                f"{r.text[:200]}")
            if r.status_code >= 400 and await w.snapshot() != before:
                failures.append(f"{case.method} {case.path} as {actor}: rejected request changed the database")

        async def main():
            for case in cases():
                for actor in ACTORS:
                    await one(case, actor)
        asyncio.run(main())
        self.assertEqual(failures, [], "\n".join(failures))
        # the matrix must genuinely exercise every outcome (guards against a vacuous pass)
        total = len(cases()) * len(ACTORS)
        self.assertEqual(sum(seen.values()), total)
        self.assertEqual(seen.get(401), len(cases()))
        for status in (200, 403, 404):
            self.assertGreater(seen.get(status, 0), 20, f"too few {status} outcomes: {seen}")

    def test_every_route_is_in_the_matrix(self):
        async def routes():
            w = await World().build()
            from api.app import route_table
            return {(m, r.path[len("/api"):]) for r in route_table(w.app) for m in getattr(r, "methods", ())
                    if r.path.startswith("/api")}
        all_routes = asyncio.run(routes())
        covered = {(c.method, c.route) for c in cases()} | COVERED_ELSEWHERE
        self.assertEqual(sorted(all_routes - covered), [], "routes without a permission-matrix entry")
        self.assertEqual(sorted(covered - all_routes), [], "matrix entries for routes that do not exist")


if __name__ == "__main__":
    unittest.main()
