# Phase 0 / Task B — Close the killswitch 2-second stale-cache window (F-05)

**Audit ref**: `docs/audit/02_client_audit.md` § F-05 (P0 #3).
**Status**: implemented and tested.

## Chosen option: (a) bypass cache on the LIVE-trade gate

Per the task brief recommendation. (a) is the smaller, safer change for
Phase 0 — one extra DB roundtrip per live trade is negligible (live
fires at human-decision cadence, not at WS-tick cadence) and the diff
is local. Option (b) (push-invalidate via `control:killswitch_changed`)
introduces a second long-lived subscriber per process and new failure
modes (subscriber lag, reconnect handling) that belong in Phase 2
alongside the dedicated-pubsub-client refactor (F-04). We intentionally
preserve the Redis cache for paper, RiskManager's master-switch check,
the dashboard, and Telegram — those readers can tolerate up to 2s of
staleness; live cannot.

## Diff summary

### `src/control/killswitch.py`
- `KillswitchService.get_state(*, bypass_cache: bool = False)` — new
  kwarg. When True, skip the Redis read and go straight to the DB.
  Continue to write-through the cache after the DB read so subsequent
  fast-path readers also observe the fresh value sooner. SAFE-OFF on DB
  failure (no fall-back to stale cache).
- `is_execution_enabled(bypass_cache=False)` and
  `is_real_execution_enabled(bypass_cache=False)` — forward the kwarg.
  Back-compat: default behavior unchanged for every existing caller.
- Docstrings cite F-05 and spell out the contract: callers about to
  place a real order via `py-clob-client` MUST pass `bypass_cache=True`.

### `src/engine/live_trader.py`
- Import `get_killswitch`.
- In `open_trade()`, after the cheap parameter vetos and before the
  conflict-check / INSERT / OrderManager call, insert a strict-path
  killswitch read. Skipped when `self.dry_run` is True (no order is
  sent, so the gate is a no-op there; preserves shadow-row behavior).
  Comment at the call site: `# F-05: bypass cache to prevent 2s leak window`.
- Fail SAFE: if the strict-path read itself raises, refuse the trade.

### Tests
- `tests/test_control/test_killswitch_unit.py` — three new tests:
  - **`test_strict_path_ignores_stale_cache`** — the leak-closure
    proof. DB says DISABLED; Redis pre-warmed with an ENABLED stale
    payload; assert `is_real_execution_enabled(bypass_cache=True)`
    returns False. Also asserts that the fast path *still* returns
    True (we did not break the cache for non-execution callers).
  - `test_strict_path_refreshes_cache_for_subsequent_readers` —
    asserts the strict path writes-through to Redis (shortens the
    leak window for paper/dashboard too).
  - `test_strict_path_fail_safe_on_db_failure` — even with a "happy"
    cache, a DB outage on the strict path returns SAFE-OFF.
- `tests/test_engine/test_live_trader.py` — four new tests + a
  fixture (`_stub_killswitch`, autouse) that defaults to ON so the
  pre-existing tests stay green:
  - `test_open_trade_vetoes_when_killswitch_real_off` — strict-path
    veto: no INSERT, no OrderManager call, no in-memory state.
  - `test_open_trade_strict_path_used_not_cached` — API contract:
    `is_real_execution_enabled(bypass_cache=True)`, exactly.
  - `test_open_trade_skips_killswitch_check_in_dry_run` — dry-run
    contract: shadow rows still get inserted.
  - `test_open_trade_killswitch_read_failure_refuses_trade` —
    fail-safe on infra failure.

## The test that proves the leak is closed

`tests/test_control/test_killswitch_unit.py::test_strict_path_ignores_stale_cache`
literally reproduces the F-05 race:

```
DB:    real_execution_enabled = False   ← truth, just flipped
Redis: real_execution_enabled = True    ← stale, still in TTL window

await svc.is_real_execution_enabled()                  # fast path  → True (still buggy by design)
await svc.is_real_execution_enabled(bypass_cache=True) # strict     → False  ← the fix
```

The second assertion is the leak closure. The first assertion is kept
as a guard: it proves we did not break the fast path for the readers
that are allowed to use it (paper, dashboard, Telegram, master-switch
check in RiskManager).

## Threat-model notes

- **What this fixes**: a flip of `real_execution_enabled` from True to
  False (operator hits the "stop live" button, or the Telegram
  `/killswitch real off`, or `POST /api/control/real`) now propagates
  to the live trade gate within one Postgres roundtrip (~ms) instead
  of up to 2s. A leader signal arriving in that window is refused at
  `LiveTrader.open_trade` BEFORE the `live_trades` INSERT, BEFORE
  `OrderManager.place_for_position`, and BEFORE any CLOB API call.
- **What it does NOT fix (and why that's acceptable for Phase 0)**:
  - The master switch (`execution_enabled`) check in
    `RiskManager.check_can_trade` still uses the cached fast path. A
    flip of the master switch from True to False can still leak up to
    2s of new paper trades. We accept this: paper PnL is not real
    money, and the live gate (strict path) covers the real-money
    case. A future change can promote the RiskManager check to strict
    when called from the live decision pipeline (would need a "called
    from live" signal threaded through ConfidenceEngine).
  - In-flight orders that have already crossed the strict-path gate
    are not recalled. Mitigation: existing OrderManager retry loop is
    short; flip-then-cancel is operator workflow.
  - The strict path defaults to SAFE-OFF on infra failure. A
    Postgres outage will refuse all live trades — this is the
    intended behavior. Operator monitoring already alerts on DB
    health, so this is not a silent failure.

## Deferred follow-ups

- **F-04 (Phase 2)**: dedicated pubsub client. Once that lands, option
  (b) becomes cheap — every reader subscribes to
  `control:killswitch_changed` and PUSH-invalidates its local cache on
  receipt. At that point we can revert this strict-path call to the
  fast path and get both safety and zero DB roundtrips. Tracked.
- **F-25 (P2)**: SAFE-OFF state currently uses `datetime.now(...)` as
  `updated_at`, which makes the dashboard show "killswitch flipped 2s
  ago" on every refresh during a DB outage. Unrelated to F-05; covered
  by audit P2 follow-up.
- **F-28 (P2)**: `inspect.isawaitable` defensive checks in the
  killswitch can be removed (redis-py 5.x asyncio is always async).
  Unrelated to F-05; covered by audit P2 follow-up.
- Consider promoting `RiskManager.check_can_trade` to the strict path
  when the decision is destined for the live channel. Requires a
  decision-context flag; not free, deferred until F-04 lands.

## Verification

```
$ pytest tests/test_control/ tests/test_engine/test_live_trader.py tests/test_safety/ -x -q
........................................                                 [100%]
40 passed in 0.15s
```

All green.
