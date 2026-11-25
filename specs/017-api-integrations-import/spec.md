# 017 — API Integrations: URL import, connection check, endpoint results, response quality

Builds on spec 015 (read-only transaction lookups, PR #7). The tester, formatter and bot path
(`run_lookup`, `format_fields`, `bot_block`) are unchanged; this adds the discovery and diagnostics
controls BitBotPro had, with what each one measures made explicit.

## Origin

Reference: `origin/bitbotpro-v1.0.0` at `1c20d2a` (= `aa55976^`).
- `backend/api_integrations_routes.py`: `discover_endpoints`, `_fetch_documentation`,
  `test_connection`, `_refresh_endpoint_counts`, `test_endpoint`, `get_stats`.
- `backend/api_integrations.py`: `parse_openapi_spec`, `_flatten_schema`.
- `backend/api_diagnostics.py`: `run_passive_scan`, `validate_response_against_schema`.
- File upload: tip `02bbd40` `backend/api/external_apis.py` `discover_endpoints` (`document`).

What changed from the reference, and why:
- **Test connection.** BitBotPro counted HTTP 401/403 (and 400/404/422 with placeholder parameters) as
  success and showed "Connected (HTTP 401)" in green. Here reachability and credentials are separate
  results, and a rejected key is never a success.
- **Counts.** "N active" meant "enabled", and every GET was enabled on import. Here the counts are
  Documented, Read-only GET, Used by lookups and Tested, and import never marks anything tested.
- **Endpoint health.** BitBotPro kept no per-endpoint result. Here each endpoint shows its latest
  observed result from a tester or bot call.
- **Schema validation.** BitBotPro checked top-level fields and the first array item only, and counted
  wrong-typed fields as "found". Here nested fields and every array item (up to 50 per array) are
  compared, and type differences are reported as such.
- **Documentation score.** BitBotPro's "Documentation Coverage" score came from the document alone.
  It is kept as a plain line labelled "what the document describes, not what the API returns",
  separate from response quality.

## 1. Import from a URL or a file

- **Documentation URL** (for example `https://ada.api.gridscan.io/api/openapi.json`):
  - The same outbound rules as lookups apply: https, a public host, every DNS answer public, the
    connection pinned to the checked address, and no redirects. A query string is allowed.
  - Documents are capped at 2 MB with a 20 s timeout and must be JSON.
  - The key is sent only when the document is on the integration's own origin.
  - The URL is remembered for **Re-import from URL**.
- **File upload:** the same parser, 2 MB, JSON only.
- **Parser** (`parse_document`): it now keeps, per endpoint:
  - `response_schema`, a compact normalised schema of the first 2xx (or `default`) JSON response.
    It handles `$ref` (cycles stop), `allOf`, `oneOf`/`anyOf` (a single non-null variant becomes
    nullable), OpenAPI 3.1 type lists, `nullable` and `x-nullable`. Limits: depth 10, 2,000 nodes per
    endpoint.
  - `requires_auth`, from the operation's or the document's `security`.
- **Re-import merges by `METHOD path`:**
  - It reports new, updated, removed and unchanged endpoints, with their ids.
  - It never touches lookups, their formatting or the saved key.
  - A lookup whose endpoint disappeared is listed in the import result and marked in the lookup
    list. Its settings are unchanged and the bot keeps calling its saved path. The tester asks for an
    endpoint to be chosen first.
  - Results for removed endpoints are deleted.
- **Base-path warning:** shown when the base URL's path is repeated by the documented paths. For
  example, base `https://ada.api.gridscan.io/api` with paths `/api/txs/{hash}` would make every
  request go to `/api/api/…`.

## 2. Test connection

Settings: a **health check** endpoint and a **credential check**. The health check must be a GET
with no required parameters. The credential check must be a GET endpoint that the document doesn't
mark as public, plus a valid sample value for each parameter. The check makes these requests:
- **Health check,** without the key. Any HTTP answer means the host is reachable; only 2xx means the
  health check passed (`reachable`, `unhealthy`, `unreachable`, `blocked`).
- **Credential check, with the key:**
  - 401/403 → `rejected`;
  - 2xx → the same request **without** the key must return 401/403 for `accepted`, otherwise
    `not_verified` ("the endpoint also answered without a key");
  - 404 (the sample doesn't exist), 429, 3xx, 5xx, other 4xx or a network error → `not_verified`,
    with the reason;
  - no key saved → `missing`; auth type none → `not_required`.
- **Without a health endpoint,** an HTTP answer to the credential check shows reachability.
- **Overall `ok`** = reachable **and** (`accepted` or `not_required`).
- **Saved and shown:** each probe (label, URL without the key, HTTP status, time) and the last check
  with its timestamp (`connection_check`). Probes count against the workspace's API-call budget of
  30 per minute.

## 3. Counts and endpoint results

- **Counts:** Documented (endpoints), Read-only GET, Used by lookups (distinct endpoints a saved
  lookup calls), Tested (endpoints with an observed result).
- **`api_endpoint_status`** holds one row per endpoint: the latest result of a tester or bot call, with
  outcome, HTTP status, reason, message, source and timestamp. Outcomes:
  - **Passed:** 2xx JSON with the configured fields;
  - **Not found for this input:** the lookup's not-found rule matched;
  - **Failed:** anything else that reached the network;
  - **Not tested:** no row.

  Configuration errors and BridgeBot's own rate limit send nothing and record nothing. Import and the
  connection check never write here.

## 4. Response quality (tester only)

`compare_response(data, schema)` walks the documented schema and the actual JSON together and gives
every field path (array items as `outputs[].amount[].quantity`) one status:
- `missing_required`;
- `type_difference` (`integer` satisfies `number`, and `null` is allowed only when nullable);
- `unexpected` (present but not documented);
- `missing_optional` (labelled separately, not an error);
- `ok`.

It also says in how many of the objects at that level each one happened. When the endpoint documents
no response schema, it says so. The raw JSON view, the field picker (leaf paths of the real response),
the formatted preview and the shared tester/bot formatter are unchanged. The bot never receives the
quality report.

## Storage

- `api_integrations`, new fields: `doc_url`, `import.{source, url|filename, changes, coverage,
  lookups_missing_endpoint, path_warning}`, `connection`, `connection_check`; `endpoints[]` gain
  `response_schema` and `requires_auth`.
- New collection `api_endpoint_status`: unique on (instance_id, integration_id, endpoint_id), removed
  with its integration or workspace.
- PR #7 records without these fields load and work. A re-import adds the schemas and reports every
  endpoint as updated.

## Testing

Tests use isolated copies of Mongo and ParadeDB with a test backend. A wrapper sends one made-up host,
`gridscan-mock.test`, to an in-process GridScan-like mock; every other host uses the real network
through the same guard.
- **Live GridScan, read-only:** the public spec (URL import and file upload) and `/api/health`.
- **Mock:** key acceptance and rejection, schema mismatches, failures and re-import.
- **Unit tests:** `tests/test_api_integrations_import_unit.py`, plus spec 015's tests, which are
  unchanged.
