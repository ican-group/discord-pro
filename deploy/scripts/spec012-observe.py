"""Read-only production observer for Spec 012 real-service validation (Phase 7).

    docker exec -i bitbotpro-v12-api /app/docker-entrypoint.sh python - < deploy/scripts/spec012-observe.py

Prints counts, states, statuses, errors and recent audit actions from bitbotpro_v12 so each
real-service step can be verified on the server. Never prints *_enc fields, hashes, tokens,
message text or e-mail bodies; writes nothing.
"""
import asyncio
import os
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000)["bitbotpro_v12"]
    c = lambda name, flt=None: db[name].count_documents(flt or {})
    print("users", await c("users"), "| superadmins", await c("users", {"is_platform_superadmin": True}),
          "| enterprises", await c("enterprises"), "| organisations", await c("organisations"), "| bots", await c("bots"))
    print("memberships", await c("memberships"), "| invitations by status",
          dict(Counter([d.get("status") async for d in db.invitations.find({}, {"status": 1})])))
    async for d in db.credentials.find({}, {"provider": 1, "status": 1, "name": 1, "organisation_id": 1}):
        print(f"  credential {d.get('provider')} '{d.get('name')}' {d.get('status')} owner={'org' if d.get('organisation_id') else 'enterprise'}")
    async for d in db.platform_credentials.find({}, {"status": 1, "fingerprint": 1}):
        print(f"  platform credential {d['_id']} {d.get('status')}")
    async for d in db.bot_instances.find({}, {"name": 1, "channel": 1, "lifecycle": 1, "telegram_webhook": 1}):
        ch = d.get("channel") or {}
        tw = d.get("telegram_webhook") or {}
        extra = f" webhook registered={tw.get('registered')} sync_error={tw.get('sync_error')}" if ch.get("type") == "telegram" else ""
        if ch.get("type") == "discord":
            extra = f" guild={'set' if ch.get('guild_id') else 'none'} listen={ch.get('listen_mode')} passive={ch.get('passive_mode')}"
        if ch.get("type") == "webchat":
            extra = f" origins={ch.get('allowed_origins')}"
        print(f"  instance '{d.get('name')}' {ch.get('type')} {(d.get('lifecycle') or {}).get('state')}{extra}")
    async for d in db.discord_applications.find({}, {"application_id": 1, "status": 1, "gateway_desired": 1}):
        print(f"  discord app {d.get('application_id')} status={d.get('status')} gateway_desired={d.get('gateway_desired')}")
    async for d in db.discord_gateway_status.find({}, {"state": 1, "last_error": 1, "attempts": 1}):
        print(f"  discord gateway state={d.get('state')} last_error={d.get('last_error')} attempts={d.get('attempts')}")
    async for d in db.bot_integrations.find({}, {"type": 1, "name": 1, "status": 1, "settings": 1, "connection": 1, "webhook": 1}):
        hook = d.get("webhook") or {}
        print(f"  integration {d.get('type')} '{d.get('name')}' status={d.get('status')} reply_mode={(d.get('settings') or {}).get('reply_mode')} "
              f"connection={(d.get('connection') or {}).get('status')} signing={'yes' if hook.get('signing_secret_enc') else 'no'} provisioned={bool(hook.get('zendesk_webhook_id'))}")
    print("knowledge sources", await c("knowledge_sources"), "| documents by state",
          dict(Counter([(d.get("job") or {}).get("state") or d.get("state") async for d in db.knowledge_documents.find({}, {"job": 1, "state": 1})])))
    print("usage ledger by state", dict(Counter([d.get("state") async for d in db.usage_ledger.find({}, {"state": 1})])),
          "| preview", await c("usage_ledger", {"preview": True}))
    print("usage events by kind", dict(Counter([d.get("kind") async for d in db.usage_events.find({}, {"kind": 1})])))
    print("conversations", await c("conversations"), "| chat requests by status",
          dict(Counter([d.get("status") async for d in db.chat_requests.find({}, {"status": 1})])))
    print("outbound deliveries by status", dict(Counter([d.get("status") async for d in db.outbound_deliveries.find({}, {"status": 1})])))
    print("zendesk events by status", dict(Counter([d.get("status") async for d in db.zendesk_events.find({}, {"status": 1})])),
          "| suggestions", await c("suggestions"))
    print("recent audit actions:")
    async for d in db.audit_events.find({}, {"at": 1, "action": 1}).sort("at", -1).limit(15):
        print("  ", d.get("at", "")[:19], d.get("action"))


asyncio.run(main())
