"""BitBotPro administrative commands (spec 004).

Never imported by server.py and never run at startup. Every data-changing
operation requires an explicit flag; without it the command only reports what it
would do.

    python manage.py bootstrap-superadmin --email <addr> --password-file <path> [--confirm]
    python manage.py legacy-migrations [--only NAME ...] [--apply --confirm] [--allow-hardcoded-accounts]
    python manage.py security-indexes [--apply --confirm]

Environment: MONGO_URL and DB_NAME (MONGO_URL_FILE is resolved by docker-entrypoint.sh).
Output never contains passwords, password hashes or connection strings.

Exit codes: 0 success, 1 error, 2 dry run / confirmation required,
            3 refused (precondition not met), 4 invalid input.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, TextIO

import password_policy
import rate_limit
import runtime_config

EXIT_OK, EXIT_ERROR, EXIT_DRY_RUN, EXIT_REFUSED, EXIT_INVALID = 0, 1, 2, 3, 4

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

SUPERADMIN_FILTER = {"$or": [{"role": "superadmin"}, {"platform_role": "superadmin"}]}

LEGACY_MIGRATIONS = ("instance-backfill", "v2", "workspaces", "discord-metadata")


def _backfill_collections() -> tuple:
    cols = ("bot_config", "knowledge_sources", "conversations", "discord_config")
    # No MongoDB Knowledge Base writes in APP_MODE=api (spec 004, R5).
    if runtime_config.kb_disabled(runtime_config.get_app_mode(os.environ)):
        cols = tuple(c for c in cols if c != "knowledge_sources")
    return cols


# ── helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254


def read_password_file(path: str) -> str:
    """Read a password from a file; only a single trailing newline is removed."""
    data = Path(path).read_text(encoding="utf-8")
    if data.endswith("\n"):
        data = data[:-1]
    if data.endswith("\r"):
        data = data[:-1]
    if "\n" in data or "\r" in data:
        raise ValueError("password file must contain a single line")
    return data


def _bcrypt_hash(password: str) -> str:
    import bcrypt  # lazy: keeps this module importable for offline tests
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode()


def build_superadmin_doc(email: str, password_hash: str, username: str, now: str) -> Dict[str, Any]:
    # Same shape as the former startup seed, plus platform_role for the v2/workspace checks.
    return {
        "id": str(uuid.uuid4()),
        "email": email,
        "username": username,
        "hashed_password": password_hash,
        "is_verified": True,
        "verification_code": None,
        "role": "superadmin",
        "platform_role": "superadmin",
        "created_at": now,
    }


# ── bootstrap-superadmin ────────────────────────────────────────────────────

async def bootstrap_superadmin(
    db,
    email: str,
    password: str,
    *,
    confirm: bool,
    username: str = "administrator",
    hasher: Callable[[str], str] = _bcrypt_hash,
    now_fn: Callable[[], str] = _now_iso,
    out: TextIO = sys.stdout,
) -> int:
    email = normalise_email(email)
    if not valid_email(email):
        print("ERROR: invalid email address", file=out)
        return EXIT_INVALID

    problems = password_policy.check_password(password, email)
    if problems:
        print("ERROR: password rejected by policy:", file=out)
        for p in problems:
            print(f"  - {p}", file=out)
        return EXIT_INVALID

    # One-time: refuse if any superadmin exists or the email is already registered.
    if await db.users.count_documents(SUPERADMIN_FILTER, limit=1):
        print("REFUSED: a superadmin already exists; bootstrap is one-time only", file=out)
        return EXIT_REFUSED
    if await db.users.count_documents({"email": email}, limit=1):
        print(f"REFUSED: a user with email {email} already exists", file=out)
        return EXIT_REFUSED

    if not confirm:
        print(f"DRY RUN: would create superadmin {email} (password passes policy). "
              "Re-run with --confirm to create it.", file=out)
        return EXIT_DRY_RUN

    # Unique email index (idempotent; the API creates the same index at startup).
    await db.users.create_index([("email", 1)], unique=True)
    now = now_fn()
    doc = build_superadmin_doc(email, hasher(password), username, now)
    await db.users.insert_one(doc)
    await db.audit_log.insert_one({
        "id": str(uuid.uuid4()),
        "action": "bootstrap_superadmin",
        "email": email,
        "user_id": doc["id"],
        "via": "cli",
        "at": now,
    })
    print(f"CREATED: superadmin {email}", file=out)
    return EXIT_OK


# ── legacy-migrations ───────────────────────────────────────────────────────

async def _discord_global_client_id(db) -> str:
    creds = await db.discord_app_config.find_one({"_id": "discord_app"}) or {}
    return creds.get("client_id") or ""


async def legacy_report(db, names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Read-only: what each migration would touch. Values are counts/booleans only."""
    report: Dict[str, Dict[str, Any]] = {}
    for name in names:
        if name == "instance-backfill":
            first = await db.bot_instances.find_one({}, {"id": 1})
            missing = {}
            for col in _backfill_collections():
                missing[col] = await db[col].count_documents({"instance_id": {"$exists": False}})
            report[name] = {"has_instance": bool(first), "docs_missing_instance_id": missing}
        elif name == "v2":
            v1 = await db.bot_instances.count_documents({})
            v2 = await db.organisations.count_documents({})
            report[name] = {"v1_instances": v1, "v2_orgs": v2, "would_run": v1 > 0 and v2 == 0}
        elif name == "workspaces":
            report[name] = {
                "legacy_owner_account_exists": bool(await db.users.count_documents({"email": "admin@bridgebot.tech"}, limit=1)),
                "platform_admin_account_exists": bool(await db.users.count_documents({"email": "admin@bitbotpro.io"}, limit=1)),
                "warning": "creates/modifies hard-coded accounts; requires --allow-hardcoded-accounts",
            }
        elif name == "discord-metadata":
            gid = await _discord_global_client_id(db)
            untagged = await db.connected_accounts.count_documents(
                {"provider": "discord", "metadata.source": {"$exists": False}}
            )
            report[name] = {"global_client_configured": bool(gid), "untagged_accounts": untagged}
    return report


async def _apply_instance_backfill(db) -> Dict[str, int]:
    first = await db.bot_instances.find_one({}, {"id": 1})
    result: Dict[str, int] = {}
    if first:
        for col in _backfill_collections():
            r = await db[col].update_many({"instance_id": {"$exists": False}}, {"$set": {"instance_id": first["id"]}})
            result[col] = r.modified_count
    return result


async def _apply_v2(db) -> Any:
    v1 = await db.bot_instances.count_documents({})
    v2 = await db.organisations.count_documents({})
    if not (v1 > 0 and v2 == 0):
        return {"skipped": "no V1-only data"}
    from migrate_v2 import migrate  # existing migration, unchanged
    return await migrate()


async def _apply_workspaces(db) -> Any:
    from migrate_workspaces import run_workspace_migration  # existing migration, unchanged
    return await run_workspace_migration(db)


async def _apply_discord_metadata(db) -> Dict[str, int]:
    gid = await _discord_global_client_id(db)
    if not gid:
        return {"updated": 0}
    untagged = await db.connected_accounts.find(
        {"provider": "discord", "metadata.source": {"$exists": False}}
    ).to_list(100)
    for acc in untagged:
        creds = acc.get("credentials") or {}
        source = "platform" if creds.get("client_id") == gid else "custom"
        await db.connected_accounts.update_one({"id": acc["id"]}, {"$set": {"metadata.source": source}})
    return {"updated": len(untagged)}


_APPLIERS = {
    "instance-backfill": _apply_instance_backfill,
    "v2": _apply_v2,
    "workspaces": _apply_workspaces,
    "discord-metadata": _apply_discord_metadata,
}


async def legacy_migrations(
    db,
    only: Optional[List[str]],
    *,
    apply: bool,
    confirm: bool,
    allow_hardcoded_accounts: bool,
    now_fn: Callable[[], str] = _now_iso,
    out: TextIO = sys.stdout,
) -> int:
    names = [n for n in LEGACY_MIGRATIONS if not only or n in only]
    report = await legacy_report(db, names)
    for name in names:
        print(f"[{name}] {report[name]}", file=out)

    if not apply:
        print("DRY RUN: nothing changed. Use --apply --confirm to run.", file=out)
        return EXIT_DRY_RUN
    if not confirm:
        print("REFUSED: --apply requires --confirm", file=out)
        return EXIT_DRY_RUN
    if "workspaces" in names and not allow_hardcoded_accounts:
        print("REFUSED: the 'workspaces' migration creates/modifies hard-coded accounts; "
              "add --allow-hardcoded-accounts or exclude it with --only", file=out)
        return EXIT_REFUSED

    for name in names:
        result = await _APPLIERS[name](db)
        await db.audit_log.insert_one({
            "id": str(uuid.uuid4()), "action": f"legacy_migration:{name}", "via": "cli", "at": now_fn(),
        })
        print(f"APPLIED [{name}]: {result}", file=out)
    return EXIT_OK


# ── security-indexes (spec 008 Stage 1) ─────────────────────────────────────

async def security_indexes(db, *, apply: bool, confirm: bool, out: TextIO = sys.stdout) -> int:
    """Report, and only with --apply --confirm create, the TTL indexes of spec 008
    (security_counters, oauth_states). Writes no documents (no audit record),
    touches no other index, and refuses if an existing index differs from spec."""
    missing, conflicts = [], []
    for collection, name in rate_limit.TTL_INDEXES:
        state = await rate_limit.describe_ttl_index(db, collection, name)
        print(f"[{collection}] {state}", file=out)
        idx = state.get("ttl_index")
        if idx is None:
            missing.append((collection, name))
        elif not (idx.get("expireAfterSeconds") == 0 and list(idx.get("key") or []) == [("expires_at", 1)]):
            conflicts.append(collection)
    if conflicts:
        print(f"REFUSED: existing TTL index differs from spec on {', '.join(conflicts)}; not changed", file=out)
        return EXIT_REFUSED
    if not missing:
        print("TTL indexes already present", file=out)
        return EXIT_OK
    if not apply:
        for collection, name in missing:
            print(f"DRY RUN: would create index {name} on {collection}.expires_at (expireAfterSeconds=0)", file=out)
        print("Use --apply --confirm.", file=out)
        return EXIT_DRY_RUN
    if not confirm:
        print("REFUSED: --apply requires --confirm", file=out)
        return EXIT_DRY_RUN
    for collection, name in missing:
        created = await rate_limit.ensure_ttl_index(db, collection, name)
        print(f"APPLIED: created index {created} on {collection}", file=out)
    return EXIT_OK


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="manage.py", description="BitBotPro administrative commands",
                                allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap-superadmin", help="one-time creation of the first superadmin",
                       allow_abbrev=False)
    b.add_argument("--email", required=True)
    b.add_argument("--password-file", required=True, help="file containing the password (never pass it as an argument)")
    b.add_argument("--username", default="administrator")
    b.add_argument("--confirm", action="store_true", help="actually create the account")

    m = sub.add_parser("legacy-migrations", help="explicitly run legacy data migrations (dry run by default)",
                       allow_abbrev=False)
    m.add_argument("--only", nargs="+", choices=LEGACY_MIGRATIONS)
    m.add_argument("--apply", action="store_true")
    m.add_argument("--confirm", action="store_true")
    m.add_argument("--allow-hardcoded-accounts", action="store_true")

    si = sub.add_parser("security-indexes",
                        help="create the security_counters TTL index (dry run by default)",
                        allow_abbrev=False)
    si.add_argument("--apply", action="store_true")
    si.add_argument("--confirm", action="store_true")
    return p


def _open_db():
    from dotenv import load_dotenv
    from motor.motor_asyncio import AsyncIOMotorClient
    load_dotenv(Path(__file__).parent / ".env")
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        raise SystemExit("ERROR: MONGO_URL and DB_NAME must be set")
    client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=10000)
    return client, client[db_name]


async def _run(args) -> int:
    if args.command == "bootstrap-superadmin":
        try:
            password = read_password_file(args.password_file)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            print(f"ERROR: cannot read password file: {type(e).__name__}")
            return EXIT_INVALID
        client, db = _open_db()
        try:
            return await bootstrap_superadmin(db, args.email, password, confirm=args.confirm, username=args.username)
        finally:
            client.close()

    if args.command == "legacy-migrations":
        client, db = _open_db()
        try:
            return await legacy_migrations(
                db, args.only, apply=args.apply, confirm=args.confirm,
                allow_hardcoded_accounts=args.allow_hardcoded_accounts,
            )
        finally:
            client.close()

    if args.command == "security-indexes":
        client, db = _open_db()
        try:
            return await security_indexes(db, apply=args.apply, confirm=args.confirm)
        finally:
            client.close()
    return EXIT_ERROR


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SystemExit:
        raise
    except Exception as e:  # never echo connection strings or secrets
        print(f"ERROR: {type(e).__name__}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
