"""API integrations (restoration R3): configuration, credentials, connection and
endpoint tests, OpenAPI discovery/import, endpoint selection, activity and stats.

An API integration belongs to one Bot (ancestry from the route and the database,
never from the client). Permissions are checked on that Bot: READ (viewer+) to
see integrations, endpoints, activity and stats; MANAGE_CHANNEL (admin+) for
everything that changes them or calls the external API. Secrets are Spec 012
credentials (provider ``api_integration``): either an existing Enterprise or
Organisation credential (``credential_id``, checked with ``check_use``) or a new
Organisation credential created from ``secret`` (needs credential management on
the Organisation). Secrets are never returned. Every outbound call goes through
the outbound guard (``runtime.api_calls``) and is recorded in the activity log.
Using integrations in conversations is switched on per Bot with
``tools.api_calls_enabled`` (Enterprise policy ``tools.external_api_calls_allowed``).
"""
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.common import Services, load_access, translate
from core import audit, throttle
from core.authz import Action
from core.credentials import (COLLECTION as CREDENTIALS, CredentialError, Provider, check_manage, check_use,
                              credential_to_doc, enterprise_credentials, new_credential)
from core.hierarchy import Level, NotFound, is_valid_id
from core.identity import User
from runtime import api_calls as ac

MAX_PER_BOT = 20


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiIntegrationCreate(_Body):
    name: str
    description: str = ""
    base_url: str
    auth_type: str = "none"
    auth_header_name: str = ""
    auth_query_param: str = ""
    credential_id: Optional[str] = None
    secret: Optional[str] = None               # creates an Organisation credential (never stored here)
    doc_source_type: str = "openapi"
    doc_source_url: str = ""
    enabled: bool = True


class ApiIntegrationPatch(_Body):
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    auth_header_name: Optional[str] = None
    auth_query_param: Optional[str] = None
    credential_id: Optional[str] = None
    secret: Optional[str] = None
    clear_credential: bool = False
    doc_source_type: Optional[str] = None
    doc_source_url: Optional[str] = None
    enabled: Optional[bool] = None


class Discover(_Body):
    document: Optional[Any] = None             # an uploaded/pasted OpenAPI or reference JSON


class EndpointCreate(_Body):
    path: str
    summary: str = ""
    description: str = ""
    category: str = "manual"
    parameters: List[Dict[str, Any]] = []


class EndpointPatch(_Body):
    enabled: Optional[bool] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None


class EndpointBulk(_Body):
    enabled: bool
    endpoint_ids: Optional[List[str]] = None
    category: Optional[str] = None


class EndpointTest(_Body):
    parameters: Dict[str, Any] = {}


def integration_view(doc: dict) -> Dict[str, Any]:
    keys = ("id", "enterprise_id", "organisation_id", "bot_id", "name", "description", "base_url", "auth_type",
            "auth_header_name", "auth_query_param", "credential_id", "doc_source_type", "doc_source_url", "enabled",
            "endpoint_count", "enabled_endpoint_count", "last_connection_test", "last_doc_sync", "created_at",
            "updated_at")
    return {k: doc.get(k) for k in keys}


def endpoint_view(doc: dict) -> Dict[str, Any]:
    keys = ("id", "integration_id", "method", "path", "operation_id", "summary", "description", "category",
            "enabled", "executable", "parameters", "response_schema", "last_synced", "created_at")
    return {k: doc.get(k) for k in keys}


def _preview(data: Any) -> Any:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return data if len(text) <= 20000 else text[:20000] + "… (truncated)"


def _params(raw: List[Dict[str, Any]], path: str) -> List[Dict[str, Any]]:
    out = []
    for p in raw[:30]:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            raise HTTPException(422, "Each parameter needs a name.")
        where = p.get("in") or ("path" if "{" + p["name"] + "}" in path else "query")
        if where not in ("path", "query"):
            raise HTTPException(422, "Parameters must be path or query parameters.")
        out.append({"name": p["name"].strip()[:80], "in": where, "type": str(p.get("type") or "string")[:20],
                    "required": bool(p.get("required", where == "path")),
                    "description": str(p.get("description") or "")[:300], "default": p.get("default")})
    return out


def register(router, S: Services, principal) -> None:
    async def record(a, action: str, target_id: str, details: Dict[str, Any]) -> None:
        await audit.record(S.store, actor=a.actor, action=action, enterprise_id=a.chain.enterprise_id,
                           target_level="api_integration", target_id=target_id, now=S.clock(), details=details)

    async def access(user: User, integration_id: str, action: Action):
        doc = await S.store.get(ac.INTEGRATIONS, integration_id) if is_valid_id(integration_id) else None
        if doc is None:
            raise NotFound("Not found.")
        a = await load_access(S, user, Level.BOT, doc["bot_id"], action)
        if a.chain.enterprise_id != doc["enterprise_id"] or a.chain.organisation.id != doc["organisation_id"]:
            raise NotFound("Not found.")
        return doc, a

    async def endpoint_access(user: User, integration_id: str, endpoint_id: str, action: Action):
        doc, a = await access(user, integration_id, action)
        ep = await S.store.get(ac.ENDPOINTS, endpoint_id) if is_valid_id(endpoint_id) else None
        if ep is None or ep.get("integration_id") != doc["id"] or ep.get("enterprise_id") != doc["enterprise_id"]:
            raise NotFound("Not found.")
        return doc, ep, a

    async def outbound_budget(user: User) -> None:
        if not (await throttle.hit(S.store, throttle.throttle_key("api-int-outbound", user.id), limit=120,
                                   window_seconds=3600, now=S.clock())).allowed:
            raise HTTPException(429, "Too many API requests. Try again later.")

    def config_error(fn):
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ac.ApiConfigError as exc:
                raise HTTPException(422, str(exc))
        import functools
        return functools.wraps(fn)(wrapper)

    async def credential_fields(a, user: User, name: str, credential_id: Optional[str],
                                secret: Optional[str]) -> Dict[str, Any]:
        """Reference an existing credential or create an Organisation credential."""
        if secret:
            if not isinstance(secret, str) or len(secret) > 4000 or any(c in secret for c in "\r\n\x00"):
                raise HTTPException(422, "Invalid secret.")
            oa = await load_access(S, user, Level.ORGANISATION, a.chain.organisation.id, None)
            check_manage(oa.actor, oa.chain, oa.roster.members, oa.policy)
            cred = new_credential(credential_id=str(uuid.uuid4()), chain=oa.chain, provider=Provider.API_INTEGRATION,
                                  name=f"API: {name}"[:120], secret=secret, box=S.box, actor_id=user.id,
                                  now=S.clock())
            await S.store.insert(CREDENTIALS, credential_to_doc(cred, 1))
            await audit.record(S.store, actor=oa.actor, action="credential.create", enterprise_id=oa.chain.enterprise_id,
                               target_level="credential", target_id=cred.id, now=S.clock(),
                               details={"provider": Provider.API_INTEGRATION.value, "scoped": False})
            return {"credential_id": cred.id}
        if credential_id:
            creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
            check_use(creds.get(credential_id), referencing_chain=a.chain, slot=ac.CREDENTIAL_SLOT, policy=a.policy)
            return {"credential_id": credential_id}
        return {}

    async def secret_of(doc: dict, a) -> Optional[str]:
        if not doc.get("credential_id"):
            return None
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        try:
            cred = check_use(creds.get(doc["credential_id"]), referencing_chain=a.chain, slot=ac.CREDENTIAL_SLOT,
                             policy=a.policy)
        except CredentialError:
            raise HTTPException(422, "The integration's credential is not available (revoked or out of scope).")
        return S.box.decrypt(cred.secret_enc)

    async def refresh_counts(integration_id: str, enterprise_id: str) -> None:
        eps = await S.store.find(ac.ENDPOINTS, {"enterprise_id": enterprise_id, "integration_id": integration_id})
        await S.store.update(ac.INTEGRATIONS, integration_id, {
            "endpoint_count": len(eps), "enabled_endpoint_count": sum(1 for e in eps if e.get("enabled")),
            "updated_at": S.clock()})

    # ── integrations ──

    @router.get("/bots/{bot_id}/api-integrations")
    @translate
    async def list_api_integrations(bot_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.READ)
        docs = await S.store.find(ac.INTEGRATIONS, {"enterprise_id": a.chain.enterprise_id, "bot_id": bot_id},
                                  sort="created_at")
        return {"integrations": [integration_view(d) for d in docs]}

    @router.post("/bots/{bot_id}/api-integrations", status_code=201)
    @translate
    @config_error
    async def create_api_integration(bot_id: str, body: ApiIntegrationCreate, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.MANAGE_CHANNEL)
        if await S.store.count(ac.INTEGRATIONS, {"enterprise_id": a.chain.enterprise_id, "bot_id": bot_id}) >= MAX_PER_BOT:
            raise HTTPException(409, f"A bot can have at most {MAX_PER_BOT} API integrations.")
        name = (body.name or "").strip()
        if not name or len(name) > 100:
            raise HTTPException(422, "The name must be 1-100 characters.")
        if body.doc_source_type not in ac.DOC_TYPES:
            raise HTTPException(422, "doc_source_type must be openapi, custom_reference or manual.")
        now = S.clock()
        doc = {"id": str(uuid.uuid4()), "enterprise_id": a.chain.enterprise_id,
               "organisation_id": a.chain.organisation.id, "bot_id": bot_id, "name": name,
               "description": (body.description or "").strip()[:1000], "base_url": ac.validated_base_url(body.base_url),
               **ac.validated_auth(body.auth_type, body.auth_header_name, body.auth_query_param),
               "credential_id": None, "doc_source_type": body.doc_source_type,
               "doc_source_url": ac.validated_doc_url(body.doc_source_url), "enabled": bool(body.enabled),
               "endpoint_count": 0, "enabled_endpoint_count": 0, "last_connection_test": None, "last_doc_sync": None,
               "created_by": user.id, "created_at": now, "updated_at": now, "version": 1}
        doc.update(await credential_fields(a, user, name, body.credential_id, body.secret))
        await S.store.insert(ac.INTEGRATIONS, doc)
        await record(a, "api_integration.create", doc["id"], {"base_url": doc["base_url"],
                                                              "auth_type": doc["auth_type"]})
        return integration_view(doc)

    @router.get("/api-integrations/{integration_id}")
    @translate
    async def get_api_integration(integration_id: str, user: User = Depends(principal)):
        doc, _ = await access(user, integration_id, Action.READ)
        return integration_view(doc)

    @router.patch("/api-integrations/{integration_id}")
    @translate
    @config_error
    async def patch_api_integration(integration_id: str, body: ApiIntegrationPatch, user: User = Depends(principal)):
        doc, a = await access(user, integration_id, Action.MANAGE_CHANNEL)
        fields: Dict[str, Any] = {}
        if body.name is not None:
            if not body.name.strip() or len(body.name.strip()) > 100:
                raise HTTPException(422, "The name must be 1-100 characters.")
            fields["name"] = body.name.strip()
        if body.description is not None:
            fields["description"] = body.description.strip()[:1000]
        if body.base_url is not None:
            fields["base_url"] = ac.validated_base_url(body.base_url)
        if any(v is not None for v in (body.auth_type, body.auth_header_name, body.auth_query_param)):
            fields.update(ac.validated_auth(body.auth_type if body.auth_type is not None else doc.get("auth_type"),
                                            body.auth_header_name if body.auth_header_name is not None
                                            else doc.get("auth_header_name"),
                                            body.auth_query_param if body.auth_query_param is not None
                                            else doc.get("auth_query_param")))
        if body.doc_source_type is not None:
            if body.doc_source_type not in ac.DOC_TYPES:
                raise HTTPException(422, "doc_source_type must be openapi, custom_reference or manual.")
            fields["doc_source_type"] = body.doc_source_type
        if body.doc_source_url is not None:
            fields["doc_source_url"] = ac.validated_doc_url(body.doc_source_url)
        if body.enabled is not None:
            fields["enabled"] = bool(body.enabled)
        if body.clear_credential:
            fields["credential_id"] = None
        fields.update(await credential_fields(a, user, fields.get("name") or doc["name"], body.credential_id,
                                              body.secret))
        if not fields:
            raise HTTPException(422, "Nothing to change.")
        fields["updated_at"] = S.clock()
        await S.store.update(ac.INTEGRATIONS, integration_id, fields, expect_version=doc["version"])
        await record(a, "api_integration.update", integration_id,
                     {"fields": sorted(k for k in fields if k != "updated_at")})
        return integration_view(await S.store.get(ac.INTEGRATIONS, integration_id))

    @router.delete("/api-integrations/{integration_id}")
    @translate
    async def delete_api_integration(integration_id: str, user: User = Depends(principal)):
        doc, a = await access(user, integration_id, Action.MANAGE_CHANNEL)
        flt = {"enterprise_id": doc["enterprise_id"], "integration_id": integration_id}
        await S.store.delete_many(ac.ENDPOINTS, flt)
        await S.store.delete_many(ac.ACTIVITY, flt)
        await S.store.delete_many(ac.INTEGRATIONS, {"enterprise_id": doc["enterprise_id"], "id": integration_id})
        await record(a, "api_integration.delete", integration_id, {})
        return {"deleted": True}

    @router.post("/api-integrations/{integration_id}/test-connection")
    @translate
    async def test_api_connection(integration_id: str, user: User = Depends(principal)):
        doc, a = await access(user, integration_id, Action.MANAGE_CHANNEL)
        await outbound_budget(user)
        secret = await secret_of(doc, a)
        eps = await S.store.find(ac.ENDPOINTS, {"enterprise_id": doc["enterprise_id"], "integration_id": doc["id"]},
                                 sort="path", limit=200)
        result = await ac.connection_test(doc, eps, secret)
        now = S.clock()
        await S.store.update(ac.INTEGRATIONS, integration_id, {"last_connection_test": {
            "at": now, "success": result["success"], "status": result["status"], "message": result["message"]}})
        await ac.log_activity(S.store, integration=doc, endpoint=None, source="connection_test", result=result, now=now)
        await record(a, "api_integration.test_connection", integration_id, {"success": result["success"]})
        return {**result, "tested_at": now}

    @router.post("/api-integrations/{integration_id}/discover")
    @translate
    @config_error
    async def discover_endpoints(integration_id: str, body: Discover, user: User = Depends(principal)):
        """Import endpoints from the documentation URL, or from an uploaded/pasted
        OpenAPI/reference JSON document. Admin choices (enabled) survive a refresh."""
        doc, a = await access(user, integration_id, Action.MANAGE_CHANNEL)
        if body.document is not None:
            if len(json.dumps(body.document, default=str)) > ac.MAX_DOC_SIZE:
                raise HTTPException(413, "The document is too large (max 2 MB).")
            data, source = body.document, "upload"
        else:
            if not doc.get("doc_source_url"):
                raise HTTPException(422, "Set a documentation URL or upload an OpenAPI document.")
            await outbound_budget(user)
            data = await ac.fetch_document(doc, doc["doc_source_url"], await secret_of(doc, a))
            source = "url"
        parsed = ac.parse_documentation(doc.get("doc_source_type") or "openapi", data)
        existing = {f"{e['method']}:{e['path']}": e
                    for e in await S.store.find(ac.ENDPOINTS, {"enterprise_id": doc["enterprise_id"],
                                                               "integration_id": doc["id"]})}
        now = S.clock()
        new = updated = 0
        for ep in parsed:
            old = existing.get(f"{ep['method']}:{ep['path']}")
            if old:
                await S.store.update(ac.ENDPOINTS, old["id"], {
                    k: ep[k] for k in ("operation_id", "summary", "description", "category", "parameters",
                                       "response_schema", "executable")} | {"last_synced": now})
                updated += 1
            else:
                await S.store.insert(ac.ENDPOINTS, {"id": str(uuid.uuid4()), "enterprise_id": doc["enterprise_id"],
                                                    "organisation_id": doc["organisation_id"], "bot_id": doc["bot_id"],
                                                    "integration_id": doc["id"], **ep, "last_synced": now,
                                                    "created_at": now, "version": 1})
                new += 1
        await refresh_counts(doc["id"], doc["enterprise_id"])
        await S.store.update(ac.INTEGRATIONS, doc["id"], {"last_doc_sync": now})
        await record(a, "api_integration.discover", doc["id"], {"source": source, "new": new, "updated": updated})
        return {"new": new, "updated": updated, "total": new + updated}

    # ── endpoints ──

    @router.get("/api-integrations/{integration_id}/endpoints")
    @translate
    async def list_endpoints(integration_id: str, user: User = Depends(principal)):
        doc, _ = await access(user, integration_id, Action.READ)
        eps = await S.store.find(ac.ENDPOINTS, {"enterprise_id": doc["enterprise_id"], "integration_id": doc["id"]},
                                 sort="path")
        return {"endpoints": [endpoint_view(e) for e in eps]}

    @router.post("/api-integrations/{integration_id}/endpoints", status_code=201)
    @translate
    @config_error
    async def add_endpoint(integration_id: str, body: EndpointCreate, user: User = Depends(principal)):
        doc, a = await access(user, integration_id, Action.MANAGE_CHANNEL)
        path = (body.path or "").strip()
        if not path.startswith("/") or len(path) > 500 or any(c in path for c in " ?#\\\r\n"):
            raise HTTPException(422, "The path must start with '/' and contain no query or spaces.")
        if await S.store.count(ac.ENDPOINTS, {"enterprise_id": doc["enterprise_id"],
                                              "integration_id": doc["id"]}) >= ac.MAX_ENDPOINTS:
            raise HTTPException(409, "Too many endpoints.")
        now = S.clock()
        ep = {"id": str(uuid.uuid4()), "enterprise_id": doc["enterprise_id"], "organisation_id": doc["organisation_id"],
              "bot_id": doc["bot_id"], "integration_id": doc["id"], "method": "GET", "path": path,
              "operation_id": "", "summary": body.summary.strip()[:300], "description": body.description.strip()[:2000],
              "category": (body.category or "manual").strip()[:80], "enabled": True, "executable": True,
              "parameters": _params(body.parameters, path), "response_schema": {}, "last_synced": None,
              "created_at": now, "version": 1}
        await S.store.insert(ac.ENDPOINTS, ep)
        await refresh_counts(doc["id"], doc["enterprise_id"])
        await record(a, "api_integration.endpoint_add", doc["id"], {"path": path})
        return endpoint_view(ep)

    @router.post("/api-integrations/{integration_id}/endpoints/bulk")
    @translate
    async def bulk_endpoints(integration_id: str, body: EndpointBulk, user: User = Depends(principal)):
        doc, a = await access(user, integration_id, Action.MANAGE_CHANNEL)
        n = 0
        for e in await S.store.find(ac.ENDPOINTS, {"enterprise_id": doc["enterprise_id"], "integration_id": doc["id"]}):
            if body.endpoint_ids is not None and e["id"] not in body.endpoint_ids:
                continue
            if body.category is not None and e.get("category") != body.category:
                continue
            if body.enabled and not e.get("executable"):
                continue                                     # only GET endpoints can be enabled
            await S.store.update(ac.ENDPOINTS, e["id"], {"enabled": body.enabled})
            n += 1
        await refresh_counts(doc["id"], doc["enterprise_id"])
        await record(a, "api_integration.endpoints_bulk", doc["id"], {"enabled": body.enabled, "count": n})
        return {"updated": n}

    @router.get("/api-integrations/{integration_id}/endpoints/{endpoint_id}")
    @translate
    async def get_endpoint(integration_id: str, endpoint_id: str, user: User = Depends(principal)):
        _, ep, _ = await endpoint_access(user, integration_id, endpoint_id, Action.READ)
        return endpoint_view(ep)

    @router.patch("/api-integrations/{integration_id}/endpoints/{endpoint_id}")
    @translate
    async def patch_endpoint(integration_id: str, endpoint_id: str, body: EndpointPatch, user: User = Depends(principal)):
        doc, ep, a = await endpoint_access(user, integration_id, endpoint_id, Action.MANAGE_CHANNEL)
        fields: Dict[str, Any] = {}
        if body.enabled is not None:
            if body.enabled and not ep.get("executable"):
                raise HTTPException(422, "Only GET endpoints can be enabled.")
            fields["enabled"] = body.enabled
        for key, limit in (("summary", 300), ("description", 2000), ("category", 80)):
            value = getattr(body, key)
            if value is not None:
                fields[key] = value.strip()[:limit]
        if not fields:
            raise HTTPException(422, "Nothing to change.")
        await S.store.update(ac.ENDPOINTS, endpoint_id, fields)
        await refresh_counts(doc["id"], doc["enterprise_id"])
        await record(a, "api_integration.endpoint_update", doc["id"], {"endpoint_id": endpoint_id,
                                                                       "fields": sorted(fields)})
        return endpoint_view(await S.store.get(ac.ENDPOINTS, endpoint_id))

    @router.delete("/api-integrations/{integration_id}/endpoints/{endpoint_id}")
    @translate
    async def delete_endpoint(integration_id: str, endpoint_id: str, user: User = Depends(principal)):
        doc, ep, a = await endpoint_access(user, integration_id, endpoint_id, Action.MANAGE_CHANNEL)
        await S.store.delete_many(ac.ENDPOINTS, {"enterprise_id": doc["enterprise_id"], "id": endpoint_id})
        await refresh_counts(doc["id"], doc["enterprise_id"])
        await record(a, "api_integration.endpoint_delete", doc["id"], {"endpoint_id": endpoint_id})
        return {"deleted": True}

    @router.post("/api-integrations/{integration_id}/endpoints/{endpoint_id}/test")
    @translate
    async def test_endpoint(integration_id: str, endpoint_id: str, body: EndpointTest,
                            user: User = Depends(principal)):
        doc, ep, a = await endpoint_access(user, integration_id, endpoint_id, Action.MANAGE_CHANNEL)
        if not ep.get("executable"):
            raise HTTPException(422, "Only GET endpoints can be executed.")
        await outbound_budget(user)
        result = await ac.execute(doc, ep, body.parameters, await secret_of(doc, a))
        await ac.log_activity(S.store, integration=doc, endpoint=ep, source="test", result=result, now=S.clock())
        if "data" in result:
            result["data"] = _preview(result["data"])
        return result

    # ── activity and stats ──

    @router.get("/api-integrations/{integration_id}/activity")
    @translate
    async def activity(integration_id: str, limit: int = 50, user: User = Depends(principal)):
        doc, _ = await access(user, integration_id, Action.READ)
        rows = await S.store.find(ac.ACTIVITY, {"enterprise_id": doc["enterprise_id"], "integration_id": doc["id"]},
                                  sort="at", descending=True, limit=max(1, min(int(limit), 200)))
        keys = ("id", "endpoint_id", "endpoint", "source", "status", "success", "duration_ms", "response_size",
                "error", "at")
        return {"activity": [{k: r.get(k) for k in keys} for r in rows]}

    @router.get("/api-integrations/{integration_id}/stats")
    @translate
    async def stats(integration_id: str, user: User = Depends(principal)):
        doc, _ = await access(user, integration_id, Action.READ)
        rows = await S.store.find(ac.ACTIVITY, {"enterprise_id": doc["enterprise_id"], "integration_id": doc["id"]},
                                  sort="at", descending=True, limit=2000)
        ok = [r for r in rows if r.get("success")]
        by_source: Dict[str, int] = {}
        for r in rows:
            by_source[r.get("source") or "other"] = by_source.get(r.get("source") or "other", 0) + 1
        return {"calls": len(rows), "succeeded": len(ok), "failed": len(rows) - len(ok),
                "average_duration_ms": int(sum(r.get("duration_ms") or 0 for r in rows) / len(rows)) if rows else None,
                "by_source": by_source, "last_call_at": rows[0]["at"] if rows else None,
                "endpoint_count": doc.get("endpoint_count", 0),
                "enabled_endpoint_count": doc.get("enabled_endpoint_count", 0)}


__all__ = ["register", "integration_view", "endpoint_view"]
