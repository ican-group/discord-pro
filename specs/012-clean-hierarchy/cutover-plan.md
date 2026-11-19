# Phase 7 — production cutover plan (PREPARED IN PHASE 6B; NOT EXECUTED)

Nothing in this plan has been run. Every step needs the approvals in §2. Production today:
`bitbotpro-api:spec011-f245d7ed3bdb` (legacy app, `APP_MODE=api`) on BBP1, MongoDB
`bitbotpro-mongo` (database `bitbotpro`), PostgreSQL `bitbotpro-postgres`, public edge
Caddy (VPS) → WireGuard → `10.50.0.20:8000`. **Gridscan, PostgreSQL, Caddy, DNS and WireGuard
are not changed by this plan.**

## Key proposal (decision 6B-E, needs approval because it amends spec §15)

**Start Spec 012 on a new, empty database (`bitbotpro_v12`) instead of resetting `bitbotpro` in
place.** The legacy database stays untouched (read by nothing) until a later, separately approved
and allowlisted removal. Benefits: no destructive step on the cutover path; rollback is "start the
old image again" with zero data restore; `prepare_database` already refuses the legacy database,
so the new API cannot touch it by mistake. The Platform Superadmin identity
(`admin@bitbotpro.io`) is preserved by bootstrapping it in the new database (same e-mail; the
operator sets the password interactively). If spec §15's in-place reset is preferred instead,
steps 4–5 below apply unchanged and steps 10a–10b are replaced by §15 steps 3–8.

## 1. Preconditions

- Release blockers closed: B-ACCT (account creation), B-API (Spec 011 API integrations ported or
  explicitly retired), 6B-D (changelog decision), open requirement 3 (model allowlist review).
- Live verification done and approved (separate approvals): Discord gateway with a test Enterprise
  application and guild; Telegram test bot; Zendesk sandbox (OAuth, signed webhooks, reply modes,
  sidebar); AI provider credential test; K-H protocol (`knowledge-eval-protocol.md`) or the
  documented "K-H off" decision.
- A release-candidate image rebuilt from the **committed** tree, all gates green on that tree;
  frontend build from the same commit.
- `RESEND_API_KEY` available (invitations and password reset need e-mail).
- Operator access to BBP1 and the Emergent frontend deployment.

## 2. Required approvals (each explicit, in writing)

1. Commit and tag the release (repository).
2. The database strategy (6B-E) or the §15 in-place reset.
3. The MongoDB open-file change and its restart.
4. The maintenance window.
5. Production deployment of API + worker + frontend.
6. Any later removal of legacy data (allowlist, separate approval).

## 3. Maintenance window

60 minutes announced (expected ≈ 30): during it the legacy admin UI, chat widgets, Telegram
and Zendesk replies stop. Low-traffic slot; Gridscan unaffected.

## 4. Production backup (read-only)

`sudo bash deploy/scripts/backup-restore-check.sh` → `~/.bitbotpro/backups/bitbotpro-<UTC>.archive.gz`
(0600) + SHA-256; a second copy off the host; legacy `ENCRYPTION_KEY` backed up offline with it.
Only the `bitbotpro` database — never `--all`, never Gridscan, never PostgreSQL.

## 5. Restore verification

The same script restores into a throwaway mongod and compares per-collection counts (must be
equal). Keep its log. Rehearsed in Phase 6B (`deploy/rehearsal/backup-restore-rehearsal.sh`).

## 6. Image identity

- `bitbotpro-api:spec012-<commit12>` built with `--build-arg SOURCE_TREE=<tree>` from
  `git archive <commit>`; record image id, labels (`io.bitbotpro.source-tree`) and the Trivy report.
- Previous image kept: `bitbotpro-api:spec011-f245d7ed3bdb` (rollback). Never pushed to a registry.

## 7. Configuration changes (`deploy/.env` and secret files, 0600, outside the repository)

| Item | Value |
|---|---|
| `BITBOTPRO_SPEC012_TAG` | the release tag |
| `BITBOTPRO_SPEC012_DB` | `bitbotpro_v12` (6B-E) |
| `encryption_keys` secret | `k1=<new Fernet key>` (fresh key for the new database; the legacy key stays for rollback) |
| `resend_api_key` secret | required |
| Existing | `backend_mongo_url`, `jwt_secret` (new value recommended: invalidates nothing in the new DB) |
| Removed from the API environment | `CODE_HMAC_KEY`, `REGISTRATION_MODE` (legacy only) |
| Compose | `deploy/proposed/docker-compose.spec012.yml` + existing `docker-compose.edge-caddy.yml` |

## 8. MongoDB open-file limit

Apply `deploy/proposed/docker-compose.db.nofile.yml` (recreates `bitbotpro-mongo`, volumes kept)
**after** the API is stopped; validate `Max open files 64000 64000`; rollback = same command
without the override (`operations.md` §2).

## 9. Encryption-key readiness

New ring file present and parsable (`docker compose run --rm api python -c "…KeyRing.from_environment…"`
prints key ids only); offline copy stored; rotation runbook available (`operations.md` §1).

## 10. Deployment order

1. Announce; stop the legacy API: `docker compose -f deploy/docker-compose.api.yml … stop api`
   (container kept, not removed — fastest rollback).
2. Final backup (§4) and restore check (§5).
3. MongoDB file limit (§8).
4. 10a. Prepare + bootstrap: `docker compose -f deploy/proposed/docker-compose.spec012.yml run --rm api
   python -m api.bootstrap --email admin@bitbotpro.io --name "Platform Admin"` (password typed by the
   operator; refuses if a superadmin exists).
5. 10b. Start the API: `… up -d api`; wait for `healthy`; Caddy's `health_uri /api/health` turns green.
6. Frontend activation (§11).
7. Worker activation (§12).
8. Smoke tests (§16); Discord activation (§13); external verification (§14).

## 11. Frontend activation

Deploy the release frontend build (same commit, `REACT_APP_API_BASE_URL=https://api.bitbotpro.io`)
to bitbotpro.io through the existing Emergent deployment. Must go live together with the API: the
new frontend has no legacy pages, and the legacy frontend cannot talk to the new API.

## 12. Worker activation

`… up -d worker` (same image, `python worker.py`); healthy when its heartbeat is fresh; check
`docker logs bitbotpro-worker` for `worker started` and no errors. Exactly one worker.

## 13. Discord gateway activation

Superadmin: Platform → Enterprise → Discord application → save application id, bot token,
client secret → enable → *Start gateway*; status must reach `connected`. Then an Organisation admin
installs a test instance into the test guild and binds one channel (D-Q14 flow).

## 14. External integration verification (live, approved test accounts only)

Telegram test bot connect + message; Zendesk sandbox OAuth + signed webhook + suggestion; webchat
on a test page from an allowed origin; AI credential test; one invitation e-mail and one reset e-mail.

## 15. Health checks

`docker inspect --format '{{.State.Health.Status}}' bitbotpro-api bitbotpro-worker` → healthy;
`curl -s https://api.bitbotpro.io/api/health` → `{"status":"ok"}`; `/api/ready` → `ready`;
MongoDB `serverStatus().connections`.

## 16. Smoke tests

Superadmin login → empty Enterprise list → create Enterprise + owner → Organisation → AI credential
(test) → Bot → web instance → embed preview answer; foreign Host header → 400; unknown route → 404;
`/docs` → 404; a webhook with a wrong secret → 404; logs contain no secret (grep for key prefixes).

## 17. Rollback triggers (any one)

API or worker not healthy within 10 minutes; login or superadmin bootstrap fails; any 5xx on the
smoke tests; a secret visible in logs or responses; data written to a database other than
`bitbotpro_v12`; Discord/Telegram/Zendesk verification failing in a way that cannot be fixed by
configuration within the window.

## 18. Rollback commands

```bash
docker compose -f deploy/proposed/docker-compose.spec012.yml --env-file deploy/.env stop api worker
docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.edge-caddy.yml \
    --env-file deploy/.env up -d api          # legacy image spec011-f245d7ed3bdb, legacy database untouched
# frontend: redeploy the previous Emergent build
# MongoDB file limit: may stay (harmless) or roll back per operations.md §2
```

With 6B-E no data restore is needed. With the §15 in-place reset:
`mongorestore --drop --gzip --archive=<step-4 archive> --nsInclude 'bitbotpro.*'` (rehearsed).

## 19. Data-loss prevention

- Backups before any change; restore verified; key backed up with the archive.
- New database for Spec 012 (6B-E); the legacy database is never written by the new code
  (`prepare_database` refuses it) and is only removed later under a separate, allowlisted approval
  (spec §15 step 6 allowlist; never other databases, PostgreSQL, Docker volumes, secret files,
  backups or Gridscan). **Do not assume every MongoDB database belongs to BitBotPro**: list them,
  and touch only names on the allowlist.
- `docker compose down` is never run with `-v`; containers are stopped, not removed, until the
  release is accepted.

## 20. Post-cutover monitoring (first 72 hours)

Health status of both containers; `docker logs` for `worker error`, `5xx`, gateway reconnect loops
and rate-limit storms; MongoDB connections and open files; usage ledger growth vs quotas; deletion
jobs and outbound delivery queue (`/api/platform/workers`); Discord gateway status; daily backup of
`bitbotpro_v12` with a restore check on day 1 and day 7; key-rotation `status` shows only `k1`.
