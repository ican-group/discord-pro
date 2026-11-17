# Spec 008 Stage 10 — Cloudflare edge configuration

> **Selected edge (2026-11-17): the existing Caddy VPS over WireGuard**
> (`deploy/wireguard/README.md`). With it, `api.bitbotpro.io` is a **DNS-only**
> A record to 37.27.153.47: Caddy terminates TLS and overwrites `CF-Connecting-IP`
> with `{remote_host}`. §2 (WAF/rate limits) applies only if the record is later
> proxied by Cloudflare — and then Caddy must instead take the client address from
> Cloudflare only for Cloudflare's IP ranges (`trusted_proxies`), which is a global
> Caddy option that also affects Gridscan; review before switching. §3 (frontend
> headers on `bitbotpro.io`) and §4 apply regardless of the API edge.

Everything here is configured in the Cloudflare dashboard for the `bitbotpro.io`
zone. **Nothing is applied yet.** Each item is part of the launch runbook
(`launch-runbook.md`) and needs approval. The API's own controls (rate limits,
signatures, session tokens, CORS, security headers) work without any of this;
Cloudflare is a second layer, never the only control (R-RL.6).

## 1. Hostnames

| Hostname | Origin | Proxy |
|---|---|---|
| `bitbotpro.io` | Emergent (frontend) | Proxied (orange cloud) — required for the response-header rules in §3 |
| `api.bitbotpro.io` | Cloudflare Tunnel `bitbotpro-api` → `http://10.250.250.3:8000` (internal) | Created by `cloudflared tunnel route dns` (CNAME to `<tunnel-id>.cfargotunnel.com`) |

The tunnel only forwards `^/api/` on `api.bitbotpro.io`; any other path or host gets
404 at the tunnel (`deploy/cloudflared/config.yml`).

**SSL/TLS:** mode *Full (strict)* for the zone; *Always Use HTTPS* on; minimum TLS
1.2; HSTS is sent by the API itself (`max-age=31536000; includeSubDomains`) and
should be enabled for `bitbotpro.io` in §3. Enable HSTS *preload* only after
every subdomain is HTTPS-only.

## 2. WAF and rate-limiting rules (`api.bitbotpro.io`)

Order matters: the skip rules come first.

| # | Rule | Expression (Cloudflare rules language) | Action |
|---|---|---|---|
| 1 | Webhooks: never challenged | `http.host eq "api.bitbotpro.io" and (starts_with(http.request.uri.path, "/api/telegram/webhook/") or starts_with(http.request.uri.path, "/api/zendesk/webhook/"))` | **Skip**: Super Bot Fight Mode, Browser Integrity Check, Security Level challenges, managed challenge rules. (Bots are the legitimate callers; authenticity is enforced by the API: path secret + Telegram secret header / Zendesk HMAC signature.) |
| 2 | Telegram source check (optional) | rule 1 path `/api/telegram/webhook/` and `not ip.src in {149.154.160.0/20 91.108.4.0/22}` | Block (Telegram's published webhook ranges; re-check before enabling) |
| 3 | Only the API surface | `http.host eq "api.bitbotpro.io" and not starts_with(http.request.uri.path, "/api/")` | Block |
| 4 | Methods | `http.host eq "api.bitbotpro.io" and not http.request.method in {"GET" "POST" "PUT" "PATCH" "DELETE" "OPTIONS"}` | Block |
| 5 | Managed rules | Cloudflare Managed Ruleset + OWASP Core Ruleset (paranoia level 1, anomaly threshold medium) on `api.bitbotpro.io`, **excluding** rule 1 paths | Managed challenge → log first for 7 days, then block |

Rate limiting (per IP, counting by `cf.connecting_ip`; the API enforces stricter
per-account limits itself):

| Path(s) | Limit | Action |
|---|---|---|
| `/api/auth/login` | 20 / minute | Block 10 min |
| `/api/auth/register`, `/resend-code`, `/forgot-password`, `/verify`, `/reset-password` | 10 / minute | Block 10 min |
| `/api/invite/*` | 30 / 10 minutes | Block 10 min |
| `/api/chat/send`, `/api/chat/stream` | 60 / minute | Block 1 min |
| `/api/chat/history/*` | 120 / minute | Block 1 min |
| `/api/discord/callback`, `/api/zendesk/oauth/callback` | 30 / minute | Block 10 min |
| `/api/telegram/webhook/*`, `/api/zendesk/webhook/*` | 600 / minute (no challenge) | Block 1 min |
| everything else under `/api/` | 300 / minute | Managed challenge |

Do **not** enable "I'm Under Attack" or JavaScript challenges on `/api/*` (API
clients, webhooks and the embedded widget's XHR cannot solve them).

## 3. Response headers for the frontend (`bitbotpro.io`, Transform Rules)

The frontend is static content on Emergent; its headers are set at Cloudflare
(*Rules → Transform Rules → Modify Response Header*). The API sets its own.

| Rule | Match | Headers to set |
|---|---|---|
| A. All pages | `http.host eq "bitbotpro.io"` | `Strict-Transport-Security: max-age=31536000; includeSubDomains` · `X-Content-Type-Options: nosniff` · `Referrer-Policy: strict-origin-when-cross-origin` · `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()` |
| B. App pages (not the widget) | `http.host eq "bitbotpro.io" and http.request.uri.path ne "/widget"` | `Content-Security-Policy: frame-ancestors 'none'` · `X-Frame-Options: DENY` (prevents clickjacking of the admin app) |
| C. Widget page | `http.host eq "bitbotpro.io" and http.request.uri.path eq "/widget"` | `Content-Security-Policy: frame-ancestors https:` (any HTTPS site may embed the chat widget; plain-HTTP and `file:` pages may not) — **remove** any `X-Frame-Options` |

A full script CSP for the React app (`script-src`, `connect-src
https://api.bitbotpro.io`, …) should be added in report-only mode first
(`Content-Security-Policy-Report-Only`), because the Emergent build injects
scripts and styles that need to be inventoried before enforcement.

## 4. Embedded widget

* Customer pages load `https://api.bitbotpro.io/api/embed.js`, which inserts an
  iframe to `https://bitbotpro.io/widget` with
  `sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"`,
  `allow="clipboard-write"` and `referrerpolicy="strict-origin"`.
* The widget's chat session token lives only in the iframe's `sessionStorage`
  (bitbotpro.io origin); the embedding page cannot read it, and it is never put
  in a URL. No `postMessage` channel exists.
* Privileged API responses are protected by CORS (exact origin
  `https://bitbotpro.io`, credentials) — customer origins can read only the
  public, allowlisted `embed/config` fields (`Access-Control-Allow-Origin: *`,
  no credentials).
* **Per-customer embedding allowlists (R-P9.2)** are not enforced: `frame-ancestors`
  is a single header for `/widget` (rule C), and a per-bot list would need the
  frontend host to vary headers by bot. Accepted risk for launch: any HTTPS site
  can embed any bot's widget; abuse is bounded by per-IP chat limits and the
  per-bot daily ceiling (spec 008 Stage 4). Revisit with a Cloudflare Worker that
  sets `frame-ancestors` from the bot's configured origins.
