# Private beta — controlled real-service validation (PREPARED IN PHASE 6C; NOT EXECUTED)

All five validations are **mandatory before the first tester is invited**, and each needs explicit
approval before it starts. Dedicated test accounts only; no customer data; no production database.
They run against the **beta stack** of `beta-activation-plan.md` (steps A1–A6), i.e. the
release candidate behind `https://beta-api.bitbotpro.io` with the isolated database
`bitbotpro_v12`, while the legacy production API keeps running unchanged.

Every validation is done by the operator through the admin app (`https://beta.bitbotpro.io/app`)
unless stated. Mocked tests (unit, real-HTTP and browser runs with fakes) prove the code paths;
**they do not prove the real service works** — only these runs do.

Common prerequisites: beta stack healthy (`/api/health`, `/api/ready`), superadmin bootstrapped, a
test Enterprise **"Beta Validation"** with an Organisation **"QA"** and a Bot **"QA Bot"**,
Resend configured (invitation and reset e-mails are part of V1).

## V1 — Real AI provider (≈ 30 min)

| Item | Detail |
|---|---|
| Credentials | A dedicated Anthropic (or OpenAI) API key with a provider-side spend limit (e.g. USD 20) |
| Configuration | Enterprise credential "QA AI key" → Credentials → **Test** must say valid; Bot → AI provider Anthropic, model `claude-sonnet-4-6` (repeat step 1 with `claude-haiku-4-5`), credential = QA AI key (beta allowlist: `beta-preflight-6d.md` §5) |
| Steps | 1. Web instance → **Open preview** → ask "What can you help with?" (GENERAL, no documents). 2. Upload `handbook.md` (a 1-page test document with a unique fact, e.g. "The QA refund window is 17 days") to an Enterprise knowledge source assigned to the bot; set `knowledge.mode=knowledge_preferred`; ask about the refund window. 3. Set `strict_knowledge_only`; ask the same (answer + citation) and an unrelated question (must refuse with the fallback). 4. Revoke the key → ask again (safe apology, no stack trace). 5. Usage tab |
| Expected / pass | 1 answers; 2 answers "17 days"; 3 cites `handbook.md`, unrelated question refused with the configured fallback; 4 shows the generic error and nothing leaks; 5 shows the AI messages counted (preview labelled) |
| Fail | Any unvalidated answer in strict mode, any provider text/key in a response or log, usage missing |
| Cleanup / rollback | Revoke and delete the test credential; disable the provider key at the provider |

## V2 — Discord (test application + test server) (≈ 60 min)

| Item | Detail |
|---|---|
| Credentials | A **new** Discord application (Developer Portal, owned by the BitBotPro team) with a bot user; bot token, client id, client secret. Privileged intents: **Message Content** and **Server Members** enabled. A test Discord server where the operator is an administrator, with channels `#help` and `#staff`, a role named as `staff_role_name` |
| Permissions | OAuth2 redirect `https://beta-api.bitbotpro.io/api/hooks/discord/oauth/callback`; scopes `bot identify guilds`; bot permissions as requested by the install URL (View Channels, Send Messages, Read Message History) |
| Configuration | Superadmin: Platform → Beta Validation → Discord application → id, token, secret → enable → **Start gateway** (status `connected`) |
| Steps | 1. As the QA Organisation admin (invited user from V-common): Bot → create Discord instance → **Add to Discord server** → authorise in Discord → result page "Connected". 2. Listen mode `specific_channels`, select `#help`. 3. Post a question in `#help` → AI reply (long answer split ≤ 2000 chars). 4. Post in another channel → no reply. 5. A member with the staff role replies → bot pauses (handoff); `!bot resume` by staff → bot resumes. 6. Passive mode on: an unaddressed message gets a reply only when relevant. 7. DM the bot → no reply (DMs off by default). 8. Org admin tries the application controls via the UI/API → not offered / 404. 9. **Disconnect** → bot stops answering; it leaves the server only if no other instance uses it |
| Pass | All 9 as described; gateway reconnects after `docker restart bitbotpro-beta-worker` within 60 s; no token in logs |
| Cleanup / rollback | Disconnect; Superadmin → gateway stop → disable; delete the bot from the test server; reset the bot token in the Developer Portal |

## V3 — Telegram (test bot) (≈ 30 min)

| Item | Detail |
|---|---|
| Credentials | A test bot from @BotFather (token); a test group with the operator |
| Configuration | QA Organisation credential "QA Telegram" (validated with getMe on save); Bot → Telegram instance using it → **Connect** (registers the webhook `https://beta-api.bitbotpro.io/api/hooks/telegram/<instance>/<secret>` with the secret header) |
| Steps | 1. DM the bot → AI reply. 2. In the group: plain message (no reply in `mention_or_command`), `@botname question` and `/ask question` → replies. 3. Ask for a long answer → split into 4096-char parts. 4. Tap a suggestion button (if offered) → follow-up answer. 5. `curl -X POST` the webhook URL **without** the secret header → 404 and nothing processed. 6. **Rotate secret** → old URL refused, messages still work. 7. **Disconnect** → `deleteWebhook` done (getWebhookInfo shows empty URL) |
| Pass | All 7; usage counted once per message (Telegram may redeliver — no double reply) |
| Cleanup / rollback | Disconnect; revoke the credential; `/revoke` the token in @BotFather |

## V4 — Webchat on a real test website (≈ 30 min)

| Item | Detail |
|---|---|
| Requirements | A static test page on an HTTPS origin the team controls (e.g. `https://beta-widget-test.bitbotpro.io` or a GitHub Pages site) |
| Configuration | Web instance → allowed origins = the test origin → Install tab → copy the embed snippet into the page (popup, then inline and full-page variants) |
| Steps | 1. Load the page → launcher → ask → streamed answer. 2. Reload → history restored. 3. Inline and full-page modes render. 4. Copy the snippet to a page on another origin → refused (no widget/API access). 5. Send 25 messages in one minute → rate-limited message, no crash. 6. Suspend the Organisation → widget unavailable; resume → works |
| Pass | All 6; no console errors from the widget; usage counted |
| Cleanup / rollback | Remove the snippet; delete the instance |

## V5 — Zendesk sandbox (mandatory; ≈ 90 min)

| Item | Detail |
|---|---|
| Credentials | A Zendesk **sandbox/trial** account (`<sandbox>.zendesk.com`) with an admin agent; for OAuth: a Zendesk OAuth client created in that account (client id `bitbotpro_beta`, secret) with redirect `https://beta-api.bitbotpro.io/api/hooks/zendesk/oauth/callback`; for API-token mode: agent e-mail + API token |
| Configuration | Superadmin: Platform credentials → Zendesk OAuth client (client id + secret). QA admin: Bot → Integrations → Zendesk |
| Steps | 1. **OAuth connect** (subdomain → Zendesk consent → result page "Connected"); **Test** passes. 2. Second integration with **API token** credential; Test passes. 3. **Provision webhook** (or set signing secret manually) → Zendesk webhook created with signing. 4. Create a ticket in Zendesk as an end user (GENERAL mode, no documents) → `agent_assist`: private note with suggested reply + tags. 5. Switch to `auto_reply` with `zendesk.public_replies_allowed` → new ticket → public comment + private audit note. 6. Strict mode with the V1 document → knowledge-grounded reply with citation; unrelated ticket → no public reply (suggestion `no_suggestion`). 7. **Sidebar app**: download the ZIP, upload as a private app, open a ticket → "Generate" → draft (nothing posted). 8. Re-send the same webhook event from Zendesk → processed once. 9. `curl` the webhook URL with a wrong signature → refused, nothing processed. 10. Usage tab of the integration shows the messages. 11. **Disconnect/delete** the integration → webhook deprovisioned in Zendesk, token revoked, state `deleted` |
| Pass | All 11; **no ticket is ever created by BitBotPro** (D-Q11); no Zendesk token in logs; ticket data of this integration not visible from another Enterprise (check with a second test Enterprise user) |
| Cleanup / rollback | Delete the integrations; delete the OAuth client and API token in Zendesk; delete the private app |

## Order and effort

V1 (needed by all) → V4 → V3 → V2 → V5: about **4–5 hours** of operator time plus account
preparation (Discord application, BotFather, Zendesk sandbox, test web page: ≈ 1–2 hours).
Record each run (date, image id, pass/fail per step, screenshots of failures) in
`evidence/beta-validation/`. A failure blocks the beta until fixed and re-run.
