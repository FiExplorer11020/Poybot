# Runbook — Paper Truth Reconciliation

**Audience:** dashboard operator (you).
**Shipped:** 2026-05-18 via PLAN-UIA-001.
**Companion:** [ADR-PMK-014](ADR/ADR-PMK-014-ui-mission-alignment.md).

---

## 30-second cheat sheet

- **Sidebar RECON chip GREEN** → trust the numbers
- **Sidebar RECON chip AMBER (`Δ $25–$250`)** → watch, don't panic
- **Sidebar RECON chip RED (`Δ ≥ $250`)** → STOP TRADING, open Inspector, drill down
- **Sidebar RECON chip GREY/UNKNOWN** → reconciliation hasn't run yet; click to open Inspector and trigger one

The chip lives in the sidebar footer next to `BOT / WS / INGESTION / EXEC`. Click it → opens Inspector tab.

---

## What the chip means

### Verdict thresholds

| Verdict | Color | Delta `|displayed - oracle|` | Operator action |
|---------|-------|------------------------------|-----------------|
| `ok` | green | < $25 | Numbers trustable. Proceed. |
| `warn` | amber | $25 – $250 | Watch but don't intervene. |
| `critical` | red | ≥ $250 | Open Inspector → drift modal → investigate top divergences. Consider EMERGENCY HALT if scale is wrong. |
| `unknown` | dim grey | n/a | No recon has run. Trigger manually if urgent. |

### How recon is computed

The `paper_close_divergences` table is populated by `scripts/reconciliation.py`
(nightly at 04:00 UTC + on operator demand via the Inspector "↻ Run now" button).
Each row records a divergence: `delta_usdc = db_pnl - truth_pnl` where truth comes
from `markets.resolved_outcome` (populated by the backfill) or a live Gamma fetch.

The dashboard summary (`/api/inspector/reconciliation`) aggregates:
- All closed paper trades in the last 30 days
- All divergence rows for those trades
- Returns `pnl_displayed_sum` (DB) vs `pnl_oracle_sum` (DB - sum_of_deltas)
- Computes verdict via the table above

The summary is cached server-side for 30s. The Inspector panel polls every 30s.

---

## Operator actions

### Drill into drift trades

1. Click the sidebar RECON chip (or press `g i` to jump to Inspector).
2. The "Paper Truth Reconciliation" panel in the right rail shows: hero delta,
   verdict badge, phantom/premature counts, 5-run sparkline.
3. Click **"View N drift trades"** → modal opens with the per-trade table.
4. Sort by `Δ abs` (descending) to find the biggest culprits.
5. Filter by classification: `phantom` / `premature` / `drift` / `ok`.

### Trigger a manual reconciliation

1. Inspector tab → Reconciliation panel → **"↻ Run now"** button.
2. Returns immediately (it's async). Backend sets the Redis key
   `recon:trigger:queued` (TTL 300s); the scheduler picks it up on its next tick
   and runs `scripts/reconciliation.py`.
3. Wait 30–90s, refresh the panel. The new `age_s` should drop to a few seconds.

### Run a full historical backfill

Required after deploying a new feature that affects close-pricing.

```bash
cd /opt/polymarket-bot
# Idempotent — re-running refreshes the existing divergence rows.
python -m scripts.reconciliation --lookback-days 180 --batch-size 200
```

Takes 10–60 min depending on `paper_trades` volume. Run in `screen`/`tmux` because
Gamma occasionally rate-limits (429); the script has exp-backoff but the wall-clock
extends.

---

## V2 dashboard lab mode

### Enable
LAB tab → **"V2 Dashboard Overlay"** card → **ENABLE V2** → confirm reload.

### Disable
Same place → **DISABLE V2** → confirm reload.

### What V2 changes
- **LivePortfolio:** TradingView lightweight-charts equity timeline + PnL ticks.
- **WalletGraph:** Cosmograph WebGL2 force-directed graph (full 2.6k node mesh).

### Why V2 is OFF by default
Per memory `project_v1_vs_v2_terminal.md`: *"V1 = source of truth ; V2 = lab gated OFF, ne pas migrer"*.

The V2 portfolio reads `/api/portfolio/*` (5 separate endpoints), a parallel
compute path that can drift from `/api/v1/live-summary`. The V1 path is the
documented source of truth.

### Browser-console fallback

If the dashboard is broken and you want V1 only:

```javascript
localStorage.removeItem('poybot_v2_lab'); location.reload();
```

---

## The 5 pillars (Bot Health tab)

| Pillar | What it does | Green when |
|--------|--------------|-----------|
| **Price Oracle** | Provides reference prices for close audit | `close_audit_log.oracle_source IN ('book','gamma')` rows in last 24h > 0 |
| **Reconciliation** | Detects displayed-vs-truth drift | `paper_close_divergences.detected_at` in last 24h, OR no closes 24h |
| **Backfill** | Populates `markets.resolved_outcome` | `markets WHERE resolved_outcome IS NOT NULL` > `WHERE resolved_outcome IS NULL AND end_date < NOW()` |
| **Spread Gates** | Rejects closes when book is stale/illiquid | `oracle_source='fail'` rate < 50% of closes in 24h |
| **Close Audit Log** | Records every close with its source | `close_audit_log` rows in 24h ≥ closed paper trades in 24h |

`overall_ok = AND of all 5`. Failing pillar → operator should not trust the dashboard PnL.

### Pillar interpretation cookbook

#### Oracle red — "no quotes in 24h"
The PriceOracle isn't being called. Likely `PaperTrader.close_trade` is hitting
the `oracle=None` fallback branch. Check `journalctl -u polymarket-bot-engine`
for `PaperTrader: force_close: trade #X no oracle quote`.

#### Reconciliation red — "never run" or "stale"
`scripts/reconciliation.py` isn't running. Check the scheduler:
```bash
grep "reconciliation" /var/log/polymarket-bot/engine.log | tail
```
If absent, the cron isn't registered — see `src/engine/scheduler.py`.

#### Backfill red — "X resolved / Y pending" with Y > X
Backfill job is behind. Manually:
```bash
python -m scripts.backfill_resolved_outcomes --batch-size 100
```

#### Spread Gates red — high `fail` rate
The book is stale/illiquid more than 50% of the time. Likely WS feed is
degraded — check Inspector → Pipeline Health.

#### Close Audit Log red — "audit gap"
Closes happening without audit rows. Bug in `paper_trader._insert_close_audit`
or the call site bypassing it. Grep the close_trade code for any return
path that doesn't go through `_insert_close_audit`.

---

## Common scenarios

### "Recon chip went red after a big trade closed"
1. Inspector → drift modal → sort by `Δ abs`.
2. Top row: note the `classification`.
   - `phantom`: price never executed. `close_audit_log.oracle_source` likely
     `fallback` — the close bypassed the oracle. Bug in `paper_trader.close_trade`.
   - `premature`: closed before market resolution. Compare `market.end_date` vs
     `paper_trade.closed_at`; gap should be 0 or negative.
3. If a single trade is dominating: invalidate it via `audit_invalidated` status
   (`paper_trades_invariants` migration 049 added this status for this exact case).

### "5 pillars gauge shows Reconciliation red after deploy"
1. `journalctl -u polymarket-bot-engine | grep reconciliation` — find last run.
2. If never ran since deploy: the scheduler didn't register the job. Restart
   the engine container.
3. If ran but error: usually a Gamma 429 (rate limit) — wait an hour, retry.

### "Recon delta is critical but operator confirms trades look right"
The Gamma `resolved_outcome` itself may be wrong (it's been known). Workflow:
1. Drift modal → sort by `Δ abs` → find the `market_id`
2. Open https://polymarket.com/market/{market_id} → check the resolution
3. If Gamma is wrong: log an `audit_invalidated` flag on the divergence row;
   file a bug against the Gamma data source.

---

## Emergency procedures

### EMERGENCY HALT button (Risk & Config tab)

Single button, two actions:
1. Flips killswitch off (`/api/control/killswitch {enabled: false}`)
2. Publishes Redis `control:halt` → `PaperTrader.force_close_all_positions`
   closes every open paper trade at the last known oracle price.

When to use:
- Reconciliation chip went RED mid-session
- A new code path is suspicious and you want to stop the bleeding
- Operator panic — better to close at a bad oracle price than to leak more

### CLI fallback (when dashboard is down)

```bash
# Halt
curl -X POST http://localhost:8000/api/control/halt \
  -H 'Content-Type: application/json' \
  -H "x-api-token: $POYBOT_TOKEN" \
  -d '{"reason":"manual_halt_via_cli","actor":"oscar"}'

# Re-enable trading after halt
curl -X POST http://localhost:8000/api/control/killswitch \
  -H 'Content-Type: application/json' \
  -H "x-api-token: $POYBOT_TOKEN" \
  -d '{"enabled":true,"reason":"resume_after_halt","actor":"oscar"}'
```

---

## ModeChip — what the sidebar mode badge means

| Display | State |
|---------|-------|
| `PAPER · OK` | Paper trading, reconciliation green |
| `PAPER · WARN` | Paper trading, reconciliation amber |
| `PAPER · DRIFT` | Paper trading, reconciliation critical — **DO NOT TRUST THE PNL** |
| `PAPER` | Paper trading, no recon yet (boot) |
| `LIVE` | Real money trades — killswitch must be off + real_execution_enabled=true |
| `DUAL` | Paper + Live shadow (`TRADING_MODE=dual`) |

The chip appears in 3 places: sidebar footer, topbar, RiskConfig KPI strip.

---

## Honest controls (Risk & Config tab)

The old UI had 3 buttons (START/STOP/PAUSE) that all hit the killswitch — same
effect, different labels. New UI exposes exactly what the backend supports:

| Button | Backend call | Effect |
|--------|-------------|--------|
| `▶ ENABLE TRADING` / `■ DISABLE TRADING` | `POST /api/control/killswitch` | Flips killswitch. Open positions stay open. |
| `⚠ EMERGENCY HALT` | `POST /api/control/halt` | Killswitch off + force-close all open paper positions. |

---

## Keyboard shortcuts

- `g a` — Alpha Terminal
- `g m` — ML Progression
- `g w` — Wallet Graph
- `g p` — Live Portfolio
- `g d` — Decision Engine
- `g i` — Inspector (recon panel lives here)
- `g r` — Risk & Config (halt button lives here)
- `g h` — Bot Health (5 pillars gauge lives here)
- `g l` — LAB (V2 toggle + R7/R8/R9/R10 gates)
- `?` — Show keyboard help

---

## References
- ADR: [ADR-PMK-014](ADR/ADR-PMK-014-ui-mission-alignment.md)
- Memory: `project_paper_trading_pillars.md`, `project_paper_trading_truth.md`, `project_v1_vs_v2_terminal.md`
- Migration 050 (close_audit_log)
- Migration 051 (paper_close_divergences)
- Nightly job: `scripts/reconciliation.py`
