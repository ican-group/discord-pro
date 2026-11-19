// Phase 6A real-browser tests. Runs only against the local e2e environment
// (E2E_BASE_URL on an isolated Docker network); never against production.
const { defineConfig, devices } = require("@playwright/test");

const baseURL = process.env.E2E_BASE_URL;
if (!baseURL || /bitbotpro\.io/.test(baseURL)) throw new Error("E2E_BASE_URL must point at the local e2e server");

module.exports = defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,                       // one shared server state; specs run in file order
  retries: 0,
  timeout: 60000,
  expect: { timeout: 10000 },
  reporter: [["list"], ["json", { outputFile: "test-results/results.json" }]],
  use: { baseURL, trace: "off", screenshot: "only-on-failure" },
  projects: [
    { name: "chromium-desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } }, grepInvert: /@mobile/ },
    { name: "chromium-mobile", use: { ...devices["Pixel 5"] }, grep: /@mobile/ },
  ],
});
