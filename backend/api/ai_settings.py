"""AI provider, model and credential selection (Stage 0, owner requirement 2026-11-20).

Customer-provided AI keys are a core feature. These routes let a normal authorised
administrator pick a provider, a model and a saved credential by *name* (never by
typing an internal id), add a credential, and run a real connection test:

* ``GET  /{enterprises|organisations|bots}/{id}/ai``       options + current selection (READ)
* ``PUT  /{enterprises|organisations|bots}/{id}/ai``       set provider/model/credential atomically (EDIT_CONFIG)
* ``POST /{enterprises|organisations|bots}/{id}/ai/test``  one tiny real generation (EDIT_CONFIG, throttled)
* ``/platform/ai-credentials``                             superadmin-owned AI keys granted per Enterprise

Everything goes through the existing pieces: the single resolver (validation,
inheritance, policy), the credential module (``check_use`` decides what is usable;
platform keys are loaded by ``enterprise_credentials`` only for granted Enterprises),
the provider adapters (fixed hosts) and the audit log. Secrets are write-only.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.common import TYPE_LEVEL, Services, jsonable, load_access, policy_nodes, require_superadmin, translate
from core import audit, resolver, throttle
from core import config_registry as reg
from core.authz import Action, AuthzError
from core.credentials import (AI_PROVIDERS, PLATFORM_AI_COLLECTION, CredentialError, CredentialStatus, Provider,
                              check_manage, check_use, enterprise_credentials, fingerprint)
from core.hierarchy import Chain, Level, NotFound, new_id
from core.identity import User

AI_KEYS = ("ai.provider", "ai.model", "ai.credential_id")
AI_TEST_RATE = (20, 3600)                 # real generations per user per hour (each costs a few tokens)
PLATFORM_CRED_TEST_RATE = (30, 3600)
MAX_PLATFORM_AI_CREDENTIALS = 50

TEST_MESSAGES = {
    None: "Connected: {model} answered.",
    "unauthorized": "The provider rejected this API key. Check the key, or replace it under Credentials.",
    "rate_limited": "The provider is rate-limiting this key. Try again in a minute.",
    "model_unavailable": "This key cannot use {model}. Choose another model or check your provider account.",
    "unavailable": "The provider could not be reached. Try again later.",
    "uncertain": "The provider did not answer in time. Try again.",
    "rejected": "The provider rejected the test request.",
}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AiSelection(_Body):
    """Fields that are present are set; ``null`` returns that field to the inherited value."""
    provider: Optional[str] = None
    model: Optional[str] = None
    credential_id: Optional[str] = None


class AiTest(_Body):
    """Omitted fields use the current effective selection."""
    provider: Optional[str] = None
    model: Optional[str] = None
    credential_id: Optional[str] = None


class PlatformAiCreate(_Body):
    provider: str
    name: str
    secret: str
    all_enterprises: bool = False
    enterprise_ids: List[str] = []


class PlatformAiPatch(_Body):
    name: Optional[str] = None
    secret: Optional[str] = None
    all_enterprises: Optional[bool] = None
    enterprise_ids: Optional[List[str]] = None


def _ai_provider(value: str) -> Provider:
    try:
        p = Provider(value)
    except ValueError:
        raise CredentialError("Unknown AI provider.")
    if p not in AI_PROVIDERS:
        raise CredentialError("Unknown AI provider.")
    return p


def _check_secret(secret: str) -> str:
    s = (secret or "").strip()
    if not (20 <= len(s) <= 400) or any(c.isspace() for c in s):
        raise CredentialError("That does not look like an API key (20-400 characters, no spaces).")
    return s


def _sub_chain(chain: Chain, level: Level) -> Chain:
    return Chain(enterprise=chain.enterprise, organisation=chain.organisation if level is Level.ORGANISATION else None)


def register(router: APIRouter, S: Services, principal) -> None:
    def level_of(type_: str) -> Level:
        level = TYPE_LEVEL.get(type_)
        if level not in (Level.ENTERPRISE, Level.ORGANISATION, Level.BOT):
            raise HTTPException(404, "Not found.")
        return level

    def provider_ids(policy: Dict[str, Any]) -> List[str]:
        allowed = policy.get("ai.allowed_providers")
        return [p.value for p in AI_PROVIDERS if allowed is None or p.value in allowed]

    def model_allowed(provider: str, model: str, policy: Dict[str, Any]) -> bool:
        allowed = policy.get("ai.allowed_models")
        return model in reg.MODEL_ALLOWLIST.get(provider, ()) and (allowed is None or model in allowed)

    async def view(a, user: User) -> Dict[str, Any]:
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        eff = resolver.resolve(a.chain, platform_policy=S.platform_policy, credentials=creds,
                               actor=a.actor, memberships=a.roster.members)
        allowed = set(provider_ids(eff.policy))
        providers = []
        for p in AI_PROVIDERS:
            models = [{"id": m, "label": reg.MODEL_LABELS.get(m, m), "allowed": model_allowed(p.value, m, eff.policy)}
                      for m in reg.MODEL_ALLOWLIST.get(p.value, ())]
            providers.append({"id": p.value, "label": reg.PROVIDER_LABELS.get(p.value, p.value),
                              "allowed": p.value in allowed, "default_model": reg.DEFAULT_MODEL[p.value],
                              "models": models})
        names = {a.chain.enterprise.id: a.chain.enterprise.name}
        if a.chain.organisation:
            names[a.chain.organisation.id] = a.chain.organisation.name
        usable = []
        for c in sorted(creds.values(), key=lambda c: (c.provider.value, c.name.lower())):
            if c.provider not in AI_PROVIDERS:
                continue
            try:
                check_use(c, referencing_chain=a.chain, slot=f"ai.{c.provider.value}", policy=eff.policy)
            except CredentialError:
                continue
            v = c.public_view()
            v["owner_name"] = "Platform" if c.platform else names.get(c.organisation_id or c.enterprise_id, "")
            v["provider_allowed"] = c.provider.value in allowed
            usable.append(v)
        add_to = []
        for lvl in (Level.ENTERPRISE, Level.ORGANISATION):
            if lvl is Level.ORGANISATION and a.chain.organisation is None:
                continue
            sub = _sub_chain(a.chain, lvl)
            pol, _, _ = resolver.effective_policy(policy_nodes(sub), S.platform_policy)
            try:
                check_manage(a.actor, sub, a.roster.members, pol)
            except AuthzError:
                continue
            add_to.append({"type": "enterprises" if lvl is Level.ENTERPRISE else "organisations",
                           "id": sub.target.id, "name": sub.target.name})
        can_edit = all(eff.overridable.get(k) for k in AI_KEYS) and \
            (a.grant.superadmin or _allowed(a, Action.EDIT_CONFIG))
        cred_id = eff.values.get("ai.credential_id")
        current_cred = next((c for c in usable if c["id"] == cred_id), None)
        if cred_id and current_cred is None:
            current_cred = eff.credentials.get("ai.credential_id")     # resolvable but e.g. provider not allowed
        return {
            "target": {"type": {Level.ENTERPRISE: "enterprises", Level.ORGANISATION: "organisations",
                                Level.BOT: "bots"}[a.target.level], "id": a.target.id, "name": a.target.name},
            "can_edit": bool(can_edit), "can_test": bool(can_edit),
            "current": {"provider": eff.values.get("ai.provider"), "model": eff.values.get("ai.model"),
                        "credential_id": cred_id, "credential": jsonable(current_cred),
                        "sources": {k: eff.sources[k].to_dict() for k in AI_KEYS if k in eff.sources},
                        "blocked": eff.blocked.get("ai")},
            "providers": providers, "credentials": usable, "can_add_credential_to": add_to,
        }

    def _allowed(a, action: Action) -> bool:
        from core.authz import can
        return can(a.actor, action, a.chain, a.roster.members, a.policy).allowed

    @router.get("/{type_:scopetype}/{object_id}/ai")
    @translate
    async def get_ai(type_: str, object_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, level_of(type_), object_id)
        return await view(a, user)

    @router.put("/{type_:scopetype}/{object_id}/ai")
    @translate
    async def put_ai(type_: str, object_id: str, body: AiSelection, user: User = Depends(principal)):
        level = level_of(type_)
        a = await load_access(S, user, level, object_id, Action.EDIT_CONFIG)
        changes = {f"ai.{k}": getattr(body, k) for k in ("provider", "model", "credential_id")
                   if k in body.model_fields_set}
        if not changes:
            raise HTTPException(422, "Choose a provider, a model or a credential.")
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        eff = resolver.resolve(a.chain, platform_policy=S.platform_policy, credentials=creds,
                               actor=a.actor, memberships=a.roster.members)
        for key in changes:
            if not eff.overridable.get(key):
                raise AuthzError(f"{key} cannot be changed here by you")
        if changes.get("ai.provider") is not None and changes["ai.provider"] not in provider_ids(eff.policy):
            raise HTTPException(422, "That AI provider is not allowed here.")
        if "ai.provider" in changes and "ai.model" not in changes:
            changes["ai.model"] = None                    # a provider switch starts from that provider's default
        new = resolver.apply_override_patch(a.chain, changes, platform_policy=S.platform_policy, credentials=creds)
        await S.hierarchy.set_overrides(level, object_id, new, expect_version=a.target.version)
        await audit.record(S.store, actor=a.actor, action="config.update", enterprise_id=a.chain.enterprise_id,
                           target_level=level.value, target_id=object_id, now=S.clock(),
                           details={"keys": sorted(changes), "via": "ai_settings"})
        return await view(await load_access(S, user, level, object_id), user)

    @router.post("/{type_:scopetype}/{object_id}/ai/test")
    @translate
    async def test_ai(type_: str, object_id: str, body: AiTest, user: User = Depends(principal)):
        level = level_of(type_)
        a = await load_access(S, user, level, object_id, Action.EDIT_CONFIG)
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        eff = resolver.resolve(a.chain, platform_policy=S.platform_policy, credentials=creds,
                               actor=a.actor, memberships=a.roster.members)
        provider_name = body.provider or eff.values.get("ai.provider")
        if provider_name not in provider_ids(eff.policy):
            raise HTTPException(422, "Choose an AI provider that is allowed here.")
        model = body.model or (eff.values.get("ai.model") if provider_name == eff.values.get("ai.provider")
                               else reg.DEFAULT_MODEL[provider_name])
        if not model_allowed(provider_name, model or "", eff.policy):
            raise HTTPException(422, "Choose a model that is allowed for this provider.")
        cred_id = body.credential_id or eff.values.get("ai.credential_id")
        if not cred_id:
            raise HTTPException(422, "Choose an API credential first.")
        cred = check_use(creds.get(cred_id), referencing_chain=a.chain, slot=f"ai.{provider_name}",
                         policy=eff.policy)
        adapter = S.providers.get(provider_name)
        if adapter is None or not hasattr(adapter, "probe"):
            raise HTTPException(422, "Testing this provider is not available.")
        d = await throttle.hit(S.store, throttle.throttle_key("ai-test", user.id), limit=AI_TEST_RATE[0],
                               window_seconds=AI_TEST_RATE[1], now=S.clock())
        if not d.allowed:
            raise HTTPException(429, "Too many tests. Try again later.", headers={"Retry-After": str(d.retry_after)})
        started = time.monotonic()
        r = await adapter.probe(S.box.decrypt(cred.secret_enc), model)
        latency = int((time.monotonic() - started) * 1000)
        category = r.get("category")
        from runtime.usage import StoreUsageTracker
        await StoreUsageTracker(S.store, platform_policy=S.platform_policy).record(
            a.chain, "ai_test", now=S.clock(),
            details={"provider": provider_name, "model": model, "ok": bool(r.get("ok")),
                     "input_tokens": int(r.get("input_tokens") or 0), "output_tokens": int(r.get("output_tokens") or 0),
                     "platform_credential": cred.platform})
        await audit.record(S.store, actor=a.actor, action="ai.test", enterprise_id=a.chain.enterprise_id,
                           target_level=level.value, target_id=object_id, now=S.clock(),
                           details={"provider": provider_name, "model": model, "credential_id": cred.id,
                                    "ok": bool(r.get("ok")), "category": category or ""})
        label = reg.MODEL_LABELS.get(model, model)
        return {"ok": bool(r.get("ok")), "category": category,
                "message": TEST_MESSAGES.get(category, TEST_MESSAGES["rejected"]).format(model=label),
                "provider": provider_name, "model": model, "credential": {"id": cred.id, "name": cred.name},
                "reply": r.get("reply") if r.get("ok") else None,
                "input_tokens": int(r.get("input_tokens") or 0), "output_tokens": int(r.get("output_tokens") or 0),
                "latency_ms": latency}

    # ── platform AI credentials (superadmin only; invisible to everyone else) ──
    async def platform_view(doc: Dict[str, Any]) -> Dict[str, Any]:
        ents = []
        for eid in doc.get("enterprise_ids") or []:
            e = await S.store.get("enterprises", eid)
            if e:
                ents.append({"id": eid, "name": e.get("name", "")})
        return {"id": doc["id"], "provider": doc["provider"], "name": doc.get("name", ""), "owner": "platform",
                "fingerprint": doc.get("fingerprint"), "status": doc.get("status"), "created_at": doc.get("created_at"),
                "rotated_at": doc.get("rotated_at"), "all_enterprises": bool(doc.get("all_enterprises")),
                "enterprises": ents}

    async def grants(ids: Optional[List[str]]) -> List[str]:
        out = []
        for eid in ids or []:
            e = await S.store.get("enterprises", eid) if isinstance(eid, str) else None
            if not e or (e.get("lifecycle") or {}).get("state") == "deleted":
                raise HTTPException(422, "One of the selected Enterprises does not exist.")
            out.append(eid)
        if len(set(out)) > 1000:
            raise HTTPException(422, "Too many Enterprises.")
        return sorted(set(out))

    async def load_platform(cred_id: str) -> Dict[str, Any]:
        doc = await S.store.get(PLATFORM_AI_COLLECTION, cred_id) if isinstance(cred_id, str) else None
        if doc is None:
            raise NotFound("Not found.")
        return doc

    async def platform_audit(user: User, action: str, cred_id: str, details: Dict[str, Any]) -> None:
        await audit.record(S.store, actor=user.actor(), action=action, enterprise_id=None,
                           target_level="platform_ai_credential", target_id=cred_id, now=S.clock(), details=details)

    @router.get("/platform/ai-credentials")
    @translate
    async def list_platform_ai(user: User = Depends(principal)):
        require_superadmin(user)
        docs = await S.store.find(PLATFORM_AI_COLLECTION, {}, sort="created_at")
        return {"credentials": [await platform_view(d) for d in docs]}

    @router.post("/platform/ai-credentials", status_code=201)
    @translate
    async def create_platform_ai(body: PlatformAiCreate, user: User = Depends(principal)):
        require_superadmin(user)
        provider = _ai_provider(body.provider)
        name = (body.name or "").strip()
        if not name or len(name) > 120:
            raise CredentialError("A credential name (1-120 characters) is required.")
        secret = _check_secret(body.secret)
        if await S.store.count(PLATFORM_AI_COLLECTION, {}) >= MAX_PLATFORM_AI_CREDENTIALS:
            raise HTTPException(422, "Too many platform AI credentials.")
        now = S.clock()
        doc = {"id": new_id(), "provider": provider.value, "name": name, "secret_enc": S.box.encrypt(secret),
               "fingerprint": fingerprint(secret), "status": CredentialStatus.ACTIVE.value, "created_by": user.id,
               "created_at": now, "rotated_at": None, "all_enterprises": bool(body.all_enterprises),
               "enterprise_ids": await grants(body.enterprise_ids), "version": 1}
        await S.store.insert(PLATFORM_AI_COLLECTION, doc)
        await platform_audit(user, "platform_ai_credential.create", doc["id"],
                             {"provider": provider.value, "all_enterprises": doc["all_enterprises"],
                              "enterprises": len(doc["enterprise_ids"])})
        return await platform_view(doc)

    @router.patch("/platform/ai-credentials/{cred_id}")
    @translate
    async def patch_platform_ai(cred_id: str, body: PlatformAiPatch, user: User = Depends(principal)):
        require_superadmin(user)
        doc = await load_platform(cred_id)
        fields: Dict[str, Any] = {}
        if body.name is not None:
            if not body.name.strip() or len(body.name) > 120:
                raise CredentialError("A credential name (1-120 characters) is required.")
            fields["name"] = body.name.strip()
        if body.secret is not None:
            if doc.get("status") != CredentialStatus.ACTIVE.value:
                raise CredentialError("A revoked credential cannot be replaced; create a new one.")
            secret = _check_secret(body.secret)
            fields.update(secret_enc=S.box.encrypt(secret), fingerprint=fingerprint(secret), rotated_at=S.clock())
        if body.all_enterprises is not None:
            fields["all_enterprises"] = bool(body.all_enterprises)
        if body.enterprise_ids is not None:
            fields["enterprise_ids"] = await grants(body.enterprise_ids)
        if not fields:
            raise CredentialError("Nothing to change.")
        await S.store.update(PLATFORM_AI_COLLECTION, cred_id, fields, expect_version=doc["version"])
        await platform_audit(user, "platform_ai_credential.update", cred_id,
                             {"keys": ",".join(sorted(k for k in fields if k not in ("secret_enc", "fingerprint")))})
        return await platform_view(await load_platform(cred_id))

    @router.post("/platform/ai-credentials/{cred_id}/revoke")
    @translate
    async def revoke_platform_ai(cred_id: str, user: User = Depends(principal)):
        require_superadmin(user)
        doc = await load_platform(cred_id)
        if doc.get("status") != CredentialStatus.REVOKED.value:
            await S.store.update(PLATFORM_AI_COLLECTION, cred_id,
                                 {"status": CredentialStatus.REVOKED.value, "secret_enc": "", "rotated_at": S.clock()},
                                 expect_version=doc["version"])
            await platform_audit(user, "platform_ai_credential.revoke", cred_id, {})
        return await platform_view(await load_platform(cred_id))

    @router.delete("/platform/ai-credentials/{cred_id}")
    @translate
    async def delete_platform_ai(cred_id: str, user: User = Depends(principal)):
        require_superadmin(user)
        doc = await load_platform(cred_id)
        if not await S.store.delete(PLATFORM_AI_COLLECTION, cred_id, expect_version=doc["version"]):
            raise HTTPException(409, "This credential was changed by another request. Reload and try again.")
        await platform_audit(user, "platform_ai_credential.delete", cred_id, {})
        return {"status": "deleted"}

    @router.post("/platform/ai-credentials/{cred_id}/test")
    @translate
    async def test_platform_ai(cred_id: str, user: User = Depends(principal)):
        """Read-only key check (list models on the fixed provider host): no generation, no cost."""
        require_superadmin(user)
        doc = await load_platform(cred_id)
        if doc.get("status") != CredentialStatus.ACTIVE.value:
            raise HTTPException(422, "The credential has been revoked.")
        adapter = S.providers.get(doc["provider"])
        if adapter is None or not hasattr(adapter, "check"):
            raise HTTPException(422, "Testing this provider is not available.")
        d = await throttle.hit(S.store, throttle.throttle_key("platform-ai-test", user.id),
                               limit=PLATFORM_CRED_TEST_RATE[0], window_seconds=PLATFORM_CRED_TEST_RATE[1], now=S.clock())
        if not d.allowed:
            raise HTTPException(429, "Too many tests. Try again later.", headers={"Retry-After": str(d.retry_after)})
        r = await adapter.check(S.box.decrypt(doc["secret_enc"]))
        await platform_audit(user, "platform_ai_credential.test", cred_id, {"ok": bool(r.get("ok"))})
        return {"ok": bool(r.get("ok")), "category": r.get("category")}
