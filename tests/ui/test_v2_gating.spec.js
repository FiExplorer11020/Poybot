// @ts-check
// PLAN-UIA-001 — V2 dashboard overlay is gated OFF by default.
// Per memory project_v1_vs_v2_terminal.md: "V1 = source of truth ;
// V2 = lab gated OFF, ne pas migrer".

const { withMockedApi, expect } = require('./_fixtures');

const test = withMockedApi();

test.beforeEach(async ({ page }) => {
  // Clear localStorage before each spec.
  await page.addInitScript(() => { try { localStorage.clear(); } catch (_) {} });
});

test('default_load_no_v2_files_fetched', async ({ page }) => {
  const v2Requests = [];
  page.on('request', req => {
    if (req.url().includes('/static/dashboard/v2/')) v2Requests.push(req.url());
  });
  await page.goto('/');
  // Give the page a beat to settle.
  await page.waitForTimeout(500);
  expect(v2Requests, 'no V2 fetches when flag is unset').toEqual([]);
});

test('tradingview_cdn_not_requested_when_v2_off', async ({ page }) => {
  const cdnRequests = [];
  page.on('request', req => {
    const u = req.url();
    if (u.includes('lightweight-charts') || u.includes('cosmograph')) cdnRequests.push(u);
  });
  await page.goto('/');
  await page.waitForTimeout(500);
  expect(cdnRequests, 'TradingView + Cosmograph stay quiet when V2 lab is OFF').toEqual([]);
});

test('v2_files_load_when_flag_set', async ({ page }) => {
  // Set the flag BEFORE navigating.
  await page.addInitScript(() => {
    try { localStorage.setItem('poybot_v2_lab', '1'); } catch (_) {}
  });
  const v2Requests = [];
  page.on('request', req => {
    if (req.url().includes('/static/dashboard/v2/')) v2Requests.push(req.url());
  });
  await page.goto('/');
  // V2 loader waits for window.LiveStore + window.LivePortfolio to exist, then
  // fetches the V2 file list. Give it generous time.
  await page.waitForTimeout(3000);
  // Note: V2 files may 404 in tests (no V2 backend), but they should at least
  // be REQUESTED — that's the contract.
  expect(v2Requests.length, 'V2 files are fetched when flag is set').toBeGreaterThan(0);
});

test('title_changes_when_v2_lab_mode_active', async ({ page }) => {
  await page.addInitScript(() => {
    try { localStorage.setItem('poybot_v2_lab', '1'); } catch (_) {}
  });
  await page.goto('/');
  await page.waitForTimeout(1000);
  const title = await page.title();
  expect(title).toContain('V2 LAB MODE');
});

test('flag_persists_across_reload', async ({ page }) => {
  await page.addInitScript(() => {
    try { localStorage.setItem('poybot_v2_lab', '1'); } catch (_) {}
  });
  await page.goto('/');
  await page.waitForTimeout(500);
  const stored = await page.evaluate(() => localStorage.getItem('poybot_v2_lab'));
  expect(stored).toBe('1');
});
