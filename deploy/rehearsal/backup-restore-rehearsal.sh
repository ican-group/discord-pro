#!/usr/bin/env bash
# Spec 012 Phase 6B: backup → integrity check → restore → validation, on DISPOSABLE databases.
#
#   bash deploy/rehearsal/backup-restore-rehearsal.sh <spec012-test-image> <report-dir>
#
# Never touches production: containers/networks are named bbp-6b-br-* (refuses if any exists),
# both mongod instances use tmpfs on an --internal network, database names start with bbp_rh_
# (enforced again by the Python helper), and everything is removed on exit. No Docker volume
# is created or deleted. The archive is written 0600 into <report-dir>.
set -euo pipefail
IMG="${1:?spec012 test image}"; OUT="${2:?report directory}"
MONGO_IMAGE="mongo:8.0.38-noble"
NET=bbp-6b-br-net; SRC=bbp-6b-br-src; DST=bbp-6b-br-dst
SRC_DB=bbp_rh_src; DST_DB=bbp_rh_restored
for n in "$NET" "$SRC" "$DST"; do
  if docker inspect "$n" >/dev/null 2>&1; then echo "refusing: $n already exists" >&2; exit 3; fi
done
mkdir -p "$OUT"; umask 077
cleanup() { docker rm -f "$SRC" "$DST" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network create --internal "$NET" >/dev/null
for c in "$SRC" "$DST"; do
  docker run -d --name "$c" --network "$NET" --tmpfs /data/db:rw,size=1g --memory 1g "$MONGO_IMAGE" \
    mongod --bind_ip_all --wiredTigerCacheSizeGB 0.25 >/dev/null
done
for c in "$SRC" "$DST"; do
  until docker exec "$c" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do sleep 0.5; done
done
ARCHIVE="$OUT/$SRC_DB-$(date -u +%Y%m%dT%H%M%SZ).archive.gz"
{
echo "== [1/6] representative Spec 012 database (real API, test-only keys)"
docker run --rm --network "$NET" "$IMG" python -m tests.integration.backup_restore_check seed \
  --url "mongodb://$SRC:27017" --db "$SRC_DB"

echo "== [2/6] backup: mongodump --gzip --archive (read-only on the source)"
docker exec "$SRC" mongodump --quiet --db "$SRC_DB" --gzip --archive > "$ARCHIVE"
chmod 0600 "$ARCHIVE"
SUM="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"; echo "$SUM  $(basename "$ARCHIVE")" > "$ARCHIVE.sha256"
echo "archive: $(basename "$ARCHIVE") bytes=$(stat -c %s "$ARCHIVE") mode=$(stat -c %a "$ARCHIVE") sha256=$SUM"

echo "== [3/6] integrity: checksum, gzip stream, archive readable by mongorestore (dry run)"
(cd "$OUT" && sha256sum -c "$(basename "$ARCHIVE").sha256")
gzip -t "$ARCHIVE" && echo "gzip: ok"
docker exec -i "$DST" mongorestore --quiet --dryRun --gzip --archive < "$ARCHIVE" && echo "mongorestore --dryRun: ok"
cp "$ARCHIVE" "$OUT/corrupted.archive.gz"; printf 'X' | dd of="$OUT/corrupted.archive.gz" bs=1 seek=200 conv=notrunc 2>/dev/null
if gzip -t "$OUT/corrupted.archive.gz" 2>/dev/null && (cd "$OUT" && echo "$SUM  corrupted.archive.gz" | sha256sum -c --quiet 2>/dev/null); then
  echo "negative control FAILED: a corrupted archive passed the integrity checks"; exit 1
else
  echo "negative control: a corrupted archive is rejected by the integrity checks"
fi
rm -f "$OUT/corrupted.archive.gz"

echo "== [4/6] restore into a SEPARATE disposable mongod under a different database name"
docker exec -i "$DST" mongorestore --quiet --gzip --archive --nsFrom "$SRC_DB.*" --nsTo "$DST_DB.*" < "$ARCHIVE"
echo "restored into $DST/$DST_DB"

echo "== [5/6] validation: documents, relationships, credentials, indexes, validators, lifecycle, audit"
docker run --rm --network "$NET" "$IMG" python -m tests.integration.backup_restore_check validate \
  --source "mongodb://$SRC:27017/$SRC_DB" --restored "mongodb://$DST:27017/$DST_DB"

echo "== [6/6] done: disposable containers and network are removed on exit"
} 2>&1 | tee "$OUT/backup-restore-rehearsal.txt"
exit "${PIPESTATUS[0]}"
