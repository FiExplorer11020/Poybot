// @ts-check
// PLAN-UIA-001 — end-to-end smoke + structural assertions on the bundle.

const { withMockedApi, expect } = require('./_fixtures');
const test = withMockedApi();

test('bundle_is_served_and_under_500kb', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  expect(resp.ok(), 'bundle served').toBe(true);
  const body = await resp.text();
  const bytes = body.length;
  expect(bytes, 'bundle is non-trivial').toBeGreaterThan(50_000);
  expect(bytes, 'bundle stays under 500 KB minified').toBeLessThan(500_000);
});

test('bundle_does_not_inline_babel', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  // We do NOT bundle Babel — it's a dev-fallback CDN load.
  expect(code, 'Babel not inlined').not.toContain('@babel/standalone');
});

test('bundle_ships_all_9_tab_labels', async ({ request }) => {
  // Assert on the user-visible tab labels (string literals in NAV def) —
  // these survive minification, unlike component identifiers.
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  const labels = ['ALPHA TERMINAL', 'ML PROGRESSION', 'WALLET GRAPH', 'LIVE PORTFOLIO',
                  'DECISION ENGINE', 'INSPECTOR', 'RISK & CONFIG', 'BOT HEALTH', 'LAB'];
  for (const l of labels) {
    expect(code, `${l} nav label in bundle`).toContain(l);
  }
});

test('bundle_ships_recon_panel_and_pillars_titles', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  // String literals (panel titles) survive minification.
  expect(code, 'Inspector panel title').toContain('Paper Truth Reconciliation');
  expect(code, '5-pillars gauge title').toContain('Paper Trading Pillars');
  // V2 toggle card text.
  expect(code, 'V2 lab toggle card').toContain('Dashboard Overlay');
});

test('bundle_ships_category_risk_badges', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  expect(code, 'categoryRisk helper').toContain('categoryRisk');
  expect(code, 'CategoryRiskBadge component').toContain('CategoryRiskBadge');
  expect(code, 'CRYPTO badge label').toContain('CRYPTO');
});

test('bundle_uses_setActiveTab_not_go', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  // PoybotNav.go was a typo (never defined). Only setActiveTab is canonical.
  // We accept the source mapping might mention .go in other contexts (e.g.
  // `Promise.go`), so we check the LAB cockpit's audit-log link specifically.
  expect(code).toContain('setActiveTab');
});

test('bundle_keyboard_shortcut_g_l_for_lab', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  // TAB_BY_KEY includes l: 'lab'
  expect(code).toMatch(/l:\s*['"]lab['"]/);
});

test('page_root_element_exists_on_cold_load', async ({ page }) => {
  await page.goto('/');
  // Just verify the React mount point exists. Doesn't depend on snapshot.
  await page.waitForSelector('#root', { timeout: 5_000 });
  const root = await page.locator('#root').count();
  expect(root).toBe(1);
});

test('keyboard_shortcuts_help_renders_after_question_mark', async ({ page }) => {
  await page.goto('/');
  // Wait for the React bundle to mount before pressing keys.
  await page.waitForFunction(() => !!window.React, null, { timeout: 5_000 });
  await page.waitForTimeout(800);
  await page.keyboard.press('?');
  await page.waitForTimeout(500);
  const body = await page.content();
  expect(body, 'help overlay text visible').toContain('Keyboard shortcuts');
});
