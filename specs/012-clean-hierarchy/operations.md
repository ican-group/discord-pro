# Spec 012 — Operations (Phase 6B release candidate)

Runbooks and design for the Spec 012 release candidate. Everything here was **rehearsed on
disposable local containers only**; nothing was applied to production
(`bitbotpro-api:spec011-f245d7ed3bdb` on BBP1). Items marked *Phase 7* need explicit approval.

## 1. Encryption keys and rotation

### 1.1 Design

| Requirement | Implementation |
|---|---|
| Versioned keys with explicit identifiers | `core/credentials.py::KeyRing`: `{key id → Fernet key}`, ids `^[a-z0-9][a-z0-9-]{0,31}$`, at most 8 keys, one **active** key. Every new ciphertext names its key: `enc1:<key id>:<Fernet token>` |
| Secure key loading | `KeyRing.from_environment`: `ENCRYPTION_KEYS` (one `<id>=<key>` per line) + `ENCRYPTION_ACTIVE_KEY_ID`, normally from `ENCRYPTION_KEYS_FILE` (Docker secret; `docker-entrypoint.sh` allowlist). A single `ENCRYPTION_KEY` is key id `k1` (or `ENCRYPTION_KEY_ID`). Both forms at once, duplicate ids, duplicate key material, an invalid key or a missing active id stop the process. Errors and `repr` never contain key material |
| Backward-compatible decryption | Every key in the ring decrypts its own `enc1:` values; pre-6B bare Fernet tokens are tried against every key (Fernet authenticates, so a wrong key fails cleanly). A ciphertext naming an unknown key fails closed (`CredentialError`) |
| Re-encryption with the new key | `core/key_rotation.py::rotate`: walks every `*_enc` field of the six collections that hold secrets (`ENCRYPTED_COLLECTIONS`) and rewrites values not under the active key with `Store.set_field_if` — an atomic field-level compare-and-set, so a concurrent writer always wins |
| Crash-safe resumability | Cursor (collection, last id) and counters saved after every batch in `key_rotation_runs`; a re-run resumes. Re-running from scratch is also safe (rotated values are skipped). Two operators at once share the run |
| Tenant isolation | A ciphertext is only ever rewritten in place, in the field it came from; no query outside the six collections; non-secret fields and documents in other collections are never written (tested) |
| Audit logging | `encryption.rotation_started` / `_resumed` / `_completed` platform audit events with counts per collection and per Enterprise id |
| No plaintext logging | Reports, audit events and the run record contain counts, key ids and document locations only — never a secret, a ciphertext or key material (tested) |
| Coverage | `test_every_encrypted_field_is_covered` builds a World with every kind of stored secret (provider keys, Telegram/Zendesk credentials, Zendesk OAuth client, Discord application token and secret, Zendesk webhook path and signing secrets, Telegram webhook secrets, PKCE verifier) and fails if any `*_enc` field lives outside `ENCRYPTED_COLLECTIONS` or a `*_enc` name in the source was not exercised |

Known limit: Fernet has no associated data, so a person with write access to the database could
move a ciphertext between two documents of the same key; access to the database is already
total compromise. A future AES-GCM format with the document id as associated data would close it.

### 1.2 Runbook (Phase 7 or later; never run against production without approval)

1. Generate a key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   **on the host, into the secret file** — never in a terminal log.
2. Write the ring file `encryption_keys` (0600): `k1=<old>` and `k2=<new>`; set
   `ENCRYPTION_ACTIVE_KEY_ID=k2`; restart API and worker (new values now use `k2`; `k1` still reads).
3. `docker compose … run --rm api python -m api.rotate_keys rotate` → JSON report; exit 0 when
   `verify.ok` (every value under `k2`, none undecryptable).
4. `python -m api.rotate_keys verify` again after a day of traffic (catches a concurrent writer
   that re-wrote a value under `k1`; `rotate` again if needed).
5. Remove `k1` from the ring; restart. Keep the old key in the offline backup for as long as the
   backups encrypted under it are retained.

**Rollback** (any time before step 5): `ENCRYPTION_ACTIVE_KEY_ID=k1`, restart, `rotate` (tested:
every value moves back to `k1`). After step 5 the old key must come back from the offline copy.

Tests: `tests/unit/test_key_rotation.py` (18) and
`tests/integration/test_spec012_mongo.py::MongoPhase6BKeyRotationTests` (3, real MongoDB: crash
and resume, two concurrent operators, rollback, the operator command).

## 2. MongoDB open-file limit

### 2.1 Findings (read-only inspection of BBP1, 2026-11-19)

| Item | Value |
|---|---|
| `bitbotpro-mongo` `HostConfig.Ulimits` | `null` (nothing set in `deploy/docker-compose.db.yml`) |
| mongod `Max open files` (soft / hard) | **1024** / 524288 (Docker's default soft limit) |
| dockerd / containerd `LimitNOFILE` | 524288 |
| Descriptors in use (legacy workload) | ≈ 128 |

mongod derives its incoming-connection cap from the soft limit (measured in the rehearsal: **409**
connections at 1024, 13 107 at 64000) and needs one descriptor per
open WiredTiger file (every collection and every index) plus one per connection. A Spec 012
database has 40 collections and 119 indexes after seeding (187 descriptors open at idle), and the API and
worker each hold a connection pool (Motor default up to 100) plus monitoring connections.
MongoDB's production notes recommend 64000.

### 2.2 Proposed change (*Phase 7*, not applied)

`deploy/proposed/docker-compose.db.nofile.yml` — an override that adds
`ulimits: nofile: {soft: 64000, hard: 64000}` to the `mongo` service; the base compose file is
unchanged.

| Step | Command / effect |
|---|---|
| Apply | `docker compose -f deploy/docker-compose.db.yml -f deploy/proposed/docker-compose.db.nofile.yml --env-file deploy/.env up -d --no-deps mongo` |
| Restart behaviour | The container is **recreated** (mongod stops and starts; typically 5–30 s). Named volumes `bitbotpro_mongo_data/config` are reused unchanged. The API must be stopped or in maintenance first (Phase 7 order) |
| Validate | `docker exec bitbotpro-mongo sh -c 'grep "Max open files" /proc/1/limits'` → `64000 64000`; `mongosh --eval 'db.serverStatus().connections'` → `current + available` ≈ 13 107 (rehearsal) |
| Rollback | Same command without the override (back to 1024); data unaffected |
| Capacity | Removes the descriptor ceiling as a limit for any foreseeable load; memory, not descriptors, becomes the bound. Postgres and Gridscan are not affected (separate containers/services) |

### 2.3 Rehearsal

`deploy/rehearsal/mongo-nofile-rehearsal.sh` (disposable `bbp-6b-rh-*` containers, bind-mounted
scratch directory, internal network): today's defaults → a Spec 012 database prepared and seeded
→ a connection storm (450 clients) → recreate with 64000 → same data → storm → rollback. Results:
§32.4 of `spec.md` and the evidence folder.

## 3. Backup and restore

### 3.1 Rehearsal (disposable data only)

`deploy/rehearsal/backup-restore-rehearsal.sh` with `tests/integration/backup_restore_check.py`:

1. **Representative database** built through the real API (test World): two Enterprises,
   Organisations, Bots, web/Telegram/Discord instances, the Discord application, credentials,
   memberships, an invitation, a Zendesk integration with signed webhook, a knowledge source,
   lifecycle states (suspended Organisation, archived Bot), a completed deletion job, audit history.
2. **Backup**: `mongodump --db … --gzip --archive` (read-only on the source), file mode 0600,
   SHA-256 recorded.
3. **Integrity**: checksum, `gzip -t`, `mongorestore --dryRun`; a deliberately corrupted copy must
   be rejected (negative control).
4. **Restore** into a *separate* disposable mongod under a different database name.
5. **Validation**: identical collections and per-document digests; relationships (no orphan
   Organisation/Bot/Instance/Integration/membership/credential); credentials decrypt with the
   test key ring (`key_rotation.verify`); identical indexes, TTL indexes and validators; the
   restored database is accepted by `prepare_database`; lifecycle states and computed
   availability identical; audit history identical and in order.

### 3.2 Production checklist (*Phase 7*, needs approval; the existing
`deploy/scripts/backup-restore-check.sh` already implements steps 2–4 read-only)

- [ ] Maintenance window announced; API stopped or read-only before the final dump.
- [ ] `mongodump --db bitbotpro --gzip --archive` (only the BitBotPro database — never `--all`,
      never Gridscan, never PostgreSQL) → `~/.bitbotpro/backups/pre-spec012-<UTC>.archive.gz`, 0600.
- [ ] SHA-256 recorded next to the archive and in the change record; a second copy off the host.
- [ ] Restore into a throwaway mongod; per-collection counts equal production's (script output kept).
- [ ] The encryption key(s) of the moment backed up offline together with the archive's id
      (a backup without its key cannot decrypt credentials).
- [ ] Rollback command prepared and tested on the throwaway copy:
      `mongorestore --drop --gzip --archive=<file> --nsInclude 'bitbotpro.*'`.
- [ ] Only then: the Phase 7 reset/cutover steps.

## 4. Release-candidate images

See `spec.md` §32.6 for the built identities. Properties (tested in
`test_core_architecture.py::test_runtime_image_is_the_spec012_release_candidate` and by the
image checks in the gate):

- **One image, two roles**: API (`APP_MODE=api`, default `CMD uvicorn server:app`) and worker
  (`APP_MODE=worker`, command `python worker.py`), D-Q4. The API process never builds the
  Discord gateway; the worker never serves HTTP.
- **Pinned**: Python base image by exact tag; every direct and transitive Python dependency
  pinned (`requirements.txt`, 34 packages, down from 140; refreshed to the fixed versions after the
  Trivy review), `pip check` in the build; base `python:3.11.18-slim-trixie`; `setuptools`/`wheel`
  removed from the image.
- **Deterministic where feasible**: built from `git archive <tree>` (no untracked files, no
  timestamps from the working copy); the source tree id is a build argument and an image label
  (`io.bitbotpro.source-tree`, `org.opencontainers.image.revision`).
- **No embedded secrets**: allowlist `.dockerignore`, no secret `ARG`/`ENV`, secrets only via
  `*_FILE` at run time.
- **Non-root**: `USER 10001:10001`; code owned by root, read-only for the runtime user; works with
  `read_only: true` + tmpfs `/tmp` (heartbeat file).
- **Health checks**: `HEALTHCHECK python /app/healthcheck.py` — API: `GET /api/health` on loopback;
  worker: heartbeat file younger than 120 s.
- **Safe start-up**: configuration validated before anything starts (exit 78 naming the variable);
  `prepare_database` refuses a legacy or foreign database before the first request.
- **Logging**: stdout, `log_redaction` installed on every handler including uvicorn's access log
  (now covering the Spec 012 secret paths), no request bodies logged.
- **Resource limits** (compose, *Phase 7*): API `mem_limit 2g`, `pids_limit 256`; worker
  `mem_limit 1g`, `pids_limit 256` (the PDF child process has its own CPU/memory/time limits).

## 5. Integration configuration audit

**Platform** = set by the operator (environment/secret files) or a Platform Superadmin;
**Enterprise / Organisation / Bot / Instance** = who may set it in the admin app (role permitting).
"Secret" = encrypted at rest, write-only in the API, never logged.

| Area | Setting | Required | Secret | Controlled by |
|---|---|---|---|---|
| MongoDB | `MONGO_URL` (`_FILE`) | yes | **yes** | Platform (operator) |
| MongoDB | `DB_NAME` | yes | no | Platform; must be an empty or Spec 012 database (legacy refused at start) |
| Encryption | `ENCRYPTION_KEYS` + `ENCRYPTION_ACTIVE_KEY_ID`, or `ENCRYPTION_KEY` | yes | **yes** | Platform (§1) |
| Sessions | `JWT_SECRET` (≥ 32 chars) | yes | **yes** | Platform |
| Admin app | `PUBLIC_APP_URL` (`https://bitbotpro.io`) | yes | no | Platform — reset links `…/app/reset-password#token=`, invitation links `…/app/invitations#token=`, OAuth result pages `…/oauth/<provider>/result` |
| Public API | `PUBLIC_API_URL` (`https://api.bitbotpro.io`) | yes | no | Platform — webhooks, OAuth callbacks, `embed.js`, widget |
| CORS | `CORS_ORIGINS` (exact https origins) | yes when the admin app is on another origin | no | Platform |
| Host header | `ALLOWED_HOSTS` | no (default: API host + loopback) | no | Platform |
| Client IP | `TRUSTED_PROXIES` (Caddy over WireGuard: `10.50.0.1`) | yes behind Caddy | no | Platform (edge override) |
| Rate limits | built in (login 30/15 min per IP, reset, invitations 30/h per inviter and 5/day per recipient, credential tests 30/h, preview 30/h, public chat per IP/instance, webhooks) | — | no | Platform (code); end-user rate `limits.end_user_msgs_per_minute` capped by policy (Enterprise/Organisation/Bot/Instance) |
| Quotas | `WEBCHAT_DAILY_LIMIT_PER_INSTANCE` (2000); policy `limits.max_monthly_ai_messages` | no | no | Platform; Enterprise/Organisation policy |
| E-mail | `RESEND_API_KEY` (`_FILE`), `FROM_EMAIL` | for invitations/reset e-mail | key **yes** | Platform. Without it no e-mail is sent (invitations report `email_sent: false`) |
| AI providers | provider key per credential (Anthropic/OpenAI) | per bot | **yes** | Enterprise or Organisation credential; Bot selects it (`ai.credential_id`, `ai.provider`, `ai.model`); allowed providers/models: Platform → Enterprise/Organisation policy |
| AI providers | model allowlist (`core/config_registry.py::MODEL_ALLOWLIST`) | — | no | Platform (code). **Finding:** predates the Claude 5 family; review before release (open requirement 3) |
| Knowledge ingestion | sources, documents, ACLs | no | no | Enterprise/Organisation; `knowledge.*` keys at Enterprise→Instance; policies `knowledge.*` tighten-only. PDF parsing in the sandboxed child process; OCR not available |
| Webchat | instance `allowed_origins`, `widget.*` keys | per instance | no | Bot/Instance; policy `webchat.allowed_embed_origins`, `webchat.public_access_allowed` |
| Telegram webhooks | bot token (credential) | per instance | **yes** | Organisation credential; Instance connect/rotate-secret (path + header secrets generated, encrypted) |
| Discord gateway | Enterprise application: application id, bot token, client secret, intents, avatar, gateway start/stop | per Enterprise | token, secret **yes** | **Platform Superadmin only (D-Q14)**; Organisation admins: install, channels, listen mode, handoff, passive mode, nickname (when sole user), disconnect |
| Discord gateway | `DISCORD_GATEWAY_URL` (default `wss://gateway.discord.gg`) | no | no | Platform |
| Zendesk OAuth | platform OAuth client (client id + secret) | for OAuth mode | secret **yes** | Platform Superadmin only (D-Q9) |
| Zendesk OAuth | redirect URI `PUBLIC_API_URL/api/hooks/zendesk/oauth/callback` | for OAuth mode | no | Platform (registered in the Zendesk OAuth client) |
| Zendesk API token | e-mail + token + subdomain | for token mode | token **yes** | Organisation credential |
| Zendesk webhooks | path secret (generated), signing secret (required, D-Q10) | per integration | **yes** | Bot integration (admin); policy `security.require_webhook_signatures` stays required |
| Background workers | `APP_MODE=worker`, `WORKER_ID`, `WORKER_INTERVAL_SECONDS` | one worker container | no | Platform (compose) |
| Legacy (not used by Spec 012) | `CODE_HMAC_KEY`, `REGISTRATION_MODE`, `CLAUDE_API_KEY`, `EMERGENT_LLM_KEY`, `DISCORD_*`, `KB_*` | — | — | remove from the production environment at cutover |
