/** Restoration R3: API integrations on the Bot page (create, discover/import, endpoints, tests, activity). */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { integrationProblem } from "../components/ApiIntegrations";
import { IDS, MockApi, SAMPLES, renderApp, withPermissions } from "../testing/harness";

const LIST = SAMPLES.api_integrations.body;
const ONE = SAMPLES.api_integration.body;
const EPS = SAMPLES.api_endpoints.body;

function botApi(perms = { edit_config: true, manage_channel: true, create_child: true }) {
  const api = new MockApi();
  api.on("GET /{type_}/{object_id}/config", withPermissions(perms));
  api.on("GET /bots/{bot_id}/api-integrations", { status: 200, body: LIST });
  api.on("GET /api-integrations/{integration_id}", { status: 200, body: ONE });
  api.on("GET /api-integrations/{integration_id}/endpoints", { status: 200, body: EPS });
  api.on("GET /api-integrations/{integration_id}/activity", { status: 200, body: SAMPLES.api_activity.body });
  api.on("GET /api-integrations/{integration_id}/stats", { status: 200, body: SAMPLES.api_stats.body });
  return api;
}

async function openTab(api) {
  renderApp(`/app/bots/${IDS.bot}`, api);
  await userEvent.click(await screen.findByRole("tab", { name: "API integrations" }));
}

test("the integration form is validated before anything is sent", () => {
  const ok = { name: "Shop", base_url: "https://api.example.com", auth_type: "bearer", secret: "t" };
  expect(integrationProblem(ok)).toBe("");
  expect(integrationProblem({ ...ok, base_url: "http://api.example.com" })).toMatch(/https/);
  expect(integrationProblem({ ...ok, base_url: "https://api.example.com/?a=1" })).toMatch(/query/);
  expect(integrationProblem({ ...ok, secret: "" })).toMatch(/key or token/);
  expect(integrationProblem({ ...ok, secret: "" }, { creating: false })).toBe("");
});

test("an admin creates an API integration with a bearer token", async () => {
  const api = botApi();
  api.on("POST /bots/{bot_id}/api-integrations", { status: 201, body: ONE });
  await openTab(api);
  await userEvent.click(await screen.findByRole("button", { name: "Create API integration" }));
  const form = screen.getByRole("form", { name: "Create API integration" });
  await userEvent.type(within(form).getByLabelText("Name"), "Orders");
  await userEvent.type(within(form).getByLabelText("Base URL"), "https://api.example.com/v1");
  await userEvent.selectOptions(within(form).getByLabelText("Authentication"), "bearer");
  await userEvent.type(within(form).getByLabelText("API key or token"), "tok-123");
  await userEvent.type(within(form).getByLabelText("Documentation URL (optional)"), "https://api.example.com/openapi.json");
  await userEvent.click(within(form).getByRole("button", { name: "Create API integration" }));
  await waitFor(() => expect(api.calls("POST /bots/{bot_id}/api-integrations")[0].body).toEqual({
    name: "Orders", base_url: "https://api.example.com/v1", auth_type: "bearer", secret: "tok-123",
    doc_source_type: "openapi", doc_source_url: "https://api.example.com/openapi.json" }));
});

test("connection test, discovery, endpoint toggle, endpoint test and activity", async () => {
  const api = botApi();
  api.on("POST /api-integrations/{integration_id}/test-connection", { status: 200,
    body: { success: true, status: 200, message: "Connected (GET /health answered HTTP 200).", probes: [] } });
  api.on("PATCH /api-integrations/{integration_id}/endpoints/{endpoint_id}", { status: 200, body: EPS.endpoints[0] });
  api.on("POST /api-integrations/{integration_id}/endpoints/{endpoint_id}/test", { status: 200,
    body: { success: true, status: 200, duration_ms: 12, data: { status: "shipped" } } });
  await openTab(api);
  await userEvent.click(await screen.findByRole("button", { name: ONE.name }));
  await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
  expect(await screen.findByRole("status")).toHaveTextContent(/Reachable.*Connected \(GET \/health answered HTTP 200\)/);
  const order = EPS.endpoints.find((e) => e.path === "/orders/{order_id}");
  await userEvent.click(screen.getByRole("checkbox", { name: `Enable GET ${order.path}` }));
  await waitFor(() => expect(api.calls("PATCH /api-integrations/{integration_id}/endpoints/{endpoint_id}")[0].body)
    .toEqual({ enabled: !order.enabled }));
  const testButtons = screen.getAllByRole("button", { name: "Test endpoint" });
  await userEvent.click(testButtons[EPS.endpoints.filter((e) => e.executable).findIndex((e) => e.id === order.id)]);
  await userEvent.type(await screen.findByLabelText("order_id (path, required)"), "A1");
  await userEvent.click(screen.getByRole("button", { name: "Send test request" }));
  await waitFor(() => expect(api.calls("POST /api-integrations/{integration_id}/endpoints/{endpoint_id}/test")[0].body)
    .toEqual({ parameters: { order_id: "A1" } }));
  expect(await screen.findByText(/"shipped"/)).toBeInTheDocument();
  expect(screen.getByRole("table", { name: "API activity" })).toBeInTheDocument();
  // non-GET endpoints cannot be enabled
  expect(screen.getAllByText("read-only calls only").length).toBeGreaterThan(0);
});

test("the conversation switch patches the bot configuration; viewers cannot manage", async () => {
  const api = botApi();
  api.on("PATCH /{type_}/{object_id}/config", { status: 200, body: {} });
  await openTab(api);
  await userEvent.click(await screen.findByRole("checkbox", { name: "Use API data in conversations" }));
  await waitFor(() => expect(api.calls("PATCH /{type_}/{object_id}/config")[0].body)
    .toEqual({ changes: { "tools.api_calls_enabled": true } }));
});

test("viewers see integrations but no management controls", async () => {
  const api = botApi({});
  await openTab(api);
  expect(await screen.findByRole("button", { name: ONE.name })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Create API integration" })).toBeNull();
  expect(screen.queryByRole("checkbox", { name: "Use API data in conversations" })).toBeNull();
});
