// @ts-check
// PLAN-UIA-001 — RiskConfig honest controls.
//
// Old UI: 3 buttons (START/STOP/PAUSE) all hit killswitch — dishonest.
// New UI: ENABLE/DISABLE TRADING + distinct EMERGENCY HALT → /api/control/halt.
//
// We assert against the precompiled bundle (deterministic) for the
// presence of the new labels + absence of the legacy aliases.

const { withMockedApi, expect } = require('./_fixtures');
const test = withMockedApi();

test('bundle_ships_enable_disable_button_labels', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  expect(code, 'ENABLE TRADING label').toContain('ENABLE TRADING');
  expect(code, 'DISABLE TRADING label').toContain('DISABLE TRADING');
});

test('bundle_ships_emergency_halt_label', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  expect(code, 'EMERGENCY HALT label').toContain('EMERGENCY HALT');
  expect(code, 'CONFIRM HALT prompt').toContain('CONFIRM HALT');
});

test('bundle_drops_legacy_start_stop_pause_button_labels', async ({ request }) => {
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  // The old button labels: '▶ START', '■ STOP', '⏸ PAUSE'.
  // (Note: '■ STOP' substring would also match '■ STOP TRADING'  if we
  // had that label — we don't, so the test is sharp.)
  expect(code, 'no legacy START button label').not.toContain('▶ START');
  expect(code, 'no legacy PAUSE button label').not.toContain('⏸ PAUSE');
});

test('halt_endpoint_distinct_from_killswitch', async ({ request }) => {
  // The new halt flow calls /api/control/halt, NOT /api/control/killswitch.
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  expect(code, 'bundle references /api/control/halt').toContain('/api/control/halt');
});

test('bundle_drops_legacy_botControl_aliases', async ({ request }) => {
  // api-client.js previously mapped start/stop/pause/resume verbs onto the
  // killswitch endpoint. The new map handles enable/disable + halt.
  // Minifier outputs `enable:!0` / `disable:!1` for boolean values.
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  expect(code, 'enable verb mapped to true').toMatch(/enable:\s*(true|!0)/);
  expect(code, 'disable verb mapped to false').toMatch(/disable:\s*(false|!1)/);
});

test('halt_button_is_separately_confirmed', async ({ request }) => {
  // The halt confirmation dialog is gated by visible text — assert on the
  // strings (which survive minification) instead of the variable name.
  const resp = await request.get('/static/dashboard/dist/dashboard.bundle.js');
  const code = await resp.text();
  expect(code, 'CONFIRM HALT? dialog text').toMatch(/CONFIRM HALT/);
  expect(code, 'CANCEL button text').toContain('CANCEL');
});
