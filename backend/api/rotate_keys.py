"""Encryption-key rotation command (Phase 6B). Operator tool; never run by the API.

    python -m api.rotate_keys status     # counts per key id (read-only)
    python -m api.rotate_keys verify     # status + decrypt every value; exit 1 unless all on the active key
    python -m api.rotate_keys rotate     # re-encrypt with the active key (resumable)

Environment: ``MONGO_URL`` and ``DB_NAME`` (the Spec 012 database), and the key
ring (``ENCRYPTION_KEYS`` + ``ENCRYPTION_ACTIVE_KEY_ID``, or ``ENCRYPTION_KEY``),
normally provided as ``*_FILE`` through the container entrypoint. Refuses a
database without the Spec 012 schema marker. Output is JSON with counts and
key ids only: never a secret, ciphertext or key.

Procedure (see spec §32.3): add the new key to the ring and make it active →
restart API and worker → ``rotate`` → ``verify`` (exit 0) → remove the old key →
restart. Rollback before the old key is removed: make it active again →
restart → ``rotate``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from core.credentials import KeyRing, KeyRingError, SecretBox
from core import key_rotation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run(cmd: str, batch: int) -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from persistence.mongo import SCHEMA_MARKER_ID, SCHEMA_NAME, MongoStore
    box = SecretBox(KeyRing.from_environment(os.environ))
    url, name = os.environ.get("MONGO_URL"), os.environ.get("DB_NAME")
    if not url or not name:
        print(json.dumps({"error": "MONGO_URL and DB_NAME are required"}))
        return 2
    client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=5000)
    try:
        db = client[name]
        marker = await db["platform_state"].find_one({"_id": SCHEMA_MARKER_ID})
        if not marker or marker.get("schema") != SCHEMA_NAME:
            print(json.dumps({"error": "refusing: not a Spec 012 database"}))
            return 2
        store = MongoStore(db)
        if cmd == "rotate":
            report = await key_rotation.rotate(store, box, now=_now, batch=batch)
            after = await key_rotation.verify(store, box)
            print(json.dumps({"rotate": report, "verify": after}, indent=2, sort_keys=True))
            return 0 if after["ok"] else 1
        report = await key_rotation.verify(store, box)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if (cmd == "status" or report["ok"]) else 1
    finally:
        client.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m api.rotate_keys")
    p.add_argument("command", choices=("status", "verify", "rotate"))
    p.add_argument("--batch", type=int, default=100)
    a = p.parse_args(argv)
    if not 1 <= a.batch <= 500:
        p.error("--batch must be 1..500")
    try:
        return asyncio.run(_run(a.command, a.batch))
    except KeyRingError as exc:
        print(json.dumps({"error": str(exc)}))        # messages never contain key material
        return 2


if __name__ == "__main__":
    sys.exit(main())
