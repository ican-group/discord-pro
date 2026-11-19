"""Credentials and platform credentials (spec §6, D-Q9).

Secrets are write-only: requests may carry a secret, responses only ever
contain ``public_view()`` (id, name, provider, owner, fingerprint, status,
rotated_at, non-secret metadata). Managing needs Admin on the owning scope
(subject to delegation policy); platform credentials are superadmin-only and
invisible (404) to everyone else.
"""

from typing import Any, Dict, List, Optional

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.common import Services, load_access, require_superadmin, translate
from core import audit
from core.authz import Action
from core.credentials import (COLLECTION, PLATFORM_COLLECTION, CredentialError, CredentialStatus,
                              PlatformCredentialKind, Provider, check_manage, credential_to_doc, fingerprint,
                              load_credential, new_credential, normalise_usable_by, replace_secret, revoke,
                              telegram_bot_id)
from core.hierarchy import Level, NotFound, new_id
from core.identity import User

OWNER_TYPES = {"enterprises": Level.ENTERPRISE, "organisations": Level.ORGANISATION}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeRef(_Body):
    level: str                      # "organisation" | "bot"
    id: str


class CredentialCreate(_Body):
    provider: str
    name: str
    secret: str
    metadata: Dict[str, str] = {}
    usable_by: List[ScopeRef] = []                 # X6: empty = whole owner subtree


class CredentialScopePut(_Body):
    usable_by: List[ScopeRef]


class CredentialPatch(_Body):
    name: Optional[str] = None
    secret: Optional[str] = None


class PlatformCredentialPut(_Body):
    secret: str
    metadata: Dict[str, str] = {}


def _provider(value: str) -> Provider:
    try:
        return Provider(value)
    except ValueError:
        raise CredentialError("Unknown credential provider.")


def _metadata(provider: Provider, secret: str, metadata: Dict[str, str]) -> Dict[str, str]:
    meta = {k: v for k, v in (metadata or {}).items() if k != "telegram_bot_id"}   # server-derived only
    if provider is Provider.TELEGRAM_BOT:
        meta["telegram_bot_id"] = telegram_bot_id(secret)
    if provider is Provider.ZENDESK_OAUTH:
        # D-Q9: OAuth tokens come only from the platform OAuth client flow
        raise CredentialError("Zendesk OAuth credentials are created by connecting an integration.")
    if provider is Provider.ZENDESK_API_TOKEN:
        import outbound_guard as og
        try:
            sub = og.normalize_zendesk_subdomain(meta.get("subdomain", ""))
        except og.OutboundBlocked as exc:
            raise CredentialError(str(exc))
        email = (meta.get("email") or "").strip()
        if len(email) > 254 or not re.fullmatch(r"[^@\s/:]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email):
            raise CredentialError("A Zendesk API token needs the agent's email address.")
        if not re.fullmatch(r"[A-Za-z0-9]{20,100}", secret or ""):
            raise CredentialError("That is not a valid Zendesk API token.")
        meta = {"subdomain": sub, "email": email}
    return meta


def register(router: APIRouter, S: Services, principal) -> None:
    async def owner_access(user: User, cred, action: Optional[Action]):
        level = Level.ORGANISATION if cred.organisation_id else Level.ENTERPRISE
        a = await load_access(S, user, level, cred.organisation_id or cred.enterprise_id, action)
        if a.chain.enterprise_id != cred.enterprise_id:
            raise NotFound("Not found.")
        return a

    async def load(credential_id: str):
        loaded = await load_credential(S.store, credential_id)
        if loaded is None:
            raise NotFound("Not found.")
        return loaded

    async def ensure_not_held(credential_id: str) -> None:
        from core.deletion import unresolved_credential_holds
        if await unresolved_credential_holds(S.store, credential_id):
            raise HTTPException(409, "This credential is still needed to finish cleaning up a deleted object.")

    async def record(a, action: str, cred_id: str, details: Dict[str, Any]):
        await audit.record(S.store, actor=a.actor, action=action, enterprise_id=a.chain.enterprise_id,
                           target_level="credential", target_id=cred_id, now=S.clock(), details=details)

    @router.get("/{owner_type:ownertype}/{owner_id}/credentials")
    @translate
    async def list_credentials(owner_type: str, owner_id: str, user: User = Depends(principal)):
        if owner_type not in OWNER_TYPES:
            raise HTTPException(404, "Not found.")
        level = OWNER_TYPES[owner_type]
        a = await load_access(S, user, level, owner_id)
        flt = {"enterprise_id": a.chain.enterprise_id,
               "organisation_id": owner_id if level is Level.ORGANISATION else None}
        from core.credentials import credential_from_doc
        docs = await S.store.find(COLLECTION, flt, sort="created_at")
        return {"credentials": [credential_from_doc(d).public_view() for d in docs]}

    @router.post("/{owner_type:ownertype}/{owner_id}/credentials", status_code=201)
    @translate
    async def create_credential(owner_type: str, owner_id: str, body: CredentialCreate,
                                user: User = Depends(principal)):
        if owner_type not in OWNER_TYPES:
            raise HTTPException(404, "Not found.")
        a = await load_access(S, user, OWNER_TYPES[owner_type], owner_id)
        check_manage(a.actor, a.chain, a.roster.members, a.policy)
        provider = _provider(body.provider)
        org_id = owner_id if a.target.level is Level.ORGANISATION else None
        scope = await normalise_usable_by(S.store, enterprise_id=a.chain.enterprise_id, organisation_id=org_id,
                                          entries=[e.model_dump() for e in body.usable_by])
        meta = _metadata(provider, body.secret, body.metadata)
        if provider is Provider.ZENDESK_API_TOKEN and S.settings.zendesk_validate_on_save:
            from runtime.zendesk import ZendeskClient, ZendeskError
            try:                                                   # Z4/Z5: the token works on that account
                await ZendeskClient(meta["subdomain"], email=meta["email"], api_token=body.secret,
                                    transport=S.zendesk_transport, resolver=S.zendesk_resolver).me()
            except ZendeskError:
                raise CredentialError("Zendesk did not accept this API token for that subdomain.")
        if provider is Provider.TELEGRAM_BOT and S.settings.telegram_validate_on_save:
            from runtime.telegram import TelegramClient, TelegramError
            try:                                                   # T1: getMe on save, same bot identity
                me = await TelegramClient(body.secret, transport=S.telegram_transport).get_me()
            except TelegramError:
                raise CredentialError("Telegram did not accept this bot token.")
            if str(me["id"]) != meta["telegram_bot_id"]:
                raise CredentialError("This is not a valid Telegram bot token.")
        cred = new_credential(credential_id=new_id(), chain=a.chain, provider=provider, name=body.name,
                              secret=body.secret, box=S.box, actor_id=user.id, now=S.clock(),
                              metadata=meta, usable_by=scope)
        await S.store.insert(COLLECTION, credential_to_doc(cred, 1))
        await record(a, "credential.create", cred.id, {"provider": provider.value, "scoped": bool(scope)})
        return cred.public_view()

    @router.get("/credentials/{credential_id}")
    @translate
    async def get_credential(credential_id: str, user: User = Depends(principal)):
        cred, _ = await load(credential_id)
        await owner_access(user, cred, Action.READ)
        return cred.public_view()

    @router.patch("/credentials/{credential_id}")
    @translate
    async def patch_credential(credential_id: str, body: CredentialPatch, user: User = Depends(principal)):
        cred, version = await load(credential_id)
        a = await owner_access(user, cred, None)
        check_manage(a.actor, a.chain, a.roster.members, a.policy)
        fields: Dict[str, Any] = {}
        if body.name is not None:
            if not body.name.strip() or len(body.name) > 120:
                raise CredentialError("A credential name (1-120 characters) is required.")
            fields["name"] = body.name.strip()
        if body.secret is not None:
            if cred.provider is Provider.TELEGRAM_BOT and \
                    telegram_bot_id(body.secret) != cred.metadata.get("telegram_bot_id"):
                raise CredentialError("A replacement token must belong to the same Telegram bot.")
            new = replace_secret(cred, secret=body.secret, box=S.box, now=S.clock())
            fields.update(secret_enc=new.secret_enc, fingerprint=new.fingerprint, rotated_at=new.rotated_at)
        if not fields:
            raise CredentialError("Nothing to change.")
        await S.store.update(COLLECTION, credential_id, fields, expect_version=version)
        await record(a, "credential.rotate" if body.secret is not None else "credential.rename", credential_id, {})
        return (await load(credential_id))[0].public_view()

    @router.put("/credentials/{credential_id}/scope")
    @translate
    async def set_credential_scope(credential_id: str, body: CredentialScopePut, user: User = Depends(principal)):
        """X6: restrict (or widen within the owner subtree) where a credential may be used.
        Existing references outside the new scope fail closed at resolution time."""
        cred, version = await load(credential_id)
        a = await owner_access(user, cred, None)
        check_manage(a.actor, a.chain, a.roster.members, a.policy)
        scope = await normalise_usable_by(S.store, enterprise_id=cred.enterprise_id,
                                          organisation_id=cred.organisation_id,
                                          entries=[e.model_dump() for e in body.usable_by])
        await S.store.update(COLLECTION, credential_id,
                             {"usable_by": [{"level": lvl, "id": oid} for lvl, oid in scope]},
                             expect_version=version)
        await record(a, "credential.scope", credential_id, {"entries": len(scope)})
        return (await load(credential_id))[0].public_view()

    @router.post("/credentials/{credential_id}/revoke")
    @translate
    async def revoke_credential(credential_id: str, user: User = Depends(principal)):
        cred, version = await load(credential_id)
        a = await owner_access(user, cred, None)
        check_manage(a.actor, a.chain, a.roster.members, a.policy)
        if cred.status is CredentialStatus.REVOKED:
            return cred.public_view()
        await ensure_not_held(credential_id)
        if cred.provider is Provider.ZENDESK_OAUTH:
            # Z6: revoke at Zendesk while we still hold the token (best effort; a deletion
            # job's mandatory revocation action covers Enterprise/Organisation deletion)
            from runtime.zendesk import ZendeskError, client_for
            try:
                doc = await S.store.get(COLLECTION, credential_id)
                await client_for(doc, S.box, transport=S.zendesk_transport,
                                 resolver=S.zendesk_resolver).revoke_current_token()
            except ZendeskError:
                pass
        if cred.provider is Provider.TELEGRAM_BOT:
            # remove webhooks while the token still works (best effort; recorded on the instance)
            from api.telegram import channel_for
            channel = channel_for(S)
            for doc in await S.store.find("bot_instances", {"enterprise_id": cred.enterprise_id,
                                                            "channel.credential_id": credential_id}):
                if (doc.get("telegram_webhook") or {}).get("registered"):
                    try:
                        await channel._save(doc["id"], {"desired": False})
                        await channel.sync(doc["id"])
                    except Exception:
                        pass
        new = revoke(cred, now=S.clock())
        await S.store.update(COLLECTION, credential_id, {"status": new.status.value, "secret_enc": "",
                                                         "rotated_at": new.rotated_at}, expect_version=version)
        await record(a, "credential.revoke", credential_id, {})
        return (await load(credential_id))[0].public_view()

    @router.delete("/credentials/{credential_id}")
    @translate
    async def delete_credential(credential_id: str, user: User = Depends(principal)):
        cred, version = await load(credential_id)
        a = await owner_access(user, cred, None)
        check_manage(a.actor, a.chain, a.roster.members, a.policy)
        await ensure_not_held(credential_id)
        if not await S.store.delete(COLLECTION, credential_id, expect_version=version):
            raise HTTPException(409, "This credential was changed by another request. Reload and try again.")
        await record(a, "credential.delete", credential_id, {})
        return {"status": "deleted"}

    # ── platform credentials (superadmin only) ──
    def _platform_view(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {"kind": doc["id"], "fingerprint": doc.get("fingerprint"), "status": doc.get("status"),
                "rotated_at": doc.get("rotated_at"), "metadata": dict(doc.get("metadata") or {})}

    def _kind(kind: str) -> PlatformCredentialKind:
        try:
            return PlatformCredentialKind(kind)
        except ValueError:
            raise NotFound("Not found.")

    @router.get("/platform/credentials")
    @translate
    async def list_platform_credentials(user: User = Depends(principal)):
        require_superadmin(user)
        return {"credentials": [_platform_view(d) for d in await S.store.find(PLATFORM_COLLECTION, {})]}

    @router.put("/platform/credentials/{kind}")
    @translate
    async def put_platform_credential(kind: str, body: PlatformCredentialPut, user: User = Depends(principal)):
        require_superadmin(user)
        k = _kind(kind)
        if k is PlatformCredentialKind.ZENDESK_OAUTH_CLIENT and \
                not re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", (body.metadata or {}).get("client_id", "")):
            raise HTTPException(422, "The Zendesk OAuth client needs its client_id (unique identifier).")
        fields = {"secret_enc": S.box.encrypt(body.secret), "fingerprint": fingerprint(body.secret),
                  "status": "active", "rotated_at": S.clock(), "metadata": dict(body.metadata or {})}
        for _ in range(8):
            doc = await S.store.get(PLATFORM_COLLECTION, k.value)
            try:
                if doc is None:
                    await S.store.insert(PLATFORM_COLLECTION, {"id": k.value, **fields, "version": 1})
                else:
                    await S.store.update(PLATFORM_COLLECTION, k.value, fields, expect_version=doc["version"])
                break
            except Exception as exc:                         # lost a race: retry on the new version
                from core.hierarchy import ConcurrentModification, DuplicateKey
                if not isinstance(exc, (ConcurrentModification, DuplicateKey)):
                    raise
        await audit.record(S.store, actor=user.actor(), action="platform_credential.set", enterprise_id=None,
                           target_level="platform_credential", target_id=k.value, now=S.clock(), details={})
        return _platform_view(await S.store.get(PLATFORM_COLLECTION, k.value))

    @router.post("/platform/credentials/{kind}/revoke")
    @translate
    async def revoke_platform_credential(kind: str, user: User = Depends(principal)):
        require_superadmin(user)
        k = _kind(kind)
        doc = await S.store.get(PLATFORM_COLLECTION, k.value)
        if doc is None:
            raise NotFound("Not found.")
        await S.store.update(PLATFORM_COLLECTION, k.value, {"status": "revoked", "secret_enc": "",
                                                           "rotated_at": S.clock()}, expect_version=doc["version"])
        await audit.record(S.store, actor=user.actor(), action="platform_credential.revoke", enterprise_id=None,
                           target_level="platform_credential", target_id=k.value, now=S.clock(), details={})
        return _platform_view(await S.store.get(PLATFORM_COLLECTION, k.value))
