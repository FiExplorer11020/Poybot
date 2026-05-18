// @ts-check
// Playwright E2E config for the Poybot dashboard (PLAN-UIA-001).
//
// Tests are pure-DOM — every /api/* request is intercepted in
// tests/ui/_fixtures.js so the suite runs without a live backend.
// For backend integration, run `bash scripts/smoke.sh` (which boots
// uvicorn + postgres + redis) before `npx playwright test`.

const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/ui',
  testMatch: '**/*.spec.js',
  timeout: 30_000,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:8000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 5_000,
    navigationTimeout: 10_000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
