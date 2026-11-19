# Phase 7 — coordinated production cutover runbook (2026-11-19)

Approved: direct production replacement at `https://bitbotpro.io` (Emergent) and
`https://api.bitbotpro.io` (BBP1 behind the VPS Caddy). No DNS, Cloudflare or Caddy change.

## Release identity

| Item | Value |
|---|---|
| Commit | `aa55976b7df76edc3a7054b80d7e80cdc08ded85` on `ican-group/discord-pro` branch `bitbotpro-v1.0.0` |
| Tag | `bitbotpro-spec012-r1` (pushed); previous release tagged locally as `bitbotpro-spec011-f245d7ed3bdb` (commit `1c20d2a`) |
| Backend | tree `4219354d14693023ccf8baa9d2dc2c53f8f1427e` = image `bitbotpro-api:spec012-beta1-4219354d1469` (`sha256:181265070b34…`), the Phase 6C-tested candidate |
| Frontend | `frontend/` (tree `c7260a71…`; identical to the tested source except `frontend/.gitignore`) |

## Prepared on BBP1 (done, no public effect)

- Legacy backup `~/.bitbotpro/backups/bitbotpro-20261119T092331Z.archive.gz` (0600, `.sha256` beside it),
  restore-verified (24 collections / 8 documents identical).
- Production configuration preserved in `~/.bitbotpro/phase7-preserved-20261119/`.
- Rollback images kept: `bitbotpro-api:spec011-f245d7ed3bdb` (current), `bitbotpro-api:launch-20261117`.
- New secrets in `~/.bitbotpro/secrets-v12/` (MongoDB user, JWT secret, key ring `k1`); the legacy
  secrets are untouched; the production Resend key is reused.
- MongoDB user `bitbotpro_v12_app` (readWrite + dbAdmin on `bitbotpro_v12` only; legacy and admin
  reads refused, verified); database `bitbotpro_v12` prepared (40 collections).
- `bitbotpro-v12-api` (bound to `127.0.0.1:8100`, loopback only) and `bitbotpro-v12-worker` healthy
  (`deploy/docker-compose.spec012.yml`, project `bitbotpro-v12`, variables in git-ignored `deploy/spec012.env`).

## A. Superadmin bootstrap (operator, before the switch)

Run in the Claude Code prompt (the password is typed, never echoed or stored):

```
! docker exec -it bitbotpro-v12-api /app/docker-entrypoint.sh python -m api.bootstrap --email admin@bitbotpro.io --name "Platform Admin"
```

## B. Emergent build settings (operator)

| Setting | Value |
|---|---|
| Repository / branch | `ican-group/discord-pro` / `bitbotpro-v1.0.0` at `aa55976b` (tag `bitbotpro-spec012-r1`) |
| App root | `frontend/` |
| Install / build | `yarn install --frozen-lockfile` / `yarn build` (CRA via craco; output `frontend/build`) |
| Node | 20.x (tested with 20.18.1) |
| Environment | `REACT_APP_API_BASE_URL=https://api.bitbotpro.io` and `REACT_APP_BACKEND_URL=https://api.bitbotpro.io` |
| SPA routing | all unknown paths → `index.html` (client routes `/app/*`, `/oauth/*/result`, `/docs`) |

Instructions: in Emergent, point the bitbotpro.io deployment at the branch/commit above with the
environment variables, **build but do not promote yet** if Emergent offers that; if Emergent can only
deploy-and-publish in one step, deploy it at step C3 below. Tell me when the build is ready.

## C. Coordinated switch (minimal mismatch window)

The old frontend only works with the old API and the new frontend only with the new API, so the two
switch back to back. Expected API downtime: about 10–30 seconds.

1. **C1 Final check (me):** v12 API/worker healthy; legacy backup ≤ 1 hour old (re-run the backup script if older).
2. **C2 Superadmin exists (A).**
3. **C3 Frontend (operator):** publish the new Emergent deployment of bitbotpro.io; say "published".
4. **C4 API (me), immediately:**
   ```bash
   docker stop bitbotpro-api                                   # legacy container stopped, NOT removed
   sed -i 's/^BITBOTPRO_V12_API_BIND=.*/BITBOTPRO_V12_API_BIND=10.50.0.20:8000/' deploy/spec012.env
   docker compose -p bitbotpro-v12 -f deploy/docker-compose.spec012.yml --env-file deploy/spec012.env up -d api
   # NOTE (executed 2026-11-19): ALLOWED_HOSTS must include 10.50.0.20 — Caddy active health checks send the
   # upstream address as Host; without it Caddy marks the API unhealthy and answers 503.
   ```
5. **C5 Verify (me):** `curl -s https://api.bitbotpro.io/api/health` → `{"status":"ok"}`, `/api/ready` → `ready`,
   `/docs` → 404; `https://bitbotpro.io/app` loads the new admin app; superadmin login; Gridscan site
   responds as before; `docker stats` of every container.
6. **C6** Real-service validation (separately approved: V1–V5, test accounts only), then tester invitations.

## D. Rollback (restores the previous application on the same domains; does not need the new one)

```bash
docker compose -p bitbotpro-v12 -f deploy/docker-compose.spec012.yml --env-file deploy/spec012.env stop api worker
docker start bitbotpro-api                                     # legacy API back on 10.50.0.20:8000
curl -s https://api.bitbotpro.io/api/health                    # legacy health
# Frontend: re-publish the previous Emergent deployment of bitbotpro.io (Emergent deployment history).
# Older fallback image: BITBOTPRO_API_TAG=launch-20261117 with deploy/scripts/api-compose.sh up -d api.
```

Nothing is restored or deleted: the legacy database `bitbotpro` was never written by Spec 012; the
`bitbotpro_v12` database, its user and secrets are kept for diagnosis. If C4 fails, the legacy
container is started again immediately (`docker start bitbotpro-api`) and the frontend rolled back.
