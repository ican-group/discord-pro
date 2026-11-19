"""Bot Integration routes: Zendesk (Phase 4E; parity Z1–Z18, D-Q9, D-Q10, D-Q11, X2).

Permissions (all checked on the integration's **Bot**, loaded from the
database; nothing in a request or webhook payload establishes ownership):
* READ (viewer+): list/get integrations, effective configuration, suggestions;
* CREATE_CHILD (admin+): create;
* MANAGE_CHANNEL (admin+): settings, credential, status, OAuth connect,
  webhook secrets/provisioning, sidebar app key and ZIP, test, deliveries,
  delete;
* EDIT_CONFIG (+ the property's ``min_role``): integration-level overrides;
  suggestion review.

Public endpoints:
* ``POST /hooks/zendesk/{integration_id}/{secret}``: authenticated **before**
  any expensive work: size limit, path secret (constant time), then the
  mandatory HMAC signature and ±5-minute timestamp (D-Q10, kept
  ``webhook_auth``). Then rate limit, strict payload validation and durable
  per-integration de-duplication; processing happens after the 200 response.
* ``GET /hooks/zendesk/oauth/callback``: authority comes from the hashed
  single-use state and a fresh re-authorisation of the initiating user.
* ``POST /hooks/zendesk/sidebar/generate``: ``X-Sidebar-Key`` (hashed app key,
  scoped to one integration, rate-limited); nothing is posted to Zendesk.
"""
import hashlib
import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

import outbound_guard as og
import webhook_auth
from api.common import Services, load_access, oauth_result, translate
from core import audit, throttle
from core import config_registry as reg
from core import integrations as zi
from core import resolver
from core.authz import Action, Role
from core.config_registry import ConfigError
from core.credentials import (PLATFORM_COLLECTION, Provider, credential_to_doc, enterprise_credentials,
                              fingerprint, new_credential)
from core.hierarchy import ConcurrentModification, Level, NotFound, is_valid_id, new_id
from core.identity import User, get_user
from runtime import delivery as dlv
from runtime.zendesk import ZendeskClient, ZendeskError, authorize_url, client_for, exchange_code, pkce_pair
from runtime.zendesk_integration import EVENT_ID, EVENTS, SUGGESTIONS, TICKET_ID, ZendeskService

APP_KEYS = "zendesk_app_keys"
OAUTH_STATES = "oauth_states"
STATE_TTL = timedelta(minutes=10)
HOOK_RATE = (120, 60)                      # legacy limit: 120 deliveries per minute per integration
SIDEBAR_RATE = (30, 60)
APP_KEY_RE = re.compile(r"bbz_[A-Za-z0-9_\-]{43}")
ASSETS = Path(__file__).resolve().parent.parent / "zendesk_sidebar_app" / "assets"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntegrationCreate(_Body):
    type: str
    name: str
    settings: Dict[str, Any] = {}


class IntegrationPatch(_Body):
    name: Optional[str] = None
    status: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    credential_id: Optional[str] = None
    clear_credential: bool = False


class ConfigPatch(_Body):
    changes: Dict[str, Any]


class OAuthStart(_Body):
    subdomain: str


class SigningSecret(_Body):
    secret: str


class SidebarGenerate(BaseModel):
    ticket_id: Any


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def service_for(S: Services) -> ZendeskService:
    return ZendeskService(S.store, box=S.box, clock=S.clock, transport=S.zendesk_transport,
                          resolver_fn=S.zendesk_resolver, providers=S.providers, retriever=S.retriever,
                          catalog=S.catalog, platform_policy=S.platform_policy)


def redirect_uri(S: Services) -> str:
    return f"{S.settings.public_api_origin}/api/hooks/zendesk/oauth/callback"


def mapped(exc: ZendeskError) -> HTTPException:
    messages = {"unauthorized": "Zendesk rejected the credential. Reconnect the integration.",
                "reauth_required": "The Zendesk connection has expired. Reconnect the integration.",
                "forbidden": "The Zendesk user lacks permission for this.",
                "not_found": "Not found on Zendesk.",
                "blocked": "That Zendesk destination is not allowed.",
                "credential_unavailable": "No usable Zendesk credential is configured.",
                "rate_limited": "Zendesk is rate limiting requests. Try again shortly."}
    return HTTPException(429 if exc.category == "rate_limited" else (404 if exc.category == "not_found" else 422),
                         messages.get(exc.category, "Zendesk is not reachable right now."))


def register(router: APIRouter, S: Services, principal) -> None:
    svc = lambda: service_for(S)                                     # noqa: E731

    async def integ_access(user: User, integration_id: str, action: Action):
        doc = await S.store.get(zi.COLLECTION, integration_id) if is_valid_id(integration_id) else None
        if doc is None:
            raise NotFound("Not found.")
        a = await load_access(S, user, Level.BOT, doc["bot_id"], action)
        if a.chain.enterprise_id != doc["enterprise_id"] or a.chain.organisation.id != doc["organisation_id"]:
            raise NotFound("Not found.")
        return doc, a

    async def outbound_budget(user: User) -> None:
        """Admin-triggered outbound Zendesk calls are rate-limited per user."""
        if not (await throttle.hit(S.store, throttle.throttle_key("zd-admin-outbound", user.id), limit=60,
                                   window_seconds=3600, now=S.clock())).allowed:
            raise HTTPException(429, "Too many requests. Try again later.")

    async def record(a, action: str, integration_id: str, details: Dict[str, Any]) -> None:
        await audit.record(S.store, actor=a.actor, action=action, enterprise_id=a.chain.enterprise_id,
                           target_level="integration", target_id=integration_id, now=S.clock(), details=details)

    async def save(doc: dict, **fields) -> dict:
        fields["updated_at"] = S.clock()
        await S.store.update(zi.COLLECTION, doc["id"], fields, expect_version=doc["version"])
        return await S.store.get(zi.COLLECTION, doc["id"])

    def hook_url(doc: dict) -> str:
        secret = S.box.decrypt(doc["webhook"]["path_enc"])
        return f"{S.settings.public_api_origin}/api/hooks/zendesk/{doc['id']}/{secret}"

    def new_hook_secret() -> Dict[str, Any]:
        secret = secrets.token_urlsafe(32)
        return {"path_hash": _hash(secret), "path_enc": S.box.encrypt(secret)}

    # ═════════════════════ CRUD ═════════════════════
    @router.post("/bots/{bot_id}/integrations", status_code=201)
    @translate
    async def create_integration(bot_id: str, body: IntegrationCreate, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.CREATE_CHILD)
        if body.type not in zi.TYPES:
            raise HTTPException(422, "Unknown integration type.")
        allowed = a.policy.get("integrations.allowed")
        if allowed is not None and body.type not in allowed:
            raise HTTPException(422, "This integration type is not permitted by policy.")
        now = S.clock()
        doc = {"id": new_id(), "type": body.type, "name": zi.validate_name(body.name),
               "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id, "bot_id": bot_id,
               "status": "enabled", "settings": zi.validate_zendesk_settings(body.settings), "credential_id": None,
               "overrides": {}, "webhook": {**new_hook_secret(), "rotated_at": now}, "sidebar_key_hint": None,
               "connection": {"status": "not_connected"}, "created_by": user.id, "created_at": now,
               "updated_at": now, "version": 1}
        await S.store.insert(zi.COLLECTION, doc)
        await record(a, "integration.create", doc["id"], {"type": body.type})
        return zi.public_view(doc)

    @router.get("/bots/{bot_id}/integrations")
    @translate
    async def list_integrations(bot_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.READ)
        docs = await S.store.find(zi.COLLECTION, {"enterprise_id": a.chain.enterprise_id, "bot_id": bot_id},
                                  sort="created_at")
        return {"integrations": [zi.public_view(d) for d in docs]}

    @router.get("/integrations/{integration_id}")
    @translate
    async def get_integration(integration_id: str, user: User = Depends(principal)):
        doc, _ = await integ_access(user, integration_id, Action.READ)
        return zi.public_view(doc)

    @router.patch("/integrations/{integration_id}")
    @translate
    async def patch_integration(integration_id: str, body: IntegrationPatch, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        if doc.get("deletion"):
            raise HTTPException(409, "This integration is being deleted.")      # Z-G: no re-enable, no changes
        provisioned = ((doc.get("webhook") or {}).get("provision") or {}).get("status") == "active"
        if provisioned and (body.clear_credential or (body.credential_id is not None and
                                                      body.credential_id != doc.get("credential_id"))):
            # Z-C: never switch accounts under a provisioned webhook
            raise HTTPException(409, "A Zendesk webhook is provisioned with the current credential. Deprovision "
                                     "it first (or recover its owner credential for the same account).")
        fields: Dict[str, Any] = {}
        if body.name is not None:
            fields["name"] = zi.validate_name(body.name)
        if body.status is not None:
            if body.status not in ("enabled", "disabled"):
                raise HTTPException(422, "status must be enabled or disabled.")
            fields["status"] = body.status
        if body.settings is not None:
            fields["settings"] = zi.validate_zendesk_settings(body.settings, doc.get("settings"))
        if body.clear_credential:
            fields["credential_id"] = None
        elif body.credential_id is not None:
            creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
            from core.credentials import CredentialError, check_use
            try:                                                  # D1: ownership and usage scope
                check_use(creds.get(body.credential_id), referencing_chain=a.chain, slot="integration.zendesk",
                          policy=a.policy)
            except CredentialError as exc:
                raise HTTPException(422, str(exc))
            if not await can_use(a):
                raise HTTPException(403, "You do not have permission to do this.")
            fields["credential_id"] = body.credential_id
        if not fields:
            raise HTTPException(422, "Nothing to change.")
        new = await save(doc, **fields)
        await record(a, "integration.update", integration_id, {"fields": sorted(fields)})
        return zi.public_view(new)

    async def can_use(a) -> bool:
        from core.authz import can
        return can(a.actor, Action.USE_CREDENTIAL, a.chain, a.roster.members, a.policy).allowed

    @router.delete("/integrations/{integration_id}")
    @translate
    async def delete_integration(integration_id: str, user: User = Depends(principal)):
        """Z-G (approved): durable disable → mandatory cleanup → finalise. The
        request is recorded atomically on the integration (new work stops
        immediately), audited idempotently, and driven by this call and by the
        worker until done. A failed cleanup leaves the integration disabled,
        audited and retryable (call again or wait for the worker)."""
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        s_ = svc()
        await s_.request_deletion(integration_id, actor_id=user.id)
        state = await s_.advance_deletion(integration_id, force=True)
        if state["state"] == "deleted":
            return {"deleted": True}
        status = 502 if state["state"] in ("cleanup_failed", "cleanup_uncertain") else 202
        return JSONResponse({"deleted": False, "state": state["state"], "error": state.get("error"),
                             "attempts": state.get("attempts", 0),
                             "detail": "The integration is disabled; deletion completes when the Zendesk cleanup "
                                       "succeeds (retry, or the worker retries automatically)."},
                            status_code=status)

    # ═════════════════════ X2: integration-level configuration ═════════════════════
    @router.get("/integrations/{integration_id}/config")
    @translate
    async def get_config(integration_id: str, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.READ)
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        eff = resolver.resolve_integration(a.chain, doc, platform_policy=S.platform_policy, credentials=creds)
        names = sorted(reg.INTEGRATION_PROPERTIES)
        return {"overrides": dict(doc.get("overrides") or {}),
                "values": {n: _plain(eff.values.get(n)) for n in names},
                "sources": {n: eff.sources[n].to_dict() for n in names},
                "conflicts": [c.__dict__ for c in eff.conflicts if c.level == "integration"],
                "runtime_blockers": list(eff.runtime_blockers), "overridable": names}

    @router.patch("/integrations/{integration_id}/config")
    @translate
    async def patch_config(integration_id: str, body: ConfigPatch, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.EDIT_CONFIG)
        for key in body.changes:
            prop = reg.get_property(key)
            if not a.grant.superadmin and (a.grant.role is None or a.grant.role < Role[prop.min_role.upper()]):
                raise HTTPException(403, f"Changing {key} requires the {prop.min_role} role.")
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        if "knowledge.approved_sources" in body.changes:
            from api.knowledge import check_source_refs
            await check_source_refs(S, a.chain, body.changes["knowledge.approved_sources"])
        overrides = resolver.apply_integration_override_patch(a.chain, doc, body.changes,
                                                              platform_policy=S.platform_policy, credentials=creds)
        new = await save(doc, overrides=overrides)
        await record(a, "integration.config", integration_id, {"keys": sorted(body.changes)})
        return {"overrides": dict(new.get("overrides") or {})}

    # ═════════════════════ connection: OAuth (Z2) and test (Z5) ═════════════════════
    @router.post("/integrations/{integration_id}/zendesk/oauth/start")
    @translate
    async def oauth_start(integration_id: str, body: OAuthStart, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        if not await can_use(a):
            raise HTTPException(403, "You do not have permission to do this.")
        try:
            sub = og.normalize_zendesk_subdomain(body.subdomain)
        except og.OutboundBlocked as exc:
            raise HTTPException(422, str(exc))
        client = await S.store.get(PLATFORM_COLLECTION, "zendesk_oauth_client")
        if not client or client.get("status") != "active" or not (client.get("metadata") or {}).get("client_id"):
            raise HTTPException(409, "Zendesk OAuth is not configured on this platform. Use an API token instead.")
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        expires = datetime.fromisoformat(S.clock()) + STATE_TTL
        await S.store.insert(OAUTH_STATES, {
            "id": _hash(state), "kind": "zendesk_install", "user_id": user.id, "user_tv": user.token_version,
            "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id,
            "bot_id": a.chain.bot.id, "integration_id": integration_id, "subdomain": sub,
            "verifier_enc": S.box.encrypt(verifier), "client_fingerprint": client.get("fingerprint"),
            "expires_at": expires.isoformat(), "expires_at_dt": expires, "version": 1})
        await record(a, "integration.oauth_started", integration_id, {"subdomain": sub})
        return {"url": authorize_url(sub, client_id=client["metadata"]["client_id"], redirect_uri=redirect_uri(S),
                                     state=state, challenge=challenge), "expires_at": expires.isoformat()}

    def page(status: int, code: str, message: str, **extra):
        return oauth_result(S.settings, "zendesk", status, code, message, extra)

    @router.get("/hooks/zendesk/oauth/callback")
    async def oauth_callback(request: Request):
        q = request.query_params
        state, code = q.get("state") or "", q.get("code") or ""
        if not 20 <= len(state) <= 128:
            return page(400, "invalid_state", "This connection link is invalid or has expired.")
        rec = await S.store.take(OAUTH_STATES, _hash(state))          # single use, even on failure
        if not rec or rec.get("kind") != "zendesk_install" or \
                datetime.fromisoformat(S.clock()) >= datetime.fromisoformat(rec["expires_at"]):
            return page(400, "invalid_state", "This connection link is invalid or has expired.")
        if q.get("error"):
            return page(400, "cancelled", "The Zendesk authorisation was cancelled.")
        try:
            user = await get_user(S.store, rec["user_id"])
            if user is None or user.status != "active" or user.token_version != rec["user_tv"]:
                raise NotFound("gone")
            doc, a = await integ_access(user, rec["integration_id"], Action.MANAGE_CHANNEL)   # re-authorised now
            if not await can_use(a):
                raise NotFound("gone")
        except Exception:
            return page(403, "not_authorised", "You are no longer allowed to connect this integration.")
        if a.chain.enterprise_id != rec["enterprise_id"] or a.chain.bot.id != rec["bot_id"] or \
                not a.chain.availability().available:
            return page(403, "not_authorised", "You are no longer allowed to connect this integration.")
        if doc.get("deletion"):
            return page(409, "integration_deleting", "This integration is being deleted.")
        client = await S.store.get(PLATFORM_COLLECTION, "zendesk_oauth_client")
        if not client or client.get("status") != "active" or client.get("fingerprint") != rec["client_fingerprint"]:
            return page(409, "client_changed", "The platform Zendesk OAuth client changed. Start again.")
        sub = rec["subdomain"]
        try:
            tokens = await exchange_code(sub, client_id=client["metadata"]["client_id"],
                                         client_secret=S.box.decrypt(client["secret_enc"]), code=code,
                                         redirect_uri=redirect_uri(S), verifier=S.box.decrypt(rec["verifier_enc"]),
                                         transport=S.zendesk_transport, resolver=S.zendesk_resolver)
            me = await ZendeskClient(sub, bearer=tokens["access_token"], transport=S.zendesk_transport,
                                     resolver=S.zendesk_resolver).me()
        except ZendeskError:
            return page(400, "oauth_failed", "Zendesk did not confirm the authorisation. Start again.")
        # account confusion: the token must belong to the account the user started with
        me_host = urlsplit(str(me.get("url") or "")).hostname
        if me_host != og.zendesk_host(sub) or me.get("role") not in ("admin", "agent"):
            return page(403, "account_mismatch", "That Zendesk account cannot be used for this integration.")
        from runtime.zendesk import oauth_secret
        org_chain = await S.hierarchy.load_chain(Level.ORGANISATION, a.chain.organisation.id)
        cred = new_credential(credential_id=new_id(), chain=org_chain, provider=Provider.ZENDESK_OAUTH,
                              name=f"Zendesk {sub} ({doc['name']})"[:120], secret=oauth_secret(tokens, S.clock()),
                              box=S.box, actor_id=user.id, now=S.clock(),
                              metadata={"subdomain": sub, "zendesk_user_id": str(me["id"]),
                                        "integration_id": doc["id"]})
        await S.store.insert("credentials", credential_to_doc(cred, 1))
        previous = doc.get("credential_id")
        try:
            await save(doc, credential_id=cred.id, connection={"status": "ok", "subdomain": sub,
                                                               "checked_at": S.clock()})
        except ConcurrentModification:
            await S.store.update("credentials", cred.id, {"status": "revoked", "secret_enc": ""})
            return page(409, "changed", "The integration changed during authorisation. Start again.")
        if previous:
            old = await S.store.get("credentials", previous)
            if old and old.get("provider") == "zendesk_oauth" and (old.get("metadata") or {}).get("integration_id") \
                    == doc["id"] and old.get("status") == "active":
                try:                                              # rotation: the replaced token is revoked
                    await client_for(old, S.box, transport=S.zendesk_transport,
                                     resolver=S.zendesk_resolver).revoke_current_token()
                except ZendeskError:
                    pass
                await S.store.update("credentials", previous, {"status": "revoked", "secret_enc": "",
                                                               "rotated_at": S.clock()})
        await record(a, "integration.oauth_connected", doc["id"], {"subdomain": sub, "credential_id": cred.id})
        return page(200, "connected", "Zendesk connected.", integration_id=doc["id"], subdomain=sub)

    @router.post("/integrations/{integration_id}/test")
    @translate
    async def test_integration(integration_id: str, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        await outbound_budget(user)
        s = svc()
        try:
            integ, chain, eff, creds = await s.context(integration_id)
            me = await (await s.client(chain, integ, eff, creds))[0].me()
        except ZendeskError as exc:
            await s._connection(integration_id, status="reauth_required" if exc.category in
                                ("unauthorized", "reauth_required") else "error", error=exc.category)
            raise mapped(exc)
        await s._connection(integration_id, status="ok")
        return {"ok": True, "zendesk_role": str(me.get("role") or "")[:20]}

    # ═════════════════════ webhook (Z7, D-Q10) ═════════════════════
    @router.get("/integrations/{integration_id}/zendesk/webhook")
    @translate
    async def webhook_info(integration_id: str, user: User = Depends(principal)):
        doc, _ = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        hook = doc.get("webhook") or {}
        return {"url": hook_url(doc), "signing_secret_configured": bool(hook.get("signing_secret_enc")),
                "provisioned_webhook_id": hook.get("zendesk_webhook_id"), "signature_required": True}

    @router.put("/integrations/{integration_id}/zendesk/signing-secret")
    @translate
    async def set_signing_secret(integration_id: str, body: SigningSecret, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        if not 16 <= len(body.secret) <= 256 or any(c.isspace() for c in body.secret):
            raise HTTPException(422, "That is not a valid Zendesk signing secret.")
        hook = {**(doc.get("webhook") or {}), "signing_secret_enc": S.box.encrypt(body.secret),
                "signing_fp": fingerprint(body.secret), "rotated_at": S.clock()}
        await save(doc, webhook=hook)
        await record(a, "integration.signing_secret", integration_id, {})
        return {"signing_secret_configured": True}

    @router.post("/integrations/{integration_id}/rotate-webhook-secret")
    @translate
    async def rotate_webhook_secret(integration_id: str, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        hook = {**(doc.get("webhook") or {}), **new_hook_secret(), "rotated_at": S.clock()}
        new = await save(doc, webhook=hook)
        if hook.get("zendesk_webhook_id"):
            try:                                                  # keep the provisioned webhook pointing at us
                client = await owner_client(new)
                async with svc().fenced(integration_id):          # Z-G: never after a deletion transition
                    current = await client.get_webhook(hook["zendesk_webhook_id"])
                if f"/api/hooks/zendesk/{integration_id}/" not in str(current.get("endpoint") or ""):
                    raise ZendeskError("ownership_mismatch")
                async with svc().fenced(integration_id):
                    await client.call("PATCH", f"/api/v2/webhooks/{hook['zendesk_webhook_id']}",
                                      {"webhook": {"endpoint": hook_url(new)}})
            except ZendeskError:
                await save(new, connection={**(new.get("connection") or {}), "status": "webhook_update_failed"})
        await record(a, "integration.rotate_webhook_secret", integration_id, {})
        return {"url": hook_url(await S.store.get(zi.COLLECTION, integration_id))}

    async def owner_client(doc: dict):
        """Z-C: the client for the *provisioning* credential and account only."""
        prov = (doc.get("webhook") or {}).get("provision") or {}
        cred = await S.store.get("credentials", prov.get("credential_id") or "")
        if not cred or cred.get("enterprise_id") != doc["enterprise_id"] or \
                (cred.get("metadata") or {}).get("subdomain") != prov.get("subdomain"):
            raise ZendeskError("credential_unavailable")
        return client_for(cred, S.box, transport=S.zendesk_transport, resolver=S.zendesk_resolver)

    @router.post("/integrations/{integration_id}/zendesk/webhook/deprovision")
    @translate
    async def deprovision_webhook(integration_id: str, user: User = Depends(principal)):
        """Removes the webhook this integration provisioned (owner credential and
        account only, after confirming it still points here). Afterwards the
        credential may be changed; webhooks are refused until a new signing
        secret is configured."""
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        if doc.get("deletion"):
            raise HTTPException(409, "This integration is being deleted.")
        hook = doc.get("webhook") or {}
        hid = hook.get("zendesk_webhook_id")
        if not hid:
            raise HTTPException(409, "No Zendesk webhook is provisioned for this integration.")
        result, category = await svc()._remove_webhook(doc)
        if result != "removed":
            await record(a, "integration.webhook_deprovision_failed", integration_id, {"error": category})
            raise HTTPException(502, "The Zendesk webhook could not be removed safely. Recover the owner "
                                     "credential or retry.")
        new_hook = {k: v for k, v in hook.items() if k not in ("zendesk_webhook_id", "provision", "signing_secret_enc",
                                                               "signing_fp")}
        await save(doc, webhook=new_hook)
        await record(a, "integration.webhook_deprovisioned", integration_id, {"zendesk_webhook_id": hid})
        return {"provisioned": False}

    class OwnerCredential(_Body):
        credential_id: str

    @router.put("/integrations/{integration_id}/zendesk/webhook/owner-credential")
    @translate
    async def recover_owner_credential(integration_id: str, body: OwnerCredential, user: User = Depends(principal)):
        """Recovery when the provisioning credential was revoked: a usable
        credential for the *same* Zendesk account that can see the provisioned
        webhook (still pointing here) becomes its owner. Never another account."""
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        prov = (doc.get("webhook") or {}).get("provision") or {}
        if not prov:
            raise HTTPException(409, "No Zendesk webhook is provisioned for this integration.")
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        from core.credentials import CredentialError, check_use
        try:
            check_use(creds.get(body.credential_id), referencing_chain=a.chain, slot="integration.zendesk",
                      policy=a.policy)
        except CredentialError as exc:
            raise HTTPException(422, str(exc))
        cred = await S.store.get("credentials", body.credential_id)
        if (cred.get("metadata") or {}).get("subdomain") != prov.get("subdomain"):
            raise HTTPException(422, "The credential belongs to a different Zendesk account.")
        try:
            current = await client_for(cred, S.box, transport=S.zendesk_transport,
                                       resolver=S.zendesk_resolver).get_webhook(prov["webhook_id"])
        except ZendeskError as exc:
            raise mapped(exc)
        if f"/api/hooks/zendesk/{integration_id}/" not in str(current.get("endpoint") or ""):
            raise HTTPException(409, "That webhook no longer points at this integration.")
        hook = {**doc["webhook"], "provision": {**prov, "credential_id": body.credential_id,
                                                "recovered_at": S.clock(), "recovered_by": user.id}}
        await save(doc, webhook=hook)
        await record(a, "integration.webhook_owner_recovered", integration_id, {"credential_id": body.credential_id})
        return {"provisioned": True, "owner_credential_id": body.credential_id}

    # ── Part C: generation review ──
    @router.get("/integrations/{integration_id}/events")
    @translate
    async def list_events(integration_id: str, status: str = "", user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        flt = {"enterprise_id": a.chain.enterprise_id, "integration_id": integration_id}
        if status:
            if status not in ("received", "processing", "generated", "done", "uncertain", "failed"):
                raise HTTPException(422, "Unknown status.")
            flt["status"] = status
        docs = await S.store.find(EVENTS, flt, sort="received_at", descending=True, limit=200)
        keys = ("id", "ticket_id", "status", "label", "attempts", "error", "admin_issue", "generation",
                "received_at", "updated_at", "resolution")
        return {"events": [{k: d.get(k) for k in keys} for d in docs]}

    class EventResolution(_Body):
        decision: str
        note: str

    @router.post("/integrations/{integration_id}/events/{event_id}/resolve")
    @translate
    async def resolve_event(integration_id: str, event_id: str, body: EventResolution,
                            user: User = Depends(principal)):
        """An uncertain generation is resolved explicitly: ``charge`` (counts as
        billed) or ``waive``. It is never regenerated and nothing is posted."""
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        if body.decision not in ("charge", "waive") or not body.note.strip() or len(body.note) > 500:
            raise HTTPException(422, "decision must be charge or waive, with a note (1-500 characters).")
        ev = await svc().resolve_event(event_id, integration_id=integration_id, enterprise_id=a.chain.enterprise_id,
                                       decision=body.decision, actor_id=user.id)
        await record(a, "integration.event_resolved", integration_id, {"event_id": event_id,
                                                                       "decision": body.decision})
        return {"id": ev["id"], "status": ev["status"], "generation": ev.get("generation")}

    @router.post("/integrations/{integration_id}/zendesk/webhook/provision")
    @translate
    async def provision_webhook(integration_id: str, user: User = Depends(principal)):
        """Creates the Zendesk webhook pointing at this integration and stores
        Zendesk's signing secret (no copy/paste of secrets)."""
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        await outbound_budget(user)
        if (doc.get("webhook") or {}).get("zendesk_webhook_id"):
            raise HTTPException(409, "A Zendesk webhook is already provisioned for this integration.")
        if doc.get("deletion"):
            raise HTTPException(409, "This integration is being deleted.")
        s = svc()
        try:
            integ, chain, eff, creds = await s.context(integration_id)
            client, cred_doc = await s.client(chain, integ, eff, creds)
            hook_id = await client.create_webhook(f"BitBotPro {doc['name']}", hook_url(doc))
            secret = await client.webhook_signing_secret(hook_id)
        except ZendeskError as exc:
            raise mapped(exc)
        # Z-C: authoritative ownership of the external resource
        provision = {"enterprise_id": doc["enterprise_id"], "organisation_id": doc["organisation_id"],
                     "integration_id": doc["id"], "subdomain": (cred_doc.get("metadata") or {}).get("subdomain"),
                     "credential_id": cred_doc["id"], "webhook_id": hook_id, "status": "active",
                     "provisioned_at": S.clock(), "provisioned_by": user.id}
        hook = {**(doc.get("webhook") or {}), "zendesk_webhook_id": hook_id, "provision": provision,
                "signing_secret_enc": S.box.encrypt(secret), "signing_fp": fingerprint(secret),
                "rotated_at": S.clock()}
        await save(await S.store.get(zi.COLLECTION, integration_id), webhook=hook)
        await record(a, "integration.webhook_provisioned", integration_id, {"zendesk_webhook_id": hook_id})
        return {"provisioned_webhook_id": hook_id, "signing_secret_configured": True}

    # registered before the generic hook route so it is never shadowed by it
    @router.post("/hooks/zendesk/sidebar/generate")
    async def sidebar_generate(body: SidebarGenerate, request: Request):
        unauthorised = JSONResponse({"detail": "Invalid sidebar API key."}, status_code=401)
        raw = (request.headers.get("x-sidebar-key") or "").strip()
        if not APP_KEY_RE.fullmatch(raw):
            return unauthorised
        key = await S.store.get(APP_KEYS, _hash(raw))
        doc = await S.store.get(zi.COLLECTION, key["integration_id"]) if key else None
        if not key or not doc or doc["enterprise_id"] != key["enterprise_id"]:
            return unauthorised
        if not (await throttle.hit(S.store, throttle.throttle_key("zd-sidebar", doc["id"]), limit=SIDEBAR_RATE[0],
                                   window_seconds=SIDEBAR_RATE[1], now=S.clock())).allowed:
            return JSONResponse({"detail": "Too many requests."}, status_code=429)
        ticket_id = str(body.ticket_id or "").strip()
        if not TICKET_ID.fullmatch(ticket_id):
            return JSONResponse({"detail": "A numeric ticket_id is required."}, status_code=400)
        try:
            return await svc().sidebar_generate(doc, ticket_id)
        except ZendeskError as exc:
            status = {"not_found": 404, "rate_limited": 429, "quota_exceeded": 429, "busy": 503,
                      "unavailable": 409, "silent": 409}.get(exc.category, 502)
            return JSONResponse({"detail": {404: "Ticket not found.", 429: "Too many requests or usage limit reached.",
                                            409: "This integration is not available.",
                                            503: "The AI service is busy. Try again."}.get(
                                                status, "Zendesk is not reachable right now.")}, status_code=status)
        except Exception:
            return JSONResponse({"detail": "This integration is not available."}, status_code=409)

    @router.post("/hooks/zendesk/{integration_id}/{secret}")
    async def zendesk_hook(integration_id: str, secret: str, request: Request, background: BackgroundTasks):
        nf = JSONResponse({"detail": "Not found."}, status_code=404)
        try:
            if int(request.headers.get("content-length") or 0) > webhook_auth.MAX_WEBHOOK_BODY_BYTES:
                return JSONResponse({"detail": "Payload too large."}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Bad request."}, status_code=400)
        if not is_valid_id(integration_id) or not 20 <= len(secret) <= 128:
            return nf
        doc = await S.store.get(zi.COLLECTION, integration_id)
        hook = (doc or {}).get("webhook") or {}
        import hmac
        if not doc or doc.get("type") != "zendesk" or not hook.get("path_hash") or \
                not hmac.compare_digest(_hash(secret), hook["path_hash"]):
            return nf
        if not hook.get("signing_secret_enc"):
            return nf                                              # D-Q10: no signature configured ⇒ refuse
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > webhook_auth.MAX_WEBHOOK_BODY_BYTES:
                return JSONResponse({"detail": "Payload too large."}, status_code=413)
        try:
            signing = S.box.decrypt(hook["signing_secret_enc"])
        except Exception:
            return nf
        if not webhook_auth.zendesk_signature_valid(
                signing, request.headers.get(webhook_auth.ZENDESK_TIMESTAMP_HEADER), bytes(body),
                request.headers.get(webhook_auth.ZENDESK_SIGNATURE_HEADER),
                now=datetime.fromisoformat(S.clock())):
            return nf                                              # forged or replayed outside the window
        if not (await throttle.hit(S.store, throttle.throttle_key("zd-hook", integration_id), limit=HOOK_RATE[0],
                                   window_seconds=HOOK_RATE[1], now=S.clock())).allowed:
            return JSONResponse({"detail": "Too many requests."}, status_code=429)
        try:
            event = json.loads(bytes(body))
        except ValueError:
            return JSONResponse({"detail": "Bad request."}, status_code=400)
        if not isinstance(event, dict):
            return JSONResponse({"detail": "Bad request."}, status_code=400)
        ticket_id = str(event.get("ticket_id") or event.get("id") or "")
        if not TICKET_ID.fullmatch(ticket_id):
            return JSONResponse({"detail": "A numeric ticket_id is required."}, status_code=400)
        event_id = str(event.get("event_id") or f"ticket:{ticket_id}:created")
        if not EVENT_ID.fullmatch(event_id):
            return JSONResponse({"detail": "Invalid event_id."}, status_code=400)
        from runtime.zendesk_integration import active
        if not active(doc):
            return {"ok": True, "skipped": "disabled"}                   # disabled or being deleted (Z-G)
        s = svc()
        if not await s.accept(doc, event_id=event_id, ticket_id=ticket_id):
            return {"ok": True, "duplicate": True}
        from runtime.zendesk_integration import event_doc_id
        background.add_task(s.process, event_doc_id(integration_id, event_id))
        return {"ok": True, "accepted": True}

    # ═════════════════════ sidebar app and app keys (Z16, D9, D10) ═════════════════════
    @router.post("/integrations/{integration_id}/sidebar-key")
    @translate
    async def rotate_sidebar_key(integration_id: str, user: User = Depends(principal)):
        """Creates (or replaces) the integration's sidebar app key. The key is
        returned only now; it is stored hashed and scoped to this integration."""
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        key = "bbz_" + secrets.token_urlsafe(32)
        await S.store.delete_many(APP_KEYS, {"enterprise_id": doc["enterprise_id"], "integration_id": integration_id})
        await S.store.insert(APP_KEYS, {"id": _hash(key), "enterprise_id": doc["enterprise_id"],
                                        "organisation_id": doc["organisation_id"], "bot_id": doc["bot_id"],
                                        "integration_id": integration_id, "hint": key[-4:], "created_by": user.id,
                                        "created_at": S.clock(), "version": 1})
        await save(doc, sidebar_key_hint=key[-4:])
        await record(a, "integration.sidebar_key_rotated", integration_id, {})
        return {"sidebar_key": key, "hint": key[-4:],
                "message": "Copy this key now; it will not be shown again."}

    @router.delete("/integrations/{integration_id}/sidebar-key")
    @translate
    async def revoke_sidebar_key(integration_id: str, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        await S.store.delete_many(APP_KEYS, {"enterprise_id": doc["enterprise_id"], "integration_id": integration_id})
        await save(doc, sidebar_key_hint=None)
        await record(a, "integration.sidebar_key_revoked", integration_id, {})
        return {"configured": False}

    @router.get("/integrations/{integration_id}/zendesk/sidebar-app.zip")
    @translate
    async def sidebar_zip(integration_id: str, user: User = Depends(principal)):
        await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        api_host = urlsplit(S.settings.public_api_origin).hostname
        manifest = {"name": "BitBot Pro — AI Agent Assist", "author": {"name": "BitBot Pro",
                                                                         "email": "support@bitbotpro.io"},
                    "defaultLocale": "en", "private": True,
                    "location": {"support": {"ticket_sidebar": {"url": "assets/iframe.html", "flexible": True}}},
                    "version": "1.1.0", "frameworkVersion": "2.0", "domainWhitelist": [api_host],
                    "parameters": [{"name": "bitbot_url", "type": "text", "required": True, "secure": False,
                                    "default": S.settings.public_api_origin},
                                   {"name": "sidebar_api_key", "type": "text", "required": True, "secure": True,
                                    "default": ""}]}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name in ("iframe.html", "main.js", "style.css"):
                path = ASSETS / name
                if path.is_file():
                    text = path.read_text(encoding="utf-8")
                    if name == "main.js":                         # the new public route (spec §11)
                        text = text.replace("/api/zendesk/sidebar/generate", "/api/hooks/zendesk/sidebar/generate")
                    zf.writestr(f"assets/{name}", text)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=bitbot-pro-zendesk-sidebar.zip",
                                 "Cache-Control": "no-store"})

    # ═════════════════════ suggestions (Z14/Z15) and deliveries ═════════════════════
    @router.get("/integrations/{integration_id}/suggestions")
    @translate
    async def list_suggestions(integration_id: str, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.READ)
        docs = await S.store.find(SUGGESTIONS, {"enterprise_id": a.chain.enterprise_id,
                                                "integration_id": integration_id},
                                  sort="created_at", descending=True, limit=100)
        keys = ("id", "ticket_id", "subject", "text", "kind", "status", "source", "action", "citations",
                "created_at", "reviewed_at")
        return {"suggestions": [{k: d.get(k) for k in keys} for d in docs]}

    @router.post("/integrations/{integration_id}/suggestions/{suggestion_id}/{decision}")
    @translate
    async def review_suggestion(integration_id: str, suggestion_id: str, decision: str,
                                user: User = Depends(principal)):
        """Z15: accept/dismiss record a status only (posting on accept is not a
        legacy feature and is not added)."""
        if decision not in ("accept", "dismiss"):
            raise NotFound("Not found.")
        doc, a = await integ_access(user, integration_id, Action.EDIT_CONFIG)
        sug = await S.store.get(SUGGESTIONS, suggestion_id)
        if not sug or sug.get("integration_id") != integration_id or sug.get("enterprise_id") != a.chain.enterprise_id:
            raise NotFound("Not found.")
        if sug.get("status") != "pending":
            raise HTTPException(409, "This suggestion has already been reviewed or has no content.")
        await S.store.update(SUGGESTIONS, suggestion_id, {"status": "accepted" if decision == "accept" else "dismissed",
                                                          "reviewed_at": S.clock(), "reviewed_by": user.id},
                             expect_version=sug["version"])
        await record(a, f"suggestion.{decision}", integration_id, {"suggestion_id": suggestion_id})
        return {"id": suggestion_id, "status": "accepted" if decision == "accept" else "dismissed"}

    @router.get("/integrations/{integration_id}/deliveries")
    @translate
    async def list_deliveries(integration_id: str, user: User = Depends(principal)):
        doc, a = await integ_access(user, integration_id, Action.MANAGE_CHANNEL)
        docs = await S.store.find(dlv.COLLECTION, {"enterprise_id": a.chain.enterprise_id,
                                                   "integration_id": integration_id},
                                  sort="created_at", descending=True, limit=200)
        return {"deliveries": [dlv.public_view(d) for d in docs]}


def _plain(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "value"):
        return value.value
    return value


__all__ = ["register", "service_for", "redirect_uri"]
