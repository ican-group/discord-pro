#!/usr/bin/env bash
# Spec 012 Phase 6B: MongoDB open-file-limit rehearsal on a DISPOSABLE mongod.
#
#   bash deploy/rehearsal/mongo-nofile-rehearsal.sh <spec012-test-image> <report-dir>
#
# Never touches production: every container/network it creates is named bbp-6b-rh-*, it
# refuses to run if one of those names already exists, it never references bitbotpro-mongo,
# it uses a bind-mounted scratch directory (no Docker volume is created or deleted) on an
# --internal network, and it removes its own containers and network on exit.
set -euo pipefail
IMG="${1:?spec012 test image}"; OUT="${2:?report directory}"
MONGO_IMAGE="mongo:8.0.38-noble"
NET=bbp-6b-rh-net; DB=bbp-6b-rh-mongo; DATA="$OUT/data-$(date +%s)-$$"
for n in "$NET" "$DB"; do
  if docker inspect "$n" >/dev/null 2>&1; then echo "refusing: $n already exists" >&2; exit 3; fi
done
mkdir -p "$DATA" && chmod 0777 "$DATA"
cleanup() {
  docker rm -f "$DB" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true
  # the data files belong to the container's mongodb user: remove them from inside a container
  docker run --rm -v "$DATA":/d "$MONGO_IMAGE" sh -c 'rm -rf /d/* /d/.[!.]*' >/dev/null 2>&1 || true
  rmdir "$DATA" 2>/dev/null || true
}
trap cleanup EXIT
docker network create --internal "$NET" >/dev/null

start() {   # $1 = extra docker run args (ulimit), mirrors deploy/docker-compose.db.yml (no ulimits today)
  docker rm -f "$DB" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  docker run -d --name "$DB" --network "$NET" $1 -v "$DATA":/data/db --memory 1g "$MONGO_IMAGE" \
    mongod --bind_ip_all --wiredTigerCacheSizeGB 0.25 >/dev/null
  until docker exec "$DB" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do sleep 0.5; done
}
limits() { docker exec "$DB" sh -c 'grep "Max open files" /proc/1/limits | awk "{print \$4\"/\"\$5}"'; }
fds() { docker exec "$DB" sh -c 'ls /proc/1/fd | wc -l'; }
maxconn() { docker exec "$DB" mongosh --quiet --eval 'const s=db.serverStatus().connections; print(s.current+s.available)'; }
py() { docker run --rm --network "$NET" -e MONGO="mongodb://$DB:27017" "$IMG" python -c "$1"; }

SEED='
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from persistence.mongo import MongoStore, prepare_database
async def main():
    c = AsyncIOMotorClient(os.environ["MONGO"]); db = c["bbp_rehearsal"]
    await prepare_database(db)
    from tests.unit._api_fixtures import World
    await World(MongoStore(db)).build()
    names = await db.list_collection_names(); idx = 0
    for n in names: idx += len(await db[n].index_information())
    docs = sum([await db[n].count_documents({}) for n in names])
    print(f"collections={len(names)} indexes={idx} documents={docs}")
asyncio.run(main())'
STORM='
import os, threading
from pymongo import MongoClient
ok, failed = [0], [0]; lock = threading.Lock(); clients = []
def one():
    try:
        c = MongoClient(os.environ["MONGO"], serverSelectionTimeoutMS=4000, connectTimeoutMS=4000, maxPoolSize=1)
        c.admin.command("ping"); clients.append(c)
        with lock: ok[0] += 1
    except Exception:
        with lock: failed[0] += 1
ts = [threading.Thread(target=one) for _ in range(450)]       # each client: 2 monitor + 1 pool connection
[t.start() for t in ts]; [t.join() for t in ts]
print(f"clients_ok={ok[0]} clients_failed={failed[0]}")
[c.close() for c in clients]'
COUNT='
import os
from pymongo import MongoClient
db = MongoClient(os.environ["MONGO"])["bbp_rehearsal"]
print("documents=" + str(sum(db[n].count_documents({}) for n in db.list_collection_names())))'

{
echo "== 1. today's configuration (no ulimits in compose = Docker default)"
start ""
echo "limits(soft/hard)=$(limits) max_connections=$(maxconn) fds_idle=$(fds)"
echo "-- Spec 012 database prepared and seeded:"; py "$SEED"
echo "fds_after_seed=$(fds)"
echo "-- connection storm (450 clients ≈ 1350 connections):"; py "$STORM"
echo "mongod_refusal_log_lines=$(docker logs "$DB" 2>&1 | grep -ciE 'too many open files|connection refused because too many open connections|connection limit' || true)"
echo
echo "== 2. proposed change: nofile 64000/64000 (container recreated, same data directory)"
start "--ulimit nofile=64000:64000"
echo "limits(soft/hard)=$(limits) max_connections=$(maxconn) fds_idle=$(fds)"
py "$COUNT"
echo "-- connection storm:"; py "$STORM"
echo
echo "== 3. rollback: recreate without the override"
start ""
echo "limits(soft/hard)=$(limits) max_connections=$(maxconn)"
py "$COUNT"
} 2>&1 | tee "$OUT/mongo-nofile-rehearsal.txt"
