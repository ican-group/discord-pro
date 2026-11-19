"""Phase 6B backup/restore rehearsal helper (disposable databases only).

    python -m tests.integration.backup_restore_check seed     --url mongodb://src:27017 --db bbp_rh_src
    python -m tests.integration.backup_restore_check validate --source mongodb://src:27017/bbp_rh_src \
                                                              --restored mongodb://dst:27017/bbp_rh_restored

``seed`` builds a representative Spec 012 database through the real API (test World):
two Enterprises, Organisations, Bots, web/Telegram/Discord instances, credentials, the
Discord application, memberships, an invitation, a Zendesk integration with webhook
secrets, knowledge, a conversation, lifecycle states (suspended, archived), a completed
deletion job and the audit history they produce. Test-only keys (``FERNET_KEY``).

``validate`` compares the restored copy with the source: per-collection document
digests, relationships (no orphans), credential decryption with the test key ring
(and ``key_rotation.verify``), indexes and validators, lifecycle availability and
the audit history. Exit 0 only if every check passes. Output: counts and check names
only (never a secret).

Refuses any database whose name does not start with ``bbp_rh_`` and any URL that
names the production container.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from typing import Any, Dict, List


def _guard(url: str, db: str) -> None:
    if "bitbotpro-mongo" in url or not db.startswith("bbp_rh_"):
        raise SystemExit(f"refusing: not a disposable rehearsal database ({db})")


def _digest(doc: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


async def seed(url: str, name: str) -> Dict[str, Any]:
    from motor.motor_asyncio import AsyncIOMotorClient
    from persistence.mongo import MongoStore, prepare_database
    from tests.unit._api_fixtures import World
    from tests.unit.test_api_webchat import FakeProvider
    _guard(url, name)
    client = AsyncIOMotorClient(url)
    await client.drop_database(name)
    db = client[name]
    await prepare_database(db)
    w = await World(MongoStore(db)).build()
    w.S.providers = {"anthropic": FakeProvider(text="Refunds are handled on the billing page.")}
    call = w.call

    async def ok(r, *codes):
        assert r.status_code in (codes or (200, 201, 202)), (r.status_code, r.text[:300])
        return r.json() if r.content else {}

    await ok(await call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}}))
    # knowledge
    await ok(await call("POST", f"/enterprises/{w.eA.id}/knowledge/sources", "owner", {"name": "Handbook"}), 200, 201)
    # Zendesk integration with webhook secrets
    z = await ok(await call("POST", f"/bots/{w.bA.id}/integrations", "admin",
                            {"type": "zendesk", "name": "Support", "settings": {}}), 201)
    await ok(await call("PUT", f"/integrations/{z['id']}/zendesk/signing-secret", "admin",
                        {"secret": "rehearsal-signing-secret-0123456789"}))
    # invitation, webchat conversation
    await ok(await call("POST", f"/enterprises/{w.eA.id}/invitations", "owner",
                        {"email": "invitee@example.test", "role": "viewer", "delivery": "link"}), 200, 201)
    pid = w.iA_web.channel.public_id
    origin = "https://shop.example"
    await ok(await call("PATCH", f"/instances/{w.iA_web.id}", "admin",
                        {"channel": {"type": "webchat", "public_id": pid, "allowed_origins": [origin]}}), 200, 422)
    # lifecycle states: a suspended Organisation (B side) and an archived bot
    await ok(await call("POST", f"/organisations/{w.oB.id}/suspend", "owner_b", {"reason": "rehearsal"}), 200, 202)
    extra = await ok(await call("POST", f"/organisations/{w.oA.id}/bots", "admin", {"name": "Archived bot"}), 201)
    await ok(await call("POST", f"/bots/{extra['id']}/archive", "admin", {}), 200, 202)
    # a completed deletion job (instance) and the audit trail it leaves
    inst = await ok(await call("POST", f"/bots/{w.bA.id}/instances", "admin",
                               {"name": "Temporary web", "channel": {"type": "webchat"}}), 201)
    pv = await ok(await call("POST", f"/instances/{inst['id']}/deletion-preview", "admin"))
    await ok(await call("POST", f"/instances/{inst['id']}/delete", "admin",
                        {"preview_token": pv["preview_token"], "confirm_name": "Temporary web"}), 202)
    from runtime.worker import Worker
    await Worker(w.store, clock=w.S.clock, worker_id="rehearsal").run_once()
    names = sorted(await db.list_collection_names())
    counts = {n: await db[n].count_documents({}) for n in names}
    client.close()
    return {"collections": len(names), "documents": sum(counts.values()), "audit_events": counts.get("audit_events", 0)}


async def validate(source_url: str, restored_url: str) -> Dict[str, Any]:
    from urllib.parse import urlsplit
    from motor.motor_asyncio import AsyncIOMotorClient
    from core import key_rotation
    from core.credentials import KeyRing, SecretBox
    from core.hierarchy import HierarchyError, HierarchyService, Level
    from persistence.mongo import MongoStore, prepare_database
    from tests.unit._api_fixtures import FERNET_KEY

    def split(u):
        p = urlsplit(u)
        return f"{p.scheme}://{p.netloc}", p.path.strip("/")
    (su, sn), (ru, rn) = split(source_url), split(restored_url)
    _guard(su, sn)
    _guard(ru, rn)
    sc, rc = AsyncIOMotorClient(su), AsyncIOMotorClient(ru)
    s, r = sc[sn], rc[rn]
    checks: Dict[str, Any] = {}
    failures: List[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"ok": bool(ok), **({"detail": detail} if detail is not None else {})}
        if not ok:
            failures.append(name)

    s_names, r_names = sorted(await s.list_collection_names()), sorted(await r.list_collection_names())
    check("same_collections", s_names == r_names, {"source": len(s_names), "restored": len(r_names)})
    mismatched, total = [], 0
    for n in s_names:
        a = sorted([_digest(d) async for d in s[n].find({})])
        b = sorted([_digest(d) async for d in r[n].find({})])
        total += len(a)
        if a != b:
            mismatched.append(n)
    check("documents_identical", not mismatched, {"documents": total, "mismatched": mismatched})
    # indexes and validators
    idx_diff, val_diff = [], []
    s_opts = {c["name"]: c.get("options", {}) for c in [x async for x in await s.list_collections()]}
    r_opts = {c["name"]: c.get("options", {}) for c in [x async for x in await r.list_collections()]}
    for n in s_names:
        si = {k: {kk: vv for kk, vv in v.items() if kk != "v"} for k, v in (await s[n].index_information()).items()}
        ri = {k: {kk: vv for kk, vv in v.items() if kk != "v"} for k, v in (await r[n].index_information()).items()}
        if si != ri:
            idx_diff.append(n)
        if s_opts.get(n, {}).get("validator") != r_opts.get(n, {}).get("validator"):
            val_diff.append(n)
    n_idx = 0
    for n in r_names:
        n_idx += len(await r[n].index_information())
    check("indexes_identical", not idx_diff, {"indexes": n_idx, "different": idx_diff})
    check("validators_identical", not val_diff, {"different": val_diff})
    ttl = []
    for n in r_names:
        if any(v.get("expireAfterSeconds") is not None for v in (await r[n].index_information()).values()):
            ttl.append(n)
    check("ttl_indexes_present", {"sessions", "chat_sessions", "oauth_states"} <= set(ttl), {"ttl_collections": len(ttl)})
    # the restored database is accepted as a Spec 012 database and preparing it again is a no-op
    try:
        await prepare_database(r)
        check("restored_is_spec012", True)
    except Exception as exc:
        check("restored_is_spec012", False, type(exc).__name__)
    store = MongoStore(r)
    # relationships: every child has its parent; memberships point at users
    async def ids(coll):
        return {d["_id"] async for d in r[coll].find({}, {"_id": 1})}
    ents, orgs, bots, insts, users = (await ids("enterprises"), await ids("organisations"), await ids("bots"),
                                      await ids("bot_instances"), await ids("users"))
    orphans = []
    async for d in r.organisations.find({}):
        orphans += [] if d["enterprise_id"] in ents else [("organisation", d["_id"])]
    async for d in r.bots.find({}):
        orphans += [] if d["organisation_id"] in orgs and d["enterprise_id"] in ents else [("bot", d["_id"])]
    async for d in r.bot_instances.find({}):
        orphans += [] if d["bot_id"] in bots else [("instance", d["_id"])]
    async for d in r.bot_integrations.find({}):
        orphans += [] if d["bot_id"] in bots else [("integration", d["_id"])]
    async for d in r.memberships.find({}):
        orphans += [] if d["user_id"] in users and d["enterprise_id"] in ents else [("membership", d["_id"])]
    async for d in r.credentials.find({}):
        orphans += [] if d["enterprise_id"] in ents else [("credential", d["_id"])]
    check("relationships_intact", not orphans, {"enterprises": len(ents), "organisations": len(orgs), "bots": len(bots),
                                                 "instances": len(insts), "orphans": len(orphans)})
    # credentials decrypt with the test key ring
    box = SecretBox(KeyRing({"k1": FERNET_KEY}, "k1"))
    v = await key_rotation.verify(store, box)
    check("credentials_decrypt", v["values"] > 0 and not v["undecryptable"],
          {"encrypted_values": v["values"], "undecryptable": len(v["undecryptable"])})
    # lifecycle: states and computed availability match the source
    H_s, H_r = HierarchyService(MongoStore(s)), HierarchyService(store)
    diffs, states = [], {}
    for level, coll in ((Level.ORGANISATION, "organisations"), (Level.BOT, "bots"), (Level.BOT_INSTANCE, "bot_instances")):
        async for d in r[coll].find({}):
            states[d["lifecycle"]["state"]] = states.get(d["lifecycle"]["state"], 0) + 1
            try:
                a = (await H_s.load_chain(level, d["_id"])).availability()
                b = (await H_r.load_chain(level, d["_id"])).availability()
                if (a.available, a.reason) != (b.available, b.reason):
                    diffs.append(d["_id"])
            except HierarchyError:
                pass
    check("lifecycle_states_preserved", not diffs and {"suspended", "archived"} <= set(states), {"states": states})
    # audit history: same events, same order
    sa = [_digest(d) async for d in s.audit_events.find({}).sort([("at", 1), ("_id", 1)])]
    ra = [_digest(d) async for d in r.audit_events.find({}).sort([("at", 1), ("_id", 1)])]
    check("audit_history_identical", sa == ra and len(ra) > 0, {"events": len(ra)})
    sc.close()
    rc.close()
    return {"ok": not failures, "failures": failures, "checks": checks}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("seed")
    a.add_argument("--url", required=True)
    a.add_argument("--db", required=True)
    b = sub.add_parser("validate")
    b.add_argument("--source", required=True)
    b.add_argument("--restored", required=True)
    args = p.parse_args(argv)
    if args.cmd == "seed":
        print(json.dumps(asyncio.run(seed(args.url, args.db)), sort_keys=True))
        return 0
    out = asyncio.run(validate(args.source, args.restored))
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
