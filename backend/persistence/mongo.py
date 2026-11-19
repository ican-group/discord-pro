"""MongoDB adapter for the Spec 012 core (Phase 3).

Implements :class:`core.hierarchy.Store` with single-document atomic operations
only, so every guarantee holds on the production topology (a standalone
``mongod`` — no replica set, hence no multi-document transactions):

* ``_id`` = ``id``; unique indexes from ``core.hierarchy.UNIQUE_FIELDS``;
* compare-and-swap updates/deletes filtered on ``version``;
* ``find_one_and_delete`` for single-use tokens;
* ``$jsonSchema`` validators reject malformed documents at the database.

:func:`prepare_database` refuses to touch a database that is neither empty nor
already marked as a Spec 012 database, so the new API can never be pointed at
the legacy production data by mistake.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from core.hierarchy import (ConcurrentModification, DuplicateKey, HierarchyError, INDEXES as CORE_INDEXES,
                            NotFound, UNIQUE_FIELDS)

SCHEMA_MARKER_ID = "schema"
SCHEMA_NAME = "spec012"
SCHEMA_VERSION = 1


class DatabaseGuardError(RuntimeError):
    pass


def _translate(flt: Dict[str, Any]) -> Dict[str, Any]:
    """Equality-only filters built by server code. Operators are rejected so a
    value can never be interpreted as a query operator."""
    out: Dict[str, Any] = {}
    for key, value in (flt or {}).items():
        if not isinstance(key, str) or not key or key.startswith("$") or "$" in key:
            raise HierarchyError("Invalid filter.")
        if isinstance(value, dict):
            raise HierarchyError("Invalid filter.")
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            if any(isinstance(v, (dict, list)) for v in values):
                raise HierarchyError("Invalid filter.")
            value = {"$in": values}
        out["_id" if key == "id" else key] = value
    return out


def _out(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


class MongoStore:
    def __init__(self, db):
        self.db = db

    async def get(self, collection: str, object_id: str) -> Optional[dict]:
        return _out(await self.db[collection].find_one({"_id": object_id}))

    async def insert(self, collection: str, doc: dict) -> None:
        try:
            await self.db[collection].insert_one({**doc, "_id": doc["id"]})
        except DuplicateKeyError:
            raise DuplicateKey("Duplicate key.")

    async def update(self, collection: str, object_id: str, fields: dict, *,
                     expect_version: Optional[int] = None) -> None:
        if "id" in fields or "_id" in fields or any(k.startswith("$") for k in fields):
            raise HierarchyError("Invalid update.")
        flt: Dict[str, Any] = {"_id": object_id}
        sets = dict(fields)
        if expect_version is not None:
            flt["version"] = expect_version
            sets["version"] = expect_version + 1
        try:
            res = await self.db[collection].update_one(flt, {"$set": sets})
        except DuplicateKeyError:
            raise DuplicateKey("Duplicate key.")
        if res.matched_count == 0:
            if await self.db[collection].find_one({"_id": object_id}, {"_id": 1}) is None:
                raise NotFound("Not found.")
            raise ConcurrentModification()

    async def find(self, collection: str, flt: dict, *, limit: int = 0, sort: Optional[str] = None,
                   descending: bool = False) -> List[dict]:
        cursor = self.db[collection].find(_translate(flt))
        if sort:
            cursor = cursor.sort("_id" if sort == "id" else sort, DESCENDING if descending else ASCENDING)
        if limit:
            cursor = cursor.limit(int(limit))
        return [_out(d) async for d in cursor]

    async def find_prefix(self, collection: str, field: str, prefix: str, *, after: Optional[str] = None,
                          limit: int = 50, flt: Optional[dict] = None) -> List[dict]:
        """Bounded, ordered prefix search (Phase 6A F2) as an index range: never a
        regular expression and never a client-supplied operator."""
        from core.hierarchy import _check_prefix_args
        _check_prefix_args(field, prefix, after, limit)
        query = _translate(flt or {})
        rng: Dict[str, Any] = {"$gte": prefix, "$lt": prefix + "\uffff"}
        if after is not None:
            rng = {"$gt": max(after, prefix), "$lt": prefix + "\uffff"} if after >= prefix else rng
        query[field] = rng
        cursor = self.db[collection].find(query).sort(field, ASCENDING).limit(int(limit))
        return [_out(d) async for d in cursor]

    async def scan(self, collection: str, *, after: Optional[str] = None, limit: int = 100) -> List[dict]:
        """Every document of one collection in ``_id`` order, ``after`` exclusive (Phase 6B)."""
        from core.hierarchy import _check_scan_args
        _check_scan_args(after, limit)
        query = {"_id": {"$gt": after}} if after is not None else {}
        cursor = self.db[collection].find(query).sort("_id", ASCENDING).limit(int(limit))
        return [_out(d) async for d in cursor]

    async def set_field_if(self, collection: str, object_id: str, path: str, expected: str, value: str) -> bool:
        """Atomic field-level compare-and-set for key rotation (Phase 6B): applies only
        while the field still holds ``expected``; the version is unchanged."""
        from core.hierarchy import _check_enc_path
        _check_enc_path(path)
        if not isinstance(expected, str) or not isinstance(value, str):
            raise HierarchyError("Invalid update.")
        res = await self.db[collection].update_one({"_id": object_id, path: expected}, {"$set": {path: value}})
        return res.matched_count == 1

    async def count(self, collection: str, flt: dict) -> int:
        return await self.db[collection].count_documents(_translate(flt))

    async def delete(self, collection: str, object_id: str, *, expect_version: Optional[int] = None) -> bool:
        flt: Dict[str, Any] = {"_id": object_id}
        if expect_version is not None:
            flt["version"] = expect_version
        res = await self.db[collection].delete_one(flt)
        return res.deleted_count == 1

    async def delete_many(self, collection: str, flt: dict) -> int:
        translated = _translate(flt)
        if not translated:
            raise HierarchyError("Refusing an unscoped bulk delete.")
        res = await self.db[collection].delete_many(translated)
        return res.deleted_count

    async def take(self, collection: str, object_id: str) -> Optional[dict]:
        return _out(await self.db[collection].find_one_and_delete({"_id": object_id}))

    async def increment(self, collection: str, object_id: str, field: str, amount: int, *,
                        initial: dict) -> int:
        """Atomic counter on one document ($inc with upsert): no compare-and-swap
        retry loop to exhaust under heavy contention (Phase 4F load finding)."""
        from pymongo import ReturnDocument
        for _ in range(3):                     # concurrent first inserts: the losers retry as updates
            try:
                doc = await self.db[collection].find_one_and_update(
                    {"_id": object_id}, {"$inc": {field: int(amount)},
                                         "$setOnInsert": {**initial, "id": object_id, "version": 1}},
                    upsert=True, return_document=ReturnDocument.AFTER)
                return int(doc[field])
            except DuplicateKeyError:
                continue
        raise ConcurrentModification()


# ═════════════════════════════════ schemas and indexes ═════════════════════════════════

_STR = {"bsonType": "string"}
_ID = {"bsonType": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
_INT = {"bsonType": ["int", "long"]}
_LIFECYCLE = {"bsonType": "object", "required": ["state"],
              "properties": {"state": {"enum": ["active", "suspended", "archived", "deleted"]},
                             "authority": {"bsonType": ["int", "long", "null"]}}}


def _node(extra_required: List[str], extra_props: Dict[str, Any]) -> Dict[str, Any]:
    props = {"id": _ID, "name": _STR, "lifecycle": _LIFECYCLE, "overrides": {"bsonType": "object"},
             "version": _INT, "level": _STR, **extra_props}
    return {"$jsonSchema": {"bsonType": "object",
                            "required": ["id", "name", "level", "lifecycle", "overrides", "version", "created_at",
                                         *extra_required],
                            "properties": props}}


SCHEMAS: Dict[str, Dict[str, Any]] = {
    "enterprises": _node(["slug", "plan", "policy"], {"slug": _STR, "plan": {"enum": ["free", "pro", "enterprise"]},
                                                      "policy": {"bsonType": "object"}}),
    "organisations": _node(["enterprise_id", "policy"], {"enterprise_id": _ID, "policy": {"bsonType": "object"}}),
    "bots": _node(["enterprise_id", "organisation_id"], {"enterprise_id": _ID, "organisation_id": _ID}),
    "bot_instances": _node(["enterprise_id", "organisation_id", "bot_id", "channel"],
                           {"enterprise_id": _ID, "organisation_id": _ID, "bot_id": _ID,
                            "channel": {"bsonType": "object", "required": ["type"],
                                        "properties": {"type": {"enum": ["discord", "telegram", "webchat"]}}}}),
    # X4 (option C): one document per (enterprise_id, user_id); grants inside it.
    "memberships": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "user_id", "grants", "version"],
        "properties": {"id": {"bsonType": "string", "pattern": "^[0-9a-f-]{36}:[0-9a-f-]{36}$"},
                       "enterprise_id": _ID, "user_id": _ID, "version": _INT,
                       "role": {"enum": ["viewer", "editor", "admin", "owner", None]},
                       "grants": {"bsonType": "array", "items": {
                           "bsonType": "object", "required": ["scope_level", "scope_id", "role"],
                           "properties": {"scope_level": {"enum": ["organisation", "bot"]}, "scope_id": _ID,
                                          "role": {"enum": ["viewer", "editor", "admin"]}}}}}}},
    "membership_guards": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "owner_count", "seq", "recent", "version"],
        "properties": {"id": _ID, "enterprise_id": _ID, "owner_count": {"bsonType": ["int", "long"], "minimum": 0},
                       "seq": _INT, "version": _INT, "recent": {"bsonType": "array"},
                       "pending": {"bsonType": ["object", "null"]}}}},
    "invitations": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "token_hash", "email", "enterprise_id", "scope_level", "scope_id", "role", "status",
                     "invited_by", "expires_at", "version"],
        "properties": {"id": _ID, "enterprise_id": _ID, "scope_id": _ID, "invited_by": _ID, "email": _STR,
                       "token_hash": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
                       "role": {"enum": ["viewer", "editor", "admin", "owner"]},
                       "status": {"enum": ["pending", "accepted", "revoked"]}, "version": _INT}}},
    "credentials": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "enterprise_id", "provider", "name", "fingerprint", "status", "secret_enc", "version"],
        "properties": {"id": _ID, "enterprise_id": _ID, "organisation_id": {"bsonType": ["string", "null"]},
                       "provider": {"enum": ["anthropic", "openai", "telegram_bot", "zendesk_oauth",
                                             "zendesk_api_token", "api_integration"]},
                       "status": {"enum": ["active", "revoked"]}, "secret_enc": _STR, "version": _INT,
                       "usable_by": {"bsonType": "array", "maxItems": 200, "items": {
                           "bsonType": "object", "required": ["level", "id"],
                           "properties": {"level": {"enum": ["organisation", "bot"]}, "id": _ID}}}}}},
    "platform_credentials": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "status", "secret_enc", "fingerprint", "version"],
        "properties": {"id": {"enum": ["zendesk_oauth_client"]},
                       "status": {"enum": ["active", "revoked"]}, "secret_enc": _STR, "version": _INT}}},
    "users": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "email", "is_platform_superadmin", "token_version", "status", "version"],
        "properties": {"id": _ID, "email": _STR, "is_platform_superadmin": {"bsonType": "bool"},
                       "token_version": _INT, "status": {"enum": ["active", "disabled"]}, "version": _INT}}},
    "channel_bindings": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "holders", "version"],
        "properties": {"id": {"bsonType": "string", "pattern": "^(discord|telegram|webchat):"},
                       "holders": {"bsonType": "array", "items": {
                           "bsonType": "object", "required": ["instance_id", "enterprise_id", "scopes"],
                           "properties": {"instance_id": _ID, "enterprise_id": _ID,
                                          "scopes": {"bsonType": "array"}}}}, "version": _INT}}},
    "deletion_jobs": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "target_level", "target_id", "status", "requested_by", "version"],
        "properties": {"status": {"enum": ["pending", "running", "awaiting_external_cleanup", "cleanup_failed",
                                           "completed", "failed"]},
                       "external_actions": {"bsonType": "array", "items": {
                           "bsonType": "object",
                           "required": ["id", "action", "target", "mandatory", "state", "attempts"],
                           "properties": {"mandatory": {"bsonType": "bool"},
                                          "state": {"enum": ["pending", "retry_wait", "succeeded", "not_required",
                                                             "failed", "abandoned"]},
                                          "attempts": _INT}}},
                       "target_level": {"enum": ["enterprise", "organisation", "bot", "bot_instance", "user"]},
                       "version": _INT}}},
    "deletion_previews": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "actor_id", "level", "target_id", "digest", "expires_at_dt"],
        "properties": {"expires_at_dt": {"bsonType": "date"}}}},
    "audit_events": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "at", "action"],
        "properties": {"id": _ID, "action": _STR, "enterprise_id": {"bsonType": ["string", "null"]}}}},
    "platform_state": {"$jsonSchema": {"bsonType": "object", "required": ["id"]}},
    # Phase 4A
    "sessions": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "user_id", "expires_at", "expires_at_dt", "revoked", "version"],
        "properties": {"id": _ID, "user_id": _ID, "revoked": {"bsonType": "bool"},
                       "expires_at_dt": {"bsonType": "date"}}}},
    "password_resets": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "user_id", "expires_at", "expires_at_dt"],
        "properties": {"id": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"}, "user_id": _ID,
                       "expires_at_dt": {"bsonType": "date"}}}},
    "auth_throttle": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "window_start", "count", "version"],
        "properties": {"count": {"bsonType": ["int", "long"], "minimum": 0}}}},
    "usage_counters": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "period", "count", "version"],
        "properties": {"enterprise_id": _ID, "count": {"bsonType": ["int", "long"], "minimum": 0}}}},
    # Phase 4B (Webchat)
    "chat_sessions": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "token_hash", "enterprise_id", "organisation_id", "bot_id", "instance_id", "expires_at_dt"],
        "properties": {"id": _ID, "token_hash": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
                       "enterprise_id": _ID, "organisation_id": _ID, "bot_id": _ID, "instance_id": _ID,
                       "expires_at_dt": {"bsonType": "date"}}}},
    "conversations": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "enterprise_id", "organisation_id", "bot_id", "instance_id", "messages", "expires_at_dt",
                     "version"],
        "properties": {"enterprise_id": _ID, "organisation_id": _ID, "bot_id": _ID, "instance_id": _ID,
                       "messages": {"bsonType": "array", "maxItems": 400}, "expires_at_dt": {"bsonType": "date"}}}},
    "chat_requests": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "session_id", "enterprise_id", "instance_id", "status"],
        "properties": {"status": {"enum": ["processing", "generated", "done", "uncertain"]}, "enterprise_id": _ID,
                       "instance_id": _ID}}},
    "usage_ledger": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "state", "period", "counters", "enterprise_id", "version"],
        "properties": {"state": {"enum": ["reserving", "reserved", "settled", "released", "rejected", "uncertain"]},
                       "enterprise_id": _ID, "counters": {"bsonType": "array"}, "version": _INT}}},
    "channel_state": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "kind", "enterprise_id", "instance_id", "expires_at_dt"],
        "properties": {"enterprise_id": _ID, "instance_id": _ID, "expires_at_dt": {"bsonType": "date"}}}},
    "bot_logos": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "bot_id", "content_type", "size", "data"],
        "properties": {"content_type": {"enum": ["image/png", "image/jpeg", "image/gif", "image/webp"]},
                       "size": {"bsonType": ["int", "long"], "maximum": 262144}, "data": {"bsonType": "binData"}}}},
    "usage_events": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "at", "kind", "enterprise_id"],
        "properties": {"enterprise_id": _ID, "kind": _STR}}},
    # Phase 4D
    "outbound_deliveries": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "request_key", "channel", "enterprise_id", "organisation_id", "bot_id",
                     "target", "parts", "status", "version"],
        # exactly one owner below the Bot: a Bot Instance or a Bot Integration (Zendesk)
        "oneOf": [{"properties": {"instance_id": _ID, "integration_id": {"bsonType": "null"}},
                   "required": ["instance_id"]},
                  {"properties": {"integration_id": _ID, "instance_id": {"bsonType": "null"}},
                   "required": ["integration_id"]}],
        "properties": {"channel": {"enum": ["telegram", "discord", "zendesk"]},
                       "status": {"enum": ["pending", "sending", "retry", "delivered", "failed_before_send",
                                           "uncertain", "resolved"]},
                       "expires_at_dt": {"bsonType": "date"},
                       "enterprise_id": _ID, "organisation_id": _ID, "bot_id": _ID,
                       "parts": {"bsonType": "array", "minItems": 1, "maxItems": 50}}}},
    "discord_applications": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "enterprise_id", "application_id", "bot_token_enc", "client_secret_enc", "status",
                     "gateway_desired", "permissions", "intents", "version"],
        "properties": {"enterprise_id": _ID, "application_id": {"bsonType": "string", "pattern": "^[1-9][0-9]{15,20}$"},
                       "status": {"enum": ["enabled", "disabled"]}, "gateway_desired": {"enum": ["running", "stopped"]},
                       "bot_token_enc": _STR, "client_secret_enc": _STR}}},
    "discord_installations": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "application_id", "guild_id", "state"],
        "properties": {"enterprise_id": _ID, "state": {"enum": ["present"]}}}},
    "discord_gateway_status": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "state", "version"],
        "properties": {"enterprise_id": _ID,
                       "state": {"enum": ["stopped", "connecting", "connected", "reconnecting", "restarting",
                                          "failed_auth"]}}}},
    "discord_share_links": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "token_hash", "enterprise_id", "organisation_id", "bot_id", "instance_id",
                     "application_id", "created_by", "status", "expires_at_dt", "version"],
        "properties": {"enterprise_id": _ID, "instance_id": _ID, "status": {"enum": ["active", "used", "revoked"]},
                       "token_hash": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
                       "expires_at_dt": {"bsonType": "date"}}}},
    "oauth_states": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "kind", "enterprise_id", "expires_at_dt"],
        "oneOf": [{"properties": {"kind": {"enum": ["discord_install", "discord_pending"]}},
                   "required": ["instance_id", "application_id"]},
                  {"properties": {"kind": {"enum": ["zendesk_install"]}},
                   "required": ["integration_id", "subdomain", "verifier_enc"]}],
        "properties": {"enterprise_id": _ID, "instance_id": _ID, "integration_id": _ID,
                       "expires_at_dt": {"bsonType": "date"}}}},
    # Phase 4K: knowledge
    "knowledge_sources": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "name", "status", "version"],
        "properties": {"enterprise_id": _ID, "organisation_id": {"bsonType": ["string", "null"]},
                       "status": {"enum": ["active", "disabled", "revoked", "archived"]}}}},
    "knowledge_documents": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "source_id", "name", "status", "version"],
        "properties": {"enterprise_id": _ID, "source_id": _ID,
                       "status": {"enum": ["processing", "ready", "failed", "deleting"]}}}},
    "knowledge_chunks": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "source_id", "document_id", "doc_version", "text",
                                           "terms", "acl_scopes"],
        "properties": {"enterprise_id": _ID, "source_id": _ID, "document_id": _ID,
                       "terms": {"bsonType": "array", "maxItems": 5000},
                       "acl_scopes": {"bsonType": "array", "minItems": 1}}}},
    "knowledge_files": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "source_id", "document_id", "data"],
        "properties": {"enterprise_id": _ID, "data": {"bsonType": "binData"}}}},
    # Phase 4E: Zendesk Bot Integrations
    "bot_integrations": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["id", "type", "name", "enterprise_id", "organisation_id", "bot_id", "status", "settings",
                     "webhook", "version"],
        "properties": {"type": {"enum": ["zendesk"]}, "status": {"enum": ["enabled", "disabled"]},
                       "deletion": {"bsonType": ["object", "null"]},
                       "enterprise_id": _ID, "organisation_id": _ID, "bot_id": _ID,
                       "settings": {"bsonType": "object", "required": ["reply_mode"],
                                    "properties": {"reply_mode": {"enum": ["auto_reply", "agent_assist",
                                                                           "suggest_only"]}}},
                       "webhook": {"bsonType": "object", "required": ["path_hash", "path_enc"]}}}},
    "zendesk_events": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "organisation_id", "bot_id", "integration_id",
                                           "ticket_id", "status", "expires_at_dt", "version"],
        "properties": {"status": {"enum": ["received", "processing", "generated", "done", "uncertain", "failed"]},
                       "enterprise_id": _ID, "integration_id": _ID,
                       "ticket_id": {"bsonType": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                       "expires_at_dt": {"bsonType": "date"}}}},
    "zendesk_app_keys": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "integration_id", "hint"],
        "properties": {"id": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"}, "enterprise_id": _ID,
                       "integration_id": _ID}}},
    "suggestions": {"$jsonSchema": {
        "bsonType": "object", "required": ["id", "enterprise_id", "organisation_id", "bot_id", "integration_id",
                                           "ticket_id", "status", "source", "version"],
        "properties": {"status": {"enum": ["pending", "accepted", "dismissed", "no_suggestion"]},
                       "source": {"enum": ["webhook", "sidebar"]}, "enterprise_id": _ID, "integration_id": _ID}}},
}

EXTRA_INDEXES: Dict[str, List[List[tuple]]] = {
    "memberships": [[("user_id", 1)], [("enterprise_id", 1), ("grants.scope_id", 1)]],
    "channel_bindings": [[("holders.instance_id", 1)]],
    "invitations": [[("enterprise_id", 1), ("status", 1)], [("email", 1), ("status", 1)], [("invited_by", 1)]],
    "credentials": [[("enterprise_id", 1), ("organisation_id", 1)]],
    "audit_events": [[("enterprise_id", 1), ("at", -1)], [("at", -1)]],
    "deletion_jobs": [[("enterprise_id", 1)], [("requested_by", 1)], [("external_actions.credential_id", 1)],
                      [("status", 1)]],
    "users": [[("is_platform_superadmin", 1), ("status", 1)]],
    "sessions": [[("user_id", 1)]],
    "password_resets": [[("user_id", 1)]],
    "usage_counters": [[("enterprise_id", 1), ("period", 1)]],
    "usage_events": [[("enterprise_id", 1), ("at", -1)], [("instance_id", 1)]],
    "chat_sessions": [[("instance_id", 1)], [("enterprise_id", 1)]],
    "conversations": [[("enterprise_id", 1), ("instance_id", 1), ("updated_at", -1)]],
    "chat_requests": [[("session_id", 1)], [("enterprise_id", 1)], [("status", 1)]],
    "usage_ledger": [[("state", 1)], [("counters", 1), ("state", 1)], [("enterprise_id", 1)],
                     # usage reporting (Phase 6A F1): every count is bounded to one target and period
                     [("enterprise_id", 1), ("period", 1), ("state", 1)],
                     [("organisation_id", 1), ("period", 1), ("state", 1)], [("bot_id", 1), ("period", 1), ("state", 1)],
                     [("instance_id", 1), ("period", 1), ("state", 1)], [("integration_id", 1), ("period", 1), ("state", 1)]],
    "channel_state": [[("instance_id", 1)]],
    "outbound_deliveries": [[("status", 1), ("created_at", 1)], [("enterprise_id", 1), ("instance_id", 1)],
                            [("enterprise_id", 1), ("integration_id", 1)]],
    "discord_installations": [[("enterprise_id", 1)]],
    "discord_gateway_status": [[("enterprise_id", 1)]],
    "oauth_states": [[("enterprise_id", 1), ("instance_id", 1)]],
    "discord_share_links": [[("enterprise_id", 1), ("instance_id", 1)]],
    "bot_integrations": [[("enterprise_id", 1), ("bot_id", 1)], [("organisation_id", 1)]],
    "knowledge_sources": [[("enterprise_id", 1), ("organisation_id", 1)]],
    "knowledge_documents": [[("enterprise_id", 1), ("source_id", 1)], [("job.state", 1)]],
    "knowledge_chunks": [[("enterprise_id", 1), ("terms", 1)], [("enterprise_id", 1), ("document_id", 1)],
                         [("enterprise_id", 1), ("source_id", 1)]],
    "knowledge_files": [[("enterprise_id", 1), ("document_id", 1)]],
    "zendesk_events": [[("status", 1)], [("enterprise_id", 1), ("integration_id", 1)]],
    "zendesk_app_keys": [[("enterprise_id", 1), ("integration_id", 1)]],
    "suggestions": [[("enterprise_id", 1), ("integration_id", 1), ("created_at", -1)]],
    "bot_instances": [[("enterprise_id", 1), ("channel.application_id", 1), ("channel.dm_enabled", 1)]],
}

LEGACY_COLLECTIONS = frozenset({"workspaces", "workspace_members", "bot_config", "connections", "discord_config",
                                "telegram_config", "api_keys", "embed_configs", "connected_accounts"})


async def prepare_database(db) -> None:
    """Create validators and indexes on a fresh or Spec 012 database; refuse anything else."""
    names = set(await db.list_collection_names())
    marker = await db["platform_state"].find_one({"_id": SCHEMA_MARKER_ID}) if "platform_state" in names else None
    if marker is None:
        if names & LEGACY_COLLECTIONS:
            raise DatabaseGuardError("Refusing: this is a legacy (pre-Spec 012) database.")
        for name in names:
            if await db[name].estimated_document_count() > 0:
                raise DatabaseGuardError("Refusing: database is not empty and not a Spec 012 database.")
    elif marker.get("schema") != SCHEMA_NAME:
        raise DatabaseGuardError("Refusing: unknown database schema marker.")
    for name, validator in SCHEMAS.items():
        if name in names:
            await db.command("collMod", name, validator=validator, validationLevel="strict",
                             validationAction="error")
        else:
            await db.create_collection(name, validator=validator, validationLevel="strict",
                                       validationAction="error")
    for name, fields in UNIQUE_FIELDS.items():
        for f in fields:
            await db[name].create_index([(f, ASCENDING)], unique=True, name=f"uniq_{f}")
    # X4: exactly one membership document per (Enterprise, user)
    await db["memberships"].create_index([("enterprise_id", ASCENDING), ("user_id", ASCENDING)], unique=True,
                                         name="uniq_enterprise_user")
    for source in (CORE_INDEXES, EXTRA_INDEXES):
        for name, specs in source.items():
            for keys in specs:
                await db[name].create_index(list(keys))
    for ttl_collection in ("deletion_previews", "sessions", "password_resets", "auth_throttle", "chat_sessions",
                           "conversations", "chat_requests", "channel_state", "oauth_states",
                           # C5: only terminal deliveries carry expires_at_dt; others never expire
                           "outbound_deliveries", "discord_share_links", "zendesk_events", "suggestions"):
        await db[ttl_collection].create_index([("expires_at_dt", ASCENDING)], expireAfterSeconds=0,
                                              name="ttl_expires")
    if marker is None:
        await db["platform_state"].update_one(
            {"_id": SCHEMA_MARKER_ID},
            {"$setOnInsert": {"id": SCHEMA_MARKER_ID, "schema": SCHEMA_NAME, "version": SCHEMA_VERSION}},
            upsert=True)
    from core.identity import ensure_superadmin_guard
    await ensure_superadmin_guard(MongoStore(db))


__all__ = ["MongoStore", "prepare_database", "DatabaseGuardError", "SCHEMAS", "EXTRA_INDEXES",
           "LEGACY_COLLECTIONS"]
