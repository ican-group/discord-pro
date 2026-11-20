/** Phase 7: the Discord setup page shows the exact Developer Portal values (with Copy) before configuration. */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IDS, MockApi, SAMPLES, renderApp } from "../testing/harness";

const UNCONFIGURED = { configured: false, redirect_uri: "https://api.bitbotpro.io/api/hooks/discord/oauth/callback",
  oauth_scopes: "bot identify guilds", permissions: 67193856, intents: 37379 };

test("the redirect URI, scopes, permissions and intents are shown with Copy buttons before set-up", async () => {
  const api = new MockApi();
  api.on("GET /auth/session", { status: 200, body: SAMPLES.session_root.body });
  api.on("GET /platform/enterprises/{enterprise_id}/discord-application", { status: 200, body: UNCONFIGURED });
  const writeText = jest.fn(() => Promise.resolve());
  Object.assign(navigator, { clipboard: { writeText } });
  renderApp(`/app/platform/enterprises/${IDS.enterprise}/discord`, api);
  expect(await screen.findByText(UNCONFIGURED.redirect_uri)).toBeInTheDocument();
  expect(screen.getByText("bot identify guilds")).toBeInTheDocument();
  expect(screen.getByText("67193856")).toBeInTheDocument();
  expect(screen.getByText("37379")).toBeInTheDocument();
  expect(screen.queryByText(/redirect URI above/)).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Copy OAuth2 redirect URI" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(UNCONFIGURED.redirect_uri));
});
