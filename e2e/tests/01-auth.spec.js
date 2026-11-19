const { test, expect } = require("@playwright/test");
const { apiFromPage, ids, login, outbox } = require("./helpers");

test.describe("C1 authentication in a real browser", () => {
  test("form validation, wrong password, keyboard submission", async ({ page }) => {
    await page.goto("/app/login");
    await page.getByRole("button", { name: "Sign in" }).click();
    expect(await page.getByLabel("Email").evaluate((el) => el.validity.valueMissing)).toBe(true);   // native validation
    await page.getByLabel("Email").focus();
    await page.keyboard.type("owner@e2e.test");
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Password")).toBeFocused();
    await page.keyboard.type("not-the-password");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("alert")).toHaveText("Invalid email or password.");
  });

  test("session cookie survives refresh; direct navigation; logout; return to the requested page", async ({ page, context }) => {
    const i = await login(page, "owner");
    await expect(page.getByRole("link", { name: "Acme Corp" })).toBeVisible();
    const cookies = await context.cookies();
    const s = cookies.find((c) => c.name === "bbp_session");
    expect(s.httpOnly).toBe(true);
    expect(s.sameSite).toBe("Strict");
    expect(s.path).toBe("/api");
    expect(await page.evaluate(() => document.cookie)).not.toContain("bbp_session");            // not readable by scripts
    expect(await page.evaluate(() => window.localStorage.length + window.sessionStorage.length)).toBe(0);
    await page.reload();
    await expect(page.getByRole("link", { name: "Acme Corp" })).toBeVisible();
    await page.goto(`/app/organisations/${i.org_support}`);
    await expect(page.getByRole("heading", { name: "Support" })).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/app\/login$/);
    await page.goto(`/app/bots/${i.bot}`);
    await expect(page).toHaveURL(/\/app\/login$/);
    await page.getByLabel("Email").fill("owner@e2e.test");
    await page.getByLabel("Password").fill(i.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Helpdesk" })).toBeVisible();                 // back where we were
  });

  test("session expiry returns to sign-in with a notice", async ({ page, context }) => {
    await login(page, "viewer");
    await context.clearCookies();
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await expect(page.getByText("Your session has ended. Sign in again to continue.")).toBeVisible();
  });

  test("a request without the CSRF header is refused and the UI says so", async ({ page }) => {
    const i = await login(page, "admin");
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      if (req.method() === "PATCH") {
        const headers = { ...req.headers() };
        delete headers["x-requested-with"];
        return route.continue({ headers });
      }
      return route.continue();
    });
    await page.goto(`/app/bots/${i.bot}`);
    await page.getByRole("tab", { name: "Logo & lifecycle" }).click();
    await page.getByRole("textbox", { name: "Name" }).fill("Helpdesk renamed");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("alert")).toHaveText("Missing request header.");
    await page.unroute("**/api/**");
    await page.reload();
    await expect(page.getByRole("heading", { name: "Helpdesk" })).toBeVisible();                 // nothing changed
  });

  test("a new person creates an account from an e-mailed invitation (B-ACCT)", async ({ page, request, browser }) => {
    const i = await login(page, "owner");
    const email = "joiner.pw@e2e.test";
    const r = await apiFromPage(page, "POST", `/enterprises/${i.ent_a}/invitations`, { email, role: "viewer" });
    expect(r.status).toBe(201);
    const mail = (await outbox(request)).filter((m) => m.kind === "invitation" && m.to === email).pop();
    const link = mail.text.match(/https?:\/\/\S+#token=\S+/)[0];
    const url = new URL(link);
    // the invitee is a different, signed-out browser
    const ctx = await browser.newContext();
    const guest = await ctx.newPage();
    await guest.goto(url.pathname + url.hash);
    await expect(guest).toHaveURL(/\/app\/invitations$/);                                         // token removed from the address bar
    await expect(guest.getByRole("heading", { name: "You have been invited" })).toBeVisible();
    await guest.getByLabel("Your name").fill("Joiner PW");
    await guest.getByLabel("Password", { exact: true }).fill("Copper-Meadow-Lantern-2026");
    await guest.getByLabel("Repeat password").fill("Copper-Meadow-Lantern-2027");
    await guest.getByRole("button", { name: "Create account and join" }).click();
    await expect(guest.getByRole("alert")).toHaveText("The passwords do not match.");
    await guest.getByLabel("Repeat password").fill("Copper-Meadow-Lantern-2026");
    await guest.getByRole("button", { name: "Create account and join" }).click();
    await expect(guest.getByRole("navigation", { name: "Main" })).toBeVisible();
    await expect(guest.getByRole("link", { name: "Acme Corp" })).toBeVisible();
    await expect(guest.getByRole("link", { name: "Globex" })).toHaveCount(0);
    await ctx.close();
  });

  test("password reset through the e-mailed link", async ({ page, request }) => {
    await page.goto("/app/forgot-password");
    await page.getByLabel("Email").fill("newcomer@e2e.test");
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByText(/a reset link has been sent/)).toBeVisible();
    const mail = (await outbox(request)).filter((m) => m.kind === "reset" && m.to === "newcomer@e2e.test").pop();
    await page.goto(mail.text);
    await expect(page).toHaveURL(/\/app\/reset-password$/);                                      // token removed from the address bar
    await page.getByLabel("New password", { exact: true }).fill("Violet-Lantern-Quarry-2026");
    await page.getByLabel("Repeat new password").fill("Violet-Lantern-Quarry-2026");
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByText(/Password changed/)).toBeVisible();
    await page.goto("/app/login");
    await page.getByLabel("Email").fill("newcomer@e2e.test");
    await page.getByLabel("Password").fill("Violet-Lantern-Quarry-2026");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    // change it back from the Account page (password change, C1)
    await page.getByRole("link", { name: "Account" }).click();
    await page.getByLabel("Current password").fill("Violet-Lantern-Quarry-2026");
    await page.getByLabel("New password").fill((await ids(request)).password);
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByText("Password changed.")).toBeVisible();
  });

  test("loading and error states are announced", async ({ page }) => {
    await login(page, "owner");
    await page.route("**/api/enterprises", async (route) => { await new Promise((r) => setTimeout(r, 1500)); return route.continue(); });
    await page.goto("/app");
    await expect(page.getByRole("status").filter({ hasText: "Loading…" })).toBeVisible();
    await page.unroute("**/api/enterprises");
    await page.route("**/api/enterprises", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));
    await page.reload();
    await expect(page.getByRole("alert")).toHaveText("The server could not complete the request.");
  });
});
