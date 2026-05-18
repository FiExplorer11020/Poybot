# UI E2E Tests — Playwright

PLAN-UIA-001 frontend test suite. Pure-DOM tests: every `/api/*` request is
intercepted in `_fixtures.js` so the suite runs **without a live backend**.

## Quick run

```bash
cd polymarket-bot
npm install               # first time only — installs Playwright + esbuild
npx playwright install chromium  # one-time browser install
npm run build             # rebuilds dashboard.bundle.js (esbuild)

# Either: spin up the API yourself
python -m uvicorn src.api.main:app --port 8000 &

# Or: use Playwright's built-in webServer (configure in playwright.config.js).
# For the mocked tests, the API doesn't need to actually be reachable.

npx playwright test tests/ui              # all specs
npx playwright test tests/ui/test_v2_gating.spec.js   # single spec
npx playwright test --headed              # see the browser
npx playwright test --debug               # step through with inspector
```

## Spec files

| File | What it covers |
|------|---------------|
| `test_v2_gating.spec.js` | V2 dashboard overlay is OFF by default; flag toggles fetching |
| `test_sidebar_recon_chip.spec.js` | RECON chip + ModeChip render with correct verdict + suffix |
| `test_honest_controls.spec.js` | RiskConfig shows ENABLE/DISABLE + EMERGENCY HALT (no legacy START/STOP/PAUSE) |
| `test_dead_code_removed.spec.js` | MarketScanner gone, PoybotNav.go gone, Tweaks panel gone, g l shortcut works |
| `test_e2e_smoke.spec.js` | All 9 tabs render, bundle loads, no 4xx/5xx on initial load |

## How the mocks work

`tests/ui/_fixtures.js` exports `withMockedApi(handlers)` which extends the
base Playwright `test` with a `page` fixture that intercepts every `/api/*`
URL and returns canned JSON. To customize, pass a `handlers` object:

```js
const test = withMockedApi({
  liveSummary: () => defaultLiveSummary({ reconVerdict: 'critical' }),
});
```

Outbound POSTs are tracked on `page._poyPosts` so specs can assert on them:

```js
const halts = page._poyPosts.filter(p => p.url.includes('/api/control/halt'));
expect(halts.length).toBe(1);
```

## Why mocked-only?

Per ADR-PMK-014.8, Playwright over Vitest because *real-browser rendering
catches the V2-rebind / CDN-load race bug class*. We mock the backend
because the backend is exercised by pytest (35+ cases in
`tests/test_api/test_reconciliation_endpoints.py` and `test_pillars_endpoint.py`).

For full-stack confidence: `bash scripts/smoke.sh` boots uvicorn + postgres
+ redis and runs both pytest and Playwright against the real stack.
