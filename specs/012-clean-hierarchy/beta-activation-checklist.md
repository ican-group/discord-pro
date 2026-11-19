# Private beta — single activation checklist (Phase 6D; NOT EXECUTED)

Legend: **[PROD-ADJACENT]** = touches a shared production component (VPS Caddy, DNS zone, the
shared `bitbotpro-mongo` instance or network) without changing any existing route, database or
container. Nothing here restarts or modifies the legacy API, its database or Gridscan.

## 1. Approvals required (each explicit)

- [ ] **AP-1** Commit and tag the beta release (tree `4219354d1469` + Phase 6D proposed files).
- [ ] **AP-2** DNS: two A records `beta-api.bitbotpro.io`, `beta.bitbotpro.io` → the VPS IP (DNS-only). **[PROD-ADJACENT]**
- [ ] **AP-3** VPS Caddy: install `beta.bitbotpro.io.caddy` (one new `import` line, graceful reload). **[PROD-ADJACENT]**
- [ ] **AP-4** MongoDB: create user `bitbotpro_v12_app` (bitbotpro_v12 only) in `bitbotpro-mongo`. **[PROD-ADJACENT]**
- [ ] **AP-5** Start the beta project `bitbotpro-beta` on BBP1 (creates database `bitbotpro_v12` on first start).
- [ ] **AP-6** Real-service validation V1–V5 with dedicated test accounts (includes the first real e-mails).
- [ ] **AP-7** Invite the first testers.

## 2. Credentials and test accounts (prepared by the operator; never pasted into chat or logs)

| Item | Where it goes |
|---|---|
| MongoDB password for `bitbotpro_v12_app` (≥ 32 chars) | `$BITBOTPRO_BETA_SECRETS_DIR/beta_mongo_app_password` + inside `beta_mongo_url` (`…&maxPoolSize=20`) |
| JWT secret (new, 48+ chars) | `beta_jwt_secret` |
| Fernet key `k1` (new) + offline copy | `beta_encryption_keys` |
| Resend API key (new, sending-only, `bitbotpro.io` domain) | `beta_resend_api_key` |
| Superadmin password (typed at bootstrap, policy-checked) | nowhere on disk |
| Anthropic test key with USD 50/month limit | entered in the admin app (Enterprise credential) |
| Discord: new application (bot token, client id, client secret), Message Content + Server Members intents, test server | Superadmin → Discord application |
| Telegram: test bot token (@BotFather), test group | Organisation credential |
| Webchat: an HTTPS test page the team controls | instance allowed origins |
| Zendesk: sandbox account, admin agent, OAuth client (`bitbotpro_beta`, secret) and an API token | Platform credential / Organisation credential |

## 3. Infrastructure changes (exact)

1. **Build** (BBP1, from the tag): `git archive <tag> -- backend | …` → `docker build --target runtime --build-arg SOURCE_TREE=<tree> -t bitbotpro-api:spec012-beta1-<tree12> -`; frontend: `BUILD_PATH=build-beta REACT_APP_API_BASE_URL=https://beta-api.bitbotpro.io yarn build`.
2. **Secrets**: create `$BITBOTPRO_BETA_SECRETS_DIR` (0700) and the files above (0640, group readable by uid 10001).
3. **MongoDB user** [PROD-ADJACENT]: `docker cp deploy/proposed/beta/mongo-create-beta-user.js bitbotpro-mongo:/tmp/` and the password file to `/tmp/beta_pw`; run the script as root (header of the script); delete `/tmp/beta_pw`. Creates one user; no database is modified.
4. **Start** (no production change): `docker compose -p bitbotpro-beta -f deploy/proposed/beta/docker-compose.beta.yml --env-file <beta.env> up -d` → all three containers healthy/running.
5. **Superadmin**: `docker exec -it bitbotpro-beta-api /app/docker-entrypoint.sh python -m api.bootstrap --email admin@bitbotpro.io --name "Platform Admin"`.
6. **DNS** [PROD-ADJACENT]: the two A records.
7. **Caddy** [PROD-ADJACENT] (on the VPS): `sudo bash vps-caddy-beta-site.sh install ./beta.bitbotpro.io.caddy` (validates first, backs up, graceful reload).
8. **Policy** (admin app, superadmin): the beta model allowlist and quotas (`beta-preflight-6d.md` §5).

## 4. Downtime

**None.** The legacy API, its database and Gridscan keep running throughout. Caddy reloads
gracefully. The MongoDB file-limit change is **not** part of the beta (it would restart MongoDB).

## 5. Rollback (any time; nothing to restore)

```bash
docker compose -p bitbotpro-beta -f deploy/proposed/beta/docker-compose.beta.yml --env-file <beta.env> stop   # beta off
sudo bash vps-caddy-beta-site.sh remove                      # on the VPS: removes the one import line, graceful reload
# DNS: delete the two A records.  MongoDB: the beta user can be dropped later
#   (db.getSiblingDB("bitbotpro_v12").dropUser("bitbotpro_v12_app")); bitbotpro_v12 is KEPT (backed up)
#   until a separately approved, allowlisted removal. Never `down -v`, never touch bitbotpro or Gridscan.
```

## 6. Smoke tests (after step 7)

`curl -s https://beta-api.bitbotpro.io/api/health` → `{"status":"ok"}`; `/api/ready` → `ready`;
`curl -s -o /dev/null -w '%{http_code}' https://beta-api.bitbotpro.io/docs` → 404;
`https://beta.bitbotpro.io/app` loads; superadmin signs in; create the "Beta Validation" Enterprise,
invite an operator-owned address → e-mail arrives → **create account from the link (B-ACCT)** →
sign in; `https://api.bitbotpro.io/api/health` (production) still `{"status":"ok"}` before and after.

## 7. Real-service tests

`real-service-validation.md` V1 → V4 → V3 → V2 → V5 on "Beta Validation" (≈ 4–5 h + 1–2 h
account preparation). AI: `claude-sonnet-4-6` (V1 also on `claude-haiku-4-5`). All five must pass.

## 8. Inviting testers

1. Superadmin creates each tester Enterprise (L-1) and applies the §3.8 policy.
2. Superadmin (or the Enterprise owner) invites by **e-mail** (link invitations cannot create accounts).
3. The tester opens the e-mail, creates an account, signs in; the owner adds Organisations, credentials,
   bots and instances, or invites further members.
4. Daily: backup of `bitbotpro_v12` (`operations.md` §3, DB name adapted) and a look at health, worker
   logs and usage per Enterprise.
