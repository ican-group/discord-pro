const { test, expect } = require("@playwright/test");
const { apiFromPage, login } = require("./helpers");

test("F5 webchat preview through the API-hosted widget is labelled preview and still counted", async ({ page, context }) => {
  const i = await login(page, "admin");
  expect((await apiFromPage(page, "PATCH", `/bots/${i.bot}/config`, { changes: { "ai.credential_id": i.ai_cred } })).status).toBe(200);
  await page.goto(`/app/instances/${i.web}`);
  await expect(page.getByLabel("Paste this into your website")).toHaveValue(/\/api\/embed\.js/);
  await page.evaluate(() => { window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; }; });
  await page.getByRole("button", { name: "Open preview" }).click();
  await expect.poll(() => page.evaluate(() => window.__opened.length)).toBe(1);
  const previewUrl = await page.evaluate(() => window.__opened[0]);
  expect(previewUrl).toMatch(/\/api\/public\/widget\/[^?]+\?mode=fullpage#preview=/);
  const widget = await context.newPage();
  await widget.goto(previewUrl);
  await expect(widget).toHaveURL(/mode=fullpage$/);                                       // fragment cleared by the widget
  await widget.locator("#bbp-input").fill("Hello from the preview");
  await widget.locator("#bbp-send").click();
  await expect(widget.locator("#bbp-messages")).toContainText("E2E answer to");
  await widget.close();
  await page.getByRole("tab", { name: "Usage" }).click();
  const row = (name) => page.locator("dt", { hasText: name }).locator("xpath=following-sibling::dd[1]");
  await expect(row("Of which preview")).toHaveText("1");
  await expect(row("Billed (settled)")).toHaveText(/^[1-9]/);
});

test("Zendesk Bot Integration: create, usage notice, safe deletion states", async ({ page }) => {
  const i = await login(page, "orgadmin");
  await page.goto(`/app/bots/${i.bot}`);
  await page.getByRole("tab", { name: "Integrations" }).click();
  await page.getByLabel("New Zendesk integration name").fill("Support desk");
  await page.getByRole("button", { name: "Add Zendesk integration" }).click();
  await page.getByRole("link", { name: "Support desk" }).click();
  await expect(page.getByText("Zendesk integration (Bot Integration)")).toBeVisible();
  await page.getByRole("tab", { name: "Replies" }).click();
  await expect(page.getByLabel("Reply mode")).toHaveValue("agent_assist");
  await expect(page.getByText(/create ticket/i)).toHaveCount(0);
  await page.getByRole("tab", { name: "Usage" }).click();
  await expect(page.getByText(/analytics dashboards are not available yet/)).toBeVisible();
  await page.getByRole("tab", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete integration…" }).click();
  await page.getByRole("button", { name: "Delete integration", exact: true }).click();
  // nothing was provisioned in Zendesk, so cleanup completes at once; completion is shown only on deleted:true
  await expect(page.getByRole("heading", { name: "Deleted" })).toBeVisible();
});
