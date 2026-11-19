#!/usr/bin/env bash
# Spec 012 Phase 6D: prove the beta MongoDB account is confined to bitbotpro_v12 (DISPOSABLE mongod with auth).
#   bash deploy/rehearsal/beta-db-isolation-rehearsal.sh <spec012-image>
set -euo pipefail
IMG="${1:?spec012 image}"; NET=bbp-6d-db-net; DB=bbp-6d-db-mongo
for n in "$NET" "$DB"; do docker inspect "$n" >/dev/null 2>&1 && { echo "refusing: $n exists" >&2; exit 3; }; done
cleanup() { docker rm -f "$DB" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
ROOTPW=$(head -c 32 /dev/urandom | base64 | tr -d '/+='); BETAPW=$(head -c 36 /dev/urandom | base64 | tr -d '/+=')
docker network create --internal "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --tmpfs /data/db:rw,size=1g --memory 1g \
  -e MONGO_INITDB_ROOT_USERNAME=root -e MONGO_INITDB_ROOT_PASSWORD="$ROOTPW" mongo:8.0.38-noble \
  mongod --bind_ip_all --auth --wiredTigerCacheSizeGB 0.25 >/dev/null
until docker exec "$DB" mongosh --quiet -u root -p "$ROOTPW" --authenticationDatabase admin --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do sleep 0.5; done
# stand-ins for the legacy database and an unrelated tenant database
docker exec "$DB" mongosh --quiet -u root -p "$ROOTPW" --authenticationDatabase admin --eval '
  db.getSiblingDB("bitbotpro").workspaces.insertOne({name:"legacy"});
  db.getSiblingDB("unrelated_service").things.insertOne({x:1});' >/dev/null
docker cp deploy/proposed/beta/mongo-create-beta-user.js "$DB":/tmp/create.js
printf '%s' "$BETAPW" | docker exec -i "$DB" sh -c 'cat > /tmp/beta_pw'
docker exec "$DB" sh -c "mongosh --quiet -u root -p '$ROOTPW' --authenticationDatabase admin /tmp/create.js; rm -f /tmp/beta_pw"
URL="mongodb://bitbotpro_v12_app:$BETAPW@$DB:27017/bitbotpro_v12?authSource=bitbotpro_v12"
docker run --rm --network "$NET" -e URL="$URL" --entrypoint python "$IMG" -c '
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from persistence.mongo import prepare_database
async def main():
    c = AsyncIOMotorClient(os.environ["URL"], serverSelectionTimeoutMS=5000)
    await prepare_database(c["bitbotpro_v12"]); await prepare_database(c["bitbotpro_v12"])
    print("prepare_database twice on bitbotpro_v12: ok;", len(await c["bitbotpro_v12"].list_collection_names()), "collections")
    for name, op in (("bitbotpro", lambda d: d.workspaces.insert_one({"x": 1})),
                     ("bitbotpro", lambda d: d.workspaces.find_one({})),
                     ("unrelated_service", lambda d: d.things.find_one({})),
                     ):
        try:
            await op(c[name]); print(f"  {name}: ALLOWED  <-- isolation failure")
        except Exception as e:
            print(f"  {name}: refused ({type(e).__name__})")
    names = [d["name"] for d in (await c["admin"].command("listDatabases"))["databases"]]
    print("  listDatabases (authorizedDatabases filter):", names, "-> OK" if set(names) <= {"bitbotpro_v12"} else "-> LEAK")
asyncio.run(main())'
docker exec "$DB" mongosh --quiet -u root -p "$ROOTPW" --authenticationDatabase admin --eval 'print("legacy untouched:", db.getSiblingDB("bitbotpro").workspaces.countDocuments({}))'
