# BitBotPro first public launch — runbook (spec 008 Stage 10)

Frontend: `https://bitbotpro.io` (Emergent). API: `https://api.bitbotpro.io`
(this VM, FastAPI). **Selected public edge: the existing Caddy VPS
(37.27.153.47) over WireGuard** — `deploy/wireguard/README.md` holds the exact
VPN/Caddy commands and their verification and rollback. The Cloudflare Tunnel
files remain as a documented alternative (never use both). Run everything from
`~/projects/discord-pro`. Docker needs `sudo`.

`EDGE="-f deploy/docker-compose.edge-caddy.yml"` — include it in **every** API
`up` once step 5b is done (it publishes the API on the WireGuard address only).
**Since step 10, use `bash deploy/scripts/api-compose.sh <compose args>` for every
API compose command**: it always adds BOTH `edge-caddy` and `resend` overrides
(e.g. `bash deploy/scripts/api-compose.sh up -d --no-deps --no-build api`).

**Every step marked [GATE] needs explicit approval before it runs.** Steps are
ordered so the API is never public before it is fully configured: the Tunnel and
DNS change (step 12) come last, after the private smoke tests pass.

Conventions: `TAG=launch-YYYYMMDD` (or the git short SHA once committed).
Logs go to `~/.bitbotpro/`. No command prints a secret value.

---

## 0. Preconditions (read-only)

- Latest gates green: `deploy/scripts/run-stage3-tests.sh` (backend unit +
  MongoDB integration) and `deploy/scripts/run-frontend-tests.sh`.
- Databases healthy: `sudo docker ps --filter name=bitbotpro- --format '{{.Names}} {{.Status}}'`.
- The current API image tag is recorded (for rollback):
  `grep BITBOTPRO_API_TAG deploy/.env`.

## 1. [GATE] Backup + verified restore (read-only on production)

```
sudo bash deploy/scripts/backup-restore-check.sh > ~/.bitbotpro/backup-check.log 2>&1; echo "exit=$?"
```
Dumps `bitbotpro` (gzip archive, `~/.bitbotpro/backups/`, mode 0600), restores it
into a disposable `mongod` on an isolated network and compares per-collection
counts. Expect `RESULT: PASS`. Repeat before every later deployment.

## 2. [GATE] Production secret `code_hmac_key` (as your user, no sudo)

```
bash deploy/scripts/generate-api-secrets.sh
```
Creates only missing files (here: `code_hmac_key`); never overwrites or prints.
Verify: `ls -l ~/.bitbotpro/secrets/code_hmac_key` → `-r--r--r--`, 65 bytes.

## 3. [GATE] Build the runtime image

```
TAG=launch-$(date -u +%Y%m%d)
sudo docker build --progress=plain --target runtime -t bitbotpro-api:$TAG backend > ~/.bitbotpro/build-runtime-$TAG.log 2>&1; echo "exit=$?"
```

## 4. R8 secret scan of the image (read-only)

```
sudo bash deploy/scripts/scan-image-secrets.sh bitbotpro-api:$TAG /home/ntfreem/.bitbotpro/secrets \
  ~/.bitbotpro/build-runtime-$TAG.log > ~/.bitbotpro/scan-$TAG.txt 2>&1; echo "exit=$?"
```
Expect `RESULT: PASS` (exit 0). INCOMPLETE (2) or FAIL (1) stops the launch.

## 5. Point the deployment at the new tag

Set `BITBOTPRO_API_TAG=$TAG` in `deploy/.env` (0600; keep the old value noted).
Validate: `docker compose -f deploy/docker-compose.api.yml --env-file deploy/.env config --quiet`
and run `python3 -m unittest tests.unit.test_compose_files` from `backend/` (host-only
deployment checks, including the tunnel files).

## 5b. [GATE] WireGuard to the Caddy VPS (no public exposure yet)

`deploy/wireguard/README.md` steps A–D: pre-checks, install `wireguard-tools`
and create the key (never printed), add the peer `10.50.0.20/32` to the VPS
`wg0` live without touching the Gridscan peers, bring `wg-bbp` up, order Docker
after it. Verify the handshake and that only `10.50.0.1/32` routes into the
tunnel.

## 6. [GATE] Recreate the API (published on 10.50.0.20 only; still not public)

```
sudo docker compose -f deploy/docker-compose.api.yml $EDGE --env-file deploy/.env up -d api
sudo docker ps --filter name=bitbotpro-api --format '{{.Names}} {{.Status}}'   # wait for (healthy)
sudo docker port bitbotpro-api                                                  # 8000/tcp -> 10.50.0.20:8000 only
```
Then `deploy/wireguard/README.md` step F (VPS can reach it; LAN and Gridscan
peers cannot). The process refuses to start without
`code_hmac_key`, `PUBLIC_APP_URL`/`PUBLIC_API_URL`, or with an invalid
`TRUSTED_PROXY_IPS`/`REGISTRATION_MODE` — check `sudo docker logs bitbotpro-api`
(contains no secrets; R8 scan step 8 confirms).

## 7. [GATE] Security indexes (admin command; never at startup)

```
C="sudo docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.admin.yml --env-file deploy/.env --profile admin"
$C run --rm admin-cli security-indexes                     # dry run: lists the 2 TTL indexes
$C run --rm admin-cli security-indexes --apply --confirm   # security_counters + oauth_states
```

## 8. Private smoke tests + R8 scan of the running container

From inside the container (no public path exists yet):
```
sudo docker exec -i bitbotpro-api python - <<'PY'   # -i: pass the script on stdin
import json, urllib.request, urllib.error
B = "http://127.0.0.1:8000"
def req(path, method="GET", body=None):
    r = urllib.request.Request(B + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp: return resp.status, dict(resp.headers), resp.read()[:200]
    except urllib.error.HTTPError as e: return e.code, dict(e.headers), e.read()[:200]
checks = [
  ("health 200", req("/api/health")[0] == 200),
  ("registration invite_only", json.loads(req("/api/auth/registration-mode")[2])["mode"] == "invite_only"),
  ("register 403", req("/api/auth/register", "POST", {"email": "a@example.org", "password": "x", "username": "a"})[0] == 403),
  ("docs disabled", req("/docs")[0] == 404 and req("/openapi.json")[0] == 404),
  ("KB 503", req("/api/knowledge/sources")[0] == 503),
  ("auth required", req("/api/auth/me")[0] == 401),
  ("security headers", all(h in req("/api/health")[1] for h in ("x-content-type-options", "content-security-policy", "strict-transport-security"))),
  ("no persona", "persona" not in req("/api/bot-info/none")[2].decode(errors="ignore")),
]
for name, ok in checks: print(("PASS " if ok else "FAIL ") + name)
PY
sudo bash deploy/scripts/scan-image-secrets.sh bitbotpro-api:$TAG /home/ntfreem/.bitbotpro/secrets > ~/.bitbotpro/scan-running-$TAG.txt 2>&1; echo "exit=$?"
```

## 9. [GATE] Superadmin bootstrap (one time)

```
$C run --rm admin-cli bootstrap-superadmin --email admin@bitbotpro.io --password-file /run/secrets/admin_password
# review the dry run, then add --confirm
```
Then: log in (after step 12, or temporarily via the container), change the
password in the UI, and delete `~/.bitbotpro/secrets/admin_password`.

## 10. [GATE] Email (required: invite-only registration sends invitations)

1. Verify `bitbotpro.io` in Resend; publish its SPF and DKIM records (and a DMARC
   record, `p=none` initially) in Cloudflare DNS.
2. Store the key (`implementation.md` → "Resend API key"; never echoed).
3. Recreate the API **with** the override from now on:
   `sudo docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.edge-caddy.yml -f deploy/docker-compose.resend.yml --env-file deploy/.env up -d --no-deps --no-build api`
   (keep `edge-caddy` — without it the API is no longer published on 10.50.0.20).

## 11. [GATE] Frontend (Emergent)

Build/deploy with `REACT_APP_API_BASE_URL=https://api.bitbotpro.io` (the Stage 9a
gate proves the origin is embedded). Until step 12 the frontend cannot reach the
API, so deploy it right before or right after the Tunnel.

## 12. [GATE] DNS + Caddy site (the API becomes public here)

`deploy/wireguard/README.md` step G: `api.bitbotpro.io A 37.27.153.47`
(DNS-only), install `deploy/caddy/api.bitbotpro.io.caddy` as a separate imported
file, `caddy validate`, graceful `systemctl reload caddy` (Gridscan keeps
serving), then the external checks in step G.3.

## 12-alt. Cloudflare Tunnel (alternative edge; not used with Caddy)

One-time setup on this VM (interactive login opens a browser URL):
```
sudo docker run --rm -it -v ~/.bitbotpro/cloudflared-setup:/home/nonroot/.cloudflared cloudflare/cloudflared:2024.12.2 tunnel login
sudo docker run --rm -v ~/.bitbotpro/cloudflared-setup:/home/nonroot/.cloudflared cloudflare/cloudflared:2024.12.2 tunnel create bitbotpro-api
```
`tunnel create` prints the tunnel ID and writes `<ID>.json` (the credentials):
```
sudo install -m 0444 -o $(id -u) -g $(id -g) ~/.bitbotpro/cloudflared-setup/<ID>.json ~/.bitbotpro/secrets/cloudflared_credentials.json
sudo rm -rf ~/.bitbotpro/cloudflared-setup/<ID>.json      # keep cert.pem only if more tunnels/DNS changes are planned
echo "CLOUDFLARE_TUNNEL_ID=<ID>" >> deploy/.env           # not a secret
```
Public DNS change (CNAME api → <ID>.cfargotunnel.com):
```
sudo docker run --rm -v ~/.bitbotpro/cloudflared-setup:/home/nonroot/.cloudflared cloudflare/cloudflared:2024.12.2 tunnel route dns bitbotpro-api api.bitbotpro.io
```
Apply the Cloudflare rules (`cloudflare.md` §2–3) **before** starting the tunnel.
Start:
```
sudo docker compose -f deploy/docker-compose.tunnel.yml --env-file deploy/.env --profile tunnel up -d
sudo docker logs bitbotpro-cloudflared 2>&1 | tail -n 20     # "Registered tunnel connection" x4
```

## 13. Post-launch configuration

- **Discord:** in Discord Developer Portal set the OAuth redirect to
  `https://api.bitbotpro.io/api/discord/callback`; the superadmin saves the
  platform credentials in *Discord App Setup*.
- **Telegram:** re-save each bot once (Settings → Telegram → Save) so its webhook
  is registered with `secret_token` (`secret_token_registered: true`).
- **Zendesk:** per workspace, copy the webhook *signing secret* from Zendesk and
  store it: `PUT /api/admin/zendesk/webhook-signing-secret {"signing_secret": "..."}`
  (admin UI field pending); OAuth callback URL is
  `https://api.bitbotpro.io/api/zendesk/oauth/callback`. Install sidebar app
  v1.1.0 (`domainWhitelist` = `api.bitbotpro.io`) and verify in a sandbox first.

## 14. External verification (from outside the LAN)

- `curl -sI https://api.bitbotpro.io/api/health` → 200, HSTS, CSP, nosniff.
- `curl -s -o /dev/null -w '%{http_code}' https://api.bitbotpro.io/docs` → 404;
  `https://api.bitbotpro.io/` → 404 (tunnel path rule).
- `nmap -Pn <public IP of the VM's network>` shows no new open ports.
- Forged `CF-Connecting-IP`/`X-Forwarded-For` from outside do not change rate-limit
  keys (Cloudflare overwrites `CF-Connecting-IP`; the API trusts it only from
  10.250.250.2).
- Browser: sign in at https://bitbotpro.io, open the widget on a test page, run
  an invitation end to end.
- R8 scan again (step 8 command; also covers cloudflared).

## Rollback (never touches database volumes)

1. Take the API off the internet: remove the Caddy `import` line and reload Caddy
   (`deploy/wireguard/README.md` → Rollback 1); then, if needed, unpublish
   (`up -d api` without `$EDGE`) and stop `wg-quick@wg-bbp`. (Tunnel alternative:
   `docker compose -f deploy/docker-compose.tunnel.yml ... --profile tunnel down`.)
2. Previous API image: set `BITBOTPRO_API_TAG=<previous>` in `deploy/.env`, then
   `sudo docker compose -f deploy/docker-compose.api.yml $EDGE --env-file deploy/.env up -d api`.
   Note: the pre-Stage-1 image does not know `code_hmac_key`, `oauth_states` or the
   new user fields; it ignores them (all database changes are additive).
3. Frontend: redeploy the previous Emergent build.
4. Never `down -v`; restore data only from a verified archive (step 1).
