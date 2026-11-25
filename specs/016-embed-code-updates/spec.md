# 016 — Embed code and chat widget updates

## Origin

BridgeBot's `EmbedCode.js`, `ChatWidget.js` and `ChatPage.js` are identical to BitBotPro
`4a405d5` (2026-04-11). The later work on `origin/bitbotpro-v1.0.0`:
- `5509ca6` (Emergent import) and `1b8e0a3` (BitBotPro v1.0.0 hardening): the redesigned Embed
  Code page and widget, `GET /api/embed.js`, `GET /api/embed/config/{id}`,
  `PUT /api/admin/embed-config`. Last version at `aa55976^`; removed in `aa55976`.
- `02bbd40`-era Spec 012 webchat (`backend/api/webchat_assets.py`): a hardened loader (asynchronous
  config, DOM APIs only, no synchronous XHR, validated id, sandboxed iframe).

## Ported

**Embed Code page**: three modes (Popup, Inline, Full page) with a script-tag snippet, a manual
iframe and a React snippet each; five theme presets and 13 theme fields written into the snippet;
widget behaviour (welcome message, open on load) and a custom stylesheet URL saved per workspace;
a live preview per mode and direct URLs.

**Loader** `GET /api/embed.js` (based on the tip's hardened loader): reads `data-instance`,
`data-mode` (popup | inline | fullpage), `data-position` (bottom-right | bottom-left), the 13
theme attributes, `data-welcome` and `data-open`. The widget origin is taken from the script's own
URL, so no server URL configuration is needed. Configuration is fetched asynchronously; values
reach the page only through DOM APIs and `encodeURIComponent`. Inline mode fills
`#bridgebot-container` (or `#bitbotpro-container`, `[data-bridgebot]`). The iframe is sandboxed
(`allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox`).

**Embed configuration** (Mongo `embed_configs`, per workspace):
- `GET /api/embed/config/{instance_id}` — public, `Access-Control-Allow-Origin: *`, no
  credentials; returns only `name`, `welcome_message` (≤ 2,000), `default_open` and an https
  `custom_css_url` (≤ 2,048).
- `PUT /api/admin/embed-config` — workspace access; the stylesheet URL must be https.

**Widget** (`/widget`): theme from URL parameters, validated (colours, a font-family list,
a radius in px/rem/em/%); modes; welcome message (URL parameter, else saved config); the saved
custom stylesheet; the bot name from the public config (the old call to the protected
`/admin/bot-config` always failed for visitors); storage guarded for sandboxed frames; the reset
button now clears the right session key; a copy button on bot messages.

## Compatibility

Existing snippets keep working: `/widget?instance=<id>` (no mode) renders the same chat panel,
and the old inline auto-inject script only creates that iframe.

## Not ported (needs infrastructure BridgeBot doesn't have)

- Streaming replies (`/api/chat/stream`) and server-issued session tokens (spec 008 stage 4).
- Bot logo upload and launcher logo (object storage).
- Markdown rendering of replies (`react-markdown` + `remark-gfm`, new dependencies).
- The Spec 012 API-hosted widget page with per-instance `frame-ancestors` allow-lists.
