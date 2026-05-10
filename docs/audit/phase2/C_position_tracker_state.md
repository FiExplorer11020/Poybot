# Phase 2 Task C — Persistent PositionTracker state

**Audit ref**: Red Flag #4 in `docs/audit/01_data_inventory.md` —
"`PositionTracker._open_positions` is unbounded and lost on restart with
no DB warm-start."

## Files

- `docs/migrations/015_position_tracker_state.sql` — new table.
- `src/observer/position_tracker.py` — UPSERT on OPEN, DELETE on CLOSE
  (inside the F-02 tx), `warm_start(conn)`, eviction.
- `src/observer/main.py` — calls `tracker.warm_start()` BEFORE
  `tracker.start()`.
- `src/config.py` — `MAX_OPEN_POSITIONS_TRACKED` (default 10 000).
- `src/monitoring/metrics.py` — 3 metrics: gauge, warm-start counter,
  eviction counter.
- `tests/test_observer/test_position_tracker_persistence.py` — 7 tests.
- `tests/test_observer/test_position_tracker.py` — surgical updates to
  6 existing tests that introspected `conn.execute.*`.

## State table shape

`position_tracker_state` is keyed by `(wallet_address, market_id,
token_id, direction)` so one row tracks all FIFO slots for a leg. Stored
columns: `open_time`, `entry_price`, `size_usdc`, `size_shares`,
`shares_remaining`, `fee_rate_pct`, and a `state_json` JSONB catch-all
reserved for future per-row fields (today: `'{}'`).

The in-memory model has multiple FIFO slots per
`(wallet, market, token)` (a wallet can buy → partial-sell → re-buy and
hold two slots). The DB row stores the AGGREGATE: `open_time` /
`entry_price` / `fee_rate_pct` from the HEAD slot (next to close), and
the SUMS of `size_usdc` / `size_shares` / `shares_remaining` across all
slots. Warm-start reconstructs a single slot from each row — a small
fidelity loss vs the live FIFO list, but the audit only cared about not
dropping in-flight opens, and re-creating per-slot rows would require a
synthetic ordinal column.

Three indexes: `idx_pts_wallet`, `idx_pts_market`, `idx_pts_open_time`
(the last supports the eviction sort).

## Atomicity (the actual point)

The `position_tracker_state` DELETE is appended to the existing
`conn.transaction():` block in `_close_position` (Phase 0 F-02), right
after the `positions_reconstructed` INSERT. Both writes commit or
neither does — a leftover state row can NEVER outlive its close row.
The OPEN-side UPSERT runs in its own short transaction (single
statement; the OPEN side has no such atomicity invariant — the in-memory
state IS the authoritative copy for the running process).

## Warm-start path

`PositionTracker.warm_start(conn=None)` runs in `src/observer/main.py`
AFTER the asyncpg pool initialises but BEFORE `tracker.start()`
subscribes to `trades:observed`. Without this ordering a SELL that
lands milliseconds after restart can fire before we've loaded the
matching OPEN and is silently dropped — the very bug the audit flagged.
Each loaded row bumps `polybot_position_tracker_warm_start_loaded_total`.
On hot-restart with N open positions in `position_tracker_state`,
warm-start is a single `SELECT * FROM position_tracker_state` followed
by O(N) dataclass construction.

## Eviction policy

`MAX_OPEN_POSITIONS_TRACKED = 10_000` (env-overridable). When the slot
count exceeds the cap we evict the OLDEST slot by `open_time` across
all keys, drop it from memory, DELETE the matching state row if the key
now has zero slots (otherwise UPSERT the new aggregate), bump
`polybot_position_tracker_evictions_total`, and emit a WARNING log.
The cap is enforced (a) after every OPEN and (b) at the end of
`warm_start` so a long outage that left more than 10 000 rows is
trimmed at boot. The audit's "unbounded growth" worry is resolved:
the dict is now provably bounded across restarts.

## Tests (7 new + 6 updated)

`tests/test_observer/test_position_tracker_persistence.py`:

1. `test_open_persists_state_row` — OPEN issues one UPSERT.
2. `test_close_deletes_state_row_same_tx` — DELETE follows INSERT
   immediately in the recorded SQL sequence.
3. `test_warm_start_rehydrates_open_positions` — two rows round-trip.
4. `test_eviction_drops_oldest_when_over_limit` — cap=2, 3 slots →
   oldest drops, DELETE on state table.
5. `test_partial_open_upserts_existing_row` — two BUYs on the same key
   produce two UPSERTs whose `size_shares` aggregate to 2000.
6. `test_crash_mid_close_rolls_back_both_writes` — injected failure on
   `INSERT INTO positions_reconstructed` rolls back the same-tx state
   DELETE too (verified via the mock transaction's rollback simulation).
7. `test_warm_start_increments_counter` — Prometheus counter ticks.

The mock `conn.transaction()` truncates `_statements` on exception, so
"crash mid-tx" can be asserted by checking neither write is observable
afterwards. asyncpg behaves the same way in production.

## Surprises

1. **The DB row is an aggregate, not per-slot.** The in-memory model has
   multiple slots per (wallet, market, token, direction); the PK on the
   state table is exactly that 4-tuple. I picked aggregate-on-write
   rather than synthetic-ordinal because (a) every FIFO slot in practice
   shares an `open_time` to within seconds and (b) the audit only
   required surviving "in-flight opens", not preserving the multi-slot
   structure. Re-validating multi-slot scenarios on warm-start would
   require a schema rev.
2. **Six existing tests had to be updated**, all surgically: they
   asserted `conn.execute.assert_awaited_once()` or indexed
   `captured_args` positionally. Now that OPEN also UPSERTs and CLOSE
   also DELETEs, the call sequence is longer. The helper
   `_close_insert_calls(conn)` filters by SQL fragment so the assertions
   stay readable. No production-side semantics changed.
3. **Coordination with Task D**: while I was working, Task D refactored
   `_subscribe_loop` into a `Subscriber` from `src/control/redis_pubsub.py`
   and added 4 metrics to `metrics.py`. My persistence patches are
   orthogonal to that change — only `_handle_buy` and `_close_position`
   got new statements; the subscriber path is untouched.

## Test command + result

```bash
cd "/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot"
source .venv/bin/activate
pytest tests/test_observer/test_position_tracker.py \
       tests/test_observer/test_position_tracker_persistence.py -x -q
```

Result: **19 passed in 0.17s**. Broader observer sweep
(`tests/test_observer/` minus `test_observer_main.py`): **66 passed**.
