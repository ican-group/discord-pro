const { expect } = require("@playwright/test");

let cached;
async function ids(request) {
  if (!cached) cached = await (await request.get("/__e2e/ids")).json();
  return cached;
}

async function login(page, who, { expectHome = true } = {}) {
  const i = await ids(page.request);
  await page.goto("/app/login");
  await page.getByLabel("Email").fill(`${who}@e2e.test`);
  await page.getByLabel("Password").fill(i.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  if (expectHome) await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  return i;
}

/** fetch from the page (same cookie session as the UI); csrf adds the required header. */
async function apiFromPage(page, method, path, body, csrf = true) {
  return page.evaluate(async ([m, p, b, c]) => {
    const headers = { "Content-Type": "application/json" };
    if (c) headers["X-Requested-With"] = "BitBotPro";
    const r = await fetch("/api" + p, { method: m, headers, credentials: "include", body: b ? JSON.stringify(b) : undefined });
    return { status: r.status, text: await r.text() };
  }, [method, path, body, csrf]);
}

async function outbox(request) {
  return (await (await request.get("/__e2e/outbox")).json()).messages;
}

module.exports = { ids, login, apiFromPage, outbox };
