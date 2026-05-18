// test_livestore.mjs — A9 LiveStore slice + dispatcher tests
//
// Vanilla Node, no Jest. Run with:
//   node tests/dashboard/test_livestore.mjs
//
// The api-client.js file is an IIFE that pokes window.LiveStore. We can't
// import it directly in Node (it expects `window`, `fetch`, `WebSocket`),
// so the harness below stubs the browser globals enough for the IIFE to
// initialise without throwing, then exercises the resulting store via the
// __test backdoor.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '../../static/dashboard/api-client.js');

// ── Minimal browser-ish sandbox ───────────────────────────────────────────
function makeSandbox() {
  const fetchCalls = [];
  const wsInstances = [];

  // Fetch stub — never resolves the snapshot path, so the bootstrap call
  // doesn't pollute the slices we want to inspect. setTimeout / setInterval
  // are available but the polling will only fire after POLL_INTERVAL_SLOW_MS
  // (60s) so it won't run during a single-shot test.
  const fetchStub = async (url) => {
    fetchCalls.push(url);
    return {
      ok: false,
      status: 599,
      headers: { get: () => null },
      json: async () => ({}),
      text: async () => '',
    };
  };

  // WebSocket stub — captures handlers so tests can simulate messages.
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.onopen = null; this.onmessage = null; this.onerror = null; this.onclose = null;
      wsInstances.push(this);
      // Fire onopen asynchronously to mirror real browser semantics.
      Promise.resolve().then(() => this.onopen && this.onopen());
    }
    close() { this.onclose && this.onclose(); }
  }

  const localStorageData = {};
  const localStorage = {
    getItem: (k) => localStorageData[k] ?? null,
    setItem: (k, v) => { localStorageData[k] = String(v); },
    removeItem: (k) => { delete localStorageData[k]; },
  };

  const win = {
    location: { origin: 'http://localhost:8000' },
    localStorage,
    fetch: fetchStub,
    WebSocket: FakeWS,
    setTimeout, clearTimeout, setInterval, clearInterval,
    console,
    __test_state: { fetchCalls, wsInstances },
  };
  win.window = win;
  // The IIFE in api-client.js reads `window.LiveStore` and `window.PoybotAPI`
  // at the end — those must be writable on this sandbox object.
  return win;
}

function loadStore() {
  const sandbox = makeSandbox();
  const src = readFileSync(SRC, 'utf-8');
  vm.createContext(sandbox);
  // The api-client.js source uses bare `fetch`, `setTimeout`, `WebSocket`,
  // `localStorage` — all live on `globalThis` once we set them on the vm
  // context. Wrap in a function so the strict-mode `this`-is-undefined
  // doesn't break the IIFE.
  vm.runInContext(
    `var fetch = window.fetch;
     var WebSocket = window.WebSocket;
     var localStorage = window.localStorage;
     ${src}`,
    sandbox,
  );
  return sandbox.LiveStore;
}

// ── Tests ─────────────────────────────────────────────────────────────────
let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); console.log(`  ok  ${name}`); passed++; }
  catch (e) { console.error(`  FAIL ${name}`); console.error('       ', e.message); failed++; }
}

console.log('LiveStore A9 tests');
console.log('──────────────────');

const store = loadStore();

// T1: subscribeSlice + dispatch trade → trades listener fires exactly once
test('T1: subscribe("trades") fires on trade dispatch', () => {
  let count = 0;
  let received = null;
  const off = store.subscribeSlice('trades', (s) => { count++; received = s; });
  store.__test.dispatchTyped({
    type: 'trade',
    data: {
      time: '2026-05-18T12:00:00Z',
      market_id: 'mkt-A',
      wallet_address: '0xabc',
      side: 'BUY',
      price: '0.65',
      size_usdc: '100',
      is_leader: true,
    },
  });
  assert.equal(count, 1, `expected 1 notify, got ${count}`);
  assert.ok(received && Array.isArray(received.recent), 'trades slice exposes .recent');
  assert.equal(received.recent.length, 1, 'one trade buffered');
  assert.equal(received.recent[0].market_id, 'mkt-A');
  // price / size are now numeric (normalised from string).
  assert.equal(typeof received.recent[0].price, 'number');
  off();
});

// T2: trade dispatch DOES NOT notify decisions subscribers
test('T2: trade dispatch leaves decisions subscribers untouched', () => {
  let decisionsNotified = 0;
  const off = store.subscribeSlice('decisions', () => { decisionsNotified++; });
  store.__test.dispatchTyped({
    type: 'trade',
    data: { time: 'x', market_id: 'mkt-B', wallet_address: '0xdef', side: 'SELL', price: '0.4', size_usdc: '50' },
  });
  assert.equal(decisionsNotified, 0, 'decisions slice must NOT fire on trade');
  off();
});

// T3: trade dispatch optimistically bumps paperPnL.observed_trades_24h
test('T3: trade bumps paperPnL.observed_trades_24h optimistically', () => {
  // Seed paperPnL via the slice cache directly (would normally come from HTTP bootstrap).
  store.slices.paperPnL = { observed_trades_24h: 10, exec_trades_24h: 2, total: 0 };
  let lastPaperPnL = null;
  const off = store.subscribeSlice('paperPnL', (s) => { lastPaperPnL = s; });
  store.__test.dispatchTyped({
    type: 'trade',
    data: { time: 'x', market_id: 'mkt', wallet_address: '0x1', side: 'BUY', price: '0.5', size_usdc: '10' },
  });
  assert.equal(lastPaperPnL.observed_trades_24h, 11, 'observed_trades_24h must ++');
  assert.equal(lastPaperPnL.exec_trades_24h, 2, 'exec_trades_24h untouched by observed trade');
  off();
});

// T4: position_closed updates paperPnL.total + exec_trades_24h
test('T4: position_closed bumps paperPnL.total by pnl_usdc and exec_trades_24h', () => {
  store.slices.paperPnL = { observed_trades_24h: 0, exec_trades_24h: 5, total: 100 };
  let lastPaperPnL = null;
  const off = store.subscribeSlice('paperPnL', (s) => { lastPaperPnL = s; });
  store.__test.dispatchTyped({
    type: 'position_closed',
    data: { pnl_usdc: 25.5, market_id: 'mkt', strategy: 'follow' },
  });
  assert.equal(lastPaperPnL.exec_trades_24h, 6, 'exec_trades_24h ++');
  assert.equal(lastPaperPnL.total, 125.5, 'total += pnl_usdc');
  off();
});

// T5: unsubscribe stops notifications
test('T5: unsubscribe stops further notifications', () => {
  let count = 0;
  const off = store.subscribeSlice('trades', () => { count++; });
  store.__test.dispatchTyped({ type: 'trade', data: { time: 'x', market_id: 'a', wallet_address: '0x', side: 'BUY', price: '0.5', size_usdc: '1' } });
  assert.equal(count, 1);
  off();
  store.__test.dispatchTyped({ type: 'trade', data: { time: 'x', market_id: 'b', wallet_address: '0x', side: 'BUY', price: '0.5', size_usdc: '1' } });
  assert.equal(count, 1, 'unsubscribed callback must NOT be called again');
});

// T6: reconciliation dispatch also propagates into systemStatus.reconciliation
test('T6: reconciliation dispatch propagates to systemStatus', () => {
  store.slices.systemStatus = { bot_status: 'RUNNING', ws_status: 'LIVE' };
  let systemStatusNotified = 0;
  let reconNotified = 0;
  const off1 = store.subscribeSlice('systemStatus', () => { systemStatusNotified++; });
  const off2 = store.subscribeSlice('reconciliation', () => { reconNotified++; });
  store.__test.dispatchTyped({
    type: 'reconciliation',
    data: { verdict: 'warn', delta_abs: 125.5, sample_size: 42 },
  });
  assert.equal(reconNotified, 1, 'reconciliation slice fires');
  assert.equal(systemStatusNotified, 1, 'systemStatus also fires (because recon was mirrored in)');
  assert.equal(store.slices.systemStatus.reconciliation.verdict, 'warn');
  off1(); off2();
});

// T7: decision dispatch increments per-action counters
test('T7: decision dispatch increments action counters', () => {
  store.slices.decisions = { recent: [], counters: {} };
  store.__test.dispatchTyped({ type: 'decision', data: { action: 'OPEN', confidence: 0.7 } });
  store.__test.dispatchTyped({ type: 'decision', data: { action: 'open', confidence: 0.6 } });
  store.__test.dispatchTyped({ type: 'decision', data: { action: 'skip', confidence: 0.1 } });
  assert.equal(store.slices.decisions.counters.open, 2, 'open counter == 2 (case-insensitive)');
  assert.equal(store.slices.decisions.counters.skip, 1);
  assert.equal(store.slices.decisions.recent.length, 3);
});

// T8: rolling buffer caps at 200 trades
test('T8: trade buffer caps at 200', () => {
  store.slices.trades = { recent: [] };
  for (let i = 0; i < 250; i++) {
    store.__test.dispatchTyped({
      type: 'trade',
      data: { time: String(i), market_id: 'm', wallet_address: '0x', side: 'BUY', price: '0.5', size_usdc: '1' },
    });
  }
  assert.equal(store.slices.trades.recent.length, 200, 'trade buffer must clamp to 200');
});

// T9: connectionState lives on the legacy bus, accessible via subscribe()
test('T9: legacy subscribe() still works for connectionState', () => {
  let lastConn = null;
  const off = store.subscribe((s) => { lastConn = s.connectionState; });
  store._set({ connectionState: 'connected' });
  assert.equal(lastConn, 'connected');
  off();
});

console.log('──────────────────');
console.log(`passed=${passed} failed=${failed}`);
// api-client.js schedules a polling setTimeout that keeps the event loop
// alive. We've finished all assertions — exit explicitly with the right code.
process.exit(failed > 0 ? 1 : 0);
