"""Knowledge sources and documents (Phase 4K).

Ownership comes only from the route and the database: an Enterprise source
(``/enterprises/{id}/knowledge/sources``) or an Organisation source
(``/organisations/{id}/knowledge/sources``). Managing needs EDIT_POLICY (admin)
on the owner; reading needs READ. Using a source additionally requires the
Enterprise's approval (``knowledge.allowed_sources`` policy, deny by default)
and a Bot/Instance/Integration selection (``knowledge.approved_sources``); see
``runtime.knowledge_store``.

Uploads are raw request bodies (``PUT …/documents/{name}`` with
``Content-Type``), at most 2 MB; extraction is bounded and nothing in a
document is executed. Document text is never returned by these routes.
"""
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from api.common import Services, load_access, translate
from core import audit
from core.authz import Action
from core.config_registry import ConfigError
from core.hierarchy import COLLECTION, Level, NotFound, is_valid_id
from core.identity import User
from runtime.knowledge_store import (CHUNKS, DOCUMENTS, MAX_UPLOAD_BYTES, SOURCES, IngestError, Ingestor)

OWNER = {"enterprises": Level.ENTERPRISE, "organisations": Level.ORGANISATION}
STATUSES = ("active", "disabled", "revoked", "archived")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCreate(_Body):
    name: str
    acl_groups: List[str] = []


class SourcePatch(_Body):
    name: Optional[str] = None
    status: Optional[str] = None
    acl_groups: Optional[List[str]] = None


class DocumentValidity(_Body):
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None


class DocumentAcl(_Body):
    organisations: List[str] = []
    bots: List[str] = []


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise HTTPException(422, "A name (1-120 characters) is required.")
    return value.strip()


def _groups(values: Iterable[str]) -> List[str]:
    values = list(values)
    if len(values) > 50 or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", v) for v in values):
        raise HTTPException(422, "acl_groups must be up to 50 group names.")
    return sorted(set(values))


def source_view(doc: dict) -> Dict[str, Any]:
    return {k: doc.get(k) for k in ("id", "enterprise_id", "organisation_id", "name", "status", "acl_groups",
                                    "created_at", "updated_at")}


def document_view(doc: dict) -> Dict[str, Any]:
    job = doc.get("job") or {}
    return {"id": doc["id"], "source_id": doc["source_id"], "name": doc.get("name"), "status": doc.get("status"),
            "content_version": doc.get("content_version"), "content_type": doc.get("content_type"),
            "size": doc.get("size"), "chunk_count": doc.get("chunk_count"), "acl": doc.get("acl"),
            "job": {"state": job.get("state"), "attempts": job.get("attempts"), "error": job.get("error")},
            "updated_at": doc.get("updated_at")}


async def check_source_refs(S: Services, chain, values: Any) -> None:
    """Policy/config writes may only name existing sources on the writer's own
    Enterprise branch (an Organisation may name Enterprise sources and its own)."""
    if values is None:
        return
    ids = list(values) if isinstance(values, (list, tuple, set, frozenset)) else []
    if not ids:
        return
    docs = {d["id"]: d for d in await S.store.find(SOURCES, {"enterprise_id": chain.enterprise_id,
                                                              "id": [i for i in ids if isinstance(i, str)]})}
    org = chain.organisation.id if chain.organisation else None
    for i in ids:
        d = docs.get(i)
        if d is None or (d.get("organisation_id") and d.get("organisation_id") != org and org is not None) or \
                (d.get("organisation_id") and org is None and chain.target.level is not Level.ENTERPRISE):
            raise ConfigError("unknown knowledge source")


def register(router: APIRouter, S: Services, principal) -> None:
    ingest = lambda: Ingestor(S.store, clock=S.clock)                    # noqa: E731

    async def record(a, action: str, target_id: str, details: Dict[str, Any]) -> None:
        await audit.record(S.store, actor=a.actor, action=action, enterprise_id=a.chain.enterprise_id,
                           target_level="knowledge", target_id=target_id, now=S.clock(), details=details)

    async def source_access(user: User, source_id: str, action: Action):
        doc = await S.store.get(SOURCES, source_id) if is_valid_id(source_id) else None
        if doc is None:
            raise NotFound("Not found.")
        if doc.get("organisation_id"):
            a = await load_access(S, user, Level.ORGANISATION, doc["organisation_id"], action)
        else:
            a = await load_access(S, user, Level.ENTERPRISE, doc["enterprise_id"], action)
        if a.chain.enterprise_id != doc["enterprise_id"]:
            raise NotFound("Not found.")
        return doc, a

    async def document_access(user: User, document_id: str, action: Action):
        doc = await S.store.get(DOCUMENTS, document_id) if is_valid_id(document_id) else None
        if doc is None:
            raise NotFound("Not found.")
        src, a = await source_access(user, doc["source_id"], action)
        if doc["enterprise_id"] != src["enterprise_id"]:
            raise NotFound("Not found.")
        return doc, src, a

    @router.post("/{owner_type:ownertype}/{owner_id}/knowledge/sources", status_code=201)
    @translate
    async def create_source(owner_type: str, owner_id: str, body: SourceCreate, user: User = Depends(principal)):
        a = await load_access(S, user, OWNER[owner_type], owner_id, Action.EDIT_POLICY)
        now = S.clock()
        doc = {"id": str(uuid.uuid4()), "enterprise_id": a.chain.enterprise_id,
               "organisation_id": owner_id if OWNER[owner_type] is Level.ORGANISATION else None,
               "name": _name(body.name), "kind": "document", "status": "active",
               "acl_groups": _groups(body.acl_groups), "created_by": user.id, "created_at": now, "updated_at": now,
               "version": 1}
        await S.store.insert(SOURCES, doc)
        await record(a, "knowledge.source_create", doc["id"], {"owner": owner_type})
        return source_view(doc)

    @router.get("/{owner_type:ownertype}/{owner_id}/knowledge/sources")
    @translate
    async def list_sources(owner_type: str, owner_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, OWNER[owner_type], owner_id, Action.READ)
        flt = {"enterprise_id": a.chain.enterprise_id,
               "organisation_id": owner_id if OWNER[owner_type] is Level.ORGANISATION else None}
        return {"sources": [source_view(d) for d in await S.store.find(SOURCES, flt, sort="created_at")]}

    @router.get("/knowledge/sources/{source_id}")
    @translate
    async def get_source(source_id: str, user: User = Depends(principal)):
        doc, _ = await source_access(user, source_id, Action.READ)
        return source_view(doc)

    @router.patch("/knowledge/sources/{source_id}")
    @translate
    async def patch_source(source_id: str, body: SourcePatch, user: User = Depends(principal)):
        doc, a = await source_access(user, source_id, Action.EDIT_POLICY)
        fields: Dict[str, Any] = {}
        if body.name is not None:
            fields["name"] = _name(body.name)
        if body.status is not None:
            if body.status not in STATUSES:
                raise HTTPException(422, "Unknown status.")
            fields["status"] = body.status                               # revocation is immediate at retrieval
        if body.acl_groups is not None:
            fields["acl_groups"] = _groups(body.acl_groups)
        if not fields:
            raise HTTPException(422, "Nothing to change.")
        fields["updated_at"] = S.clock()
        await S.store.update(SOURCES, source_id, fields, expect_version=doc["version"])
        await record(a, "knowledge.source_update", source_id, {"fields": sorted(fields)})
        return source_view(await S.store.get(SOURCES, source_id))

    @router.delete("/knowledge/sources/{source_id}")
    @translate
    async def delete_source(source_id: str, user: User = Depends(principal)):
        doc, a = await source_access(user, source_id, Action.EDIT_POLICY)
        await ingest().delete_source(doc)
        await record(a, "knowledge.source_delete", source_id, {})
        return {"deleted": True}

    @router.put("/knowledge/sources/{source_id}/documents/{name}")
    @translate
    async def upload_document(source_id: str, name: str, request: Request, user: User = Depends(principal)):
        src, a = await source_access(user, source_id, Action.EDIT_POLICY)
        if src.get("status") != "active":
            raise HTTPException(409, "The source is not active.")
        from core import throttle
        if not (await throttle.hit(S.store, throttle.throttle_key("kb-upload", user.id), limit=120,
                                   window_seconds=3600, now=S.clock())).allowed:
            raise HTTPException(429, "Too many uploads. Try again later.")
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            raise HTTPException(400, "Bad request.")
        if declared > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "The document is too large.")
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "The document is too large.")
        try:
            doc = await ingest().submit(src, name=name, content_type=request.headers.get("content-type", ""),
                                        data=bytes(data), actor_id=user.id)
        except IngestError as exc:
            raise HTTPException(415 if "type" in str(exc) else 422, str(exc))
        await record(a, "knowledge.document_upload", doc["id"], {"source_id": source_id, "size": len(data),
                                                                 "status": doc.get("status")})
        return document_view(doc)

    @router.get("/knowledge/sources/{source_id}/documents")
    @translate
    async def list_documents(source_id: str, user: User = Depends(principal)):
        src, a = await source_access(user, source_id, Action.READ)
        docs = await S.store.find(DOCUMENTS, {"enterprise_id": src["enterprise_id"], "source_id": source_id},
                                  sort="created_at")
        return {"documents": [document_view(d) for d in docs]}

    @router.get("/knowledge/documents/{document_id}")
    @translate
    async def get_document(document_id: str, user: User = Depends(principal)):
        doc, _, _ = await document_access(user, document_id, Action.READ)
        return document_view(doc)

    @router.put("/knowledge/documents/{document_id}/acl")
    @translate
    async def set_document_acl(document_id: str, body: DocumentAcl, user: User = Depends(principal)):
        """Restrict a document to some Organisations/Bots *inside* its source's
        scope (never wider)."""
        doc, src, a = await document_access(user, document_id, Action.EDIT_POLICY)
        orgs, bots = sorted(set(body.organisations)), sorted(set(body.bots))
        for oid in orgs:
            o = await S.store.get(COLLECTION[Level.ORGANISATION], oid) if is_valid_id(oid) else None
            if not o or o["enterprise_id"] != src["enterprise_id"] or \
                    (src.get("organisation_id") and oid != src["organisation_id"]):
                raise HTTPException(422, "Organisations must belong to the source's scope.")
        for bid in bots:
            b = await S.store.get(COLLECTION[Level.BOT], bid) if is_valid_id(bid) else None
            if not b or b["enterprise_id"] != src["enterprise_id"] or \
                    (src.get("organisation_id") and b["organisation_id"] != src["organisation_id"]):
                raise HTTPException(422, "Bots must belong to the source's scope.")
        new = await ingest().set_acl(document_id, {"organisations": orgs, "bots": bots})
        await record(a, "knowledge.document_acl", document_id, {"organisations": len(orgs), "bots": len(bots)})
        return document_view(new)

    @router.put("/knowledge/documents/{document_id}/validity")
    @translate
    async def set_document_validity(document_id: str, body: DocumentValidity, user: User = Depends(principal)):
        """Effective window of a document (e.g. an expired policy). Outside it the
        document is never used as evidence."""
        from datetime import datetime as _dt
        doc, _, a = await document_access(user, document_id, Action.EDIT_POLICY)
        fields = {}
        for key in ("valid_from", "valid_until"):
            value = getattr(body, key)
            if value is not None:
                try:
                    parsed = _dt.fromisoformat(value)
                except ValueError:
                    raise HTTPException(422, f"{key} must be an ISO 8601 timestamp with a timezone.")
                if parsed.tzinfo is None:
                    raise HTTPException(422, f"{key} must include a timezone.")
                fields[key] = parsed.isoformat()
            else:
                fields[key] = None
        await S.store.update(DOCUMENTS, document_id, fields, expect_version=doc["version"])
        await record(a, "knowledge.document_validity", document_id, fields)
        return {**document_view(await S.store.get(DOCUMENTS, document_id)), **fields}

    @router.post("/knowledge/documents/{document_id}/reprocess")
    @translate
    async def reprocess_document(document_id: str, user: User = Depends(principal)):
        doc, _, a = await document_access(user, document_id, Action.EDIT_POLICY)
        if doc.get("pending_version"):
            job = {**(doc.get("job") or {}), "state": "queued", "attempts": 0, "lease_until": None}
            await S.store.update(DOCUMENTS, document_id, {"job": job}, expect_version=doc["version"])
            await ingest().process(document_id)
        await record(a, "knowledge.document_reprocess", document_id, {})
        return document_view(await S.store.get(DOCUMENTS, document_id))

    @router.delete("/knowledge/documents/{document_id}")
    @translate
    async def delete_document(document_id: str, user: User = Depends(principal)):
        doc, _, a = await document_access(user, document_id, Action.EDIT_POLICY)
        await ingest().delete_document(document_id)
        await record(a, "knowledge.document_delete", document_id, {"source_id": doc["source_id"]})
        return {"deleted": True}


__all__ = ["register", "check_source_refs", "source_view", "document_view"]
