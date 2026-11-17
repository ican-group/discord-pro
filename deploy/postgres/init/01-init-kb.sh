#!/usr/bin/env bash
# Runs once, on first start with an empty data volume, as the postgres user.
#
# Roles:
#   BITBOTPRO_PG_MIGRATOR  owns the KB database and `kb` schema; runs DDL/migrations.
#   BITBOTPRO_PG_APP       runtime role for the API: DML only, NOSUPERUSER,
#                          NOBYPASSRLS and not a table owner, so future
#                          row-level-security tenant policies apply to it.
set -euo pipefail

read_secret() {
  local v
  v="$(head -n1 "/run/secrets/$1")"
  if [ "${#v}" -lt 32 ]; then
    echo "secret $1 missing or too short" >&2
    exit 1
  fi
  printf '%s' "$v"
}

MIGRATOR_PW="$(read_secret postgres_migrator_password)"
APP_PW="$(read_secret postgres_app_password)"

psql -v ON_ERROR_STOP=1 --no-psqlrc -U "$POSTGRES_USER" -d postgres \
  -v db="$BITBOTPRO_PG_DB" -v migrator="$BITBOTPRO_PG_MIGRATOR" -v app="$BITBOTPRO_PG_APP" \
  -v migrator_pw="$MIGRATOR_PW" -v app_pw="$APP_PW" <<'SQL'
CREATE ROLE :"migrator" LOGIN PASSWORD :'migrator_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE :"app" LOGIN PASSWORD :'app_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE :"db" OWNER :"migrator";
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"app";
SQL

psql -v ON_ERROR_STOP=1 --no-psqlrc -U "$POSTGRES_USER" -d "$BITBOTPRO_PG_DB" \
  -v db="$BITBOTPRO_PG_DB" -v migrator="$BITBOTPRO_PG_MIGRATOR" -v app="$BITBOTPRO_PG_APP" <<'SQL'
-- pgvector (creating it needs superuser; all roles can use it afterwards)
CREATE EXTENSION IF NOT EXISTS vector;

-- Lock down the default schema; KB objects will live in `kb`.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"migrator", :"app";

CREATE SCHEMA kb AUTHORIZATION :"migrator";
GRANT USAGE ON SCHEMA kb TO :"app";

-- Objects the migrator creates in `kb` later are usable (DML only) by the app.
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA kb
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA kb
  GRANT USAGE, SELECT ON SEQUENCES TO :"app";

ALTER ROLE :"migrator" IN DATABASE :"db" SET search_path = kb, public;
ALTER ROLE :"app"      IN DATABASE :"db" SET search_path = kb, public;
SQL

echo "[init] KB database '$BITBOTPRO_PG_DB' ready: pgvector enabled, schema kb, roles '$BITBOTPRO_PG_MIGRATOR' / '$BITBOTPRO_PG_APP'"
