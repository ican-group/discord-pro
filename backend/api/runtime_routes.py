"""Runtime configuration resolution and worker status (Phase 4A)."""
from fastapi import APIRouter, Depends

from api.common import Services, load_access, require_superadmin, translate
from core.hierarchy import Level
from core.identity import User
from runtime.pipeline import Pipeline
from runtime.worker import HEARTBEATS


def register(router: APIRouter, S: Services, principal) -> None:
    @router.get("/instances/{instance_id}/runtime-config")
    @translate
    async def runtime_config(instance_id: str, user: User = Depends(principal)):
        """What the runtime pipeline would resolve for this instance: availability,
        blockers, provider/model, credential (public view only), knowledge plan and
        limits. No provider call, no secrets."""
        await load_access(S, user, Level.BOT_INSTANCE, instance_id)
        pipeline = Pipeline(S.store, box=S.box, clock=S.clock, providers=S.providers, retriever=S.retriever,
                            catalog=S.catalog, platform_policy=S.platform_policy)
        return await pipeline.runtime_config(instance_id)

    @router.get("/platform/workers")
    @translate
    async def worker_status(user: User = Depends(principal)):
        require_superadmin(user)
        from core import deletion as dl
        counts = {}
        for status in ("pending", "running", "awaiting_external_cleanup", "cleanup_failed", "failed", "completed"):
            counts[status] = await S.store.count(dl.JOBS, {"status": status})
        beats = await S.store.find(HEARTBEATS, {}, sort="at", descending=True, limit=50)
        from runtime import delivery as dlv
        deliveries = {st: await S.store.count(dlv.COLLECTION, {"status": st})
                      for st in ("pending", "sending", "retry", "uncertain", "failed_before_send")}
        return {"deletion_jobs": counts, "deliveries": deliveries,
                "workers": [{"id": b["id"], "state": b.get("state"), "at": b.get("at")} for b in beats]}
