#!/usr/bin/env bash
# MongoDB backup + verified restore (spec 008 Stage 10, G-final item 5).
#
#   sudo bash deploy/scripts/backup-restore-check.sh > ~/.bitbotpro/backup-check.log 2>&1; echo "exit=$?"
#
# READ-ONLY on production: runs `mongodump` inside bitbotpro-mongo (no writes) and
# stores a gzip archive in ~/.bitbotpro/backups (0700 dir, 0600 file). The archive
# is then restored into a DISPOSABLE mongod (new internal network, tmpfs, removed
# afterwards) and per-collection document counts are compared with production.
# Credentials are read from the container's Docker secret into a temporary 0600
# config file INSIDE the container (never in argv, env or output) and removed.
set -euo pipefail

PROD=bitbotpro-mongo
DB="${MONGO_APP_DB:-bitbotpro}"
MONGO_IMAGE="mongo:8.0.38-noble"
USER_HOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
OWNER="$(id -u "${SUDO_USER:-$(id -un)}"):$(id -g "${SUDO_USER:-$(id -un)}")"
BACKUP_DIR="$USER_HOME/.bitbotpro/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$BACKUP_DIR/bitbotpro-$STAMP.archive.gz"
SUFFIX="$(date +%s)-$$"; NET="bbp-restore-net-$SUFFIX"; TMP_MONGO="bbp-restore-mongo-$SUFFIX"

cleanup() {
  docker exec "$PROD" sh -c 'rm -f /tmp/.bbp-dump.yml /tmp/.bbp-count.js' >/dev/null 2>&1 || true
  docker rm -f "$TMP_MONGO" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

install -d -m 0700 -o "${OWNER%:*}" -g "${OWNER#*:}" "$BACKUP_DIR"

# Count script shared by production and the restored copy (no credentials inside).
COUNT_JS='const d = db.getSiblingDB(process.env.BBP_DB); const out = {};
d.getCollectionNames().sort().forEach(c => { out[c] = d.getCollection(c).countDocuments({}); });
print("BBP_COUNTS " + JSON.stringify(out));'

echo "== [1/4] mongodump (read-only) from $PROD, db=$DB"
docker exec -e BBP_DB="$DB" "$PROD" sh -c '
  umask 077
  printf "password: %s\n" "$(cat /run/secrets/mongo_root_password)" > /tmp/.bbp-dump.yml
  mongodump --quiet --config /tmp/.bbp-dump.yml --username "${MONGO_INITDB_ROOT_USERNAME:-root}" \
    --authenticationDatabase admin --db "$BBP_DB" --archive --gzip
  rc=$?; rm -f /tmp/.bbp-dump.yml; exit $rc' > "$ARCHIVE"
chmod 0600 "$ARCHIVE"; chown "$OWNER" "$ARCHIVE"
echo "archive: $ARCHIVE ($(stat -c %s "$ARCHIVE") bytes)"

echo "== [2/4] production document counts (read-only)"
PROD_COUNTS="$(docker exec -e BBP_DB="$DB" -e COUNT_JS="$COUNT_JS" "$PROD" sh -c '
  umask 077
  { printf "const pw = fs.readFileSync(\"/run/secrets/mongo_root_password\", \"utf8\").trim();\n"
    printf "db.getSiblingDB(\"admin\").auth(\"%s\", pw);\n" "${MONGO_INITDB_ROOT_USERNAME:-root}"
    printf "%s\n" "$COUNT_JS"; } > /tmp/.bbp-count.js
  mongosh --quiet --nodb --eval "db = connect(\"mongodb://127.0.0.1:27017/admin\"); load(\"/tmp/.bbp-count.js\")"
  rc=$?; rm -f /tmp/.bbp-count.js; exit $rc')"
# mongosh also echoes the value of the last --eval expression (load() -> "true"):
# compare only the tagged counts line from each side.
counts_line() { printf '%s\n' "$1" | sed -n 's/^BBP_COUNTS //p'; }
PROD_COUNTS="$(counts_line "$PROD_COUNTS")"
echo "production: $PROD_COUNTS"

echo "== [3/4] restore into a disposable mongod"
docker network create --internal "$NET" >/dev/null
docker run -d --rm --name "$TMP_MONGO" --network "$NET" --tmpfs /data/db --tmpfs /data/configdb --tmpfs /tmp \
  --label bitbotpro.purpose=restore-check "$MONGO_IMAGE" >/dev/null
for i in $(seq 1 60); do
  docker exec "$TMP_MONGO" mongosh --quiet --eval 'db.adminCommand({ping: 1}).ok' 2>/dev/null | grep -q 1 && break
  [ "$i" = 60 ] && { echo "FAIL: disposable mongod not ready"; exit 1; }
  sleep 1
done
docker exec -i "$TMP_MONGO" mongorestore --quiet --archive --gzip < "$ARCHIVE"
RESTORED_COUNTS="$(docker exec -e BBP_DB="$DB" "$TMP_MONGO" mongosh --quiet --eval "$COUNT_JS")"
RESTORED_COUNTS="$(counts_line "$RESTORED_COUNTS")"
echo "restored:   $RESTORED_COUNTS"

echo "== [4/4] compare"
if [ -z "$PROD_COUNTS" ] || [ -z "$RESTORED_COUNTS" ] \
   || [ "$(printf '%s\n' "$PROD_COUNTS" | wc -l)" -ne 1 ] || [ "$(printf '%s\n' "$RESTORED_COUNTS" | wc -l)" -ne 1 ]; then
  echo "== RESULT: FAIL (could not read exactly one counts line from each side)"
  exit 1
elif [ "$PROD_COUNTS" = "$RESTORED_COUNTS" ]; then
  echo "== RESULT: PASS (backup restores with identical per-collection counts)"
else
  echo "== RESULT: FAIL (counts differ; writes during the dump can cause small differences — rerun when idle)"
  exit 1
fi
