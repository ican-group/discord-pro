"""Encryption-key rotation (Phase 6B).

Every stored secret is a field whose name ends in ``_enc`` and whose value is a
:class:`core.credentials.KeyRing` ciphertext (``enc1:<key id>:<token>``, or a
bare pre-6B Fernet token). Rotation re-encrypts every such field with the
*active* key, in place, one field at a time:

* **Backward compatible.** Every key in the ring decrypts, so the API and the
  worker keep working while some values are still under the previous key.
* **Atomic per field.** ``Store.set_field_if`` writes the new ciphertext only if
  the field still holds the value that was read; a concurrent writer always
  wins (its value is already under the active key, or the next pass rotates it).
  The document version is not changed: the secret itself is unchanged.
* **Crash-safe and resumable.** The cursor (collection + last id) is saved after
  every batch in ``key_rotation_runs``; a re-run continues from it. Re-running
  from the start is also safe: already-rotated fields are skipped.
* **Tenant isolation.** A ciphertext is only ever rewritten in the field it came
  from; nothing is copied between documents, and no query crosses into a
  collection outside ``ENCRYPTED_COLLECTIONS``.
* **Audited, never logged.** Start and completion are audit events with counts
  (per collection and per Enterprise id); no plaintext, ciphertext or key
  material is ever written to the report, the audit log or the run record.

A key may be removed from the ring only after :func:`verify` reports no value
under it (and no undecryptable value). Rollback = make the previous key active
again and run :func:`rotate` (every value moves back to it).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from core import audit
from core.credentials import CredentialError, SecretBox
from core.hierarchy import DuplicateKey

# Every collection that stores ``*_enc`` fields (enforced by
# tests/unit/test_key_rotation.py::test_every_encrypted_field_is_covered).
ENCRYPTED_COLLECTIONS: Tuple[str, ...] = (
    "credentials",            # tenant credentials: secret_enc
    "platform_credentials",   # Zendesk OAuth client: secret_enc
    "discord_applications",   # D-Q14 Enterprise application: bot_token_enc, client_secret_enc
    "bot_integrations",       # Zendesk webhook: webhook.path_enc, webhook.signing_secret_enc
    "bot_instances",          # Telegram webhook: telegram_webhook.path_enc, telegram_webhook.header_enc
    "oauth_states",           # Zendesk PKCE verifier (10-minute TTL): verifier_enc
)
RUNS = "key_rotation_runs"
MAX_DEPTH = 4


def encrypted_fields(doc: Any, prefix: str = "", depth: int = 0) -> Iterator[Tuple[str, Any]]:
    """Yield ``(dotted path, value)`` for every ``*_enc`` field of a document."""
    if not isinstance(doc, dict) or depth >= MAX_DEPTH:
        return
    for key, value in doc.items():
        if not isinstance(key, str) or key in ("_id",):
            continue
        path = f"{prefix}{key}"
        if key.endswith("_enc"):
            yield path, value
        elif isinstance(value, dict):
            yield from encrypted_fields(value, path + ".", depth + 1)


def _key_label(box: SecretBox, value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "empty"
    kid = box.key_id_of(value)
    return "unversioned" if kid is None else (kid or "malformed")


async def verify(store, box: SecretBox, *, batch: int = 200) -> Dict[str, Any]:
    """Count encrypted values per collection and key id; decrypt each one.
    ``ok`` means every value is under the active key and decrypts."""
    by_key: Dict[str, Counter] = {}
    undecryptable: List[Dict[str, str]] = []
    for coll in ENCRYPTED_COLLECTIONS:
        counts: Counter = Counter()
        after = None
        while True:
            docs = await store.scan(coll, after=after, limit=batch)
            if not docs:
                break
            for doc in docs:
                for path, value in encrypted_fields(doc):
                    if not isinstance(value, str) or not value:
                        continue                      # absent/cleared secret: nothing to rotate
                    counts[_key_label(box, value)] += 1
                    try:
                        box.decrypt(value)
                    except CredentialError:
                        undecryptable.append({"collection": coll, "id": doc["id"], "field": path})
            after = docs[-1]["id"]
        by_key[coll] = counts
    total = sum(sum(c.values()) for c in by_key.values())
    on_active = sum(c.get(box.ring.active, 0) for c in by_key.values())
    return {"active_key_id": box.ring.active, "key_ids_loaded": list(box.ring.key_ids),
            "values": total, "on_active_key": on_active,
            "by_collection": {k: dict(v) for k, v in by_key.items()},
            "undecryptable": undecryptable, "ok": total == on_active and not undecryptable}


async def rotate(store, box: SecretBox, *, now: Callable[[], str], actor=None, batch: int = 100,
                 on_batch: Optional[Callable[[str, str], Any]] = None) -> Dict[str, Any]:
    """Re-encrypt every value that is not under the active key. Resumes an
    interrupted run for the same active key from its saved cursor."""
    target = box.ring.active
    run_id = f"to-{target}"
    run = await store.get(RUNS, run_id)
    if run is None or run.get("status") == "completed":
        run = {"id": run_id, "target_key_id": target, "status": "running", "started_at": now(),
               "cursor": {"collection": ENCRYPTED_COLLECTIONS[0], "after": None},
               "rotated": {}, "raced": {}, "undecryptable": {}, "by_enterprise": {}, "passes": (run or {}).get("passes", 0),
               "version": 1}
        if await store.get(RUNS, run_id) is None:
            try:
                await store.insert(RUNS, run)
            except DuplicateKey:              # a second operator started at the same moment: share the run
                pass
        else:
            await store.update(RUNS, run_id, {k: v for k, v in run.items() if k != "id"})
        await audit.record(store, actor=actor, action="encryption.rotation_started", enterprise_id=None,
                           target_level="platform", target_id=run_id, now=now(),
                           details={"target_key_id": target, "key_ids_loaded": list(box.ring.key_ids)})
    else:
        await audit.record(store, actor=actor, action="encryption.rotation_resumed", enterprise_id=None,
                           target_level="platform", target_id=run_id, now=now(),
                           details={"target_key_id": target, "cursor_collection": run["cursor"]["collection"]})
    rotated = Counter(run.get("rotated") or {})
    raced = Counter(run.get("raced") or {})
    bad = Counter(run.get("undecryptable") or {})
    by_ent = Counter(run.get("by_enterprise") or {})
    start = ENCRYPTED_COLLECTIONS.index(run["cursor"]["collection"])
    after = run["cursor"]["after"]
    for coll in ENCRYPTED_COLLECTIONS[start:]:
        while True:
            docs = await store.scan(coll, after=after, limit=batch)
            if not docs:
                break
            for doc in docs:
                for path, value in encrypted_fields(doc):
                    if not isinstance(value, str) or not value or not box.needs_rotation(value):
                        continue
                    try:
                        fresh = box.reencrypt(value)
                    except CredentialError:
                        bad[coll] += 1                # reported by verify with its location; never fatal
                        continue
                    if await store.set_field_if(coll, doc["id"], path, value, fresh):
                        rotated[coll] += 1
                        if doc.get("enterprise_id"):
                            by_ent[doc["enterprise_id"]] += 1
                    else:
                        raced[coll] += 1              # a concurrent write won; a later pass/verify covers it
            after = docs[-1]["id"]
            await store.update(RUNS, run_id, {"cursor": {"collection": coll, "after": after},
                                              "rotated": dict(rotated), "raced": dict(raced),
                                              "undecryptable": dict(bad), "by_enterprise": dict(by_ent),
                                              "updated_at": now()})
            if on_batch is not None:
                await on_batch(coll, after)
        after = None
        nxt = ENCRYPTED_COLLECTIONS.index(coll) + 1
        if nxt < len(ENCRYPTED_COLLECTIONS):
            await store.update(RUNS, run_id, {"cursor": {"collection": ENCRYPTED_COLLECTIONS[nxt], "after": None}})
    report = {"target_key_id": target, "rotated": dict(rotated), "raced": dict(raced),
              "undecryptable": dict(bad), "enterprises_touched": len(by_ent)}
    await store.update(RUNS, run_id, {"status": "completed", "finished_at": now(),
                                      "passes": int(run.get("passes", 0)) + 1,
                                      "cursor": {"collection": ENCRYPTED_COLLECTIONS[0], "after": None}})
    await audit.record(store, actor=actor, action="encryption.rotation_completed", enterprise_id=None,
                       target_level="platform", target_id=run_id, now=now(),
                       details={**report, "by_enterprise": dict(by_ent)})
    return report


__all__ = ["ENCRYPTED_COLLECTIONS", "RUNS", "encrypted_fields", "rotate", "verify"]
