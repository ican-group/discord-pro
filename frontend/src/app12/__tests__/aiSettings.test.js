/** Stage 0: AI provider, model and saved API key are chosen by name (no internal ids typed). */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ADD_NEW, credentialLabel, modelOptions } from "../components/AiSettings";
import { IDS, MockApi, SAMPLES, renderApp, withPermissions } from "../testing/harness";

const AI = SAMPLES.ai_settings_bot.body;
const ANTH = AI.credentials.find((c) => c.owner === "enterprise");
const SHARED = AI.credentials.find((c) => c.owner === "platform");

function botApi(ai = AI, perms = { edit_config: true }) {
  const api = new MockApi();
  api.on("GET /{type_}/{object_id}/config", withPermissions(perms));
  api.on("GET /{type_}/{object_id}/ai", { status: 200, body: ai });
  return api;
}

async function openBehaviour(api) {
  renderApp(`/app/bots/${IDS.bot}`, api);
  await userEvent.click(await screen.findByRole("tab", { name: "Behaviour" }));
  return screen.findByRole("form", { name: "AI settings" });
}

test("labels and model options come from the server's options", () => {
  expect(credentialLabel(ANTH)).toBe("Anthropic A — Ent A (Enterprise)");
  expect(credentialLabel(SHARED)).toBe("BitBotPro shared Claude — shared by BitBotPro");
  const anth = AI.providers.find((p) => p.id === "anthropic");
  const opts = modelOptions(anth);
  expect(opts[0][0]).toBe("");
  expect(opts[0][1]).toMatch(/Provider default \(Claude Sonnet 4\.6\)/);
  expect(opts.map((o) => o[0])).toContain("claude-haiku-4-5");
});

test("a bot editor picks a saved key by name, saves, and tests the connection", async () => {
  const api = botApi();
  api.on("PUT /{type_}/{object_id}/ai", { status: 200, body: AI });
  api.on("POST /{type_}/{object_id}/ai/test", { status: 200, body: SAMPLES.ai_test.body });
  const form = await openBehaviour(api);
  const key = within(form).getByLabelText("API key");
  const names = within(key).getAllByRole("option").map((o) => o.textContent);
  expect(names).toEqual(expect.arrayContaining([credentialLabel(ANTH), credentialLabel(SHARED), "+ Add a new API key…"]));
  expect(names.join(" ")).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}/);                  // no internal ids shown
  expect(screen.queryByLabelText("ai.credential_id")).toBeNull();                   // the raw field is gone
  await userEvent.selectOptions(key, SHARED.id);
  await userEvent.selectOptions(within(form).getByLabelText("Model"), "claude-sonnet-4-6");
  await userEvent.click(within(form).getByRole("button", { name: "Test connection" }));
  await waitFor(() => expect(api.calls("POST /{type_}/{object_id}/ai/test")[0].body)
    .toEqual({ provider: "anthropic", model: "claude-sonnet-4-6", credential_id: SHARED.id }));
  expect(await screen.findByText(/Connected: Claude Haiku 4\.5 answered\..*Reply: “OK”/)).toBeInTheDocument();
  await userEvent.click(within(form).getByRole("button", { name: "Save AI settings" }));
  await waitFor(() => expect(api.calls("PUT /{type_}/{object_id}/ai")[0].body)
    .toEqual({ provider: "anthropic", model: "claude-sonnet-4-6", credential_id: SHARED.id }));
  expect(await screen.findByText("AI settings saved.")).toBeInTheDocument();
});

test("a failed test is explained without provider details", async () => {
  const api = botApi();
  api.on("POST /{type_}/{object_id}/ai/test", { status: 200, body: { ok: false, category: "unauthorized",
    message: "The provider rejected this API key. Check the key, or replace it under Credentials." } });
  const form = await openBehaviour(api);
  await userEvent.click(within(form).getByRole("button", { name: "Test connection" }));
  expect(await screen.findByText(/The provider rejected this API key\./)).toBeInTheDocument();
});

test("an admin adds a new API key inline and it is selected", async () => {
  const api = botApi();
  const created = { ...ANTH, id: "11111111-2222-4333-8444-555555555555", name: "Support Claude", owner: "organisation", owner_name: "Org A" };
  api.on("POST /{owner_type}/{owner_id}/credentials", { status: 201, body: created });
  let reloads = 0;
  api.on("GET /{type_}/{object_id}/ai", () => { reloads += 1; return { status: 200, body: reloads > 1 ? { ...AI, credentials: [...AI.credentials, created] } : AI }; });
  const form = await openBehaviour(api);
  await userEvent.selectOptions(within(form).getByLabelText("API key"), ADD_NEW);
  const add = await screen.findByRole("form", { name: "Add AI credential" });
  await userEvent.selectOptions(within(add).getByLabelText("Save the key for"), `organisations:${AI.can_add_credential_to[1].id}`);
  await userEvent.type(within(add).getByLabelText("Credential name"), "Support Claude");
  await userEvent.type(within(add).getByLabelText("API key"), "sk-ant-test-key-not-real-0000000000");
  await userEvent.click(within(add).getByRole("button", { name: "Save key" }));
  await waitFor(() => expect(api.calls("POST /{owner_type}/{owner_id}/credentials")[0].body)
    .toEqual({ provider: "anthropic", name: "Support Claude", secret: "sk-ant-test-key-not-real-0000000000" }));
  expect(api.calls("POST /{owner_type}/{owner_id}/credentials")[0].params.owner_type).toBe("organisations");
  await waitFor(() => expect(within(screen.getByRole("form", { name: "AI settings" })).getByLabelText("API key")).toHaveValue(created.id));
  expect(screen.getByText(/API key saved/)).toBeInTheDocument();
});

test("viewers see the selection but cannot change it; a missing key is flagged", async () => {
  const api = botApi({ ...AI, can_edit: false, can_test: false, can_add_credential_to: [],
    current: { ...AI.current, credential: null, credential_id: null, blocked: "no AI credential is configured" } }, {});
  renderApp(`/app/bots/${IDS.bot}`, api);
  await userEvent.click(await screen.findByRole("tab", { name: "Behaviour" }));
  expect(await screen.findByText(/Not ready to answer: no AI credential is configured/)).toBeInTheDocument();
  expect(screen.queryByRole("form", { name: "AI settings" })).toBeNull();
});

test("a superadmin adds a platform AI key and grants it to one Enterprise", async () => {
  const api = new MockApi();
  api.on("GET /auth/session", { status: 200, body: SAMPLES.session_root.body });
  api.on("POST /platform/ai-credentials", { status: 201, body: SAMPLES.platform_ai_credentials.body.credentials[0] });
  renderApp("/app/platform", api);
  await userEvent.click(await screen.findByRole("tab", { name: "Platform credentials" }));
  const form = await screen.findByRole("form", { name: "Add platform AI key" });
  await userEvent.type(within(form).getByLabelText("Name"), "Shared Claude");
  await userEvent.type(within(form).getByLabelText("API key"), "sk-ant-platform-not-real-000000000000");
  const ent = SAMPLES.enterprises.body.enterprises[0];
  await userEvent.click(within(form).getByLabelText(ent.name));
  await userEvent.click(within(form).getByRole("button", { name: "Save platform key" }));
  await waitFor(() => expect(api.calls("POST /platform/ai-credentials")[0].body).toEqual({
    provider: "anthropic", name: "Shared Claude", secret: "sk-ant-platform-not-real-000000000000",
    all_enterprises: false, enterprise_ids: [ent.id] }));
});
