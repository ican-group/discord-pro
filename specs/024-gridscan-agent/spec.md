# 024 — GridScan Agent connection

A second connection type under **API Integrations**. It sends a bot's live Cardano lookups to a
GridScan Agent service running on the same server. The agent gives a model all of the Gridscan API's
endpoints as tools, and that model picks and chains the calls. This is unlike the deterministic
router (spec 018), which picks one endpoint per message by keywords.

## The agent

BridgeBot talks to an existing service. It does not run the agent itself. It uses two of the
agent's endpoints:

| Request | Use |
|---|---|
| `GET /api/health` | `{status, tools_loaded, initialized}` |
| `POST /api/chat` `{message, session_id}` | `{reply, tool_calls[], raw_json, tokens_used}` |

**Raw JSON.** A message that starts with `/raw` makes the agent return each API response it fetched
in `raw_json`. These payloads come from the API calls, not from the agent's model.

**Size limit.** The agent cuts any single API response at 50,000 characters. In `/raw` mode, it then
fails with HTTP 500. This is a bug in the agent, outside this change.

**Address.** The server operator sets the agent's address in `GRIDSCAN_AGENT_URL`, usually
`http://127.0.0.1:8099`. An optional `GRIDSCAN_AGENT_TOKEN` is sent as a bearer token.

**Why workspaces can't set the URL.** A workspace can't enter an agent URL. A per-workspace URL
would let any workspace admin make the server call internal addresses. The direct integration
refuses those URLs for the same reason.

## Behaviour

**Adding the connection**

- **Add GridScan Agent** appears on API Integrations only when `GRIDSCAN_AGENT_URL` is set.
- Each workspace (bot) can have one agent connection.
- Bot use starts **off**. Nothing else changes when it is added.
- The connection shows the server-set address. It has no import, endpoint table or editable
  settings.

**Test connection**

- Calls `GET /api/health`, then `POST /api/chat` with `/tools`. The agent answers `/tools` itself,
  without a model call or an API call.
- Reports whether the agent is reachable, how many endpoints it loaded, and each probe's HTTP status
  and time.
- Saves the result like the direct connection check.

**Use GridScan Agent for this bot (On/Off)**

- When on, and the workspace's **Bot lookups** switch is on, the agent is the bot's **only** lookup
  path.
- Saved lookups (spec 015) and direct integrations (spec 018) aren't used. Their settings, keys and
  bot access are kept unchanged.
- The page says so on the agent panel, the direct integration's bot panel and the routing tester.

**Each message, when the agent is on** (web chat, Discord, external API)

- **Session.** Session id is `bridgebot:<bot id>:<conversation>`. On Discord the conversation is
  `discord:<user>:<channel>`, so each user in each channel of each bot has their own memory in the
  agent. Web and API use their chat session.
- **Raw JSON requests** ("show me the raw JSON", "the exact API response", `/raw …`):
  - BridgeBot sends `/raw <message>` and posts the `raw_json` payloads itself. BridgeBot's model is
    not called.
  - **Discord:**
    - Each payload is split at line breaks into messages under 2,000 characters. Each message is its
      own `json` code block, labelled "part i/n", and has room for a mention.
    - Joined back together, the parts are byte for byte the payload the agent returned. A backtick is
      written as ``` so it can't end the code block, and it parses to the same JSON.
    - A payload that would need more than 10 messages is attached as a `.json` file, complete, with
      a message giving its size.
  - **Web chat:** the payloads as plain text, complete. **External API:** the payloads in Markdown
    code blocks, complete.
  - **When the agent cut a payload,** the message says so.
  - **When the agent answers from memory** (its model wrote the JSON), BridgeBot asks once more for
    a fresh API call. If there is still no payload, it says no API response is available. It never
    posts model-written JSON as the API response.
  - **When the agent fails or can't be reached,** BridgeBot says so. If the agent returned an error,
    it suggests a narrower query.
- **Other messages:**
  - The agent's answer and the list of API calls it made (tool name and HTTP status, with no
    arguments) go into the bot's prompt as the lookup result. BridgeBot's model writes the reply.
  - So the knowledge base and Knowledge Base Focus (spec 023) apply unchanged. At 100% focus, the
    agent's live data counts as grounding.
  - If the agent answered from memory without a new API call, the data is labelled as from an
    earlier lookup. If the message explicitly asks for a lookup ("look up …", "check again", "latest
    …"), BridgeBot first asks the agent once more for a fresh call.
  - If the agent is down, the prompt says the lookup is unavailable.
- **Discord typing indicator** shows while the agent works. It can take 10–40 seconds.
- **Limits and logging:**
  - Each agent request counts against the workspace's 30 lookups per minute.
  - Each one is logged in `api_lookup_log` as "GridScan Agent", with its status, time and calls.
  - The log has no addresses or hashes.

**Full identifiers** (added after the first deployment):

- In a lookup reply, the bot writes addresses, hashes, pool IDs and DRep IDs in full, in inline code.
  It never shortens them to "first 8…last 6", because users copy them into their next question. This
  applies to direct integrations too.
- The agent's answer may still shorten them. The prompt therefore also gets the full identifiers that
  the agent's successful calls used ("Looked up (full values): tx_utxos…: hash=…"). This is in the
  prompt only. The activity log still has no addresses or hashes.
- Identifiers from failed calls aren't offered, because the agent may have guessed them.

**Unchanged:**

- Bots without an agent connection, or with it off.
- The direct integrations and router.
- Passive Discord messages, which never trigger lookups.
- Knowledge Base Focus.
