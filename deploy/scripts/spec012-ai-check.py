"""Read-only Stage 0 AI-key check for production (run after the real Anthropic test).

    docker exec -i bitbotpro-v12-api /app/docker-entrypoint.sh python - < deploy/scripts/spec012-ai-check.py

Prints, without any secret:
* AI credentials per owner (name, provider, status, fingerprint) and platform AI keys;
* each Bot's stored AI selection (provider, model, credential name) and its owner;
* recent ``ai_test`` / ``llm_call`` usage events (tokens) and the AI-message ledger by state;
* a scan of EVERY collection for strings that look like raw provider keys (``sk-ant-``, ``sk-proj-``,
  ``sk-`` + 32 chars): it prints only counts per collection, never a value. Expected: 0 everywhere.
Writes nothing.
"""
import asyncio
import os
import re
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient

KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}|sk-proj-[A-Za-z0-9_\-]{10,}|\bsk-[A-Za-z0-9]{32,}")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000)["bitbotpro_v12"]
    names = {}
    for coll in ("enterprises", "organisations", "bots"):
        async for d in db[coll].find({}, {"id": 1, "name": 1}):
            names[d["id"]] = d.get("name", "")
    creds = {}
    print("== AI credentials (tenant)")
    async for c in db.credentials.find({"provider": {"$in": ["anthropic", "openai"]}}):
        creds[c["id"]] = c.get("name", "")
        owner = names.get(c.get("organisation_id") or c.get("enterprise_id"), "?")
        print(f"  {c.get('name')!r} provider={c.get('provider')} owner={owner!r} "
              f"({'organisation' if c.get('organisation_id') else 'enterprise'}) status={c.get('status')} "
              f"fp={c.get('fingerprint')} encrypted={str(c.get('secret_enc', '')).startswith('enc1:')}")
    print("== platform AI keys")
    async for c in db.platform_ai_credentials.find({}):
        creds[c["id"]] = c.get("name", "")
        print(f"  {c.get('name')!r} provider={c.get('provider')} status={c.get('status')} fp={c.get('fingerprint')} "
              f"all={c.get('all_enterprises')} granted={[names.get(e, '?') for e in c.get('enterprise_ids') or []]}")
    print("== stored AI selections")
    for coll in ("enterprises", "organisations", "bots"):
        async for d in db[coll].find({}, {"id": 1, "name": 1, "overrides": 1}):
            o = d.get("overrides") or {}
            if any(k.startswith("ai.") for k in o):
                print(f"  {coll[:-1]} {d.get('name')!r}: provider={o.get('ai.provider')} model={o.get('ai.model')} "
                      f"key={creds.get(o.get('ai.credential_id'), o.get('ai.credential_id'))!r}")
    print("== recent AI usage events")
    async for e in db.usage_events.find({"kind": {"$in": ["ai_test", "llm_call", "config_error"]}}).sort("at", -1).limit(15):
        d = e.get("details") or {}
        print(f"  {e.get('at')} {e.get('kind')} bot={names.get(e.get('bot_id'), '-')!r} "
              f"{ {k: d[k] for k in sorted(d) if k in ('provider', 'model', 'ok', 'input_tokens', 'output_tokens', 'issue', 'purpose')} }")
    ledger = Counter()
    async for l in db.usage_ledger.find({}, {"state": 1, "preview": 1}):
        ledger[(l.get("state"), bool(l.get("preview")))] += 1
    print("== AI-message ledger (state, preview): count", dict(ledger))
    print("== raw-key scan (counts only; expected 0)")
    total = 0
    for coll in sorted(await db.list_collection_names()):
        hits = 0
        async for doc in db[coll].find({}):
            hits += sum(1 for s in _strings(doc) if KEY_PATTERN.search(s))
        total += hits
        if hits:
            print(f"  {coll}: {hits} value(s) look like a raw key")
    print(f"  total: {total}")


asyncio.run(main())
