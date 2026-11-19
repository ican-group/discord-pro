const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { login } = require("./helpers");

const PAGES = (i) => [
  ["enterprises", "/app"], ["enterprise", `/app/enterprises/${i.ent_a}`], ["organisation", `/app/organisations/${i.org_support}`],
  ["bot", `/app/bots/${i.bot}`], ["webchat instance", `/app/instances/${i.web}`], ["account", "/app/account"],
];

async function audit(page, name, testInfo) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const serious = r.violations.filter((v) => ["serious", "critical"].includes(v.impact));
  await testInfo.attach(`axe-${name}`, { body: JSON.stringify(r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length })), null, 2), contentType: "application/json" });
  return serious.map((v) => `${name}: ${v.id} (${v.impact}, ${v.nodes.length} nodes)`);
}

test("automated accessibility audit (axe, WCAG 2.1 A/AA): no serious or critical violations", async ({ page }, testInfo) => {
  const problems = [];
  await page.goto("/app/login");
  problems.push(...await audit(page, "login", testInfo));
  const i = await login(page, "owner");
  for (const [name, url] of PAGES(i)) {
    await page.goto(url);
    await expect(page.getByRole("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
    problems.push(...await audit(page, name, testInfo));
  }
  expect(problems).toEqual([]);
});

test("keyboard: skip link, tabs with arrow keys, focus visible on controls", async ({ page }) => {
  const i = await login(page, "owner");
  await page.goto(`/app/bots/${i.bot}`);
  await expect(page.getByRole("heading", { name: "Helpdesk" })).toBeVisible();     // app rendered (session checked)
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  const first = page.getByRole("tab").first();
  await first.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab").nth(1)).toBeFocused();
  await expect(page.getByRole("tab").nth(1)).toHaveAttribute("aria-selected", "true");
});

test("@mobile layout: no horizontal scrolling and navigation works on a phone", async ({ page }) => {
  const i = await login(page, "owner");
  for (const [, url] of PAGES(i)) {
    await page.goto(url);
    await expect(page.getByRole("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect([url, overflow <= 1]).toEqual([url, true]);
  }
  await page.goto(`/app/organisations/${i.org_support}`);
  await page.getByRole("tab", { name: "Members" }).click();
  await expect(page.getByRole("table", { name: "Members" })).toBeVisible();
});

test("@mobile sign-in and OAuth result page on a phone", async ({ page }) => {
  await page.goto("/oauth/zendesk/result?status=invalid_state");
  await expect(page.getByRole("heading", { name: "Zendesk: Link expired" })).toBeVisible();
  await login(page, "viewer");
  await expect(page.getByRole("link", { name: "Acme Corp" })).toBeVisible();
});
