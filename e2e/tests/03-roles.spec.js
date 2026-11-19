const { test, expect } = require("@playwright/test");
const { apiFromPage, login } = require("./helpers");

async function openOrg(page, i, tab) {
  await page.goto(`/app/organisations/${i.org_support}`);
  await expect(page.getByRole("heading", { name: "Support" })).toBeVisible();
  if (tab) await page.getByRole("tab", { name: tab }).click();
}

test.describe("Part E role journeys (UI + backend enforcement)", () => {
  test("Enterprise Viewer: read-only everywhere", async ({ page }) => {
    const i = await login(page, "viewer");
    await openOrg(page, i);
    await expect(page.getByLabel("New bot name")).toHaveCount(0);
    await page.getByRole("tab", { name: "Defaults" }).click();
    await expect(page.getByRole("button", { name: /^Override / })).toHaveCount(0);
    await page.getByRole("tab", { name: "Lifecycle" }).click();
    await expect(page.getByRole("button", { name: /Suspend|Archive|Delete permanently/ })).toHaveCount(0);
    expect((await apiFromPage(page, "PATCH", `/organisations/${i.org_support}/config`, { changes: { "behaviour.language": "es" } })).status).toBe(403);
  });

  test("Enterprise Editor: can override configuration, cannot manage members", async ({ page }) => {
    const i = await login(page, "editor");
    await page.goto(`/app/bots/${i.bot}`);
    await page.getByRole("tab", { name: "Behaviour" }).click();
    await expect(page.getByRole("button", { name: "Override behaviour.persona" })).toBeVisible();
    await page.getByRole("tab", { name: "Members" }).click();
    await expect(page.getByRole("button", { name: "Send invitation" })).toHaveCount(0);
    expect((await apiFromPage(page, "POST", `/enterprises/${i.ent_a}/invitations`, { email: "x@e2e.test", role: "viewer" })).status).toBe(403);
  });

  test("Enterprise Admin: creates bots and invites by e-mail", async ({ page }) => {
    const i = await login(page, "admin");
    await openOrg(page, i);
    await page.getByLabel("New bot name").fill("FAQ bot");
    await page.getByRole("button", { name: "Create bot" }).click();
    await expect(page.getByRole("link", { name: "FAQ bot" })).toBeVisible();
    await page.getByRole("tab", { name: "Members" }).click();
    await page.getByLabel("Email").fill("someone.new@e2e.test");
    await page.getByRole("button", { name: "Send invitation" }).click();
    await expect(page.getByText("Invitation e-mail sent.")).toBeVisible();
    await expect(page.getByTestId("one-time-secret")).toHaveCount(0);                            // the link went by e-mail only
  });

  test("Enterprise Owner: lifecycle and permanent deletion controls", async ({ page }) => {
    const i = await login(page, "owner");
    await openOrg(page, i, "Lifecycle");
    await expect(page.getByRole("button", { name: "Suspend" })).toBeVisible();
    await page.getByRole("button", { name: "Delete permanently…" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("What will be deleted")).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Delete permanently" })).toBeDisabled();
    await page.keyboard.press("Escape");                                                         // focus-trapped dialog closes
    await expect(dialog).toHaveCount(0);
  });

  test("Organisation Admin: only their Organisation", async ({ page }) => {
    const i = await login(page, "orgadmin");
    await page.goto(`/app/organisations/${i.org_sales}`);
    await expect(page.getByRole("alert")).toHaveText("Not found.");
    expect((await apiFromPage(page, "GET", `/enterprises/${i.ent_a}/credentials`)).status).toBe(404);
  });

  test("Restricted Organisation member: sees Sales only, nothing else", async ({ page }) => {
    const i = await login(page, "restricted");
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await expect(page.getByRole("link", { name: "Sales" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Support" })).toHaveCount(0);
    await page.goto(`/app/bots/${i.bot}`);
    await expect(page.getByRole("alert")).toHaveText("Not found.");
  });

  test("Another Enterprise's owner: no access, no disclosure, no authority from headers", async ({ page }) => {
    const i = await login(page, "ownerb");
    await expect(page.getByRole("link", { name: "Acme Corp" })).toHaveCount(0);
    await page.goto(`/app/enterprises/${i.ent_a}`);
    await expect(page.getByRole("alert")).toHaveText("Not found.");
    const r = await page.evaluate(async ([bot, ent]) => {
      const res = await fetch(`/api/bots/${bot}`, { credentials: "include", headers: { "X-Enterprise-ID": ent, "X-Instance-ID": ent } });
      return res.status;
    }, [i.bot, i.ent_a]);
    expect(r).toBe(404);
  });
});
