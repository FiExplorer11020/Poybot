// @ts-check
// Shared Playwright fixtures + mocked API responses.
//
// Each spec file imports `withMockedApi()` and gets a `page` already wired
// with /api/* interceptors. Lets us run the suite without spinning up
// uvicorn + postgres + redis on every test run.

const { test: base, expect } = require('@playwright/test');

function defaultLiveSummary({ reconVerdict = 'ok', execMode = 'paper', winRate = 0.45 } = {}) {
  return {
    data: {
      bot: {
        status: 'running',
        execution_enabled: false,
        execution_mode: execMode,
        uptime_seconds: 3600,
        latency_ms: 25,
        cycle_latency_ms: 12,
        control_available: true,
        config_mutable: true,
        started_at: new Date(Date.now() - 3600_000).toISOString(),
        accumulated_run_seconds: 3600,
      },
      stats: {
        total_pnl: 4.17,
        win_rate: winRate,
        portfolio_total: 10004,
        open_positions: 2,
        capital_in_trade: 200,
        active_markets: 48,
        pnl_percent: 0.000417,
        detected_arbs_today: 5,
      },
      ingestion: {
        total_markets: 50,
        live_markets: 48,
        stale_market_count: 0,
        updates_last_minute: 120,
        avg_freshness_ms: 2300,
        ws_last_message_age_s: 2.3,
        sources: [],
        markets: [],
      },
      reconciliation: {
        verdict: reconVerdict,
        pnl_delta_abs: reconVerdict === 'critical' ? 500 : reconVerdict === 'warn' ? 100 : 4.17,
        age_s: 120,
        trades_evaluated: 142,
        phantom_count: reconVerdict === 'critical' ? 2 : 0,
      },
      health_pillars: defaultPillars(),
      recent_trades: [],
      positions: { items: [], open_count: 0, capital_in_trade: 0, exposure_pct: 0 },
      decision_engine: { summary: {}, ranked: [] },
      wallet_graph: { nodes: [], edges: [], stats: {} },
      alpha_extras: { totals: {}, timeline: [], follow_ready: [] },
      analytics: { summary: {} },
      data_quality_full: { markets: {}, leaders: {}, profiles: {}, feed: { ws_healthy: true } },
      adaptive_thresholds: { maturity: 0.3, values: {}, ranges: {} },
      logs: [],
      equity_curve: { series: [], by_leader: [], by_strategy: [] },
      rejections: { total: 0, breakdown: [] },
      risk_config: { config_mutable: true },
      clock: { updated_at: new Date().toISOString() },
      meta: { paper_only: true, leaders_active: 100, readiness_blockers: [] },
    },
  };
}

function defaultInspectorSnapshot() {
  return {
    raw_trades: [],
    decisions: [],
    source_mix: [],
    counters: {
      trades_1h: 12, leader_trades_1h: 3, decisions_1h: 5,
      actionable_1h: 2, closes_1h: 1,
    },
    pipeline: {
      redis_reachable: true,
      ws_last_message_age_s: 2.3,
      ws_msgs_per_min: 120,
      trades_pubsub_subscribers: 3,
    },
  };
}

function defaultReconciliation({ verdict = 'ok' } = {}) {
  const delta = verdict === 'critical' ? 500 : verdict === 'warn' ? 100 : 4.17;
  return {
    window_days: 30,
    run_at_iso: new Date(Date.now() - 120_000).toISOString(),
    age_s: 120,
    trades_evaluated: 142,
    trades_drift_count: verdict === 'critical' ? 3 : verdict === 'warn' ? 1 : 0,
    pnl_displayed_sum: 1234.56,
    pnl_oracle_sum: 1234.56 - delta,
    pnl_delta_abs: delta,
    pnl_delta_pct: delta / 1234.56,
    phantom_count: verdict === 'critical' ? 2 : 0,
    premature_count: verdict === 'critical' ? 1 : 0,
    verdict,
    last_5_runs: Array.from({ length: 5 }, (_, i) => ({
      run_at_iso: new Date(Date.now() - (5 - i) * 120_000).toISOString(),
      pnl_delta_abs: delta * (0.7 + i * 0.075),
    })),
  };
}

function defaultPillars({ overallOk = true } = {}) {
  return {
    overall_ok: overallOk,
    computed_at_iso: new Date().toISOString(),
    pillars: {
      oracle:         { ok: true, detail: '24 quotes/24h', last_quote_age_s: 30, quotes_24h: 24 },
      reconciliation: { ok: true, detail: 'ran 2m ago, 0 divergences', last_run_age_s: 120, divergences_24h: 0 },
      backfill:       { ok: true, detail: '142 resolved / 8 pending', markets_resolved: 142, markets_pending: 8 },
      spread_gates:   { ok: overallOk, detail: '1/12 rejects', rejects_24h: 1 },
      audit_log:      { ok: true, detail: '12 rows · 0 fallbacks', rows_24h: 12, phantom_count_24h: 0, fallback_count_24h: 0 },
    },
  };
}

function withMockedApi(handlers = {}) {
  return base.extend({
    page: async ({ page }, use) => {
      // Track outbound POSTs so specs can assert on them.
      page._poyPosts = [];

      await page.route('**/api/v1/live-summary', async route => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(handlers.liveSummary ? handlers.liveSummary() : defaultLiveSummary()),
        });
      });
      await page.route('**/api/inspector/snapshot*', async route => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(handlers.inspectorSnapshot ? handlers.inspectorSnapshot() : defaultInspectorSnapshot()),
        });
      });
      await page.route('**/api/inspector/reconciliation', async route => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(handlers.reconciliation ? handlers.reconciliation() : defaultReconciliation()),
        });
      });
      await page.route('**/api/inspector/reconciliation/trades*', async route => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ trades: [] }) });
      });
      await page.route('**/api/inspector/reconciliation/run', async route => {
        page._poyPosts.push({ url: route.request().url(), method: 'POST', body: route.request().postData() });
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ scheduled: true, queued_at_iso: new Date().toISOString(), key: 'recon:trigger:queued' }) });
      });
      await page.route('**/api/health/pillars', async route => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(handlers.pillars ? handlers.pillars() : defaultPillars()),
        });
      });
      await page.route('**/api/control/killswitch', async route => {
        page._poyPosts.push({ url: route.request().url(), method: 'POST', body: route.request().postData() });
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ execution_enabled: false }) });
      });
      await page.route('**/api/control/halt', async route => {
        page._poyPosts.push({ url: route.request().url(), method: 'POST', body: route.request().postData() });
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ killswitched: true, halt_published: true }) });
      });
      // Default: any other /api/* returns 200 empty so we don't depend on a real backend.
      await page.route('**/api/**', async route => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
      });
      await page.route('**/ws/live', async route => { await route.abort('failed'); });

      await use(page);
    },
  });
}

module.exports = {
  withMockedApi,
  defaultLiveSummary,
  defaultInspectorSnapshot,
  defaultReconciliation,
  defaultPillars,
  expect,
};
