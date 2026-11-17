# BitBotPro Backend 1 ↔ Caddy VPS over WireGuard (spec 008 Stage 10)

```
Client ──HTTPS──▶ Caddy VPS 37.27.153.47 (api.bitbotpro.io, TLS)
                     │ wg0 10.50.0.1  ── existing peers 10.50.0.11, 10.50.0.12 (Gridscan, unchanged)
                     │ WireGuard UDP 51820 (VM initiates; VM is behind home NAT)
                     ▼
              BBP1 VM wg-bbp 10.50.0.20 ──▶ 10.50.0.20:8000 (Docker DNAT) ──▶ bitbotpro-api:8000
                                            (MongoDB/PostgreSQL: internal Docker network only)
```

## Guarantees

- Only `10.50.0.1/32` is routed into the tunnel (no default route, DNS or LAN
  change); WireGuard drops packets from the VPS peer unless their source is
  `10.50.0.1`, so the Gridscan peers cannot reach this VM.
- The API is published **only** on `10.50.0.20:8000`. Four firewall rules, all
  tagged `bitbotpro-wg-bbp`, are added in `PreUp` (before the address exists) and
  removed in `PostDown` (after it is gone):
  - **F1/F2** (`DOCKER-USER`): client→API connections are dropped unless they arrive
    on `wg-bbp` from `10.50.0.1` (blocks a LAN host routing to 10.50.0.20, and a LAN
    host spoofing 10.50.0.1).
  - **F3** (`DOCKER-USER`): anything forwarded from `wg-bbp` that is not addressed to
    the API is dropped — the VPS cannot use BBP1 to reach the home LAN, other
    containers (databases) or the internet.
  - **I1** (`INPUT`): new non-ICMP connections from `wg-bbp` to the VM itself are
    dropped — the VPS cannot reach host services (ping stays allowed).
  F1–F3 use `--ctdir ORIGINAL`, so API replies are never dropped.
- Fail closed: Docker only DNATs to addresses the VM owns (`--dst-type LOCAL`), and
  10.50.0.20 exists only while the rules are installed. Tunnel down ⇒ API
  unreachable (and not started at boot), never exposed elsewhere.
- Databases are never published. The VPS gets no new open port (it already listens
  on UDP 51820). Gridscan's peers and Caddy site cannot be modified by the scripts.
- Caddy overwrites `CF-Connecting-IP` with the connecting client's address; the API
  trusts it only from `10.50.0.1` (`TRUSTED_PROXY_IPS`).

## Scripts (the only supported way to make each change)

| Script | Where | Approval | What it changes |
|---|---|---|---|
| `bbp-create-key.sh` | VM | 1 | installs `wireguard-tools` if missing; creates `/etc/wireguard/wg-bbp.key` (0600) once, never overwrites; prints only the public key |
| `vps-add-peer.sh` | VPS | 2 | appends one `[Peer]` block to `wg0.conf` and adds the peer live (`wg set`); refuses anything that could alter an existing peer |
| `bbp-install-conf.sh` | VM | 3 | renders and installs `/etc/wireguard/wg-bbp.conf` (0600); never overwrites; identical rerun is a no-op |
| `../caddy/vps-caddy-site.sh install` | VPS | 5 | adds one `import` line after validating a candidate config; graceful reload |
| `vps-remove-peer.sh`, `vps-caddy-site.sh remove` | VPS | rollback | remove exactly what the scripts added |

All are covered by `backend/tests/unit/test_wireguard_scripts.py` (mocked `wg`,
`wg-quick`, `caddy`, `systemctl`, `apt-get`; no root). Run every command from the
repository root on the VM, and from `~/bitbotpro-edge` on the VPS.

**VPS rule:** never run `systemctl restart wg-quick@wg0`, `wg-quick down wg0`,
`wg-quick save wg0`, `wg syncconf` or `systemctl restart caddy` during this procedure.

---

## A. Read-only pre-checks (no approval needed)

VPS:
```bash
sudo wg show wg0 allowed-ips                          # record: 10.50.0.11/32, 10.50.0.12/32 only
sudo wg show wg0 public-key                           # wg0 PUBLIC key (safe to copy) -> step D
sudo grep -ciE '^\s*SaveConfig\s*=\s*true' /etc/wireguard/wg0.conf   # must print 0
systemctl is-active caddy                             # active (Caddy runs as the systemd unit)
sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
sudo grep -rn 'bitbotpro' /etc/caddy/ || echo "no existing bitbotpro config"
sudo grep -nE '^\s*import' /etc/caddy/Caddyfile || echo "no imports"   # no sites/* glob
curl -sS -o /dev/null -w '%{http_code}\n' https://ada.api.gridscan.io/   # record Gridscan baseline
```
STOP if `SaveConfig = true`, if Caddy is not the `caddy` systemd unit, or if the
Caddyfile already imports a `sites/*` glob: the scripts refuse these cases.

VM (read-only; sudo only for listing rules):
```bash
sudo iptables -S DOCKER-USER                          # exists (or absent: PreUp creates it); no bitbotpro rules
sudo iptables -t nat -S PREROUTING | grep -c -- '--dst-type LOCAL -j DOCKER'   # 1 (fail-closed DNAT)
sudo iptables -S FORWARD | grep -m1 -- '-j DOCKER-USER'                         # Docker jumps to DOCKER-USER
sudo ufw status | head -1                             # Status: inactive
sudo docker info 2>/dev/null | grep -iA1 firewall     # backend: iptables
iptables -V                                           # (nf_tables)
```
UFW is inactive; I1 covers the tunnel. If UFW is enabled later, run
`sudo systemctl restart wg-quick@wg-bbp` afterwards and re-check step D's counts.

## B. [APPROVAL 1] VM: WireGuard tools and key pair

```bash
sudo bash deploy/wireguard/bbp-create-key.sh
```
Prints `BBP1 WireGuard PUBLIC key: …` (needed in step C). Rerunning is safe: an
existing key is kept (`EXISTS: … left unchanged`).

Verify (prints no secrets):
```bash
sudo bash -c 'stat -c "%a %U:%G %s %n" /etc/wireguard /etc/wireguard/wg-bbp.key /etc/wireguard/wg-bbp.pub; wg pubkey < /etc/wireguard/wg-bbp.key | cmp -s - /etc/wireguard/wg-bbp.pub && echo "public key matches private key"; ls -A /etc/wireguard'
dpkg-query -W -f='${Status} ${Version}\n' wireguard-tools
ip link show wg-bbp 2>&1 | head -1; systemctl is-enabled wg-quick@wg-bbp; systemctl is-active wg-quick@wg-bbp
ip -4 route get 10.50.0.1 | head -1
```
Expected: `700 root:root … /etc/wireguard`, `600 root:root 45 …wg-bbp.key`,
`644 root:root 45 …wg-bbp.pub`; `public key matches private key`; only
`wg-bbp.key wg-bbp.pub`; `install ok installed 1.0.20210914-1ubuntu4`;
`Device "wg-bbp" does not exist.`; `disabled`; `inactive`; route
`via 192.168.0.1 dev ens18`.

## C. [APPROVAL 2] VPS: add the BBP1 peer (live, no restart)

C1 — on BBP1 (repository root): read the public key and copy the three peer
scripts. `VPS_SSH` is the account you normally use on the VPS (previously `root`).
```bash
VPS_SSH='root@37.27.153.47'
sudo cat /etc/wireguard/wg-bbp.pub                    # BBP1 PUBLIC key (the private key is never read)
sha256sum deploy/wireguard/vps-add-peer.sh deploy/wireguard/vps-remove-peer.sh deploy/wireguard/vps-check-peer.sh
ssh "$VPS_SSH" 'install -d -m 0700 ~/bitbotpro-edge'
scp deploy/wireguard/vps-add-peer.sh deploy/wireguard/vps-remove-peer.sh deploy/wireguard/vps-check-peer.sh "$VPS_SSH":bitbotpro-edge/
ssh "$VPS_SSH" 'cd ~/bitbotpro-edge && sha256sum vps-add-peer.sh vps-remove-peer.sh vps-check-peer.sh'   # must match
```

C2 — on the VPS, read-only pre-checks (`sudo` is harmless when logged in as root):
```bash
cd ~/bitbotpro-edge
PUB='paste the BBP1 public key from C1'
systemctl is-enabled wg-quick@wg0; systemctl is-active wg-quick@wg0      # enabled, active
systemctl show wg-quick@wg0 -p ActiveEnterTimestamp                      # note it; compared in C4
sudo wg show wg0 allowed-ips                                             # only 10.50.0.11/32 and 10.50.0.12/32
sudo grep -ciE '^\s*SaveConfig\s*=\s*true' /etc/wireguard/wg0.conf       # 0
sudo stat -c '%a %U:%G %n' /etc/wireguard/wg0.conf                       # 600 root:root
sudo bash vps-check-peer.sh status "$PUB"                                # BBP1 peer state: ABSENT
```
STOP if wg0 is not managed by `wg-quick@wg0` (then `wg0.conf` may not be the
persistent configuration) or if `SaveConfig = true`.

C3 — add the peer (same shell):
```bash
sudo bash vps-check-peer.sh snapshot > before-wg0.txt    # public keys + allowed-ips only
sudo bash vps-add-peer.sh "$PUB"
```

C4 — automated verification (read-only; exit code 0 only if every condition holds):
```bash
sudo bash vps-check-peer.sh added "$PUB" before-wg0.txt; echo "exit=$?"
systemctl show wg-quick@wg0 -p ActiveEnterTimestamp      # identical to C2: wg0 was not restarted
sudo wg show wg0 latest-handshakes                       # Gridscan peers recent; BBP1 0 until Approval 3
```

### Reading the result

- **Successful addition:** `vps-add-peer.sh` printed
  `OK: BBP1 peer added live and persisted … Existing peers unchanged.` **and** C4
  prints `BBP1 peer state: COMPLETE (live and persisted)`,
  `PASS: Gridscan/existing peers unchanged; exactly one new peer (BBP1, 10.50.0.20/32), live and persisted`
  and `exit=0`.
- **Refused or automatically rolled back:** `vps-add-peer.sh` printed `STOP: …`. C4
  then reports `ABSENT` and `exit=1`; wg0 is as before. Send the output for review
  before retrying.
- **`STOP: wg-quick was DENIED READ access …`**: a file-permission or AppArmor/LSM
  denial, **not** a WireGuard configuration error; nothing was changed. Run the
  read-only diagnosis printed with it (`aa-status 2>/dev/null | grep -i wg`;
  `journalctl -k --since '-10 min' | grep -i denied`) and send the output.
  (Validation copies are made inside `/etc/wireguard` — root-only — because
  `wg-quick` may be confined to that directory; this is why the first attempt,
  which validated a copy in `/tmp`, stopped safely.)
- **Incomplete addition** (the run was killed, e.g. power loss or `kill -9`; an SSH
  disconnect alone cannot interrupt it — hang-ups are ignored from the backup
  onwards): C4 reports `BBP1 peer state: INCOMPLETE — persisted … but NOT live` or
  `… live but NOT persisted` and `exit=1`. Recover with
  `sudo bash vps-remove-peer.sh "$PUB"` and then repeat C3 and C4.
- If the SSH session dropped and you are unsure what happened, log in again and run
  `cd ~/bitbotpro-edge && sudo bash vps-check-peer.sh added "$PUB" before-wg0.txt`.

## D. [APPROVAL 3] VM: install the config and bring the tunnel up (API not published)

D0 — read-only pre-checks on BBP1 (record the outputs):
```bash
ip link show wg-bbp 2>&1 | head -1                    # Device "wg-bbp" does not exist.
sudo test -e /etc/wireguard/wg-bbp.conf && echo "wg-bbp.conf EXISTS" || echo "no wg-bbp.conf"
sudo iptables -S | grep -c bitbotpro-wg-bbp           # 0
sudo iptables -S FORWARD | grep -c -- '-j DOCKER-USER'                          # 1
sudo iptables -t nat -S PREROUTING | grep -c -- '--dst-type LOCAL -j DOCKER'    # 1
sudo docker ps --format '{{.Names}}  ports=[{{.Ports}}]'                        # every container: ports=[] (nothing published)
sudo docker network inspect $(sudo docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' | grep -c '10\.50\.' # 0
ip -4 route | grep -c '10\.50\.'                    # 0
```
On the VPS: `sudo wg show wg0 public-key` → the VPS PUBLIC key for D1.

D1 — install the config (does not start anything):
```bash
VPS_PUB='paste the VPS wg0 public key'
sudo bash deploy/wireguard/bbp-install-conf.sh "$VPS_PUB"
sudo grep -E '^(Address|PublicKey|Endpoint|AllowedIPs|PersistentKeepalive) =' /etc/wireguard/wg-bbp.conf
sudo grep -ciE '^\s*(PrivateKey|DNS|Table|ListenPort)\s*=' /etc/wireguard/wg-bbp.conf   # 0
```
`bbp-install-conf.sh` is the only way the config is installed: it never overwrites
(an identical existing file is accepted, a different one is a `STOP`).

D2 — start the tunnel (the four firewall rules are installed by `PreUp` BEFORE the
interface and its address exist):
```bash
sudo systemctl enable --now wg-quick@wg-bbp
```

D3 — start Docker after the tunnel on future boots (no Docker restart now):
```bash
sudo install -D -m 0644 deploy/wireguard/docker-after-wg-bbp.conf /etc/systemd/system/docker.service.d/10-after-wg-bbp.conf
sudo systemctl daemon-reload                          # reloads unit files only; Docker keeps running
systemctl show docker -p After | grep -o 'wg-quick@wg-bbp.service'
```

D4 — verify on BBP1:
```bash
ping -c 3 -W 2 10.50.0.1                              # 3 replies through the tunnel
sudo wg show wg-bbp                                   # private key: (hidden); peer endpoint 37.27.153.47:51820; latest handshake: N seconds ago
ip -4 route get 10.50.0.1 | head -1                   # 10.50.0.1 dev wg-bbp src 10.50.0.20
ip -4 route get 1.1.1.1 | head -1                     # 1.1.1.1 via 192.168.0.1 dev ens18 src 192.168.0.32 (unchanged)
ip -4 route get 192.168.0.10 | head -1                # 192.168.0.10 dev ens18 src 192.168.0.32 (unchanged)
ip -4 route | grep -c '^default'                      # 1 (unchanged)
resolvectl dns ens18                                  # Link 2 (ens18): 192.168.0.1 (unchanged)
resolvectl dns wg-bbp                                 # Link N (wg-bbp): (empty)
sudo iptables -S DOCKER-USER | grep -c bitbotpro-wg-bbp   # 3
sudo iptables -S INPUT | grep -c bitbotpro-wg-bbp         # 1
sudo systemctl restart wg-quick@wg-bbp && sudo iptables -S | grep -c bitbotpro-wg-bbp   # still 4 (idempotent)
sudo docker ps --format '{{.Names}}  ports=[{{.Ports}}]'                         # unchanged from D0
```

D5 — verify from the VPS:
```bash
ping -c 3 -W 2 10.50.0.20                             # 3 replies (ICMP allowed by I1)
timeout 5 bash -c '</dev/tcp/10.50.0.20/22'; echo "rc=$?"     # rc=124: dropped by I1 (rc=1 "refused" would mean I1 is missing)
sudo wg show wg0 allowed-ips                          # .11, .12 unchanged; 10.50.0.20/32 present
```
Then on BBP1: `sudo iptables -L INPUT -v -n | grep bitbotpro-wg-bbp` — the packet
counter is > 0 (the D5 connection attempt was dropped).

Rollback of Approval 3 (BBP1 only; the VPS peer and Gridscan are not touched):
```bash
sudo systemctl disable --now wg-quick@wg-bbp
sudo iptables -S | grep -c bitbotpro-wg-bbp           # 0 (PostDown removed exactly the four tagged rules)
ip link show wg-bbp 2>&1 | head -1                    # does not exist
sudo rm /etc/systemd/system/docker.service.d/10-after-wg-bbp.conf
sudo systemctl daemon-reload
```
`/etc/wireguard/wg-bbp.conf` and the key may stay (unused while disabled).

## E. [APPROVAL 4] VM: publish the API on the WireGuard address

Part of the launch deployment (`specs/008-pre-public-hardening/launch-runbook.md`
step 6). Without the Resend override:
```bash
sudo docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.edge-caddy.yml --env-file deploy/.env up -d api
```
With it, add `-f deploy/docker-compose.resend.yml` before `--env-file`. Verify:
```bash
sudo docker port bitbotpro-api                        # exactly: 8000/tcp -> 10.50.0.20:8000
sudo ss -ltn | grep ':8000'                           # only 10.50.0.20:8000, never 0.0.0.0 or 192.168.0.32
sudo iptables -t nat -S DOCKER | grep -- '--dport 8000'          # -d 10.50.0.20/32 ... -j DNAT
sudo iptables -t raw -S PREROUTING | grep -- '-j DROP'           # direct container-IP access dropped (Docker 28+)
```
From now on every `up` for the API includes `docker-compose.edge-caddy.yml`; since
Resend (runbook step 10) use `bash deploy/scripts/api-compose.sh up -d --no-deps --no-build api`,
which always adds both `edge-caddy` and `resend`.

## F. Connectivity verification (read-only)

VPS:
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://10.50.0.20:8000/api/health        # 200
curl -sS -D - -o /dev/null http://10.50.0.20:8000/api/health | grep -ci content-security-policy   # 1
```
A Gridscan peer (only if it can route to 10.50.0.20 at all):
`curl -m 5 http://10.50.0.20:8000/api/health` → timeout.
Another LAN host: `curl -m 5 http://192.168.0.32:8000/` → refused or timeout.
Optional, needs the owner's approval for that host: add a temporary route there
(`sudo ip route add 10.50.0.20/32 via 192.168.0.32`), confirm
`curl -m 5 http://10.50.0.20:8000/api/health` times out, then
`sudo ip route del 10.50.0.20/32 via 192.168.0.32`.
VM drop counters: `sudo iptables -L DOCKER-USER -v -n | grep bitbotpro-wg-bbp`.

## G. [APPROVAL 5] DNS and Caddy (the API becomes public here)

1. DNS: `api.bitbotpro.io A 37.27.153.47`, DNS-only (not proxied). Wait until
   `dig +short api.bitbotpro.io` returns `37.27.153.47` (Caddy needs it for the
   certificate).
2. Copy the Caddy files (from the VM, repository root) and compare checksums:
```bash
sha256sum deploy/caddy/vps-caddy-site.sh deploy/caddy/api.bitbotpro.io.caddy
scp deploy/caddy/vps-caddy-site.sh deploy/caddy/api.bitbotpro.io.caddy "$VPS_SSH":bitbotpro-edge/
ssh "$VPS_SSH" 'cd ~/bitbotpro-edge && sha256sum vps-caddy-site.sh api.bitbotpro.io.caddy'   # must match
```
   Then on the VPS:
```bash
cd ~/bitbotpro-edge
sudo bash vps-caddy-site.sh install ./api.bitbotpro.io.caddy
```
   The script validates the current config, installs the site file, validates a
   candidate Caddyfile with the one extra `import` line, and only then replaces the
   Caddyfile (backup kept) and runs `systemctl reload caddy`. On any failure the live
   Caddyfile is unchanged.
3. External checks:
```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.bitbotpro.io/api/health      # 200
curl -sS -o /dev/null -w '%{http_code}\n' https://api.bitbotpro.io/                # 404 (edge)
curl -sS -o /dev/null -w '%{http_code}\n' https://api.bitbotpro.io/docs            # 404
curl -sS -o /dev/null -w '%{http_code}\n' https://ada.api.gridscan.io/             # same as the step A baseline
curl -sSI https://api.bitbotpro.io/api/health | grep -ciE '^(strict-transport-security|x-content-type-options):'   # 2
```
Chat streaming: open the widget and confirm tokens arrive progressively.
Forged-IP check: 11 failed logins with different `CF-Connecting-IP` /
`X-Forwarded-For` values from one machine → the 11th returns 429 (that machine is
then rate-limited for the login window).

## Rollback (reverse order; each step is a separate approval; databases untouched)

1. **Public off** (VPS): `cd ~/bitbotpro-edge && sudo bash vps-caddy-site.sh remove`
   — removes only the import line after validating the result, then reloads.
   DNS may stay.
2. **Unpublish the API** (VM): the step E command **without**
   `-f deploy/docker-compose.edge-caddy.yml`; `sudo docker port bitbotpro-api`
   then prints nothing.
3. **Tunnel off** (VM):
```bash
sudo systemctl disable --now wg-quick@wg-bbp
sudo iptables -S | grep -c bitbotpro-wg-bbp           # 0 (PostDown removed the rules)
sudo rm /etc/systemd/system/docker.service.d/10-after-wg-bbp.conf
sudo systemctl daemon-reload
```
   Do step 2 before step 3: with the edge override still active and the tunnel
   off, the API does not start after the next Docker restart.
4. **Peer removal** (VPS):
```bash
cd ~/bitbotpro-edge
sudo bash vps-remove-peer.sh "$PUB"
sudo bash vps-check-peer.sh removed "$PUB" before-wg0.txt; echo "exit=$?"   # PASS … exit=0
```
   `vps-remove-peer.sh` removes the live peer only if its AllowedIPs are exactly
   `10.50.0.20/32`, and the file block only if it is exactly as `vps-add-peer.sh`
   wrote it; otherwise nothing is changed. It also clears an INCOMPLETE state
   (persisted-only or live-only). Never restore an older `wg0.conf` backup over a
   newer file. The VM key files may stay; they are unused once the tunnel is off.
