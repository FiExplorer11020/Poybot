// api-client.js — Poybot live data client
// Exposes window.LiveStore (reactive pub/sub) and window.PoybotAPI (actions)

(function () {
  const API_BASE  = localStorage.getItem('poybot_base')  || window.location.origin;
  const API_TOKEN = localStorage.getItem('poybot_token') || '';
  const READ_ONLY_ERROR = 'Dashboard is wired in read-only display mode for this backend.';

  const normalizeObservedTrade = trade => {
    if (!trade) return null;
    return {
      id: trade.id || [
        trade.market_id || 'market',
        trade.token_id || 'token',
        trade.wallet_address || 'wallet',
        trade.side || 'side',
        trade.price != null ? String(trade.price) : 'na',
        trade.notional != null ? String(trade.notional) : (trade.size_usdc != null ? String(trade.size_usdc) : 'na'),
        trade.time || trade.timestamp || Date.now(),
      ].join(':'),
      timestamp: trade.timestamp || trade.time || null,
      market_title: trade.market_title || trade.market_question || trade.question || trade.market_id || 'Unknown market',
      side: trade.side || '—',
      price: trade.price != null ? Number(trade.price) : null,
      notional: trade.notional != null ? Number(trade.notional) : (trade.size_usdc != null ? Number(trade.size_usdc) : null),
      fees: trade.fees != null ? Number(trade.fees) : null,
      pnl_abs: trade.pnl_abs != null ? Number(trade.pnl_abs) : null,
      pnl_pct: trade.pnl_pct != null ? Number(trade.pnl_pct) : null,
      execution_mode: trade.execution_mode || 'observed',
      status: trade.status || 'observed',
    };
  };

  // ── Reactive store ─────────────────────────────────────────────────────────
  const store = {
    snapshot: null,
    connectionState: 'connecting', // 'connected' | 'reconnecting' | 'disconnected'
    reconnectAttempt: 0,
    lastUpdate: null,
    _ls: new Set(),

    subscribe(fn) { this._ls.add(fn); return () => this._ls.delete(fn); },
    _emit()       { this._ls.forEach(fn => fn(this)); },
    _set(p)       { Object.assign(this, p); this._emit(); },

    processBootstrap(data) {
      this.snapshot    = data;
      this.lastUpdate  = Date.now();
      this._emit();
    },

    processTick(p) {
      if (!this.snapshot) { this.processBootstrap(p); return; }
      ['analytics','bot','stats','positions','ingestion',
       'decision_engine','markets','price_history','clock',
       'recent_trades','risk_config','logs','meta'].forEach(k => {
        if (p[k] !== undefined) this.snapshot[k] = p[k];
      });
      this.lastUpdate = Date.now();
      this._emit();
    },

    processTrade(trade) {
      if (!this.snapshot || !trade) return;
      this.snapshot.recent_trades = [trade, ...(this.snapshot.recent_trades || [])].slice(0, 100);
      this.lastUpdate = Date.now();
      this._emit();
    },
  };

  // ── HTTP helpers ───────────────────────────────────────────────────────────
  const hdrs = (x = {}) => {
    const h = { 'Content-Type': 'application/json', ...x };
    if (API_TOKEN) h['x-api-token'] = API_TOKEN;
    return h;
  };

  const apiFetch = async (path, opts = {}) => {
    const r = await fetch(`${API_BASE}${path}`, { headers: hdrs(opts.headers || {}), ...opts });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  };

  // ── Snapshot poll ──────────────────────────────────────────────────────────
  const loadSnap = async () => {
    try {
      const { data } = await apiFetch('/api/v1/live-summary');
      if (data) store.processBootstrap(data);
    } catch (e) {
      console.warn('[Poybot] poll failed:', e.message);
    }
  };

  // ── WebSocket ──────────────────────────────────────────────────────────────
  let ws = null, rtimer = null, attempts = 0, alive = true;

  const clearT = () => { clearTimeout(rtimer); rtimer = null; };

  const reconn = () => {
    attempts++;
    store._set({ connectionState: 'reconnecting', reconnectAttempt: attempts });
    clearT();
    rtimer = setTimeout(connect, Math.min(1000 * Math.pow(2, attempts - 1), 15000));
  };

  const onEvent = ({ type, payload, snapshot, data } = {}) => {
    const body = payload !== undefined ? payload : data;
    if (snapshot) store.processBootstrap(snapshot);
    if      (type === 'bootstrap' || type === 'control') { if (body) store.processBootstrap(body); }
    else if (type === 'tick' || type === 'stats')        { if (body) store.processTick(body); }
    else if ((type === 'trade' || type === 'trade_closed') && !snapshot && body) store.processTrade(normalizeObservedTrade(body));
  };

  function connect() {
    if (!alive) return;
    try { ws = new WebSocket(`${API_BASE.replace(/^http/, 'ws')}/ws/live`); }
    catch (e) { reconn(); return; }
    ws.onopen    = () => { attempts = 0; store._set({ connectionState: 'connected', reconnectAttempt: 0 }); };
    ws.onmessage = e  => { try { onEvent(JSON.parse(e.data)); } catch (_) {} };
    ws.onerror   = ()  => {};
    ws.onclose   = ()  => { if (alive) reconn(); else store._set({ connectionState: 'disconnected' }); };
  }

  // ── Poll fallback every 5 s ────────────────────────────────────────────────
  const poll = () => { if (!alive) return; loadSnap().finally(() => alive && setTimeout(poll, 5000)); };

  // ── Public API actions ─────────────────────────────────────────────────────
  window.PoybotAPI = {
    botControl: async (cmd) => {
      // /api/control/killswitch is the only control surface exposed by the
      // backend; map dashboard verbs to its enabled flag.
      const enabledByCmd = { start: true, resume: true, stop: false, pause: false, halt: false };
      if (!(cmd in enabledByCmd)) throw new Error(`Unknown control command: ${cmd}`);
      const r = await fetch(`${API_BASE}/api/control/killswitch`, {
        method: 'POST',
        headers: hdrs(),
        body: JSON.stringify({ enabled: enabledByCmd[cmd], reason: `dashboard:${cmd}`, actor: 'dashboard' }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      // Force a snapshot refresh so the UI reflects the new state immediately.
      loadSnap();
      return r.json();
    },
    updateConfig: async (edits) => {
      const r = await fetch(`${API_BASE}/api/risk/update`, {
        method: 'POST',
        headers: hdrs(),
        body: JSON.stringify({ edits, actor: 'dashboard' }),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          const body = await r.json();
          if (body.detail) msg += `: ${body.detail}`;
        } catch (_) {}
        throw new Error(msg);
      }
      loadSnap();
      return r.json();
    },
    closePosition: async () => { throw new Error(READ_ONLY_ERROR); },
    pnlTimeframe: async () => { throw new Error(READ_ONLY_ERROR); },
    runBacktest: async () => { throw new Error(READ_ONLY_ERROR); },
    getSettings:    () => ({ API_BASE, API_TOKEN }),
    setSettings: (base, token) => {
      localStorage.setItem('poybot_base',  base);
      if (token != null) localStorage.setItem('poybot_token', token);
      location.reload();
    },
  };

  // ── Init ──────────────────────────────────────────────────────────────────
  store._set({ connectionState: 'connecting' });
  loadSnap();
  connect();
  setTimeout(poll, 5000);

  window.LiveStore = store;
})();
