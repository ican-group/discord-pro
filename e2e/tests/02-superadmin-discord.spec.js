const { test, expect } = require("@playwright/test");
const { apiFromPage, login } = require("./helpers");

test.describe("Platform Superadmin", () => {
  test("configures the Enterprise Discord application (secrets write-only)", async ({ page }) => {
    const i = await login(page, "root");
    await expect(page.getByRole("link", { name: "Platform" })).toBeVisible();
    await page.goto(`/app/platform/enterprises/${i.ent_a}/discord`);
    await expect(page.getByText(/Not configured\. Organisations in this Enterprise cannot connect/)).toBeVisible();
    await page.getByLabel("Application id").fill(i.discord.app_id);
    await page.getByLabel("Bot token").fill(i.discord.bot_token);
    await page.getByLabel("Client secret").fill(i.discord.client_secret);
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Discord application saved.")).toBeVisible();
    await expect(page.getByText(i.discord.app_id).first()).toBeVisible();
    const html = await page.content();
    expect(html).not.toContain(i.discord.bot_token);
    expect(html).not.toContain(i.discord.client_secret);
    await page.getByRole("tab", { name: "Gateway & availability" }).click();
    await page.getByRole("button", { name: "Restart gateway" }).click();
    await expect(page.getByText("Gateway restart requested.")).toBeVisible();
  });

  test("finds users with the bounded search", async ({ page }) => {
    await login(page, "root");
    await page.goto("/app/platform");
    await page.getByRole("tab", { name: "Users" }).click();
    await page.getByLabel("Search users by e-mail").fill("own");
    const table = page.getByRole("table", { name: "Users" });
    await expect(table.getByText("owner@e2e.test")).toBeVisible();
    await expect(table.getByText("ownerb@e2e.test")).toBeVisible();
    await expect(table.getByText("viewer@e2e.test")).toHaveCount(0);
  });
});

test.describe("Organisation Admin and the Enterprise Discord application (D-Q14)", () => {
  test("creates, installs, binds channels and disconnects their own instance — no application controls", async ({ page, context }) => {
    const i = await login(page, "orgadmin");
    // navigation: the Enterprise is shown as a limited view with their Organisation only
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await expect(page.getByRole("heading", { name: "Acme Corp" })).toBeVisible();
    await page.getByRole("link", { name: "Support" }).click();
    await page.getByRole("link", { name: "Helpdesk" }).click();
    await page.getByRole("textbox", { name: "Name" }).fill("Support Discord");
    await page.getByLabel("Channel").selectOption("discord");
    await page.getByRole("button", { name: "Create instance" }).click();
    await page.getByRole("link", { name: "Support Discord" }).click();
    await expect(page.getByRole("heading", { name: "Discord connection" })).toBeVisible();
    const body = await page.locator("main").innerText();
    expect(body).not.toMatch(/bot token|client secret|Start gateway|Restart gateway|intents|avatar/i);
    // installation opens Discord in a new tab; the isolated network has no internet, so we take the URL
    await page.evaluate(() => { window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; }; });
    await page.getByRole("button", { name: "Add to Discord server" }).click();
    await expect.poll(() => page.evaluate(() => window.__opened.length)).toBe(1);
    const url = new URL(await page.evaluate(() => window.__opened[0]));
    expect(url.hostname).toBe("discord.com");
    expect(url.searchParams.get("client_id")).toBe(i.discord.app_id);                       // the Enterprise application
    // Discord redirects back to the API callback; the API redirects to the result page
    await page.goto(`/api/hooks/discord/oauth/callback?state=${url.searchParams.get("state")}&code=${i.discord.oauth_code}&guild_id=${i.discord.guild}`);
    await expect(page).toHaveURL(/\/oauth\/discord\/result\?status=connected/);
    await expect(page.getByRole("heading", { name: "Discord: Connected" })).toBeVisible();
    await page.getByRole("link", { name: "Continue" }).click();
    await expect(page.getByText("connected", { exact: true })).toBeVisible();
    await page.getByLabel("Listen mode").selectOption("specific_channels");
    await page.getByLabel("#help").check();
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByText("Discord settings saved.")).toBeVisible();
    // the state is single use: replaying the callback lands on the expiry page
    await page.goto(`/api/hooks/discord/oauth/callback?state=${url.searchParams.get("state")}&code=${i.discord.oauth_code}&guild_id=${i.discord.guild}`);
    await expect(page.getByRole("heading", { name: "Discord: Link expired" })).toBeVisible();
    await page.goBack();                                                                          // back to the instance page
    await expect(page.getByRole("heading", { name: "Discord connection" })).toBeVisible();
    await page.getByRole("button", { name: "Disconnect" }).click();
    await page.getByRole("button", { name: "Disconnect" }).click();                              // confirm
    await expect(page.getByText("Disconnected.")).toBeVisible();
  });

  test("direct API calls to the application controls are refused (non-disclosing 404)", async ({ page }) => {
    const i = await login(page, "orgadmin");
    const base = `/platform/enterprises/${i.ent_a}/discord-application`;
    for (const [m, p, b] of [["GET", base], ["PUT", base, { application_id: i.discord.app_id }],
      ["POST", `${base}/rotate-token`, { value: i.discord.bot_token }], ["POST", `${base}/rotate-secret`, { value: "x".repeat(32) }],
      ["POST", `${base}/avatar`, { image: "data:image/png;base64,AA==" }], ["POST", `${base}/gateway/restart`],
      ["POST", `${base}/disable`]]) {
      const r = await apiFromPage(page, m, p, b);
      expect([m, p, r.status]).toEqual([m, p, 404]);
      expect(r.text).not.toContain(i.discord.bot_token);
    }
    const avail = await apiFromPage(page, "GET", `/enterprises/${i.ent_a}/discord`);
    expect(JSON.parse(avail.text)).toEqual({ available: true, configured: true });
  });
});
