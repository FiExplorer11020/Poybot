// api-client.js — Poybot live data client
// Exposes window.LiveStore (reactive pub/sub) and window.PoybotAPI (actions)

(function () {
  const API_BASE  = localStorage.getItem('poybot_base')  || 'http://localhost:8000';
  const API_TOKEN = localStorage.getItem('poybot_token') || '';

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
       'decision_engine','markets','price_history','clock'].forEach(k => {
        if (p[k] !== undefined) this.snapshot[k] = p[k];
      });
      this.lastUpdate = Date.now();
      this._emit();
    },

    processTrade(trade) {
      if (!this.snapshot) return;
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

  const onEvent = ({ type, payload, snapshot } = {}) => {
    if (snapshot) store.processBootstrap(snapshot);
    if      (type === 'bootstrap' || type === 'control') { if (payload) store.processBootstrap(payload); }
    else if (type === 'tick')                             { if (payload) store.processTick(payload); }
    else if ((type === 'trade' || type === 'trade_closed') && !snapshot && payload) store.processTrade(payload);
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
    botControl:    cmd => apiFetch('/api/v1/bot/control', { method: 'POST', body: JSON.stringify({ command: cmd }) }),
    updateConfig:  cfg => apiFetch('/api/v1/bot/config',  { method: 'PATCH', body: JSON.stringify(cfg) }),
    closePosition:  id => apiFetch(`/api/v1/trades/${id}/close`, { method: 'POST' }),
    pnlTimeframe:   tf => apiFetch(`/api/v1/portfolio/pnl-by-timeframe?timeframe=${tf}`),
    runBacktest:   cfg => apiFetch('/api/v1/backtest', { method: 'POST', body: JSON.stringify(cfg) }),
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
