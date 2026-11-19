"""Bot Integrations (spec 012 §5; Phase 4E Zendesk). Pure domain, stdlib only.

A Zendesk integration is a **Bot Integration**, not a Bot Instance:
Enterprise → Organisation → Bot → Zendesk Integration. It has exactly one
parent Bot, its ancestry ids are server-assigned and immutable, and it is
available only when its Bot chain is available, it is ``enabled``, and the
effective policy allows the ``zendesk`` integration type.

Object-specific settings (validated here, stored on the integration):
``reply_mode`` (``auto_reply`` | ``agent_assist`` | ``suggest_only``),
``ticket_groups`` and ``ticket_tags`` routing filters (Z13: no match ⇒
ignore), ``credential_id`` (a reference, never a secret).

Inheritable configuration (``behaviour.*``, ``knowledge.*`` …) is *not*
stored here as values: the integration keeps sparse ``overrides`` that the
one registry/resolver applies on top of the Bot's effective configuration
(``core.resolver.resolve_integration``, closes X2).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List

from core.hierarchy import HierarchyError

COLLECTION = "bot_integrations"
TYPES = ("zendesk",)


class ReplyMode(str, Enum):
    AUTO_REPLY = "auto_reply"          # public comment (+ private audit note)
    AGENT_ASSIST = "agent_assist"      # private internal note with the suggested reply
    SUGGEST_ONLY = "suggest_only"      # stored suggestion only; nothing posted


_GROUP = re.compile(r"[1-9][0-9]{0,19}")
_TAG = re.compile(r"[a-z0-9][a-z0-9_\-:/.]{0,79}")
_SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NAME_MAX = 100


class IntegrationError(HierarchyError):
    pass


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > _NAME_MAX:
        raise IntegrationError("The name must be 1-100 characters.")
    return name.strip()


def validate_zendesk_settings(raw: Any, current: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return the full, normalised settings (``current`` merged with ``raw``)."""
    base = {"reply_mode": ReplyMode.AGENT_ASSIST.value, "ticket_groups": [], "ticket_tags": []}
    base.update(current or {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise IntegrationError("settings must be an object")
    unknown = set(raw) - set(base)
    if unknown:
        raise IntegrationError(f"Unknown Zendesk settings: {', '.join(sorted(unknown))}.")
    out = dict(base)
    if "reply_mode" in raw:
        try:
            out["reply_mode"] = ReplyMode(raw["reply_mode"]).value
        except ValueError:
            raise IntegrationError("reply_mode must be auto_reply, agent_assist or suggest_only.")
    if "ticket_groups" in raw:
        groups = raw["ticket_groups"]
        if not isinstance(groups, list) or len(groups) > 50 or \
                not all(isinstance(g, (str, int)) and _GROUP.fullmatch(str(g)) for g in groups):
            raise IntegrationError("ticket_groups must be up to 50 numeric Zendesk group ids.")
        out["ticket_groups"] = sorted({str(g) for g in groups})
    if "ticket_tags" in raw:
        tags = raw["ticket_tags"]
        if not isinstance(tags, list) or len(tags) > 50 or \
                not all(isinstance(t, str) and _TAG.fullmatch(t.strip().lower()) for t in tags):
            raise IntegrationError("ticket_tags must be up to 50 Zendesk tags.")
        out["ticket_tags"] = sorted({t.strip().lower() for t in tags})
    return out


def ticket_matches(settings: Dict[str, Any], ticket: Dict[str, Any]) -> bool:
    """Z13 routing filters. No filter ⇒ every ticket; filters set and no match ⇒
    ignore (no fallback to other integrations)."""
    groups = settings.get("ticket_groups") or []
    tags = settings.get("ticket_tags") or []
    if groups and str(ticket.get("group_id") or "") not in set(groups):
        return False
    if tags and not set(tags) & {str(t).lower() for t in ticket.get("tags") or []}:
        return False
    return True


def valid_subdomain(value: Any) -> bool:
    return isinstance(value, str) and bool(_SUBDOMAIN.fullmatch(value))


def _deletion_view(d):
    if not d:
        return None
    state = {"requested": "requested", "cleanup": "cleanup_pending", "cleanup_failed": "cleanup_failed",
             "cleanup_uncertain": "cleanup_failed", "finalising": "cleanup_pending"}.get(d.get("state"), d.get("state"))
    return {"state": state, "error": d.get("error"), "attempts": d.get("attempts", 0),
            "requested_at": d.get("requested_at"), "next_attempt_at": d.get("next_attempt_at"), "retryable": True}


def public_view(doc: dict) -> Dict[str, Any]:
    """Safe view: never secrets, hashes or the webhook path secret."""
    hook = doc.get("webhook") or {}
    return {"id": doc["id"], "type": doc["type"], "name": doc.get("name"), "status": doc.get("status"),
            "enterprise_id": doc["enterprise_id"], "organisation_id": doc["organisation_id"], "bot_id": doc["bot_id"],
            "settings": dict(doc.get("settings") or {}), "credential_id": doc.get("credential_id"),
            "overrides": dict(doc.get("overrides") or {}),
            "webhook": {"signing_secret_configured": bool(hook.get("signing_secret_enc")),
                        "provisioned": bool(hook.get("zendesk_webhook_id")),
                        "rotated_at": hook.get("rotated_at")},
            "sidebar_key": {"configured": bool(doc.get("sidebar_key_hint")), "hint": doc.get("sidebar_key_hint")},
            "connection": dict(doc.get("connection") or {}),
            # Z-G lifecycle as the UI must show it: requested (an in-flight request is finishing),
            # cleanup_pending, cleanup_failed (retryable), finalising; absent record ⇒ deleted
            "deletion": _deletion_view(doc.get("deletion")),
            "created_at": doc.get("created_at"), "updated_at": doc.get("updated_at"), "version": doc.get("version")}


__all__ = ["COLLECTION", "TYPES", "ReplyMode", "IntegrationError", "validate_name", "validate_zendesk_settings",
           "ticket_matches", "valid_subdomain", "public_view"]
