"""Platform superadmin bootstrap for a Spec 012 database (Phase 4A).

    python -m api.bootstrap --email admin@example.com --name "Platform Admin"

Reads MONGO_URL and DB_NAME from the environment and the password
interactively (never from argv or logs). Refuses unless the database is
empty or already a Spec 012 database (``persistence.prepare_database``), and
refuses if any platform superadmin exists or a bootstrap already happened.
Not wired into the production application.
"""
import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime, timezone

from api import passwords
from core.identity import bootstrap_superadmin, normalise_email


async def run(store, *, email: str, name: str, password: str, now: str):
    email = normalise_email(email)
    problem = passwords.superadmin_password_error(password, email)
    if problem:
        raise ValueError(problem)
    return await bootstrap_superadmin(store, email=email, name=name, password_hash=passwords.hash_password(password),
                                      now=now)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Platform Admin")
    args = parser.parse_args(argv)
    url, db_name = os.environ.get("MONGO_URL"), os.environ.get("DB_NAME")
    if not url or not db_name:
        print("MONGO_URL and DB_NAME must be set.", file=sys.stderr)
        return 2
    password = getpass.getpass("New superadmin password: ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 2

    async def go():
        from motor.motor_asyncio import AsyncIOMotorClient
        from persistence.mongo import MongoStore, prepare_database
        client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=10000)
        try:
            db = client[db_name]
            await prepare_database(db)                       # refuses legacy / foreign databases
            user = await run(MongoStore(db), email=args.email, name=args.name, password=password,
                             now=datetime.now(timezone.utc).isoformat())
            print(f"Platform superadmin created: {user.id}")
        finally:
            client.close()
    try:
        asyncio.run(go())
    except Exception as exc:                                 # message only; never the password
        print(f"Bootstrap refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
