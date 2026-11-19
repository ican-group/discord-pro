"""Usage reporting (Phase 6A, F1).

``GET /{enterprises|organisations|bots|instances}/{id}/usage?period=YYYY-MM``
and ``GET /integrations/{id}/usage``: AI messages for the period by billing
state (from the idempotent usage ledger), preview and verification usage
reported separately (F5, K-H), and the monthly quotas that apply.

Tenant boundaries: the caller needs READ on the target (else 404). Counts
are filtered by the target's own id in the ledger, so nothing outside the
target is counted. An Enterprise-wide quota counter is shown only to callers
who can read the Enterprise itself (an Organisation member never learns
other Organisations' consumption).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.common import TYPE_LEVEL, Services, load_access, translate
from core import resolver
from core.authz import Action
from core.hierarchy import Level, NotFound, is_valid_id
from core.identity import User
from runtime.usage import COUNTERS, LEDGER, period_of

STATES = ("settled", "reserved", "reserving", "uncertain", "released", "rejected")
FIELD = {Level.ENTERPRISE: "enterprise_id", Level.ORGANISATION: "organisation_id", Level.BOT: "bot_id",
         Level.BOT_INSTANCE: "instance_id"}
_PERIOD = re.compile(r"20[0-9]{2}-(0[1-9]|1[0-2])")


def register(router: APIRouter, S: Services, principal) -> None:
    def period_arg(period: Optional[str]) -> str:
        if not period:
            return period_of(S.clock())
        if not _PERIOD.fullmatch(period):
            raise HTTPException(422, "period must be YYYY-MM.")
        return period

    async def ledger_counts(flt: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for st in STATES:
            out[st] = await S.store.count(LEDGER, {**flt, "state": st})
        out["preview_settled"] = await S.store.count(LEDGER, {**flt, "state": "settled", "preview": True})
        out["verification_settled"] = await S.store.count(LEDGER, {**flt, "state": "settled", "purpose": "verification"})
        return out

    async def quotas(user: User, a, period: str):
        ent_ok = True
        if a.chain.target.level is not Level.ENTERPRISE:
            try:
                await load_access(S, user, Level.ENTERPRISE, a.chain.enterprise_id, Action.READ)
            except Exception:
                ent_ok = False
        out = []
        ent_policy = resolver.effective_policy([a.chain.enterprise], S.platform_policy)[0]
        if ent_ok:
            c = await S.store.get(COUNTERS, f"{a.chain.enterprise_id}:{period}")
            out.append({"scope": "enterprise", "id": a.chain.enterprise_id,
                        "limit": ent_policy.get("limits.max_monthly_ai_messages"), "used": int((c or {}).get("count", 0))})
        if a.chain.organisation is not None:
            org_policy = resolver.effective_policy([a.chain.enterprise, a.chain.organisation], S.platform_policy)[0]
            c = await S.store.get(COUNTERS, f"{a.chain.organisation.id}:{period}")
            out.append({"scope": "organisation", "id": a.chain.organisation.id,
                        "limit": org_policy.get("limits.max_monthly_ai_messages"), "used": int((c or {}).get("count", 0))})
        return out

    def view(target_type: str, target_id: str, period: str, counts, quota_list, extra=None):
        return {"target": {"type": target_type, "id": target_id}, "period": period,
                "ai_messages": {k: counts[k] for k in STATES},
                "preview": {"settled": counts["preview_settled"]},
                "verification": {"settled": counts["verification_settled"]},
                "billing_note": "settled = billed; uncertain = may have been billed, awaiting review; "
                                "reserved/reserving = in progress; released/rejected = not billed. "
                                "Preview traffic is counted and billed like live traffic and reported separately.",
                "quotas": quota_list, **(extra or {})}

    @router.get("/{type_:objtype}/{object_id}/usage")
    @translate
    async def object_usage(type_: str, object_id: str, period: Optional[str] = Query(None),
                           user: User = Depends(principal)):
        level = TYPE_LEVEL.get(type_)
        if level is None:
            raise NotFound("Not found.")
        a = await load_access(S, user, level, object_id, Action.READ)
        p = period_arg(period)
        flt = {FIELD[level]: object_id, "enterprise_id": a.chain.enterprise_id, "period": p}
        extra = None
        if level is Level.BOT_INSTANCE and a.chain.instance.channel.type.value == "webchat":
            extra = {"webchat_daily_limit": S.settings.webchat_daily_limit_per_instance}
        return view(type_, object_id, p, await ledger_counts(flt), await quotas(user, a, p), extra)

    @router.get("/integrations/{integration_id}/usage")
    @translate
    async def integration_usage(integration_id: str, period: Optional[str] = Query(None),
                                user: User = Depends(principal)):
        from core import integrations as zi
        doc = await S.store.get(zi.COLLECTION, integration_id) if is_valid_id(integration_id) else None
        if doc is None:
            raise NotFound("Not found.")
        a = await load_access(S, user, Level.BOT, doc["bot_id"], Action.READ)
        if a.chain.enterprise_id != doc["enterprise_id"]:
            raise NotFound("Not found.")
        p = period_arg(period)
        flt = {"integration_id": integration_id, "enterprise_id": a.chain.enterprise_id, "period": p}
        return view("integrations", integration_id, p, await ledger_counts(flt), await quotas(user, a, p))


__all__ = ["register"]
