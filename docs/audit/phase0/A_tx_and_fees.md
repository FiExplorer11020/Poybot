# Phase 0 Task A — Transaction Safety + Gamma Fee Mis-interpretation

**Scope**: audit findings F-01, F-02, F-03 (`docs/audit/02_client_audit.md`)
and Red Flag R-12 (`docs/audit/01_data_inventory.md`).
**Files touched (production)**:

- `src/engine/paper_trader.py`
- `src/engine/portfolio_state.py` (transitive — `save_state` / `record_equity`
  now take an optional `conn` so paper_trader can thread its tx through)
- `src/observer/position_tracker.py`
- `src/observer/trade_observer.py`

**Files touched (tests)** — fixture updates only:

- `tests/test_engine/test_paper_trader.py`
- `tests/test_observer/test_trade_observer.py`
- `tests/test_observer/test_trade_observer_live_labels.py`
- `tests/test_observer/test_position_tracker.py`

---

## Part 1 — Transaction safety

### Why mocks needed touching at all

Production code now wraps each multi-statement chain in
`async with conn.transaction():`. asyncpg's `Connection.transaction()` is a
sync method returning a `Transaction` async-CM. A bare `AsyncMock` returns
a coroutine — incompatible with `async with`. Each test module gained one
small helper that attaches a no-op async-CM to the mock:

```python
@asynccontextmanager
async def _tx():
    yield None
conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
```

No production semantics asserted by tests had to change, except the dedup
short-circuit test discussed below.

### F-01 — `PaperTrader.open_trade` / `close_trade`

**`open_trade`** (`src/engine/paper_trader.py` around lines 495–578):

- Wrapped the `paper_trades` INSERT, the `portfolio_state` UPSERT, and the
  `portfolio_equity` INSERT in a single `conn.transaction()`.
- `save_state` / `record_equity` (in `portfolio_state.py`) gained an
  optional `conn=` kwarg routed through a new `_conn_ctx` helper so they
  participate in the caller's transaction when given one and still
  acquire a fresh pooled connection when called from anywhere else
  (`load_state`'s default-create path, monitor loop's mark-to-market tick,
  etc.).
- In-memory bookkeeping (`self._capital`, `self._open_trades`) is updated
  BEFORE the `async with conn.transaction()` so the UPSERT carries the
  post-trade values. On rollback the except block restores the snapshot
  to avoid phantom open trades after a DB failure.
- The Redis publish to `positions:paper_opened` stays OUTSIDE the
  transaction (publish-after-commit semantics).

**`close_trade`** (around lines 605–698 of the new file):

- Same pattern. `paper_trades` UPDATE, `decision_log` UPDATE,
  `_persist_state(conn=conn)`, and `_record_equity_sample(conn=conn)` now
  commit atomically.
- The original second pair of `_persist_state()` / `_record_equity_sample()`
  calls at the end of the function (separate roundtrips) were removed —
  redundant now that the persist runs inside the tx.
- The Redis `positions:paper_closed` publish stays outside the tx.
- Capital / peak / realized PnL mutations are tracked inside the tx and
  rolled back in the except handler if anything raises.

### F-02 — `PositionTracker._close_position`

`src/observer/position_tracker.py` around lines 302–376. The two SELECTs
(category lookup + trend snapshot for `is_contrarian`) and the INSERT into
`positions_reconstructed` are now one `conn.transaction()`. asyncpg's
default isolation is fine here; switching to `repeatable_read` (audit
suggestion) was kept out of scope — the audit itself flagged it as
optional and changing isolation has subtle deadlock-window implications I
didn't want to ship in a P0 patch. Filed as Phase 1 follow-up below.

### F-03 — `TradeObserver._process_trade`

`src/observer/trade_observer.py` around lines 924–1042 + 1037–1095.

- Main insert chain (`markets` stub upsert → `trades_observed` insert →
  `markets` fetch → `_repair_market_from_trade_hint` → conditional
  `trades_observed` UPDATE → `leaders` fetch) wrapped in one
  `conn.transaction()`.
- `self._inserted += 1` moved OUT of the inner block. It now fires AFTER
  the `async with` exits successfully, so the metric tracks committed rows
  rather than attempted-and-rolled-back rows.
- Second `get_db()` block (Gamma enrichment UPSERT into `markets`) also
  wrapped in a `conn.transaction()`. Single statement today, but R-1 in
  the inventory will add a `fee_snapshots` INSERT next to it — wrapping
  now keeps the future patch surgical.
- The Redis publish to `trades:observed` stays outside the tx.
- `_repair_market_from_trade_hint` was already connection-agnostic (it
  takes a `conn` argument and runs SQL on it). No change needed.

### Test fixture surgery

- All three test modules' `_make_conn` / `_make_db_cm` helpers attach the
  no-op `conn.transaction()` mock.
- `test_trade_observer_live_labels.py` had inline `conn = AsyncMock()`
  blocks per-test; added a small `_attach_transaction(conn)` helper and
  called it after each inline mock setup.
- One test assertion change:
  `test_process_trade_db_layer_dedup_short_circuits` previously asserted
  `conn.execute.assert_not_awaited()` after a DB-level dedup hit. This
  relied on the pre-existing-working-tree behaviour where the markets stub
  upsert ran *after* `fetchval`. That ordering had a known correctness bug
  (trades.category was always NULL on the very first trade of a
  brand-new market — comment to that effect was already in the file).
  The fix preserved the reordered semantics (markets stub upsert FIRST,
  inside the transaction) and the test now asserts the more precise
  contract: exactly one `execute` call, on the markets stub SQL, plus no
  fetchrow / no publish / no counter bump. Same shape as before, more
  honest assertion.

---

## Part 2 — Gamma fee mis-interpretation (R-12)

`src/observer/trade_observer.py` `_fetch_market_metadata_from_gamma`
(around lines 1310–1360 in the new file).

The old read was:

```python
"fee_rate_pct": float(
    market.get("makerBaseFee")
    or market.get("baseFee")
    or market.get("fee")
    or 0.0
),
```

`makerBaseFee` is the maker fee per Polymarket's gamma docs.
`paper_trader.calculate_polymarket_fee(..., liquidity_role=LiquidityRole.TAKER)`
and `position_tracker._close_position` both consume this value — so we
were using the maker rate as the taker rate, systematically
under-estimating cost and over-stating PnL.

Fix:

1. Prefer `takerBaseFee` (with `taker_base_fee` snake-case fallback).
2. Fall back to `makerBaseFee` only if `takerBaseFee` is missing, with a
   debug log so the coverage gap is visible.
3. Last-resort fallback to `baseFee` / `fee` (legacy keys).
4. Renamed the local to `gamma_taker_fee_bps` and added a comment at the
   `markets.fee_rate_pct` write so future readers don't think the column
   stores a maker rate.
5. The DB column rename (`fee_rate_pct` → `taker_fee_rate_pct`) is a
   migration and was kept OUT of scope per "surgical, not rewrite".

---

## Test command + result

```bash
cd "/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot"
source .venv/bin/activate
pytest tests/test_engine/test_paper_trader.py \
       tests/test_observer/test_trade_observer.py \
       tests/test_observer/test_position_tracker.py -x -q
```

Result: **41 passed in 1.08s**.

Adjacent sweep (`test_paper_trader` + `test_confidence_engine` +
`test_decision_router` + `test_readiness_persistence` +
`test_neural_readiness` + every test under `tests/test_observer/` except
`test_observer_main.py` which imports a daemon module): **125 passed**.

Pre-existing failures (NOT caused by this task; reproduced on bare HEAD
before any edits):

- `tests/test_engine/test_risk_manager.py::test_check_can_trade_passes_clean_state` — DB pool not initialised in test setup, killswitch unreachable.
- `tests/test_engine/test_scheduler_aps.py` (7 tests) — `APScheduler` not installed in the local venv.
- `tests/test_telegram_bot/*` and `tests/test_engine/{test_dual_routing_integration,test_jobs,test_watchdog}.py` — `fakeredis` not installed.
- `tests/test_docker.py` — `pyyaml` not installed.

These are environment / fixture issues, not code regressions.

---

## Surprises

1. The pre-existing working tree (before my edits) had already swapped the
   markets-stub-upsert order in `_process_trade` to run BEFORE the
   trades_observed insert (with a comment explaining the
   "trades.category always NULL on first trade" bug fix). I preserved
   that ordering — but it had also silently broken one test assertion in
   `test_process_trade_db_layer_dedup_short_circuits`, which I had to
   update to reflect the new (more correct) ordering. Worth noting
   because the test was passing against HEAD and failing against the
   unstaged tree before my changes.
2. `save_state` and `record_equity` had to gain an optional `conn` param.
   This is a public-ish API change for `portfolio_state.py`. All existing
   callers pass nothing → fallback to acquiring a pooled connection → no
   behaviour change. Only the new paper_trader paths supply `conn`.

---

## Follow-ups deferred to Phase 1+

- **Repeatable-read isolation for F-02.** The audit suggested wrapping
  `_close_position` in `conn.transaction(isolation='repeatable_read')` to
  pin a snapshot across the two SELECTs and the INSERT. I kept the
  default isolation to minimise the patch; switching isolation can
  surface deadlocks under concurrent writers and deserves its own test
  pass.
- **DB column rename `markets.fee_rate_pct` → `markets.taker_fee_rate_pct`**
  (needs a migration; out of scope for a P0 code patch).
- **`fee_snapshots` writes (R-1 / R-12 long-term resolution).** Today the
  `markets.fee_rate_pct` column is the only fee feed. The proper fix is
  to populate the `fee_snapshots` table (migration 003) from CLOB
  `getClobMarketInfo` (which exposes a real `takerFeeRate` field per
  `src/economics/fee_snapshots.py`) and have paper_trader / position_tracker
  read from snapshots instead of the denormalised gamma copy. Phase 1
  task.
- **Audit F-04 (Redis pubsub on shared client) and F-05 (killswitch
  cache TTL leak)** are P0 in the audit but out of scope for this
  task — separate Phase 0 chunks.
- **Audit F-08** (`_repair_market_from_trade_hint` claim about no
  transaction) is now resolved as a side-effect of F-03: that method
  runs inside the same `conn.transaction()` because it gets the same
  conn from the caller.
- **`PaperTrader._compute_unrealized_pnl` inside the tx**: `open_trade`'s
  `_record_equity_sample(conn=conn)` indirectly calls
  `_compute_unrealized_pnl` which iterates open trades and acquires
  ANOTHER pooled connection per trade to fetch the current price. Not a
  deadlock (separate conns) but it extends the tx window. Worth
  collapsing into a single CTE in a Phase 1 perf pass.
