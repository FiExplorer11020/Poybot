# Cross-View Consistency Tests

Anti-regression safety net for the V1 dashboard.

## Why this exists

The 2026-05-18 audit (`AUDIT_PROBLEMES_TECHNIQUES_2026_05_18.md`) found
the same fact rendered with **different values** in different panels of
the same dashboard:

- RECON delta `−$2 062` (reality) vs `+$39 784` displayed (corrupt
  source on another card).
- Topbar `WS lag` reading 60-market book freshness (dominated by stale
  markets) instead of the real WS message age.
- `DECISIONS 24h` counter returning `0` because the consumer read
  uppercase actions while the producer wrote lowercase.
- `observed_trades_24h` mirrored at `/api/v1/live-summary` with a stale
  value while `/api/portfolio/pipeline_status` was already correct.

Each was traced to **two endpoints reading two different sources** for
the same logical fact. The 6-batch refactor (A2-A12) collapsed these to
**one producer, many mirrors**. This test suite is the safety net: every
future PR runs these tests, and any new endpoint that wires a fresh
divergent source is caught BEFORE the dashboard turns red.

## Test pattern

For each canonical fact:

1. Fetch every endpoint that exposes it (one HTTP call per session,
   cached in the `snapshots` fixture so a single ground truth is
   compared).
2. Collect the value from every exposure path.
3. `skip` if fewer than 2 paths return a non-None value (the post-fix
   mirror hasn't shipped yet — not a regression).
4. **Otherwise** every non-None value must agree under
   `rtol=0.005, atol=1.0` (numeric) or exact equality (strings).

If a future PR adds a new endpoint with a divergent source, the test
fails fast with the exact pair of values that disagree.

## Facts covered (8 tests)

| # | Fact | Sources tested |
|---|------|----------------|
| 1 | `observed_trades_24h` | `snapshot.observed_trades_24h`, `snapshot.stats.observed_trades_24h`, `snapshot.ingestion.observed_trades_24h`, `pipeline_status.observed_trades_24h` |
| 2 | `exec_trades_24h` | `snapshot.exec_trades_24h`, `snapshot.stats.exec_trades_24h` |
| 3 | `bot_status` (UPPERCASE canonical) | `snapshot.bot.bot_status`, `pipeline_status.bot_status_canonical` |
| 4 | `ws_status` (UPPERCASE canonical) | `snapshot.bot.ws_status`, `pipeline_status.ws_status_canonical` |
| 5 | `ws_last_message_age_s` (REAL WS lag, not book freshness) | `snapshot.ingestion.ws_last_message_age_s`, `pipeline_status.ws_last_message_age_s` |
| 6 | `paper_pnl` (paper bot displayed P&L) | `snapshot.stats.total_pnl`, `inspector/reconciliation.pnl_displayed_sum` |
| 7 | `reconciliation_verdict` | `snapshot.bot.reconciliation.verdict`, `snapshot.reconciliation.verdict`, `inspector/reconciliation.verdict` |
| 8 | `decisions_24h.total` | `ml/diagnostics.decisions_24h.total`, `inspector/reconciliation.decisions.total` |

For tests 3, 4, 7 — the canonical UPPERCASE values must additionally
be in the expected enum (e.g. `bot_status ∈ {RUNNING, STOPPED, DEGRADED}`).

## Running

```bash
# Default — local backend at http://localhost:8000
pytest tests/integration/test_cross_view_consistency.py -v -m integration

# Against prod (or any other deployed instance)
POLYBOT_TEST_BASE_URL=http://89.167.23.215:8080 \
    pytest tests/integration/test_cross_view_consistency.py -v -m integration

# As part of the full integration sweep
pytest tests/integration/ -v -m integration
```

### When the backend isn't reachable

The suite **skips with a clear reason** rather than failing:

```
SKIPPED [9] conftest.py:88: Polymarket backend not reachable at
'http://localhost:8000': connect refused: ... Start it via
`uvicorn src.api.main:app --port 8000` or set POLYBOT_TEST_BASE_URL
to a reachable instance.
```

This keeps CI green for unit-only runs.

### When the post-fix code isn't yet deployed

Each individual test does its own `skip` when fewer than 2 sources
expose the canonical fact. So if you point the suite at a server still
running the pre-fix code, you'll see something like:

```
SKIPPED [...] Only 1 source(s) expose observed_trades_24h
(post-fix code not yet deployed?). Saw: {...}
```

That is the expected behaviour — you're not asserting "post-fix is
correct", you're asserting "if multiple sources DO claim a fact, they
must agree". Once the post-fix code ships, the skips flip to passes
automatically.

## Adding a new fact

When you introduce a new cross-view fact:

1. Open `tests/integration/test_cross_view_consistency.py`.
2. Add a new `test_<fact>_consistent` function following the template
   below.
3. Extend `test_capture_post_fix_baseline` so the new fact is captured
   in the baseline JSON.
4. Document the new fact in this table.

### Template

```python
@pytest.mark.asyncio
async def test_my_new_fact_consistent(snapshots):
    """Doc string — explain WHAT the fact is and WHERE it must agree."""
    snap = _live_summary(snapshots)
    other = _other_endpoint(snapshots)

    val_snap = (snap.get("...") or {}).get("...")
    val_other = other.get("...")

    candidates = {
        "snapshot.path.to.fact": val_snap,
        "other_endpoint.fact": val_other,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose my_new_fact. "
            f"Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"my_new_fact divergent: {reason}"
```

## Baselines

Two baselines live in `tests/baselines/`:

- `2026-05-18_pre_fix.json` — captured BEFORE the 6-batch refactor.
  This is the reference for "what was broken" and is read-only / sacred.
- `2026-05-18_post_fix.json` — captured AFTER the refactor lands in
  production. Re-generated each time
  `test_capture_post_fix_baseline` runs against a deployed backend.

To regenerate the post-fix baseline:

```bash
POLYBOT_TEST_BASE_URL=http://89.167.23.215:8080 \
    pytest tests/integration/test_cross_view_consistency.py::test_capture_post_fix_baseline \
    -v -m integration -s
```

The diff between the two files documents the exact behavioural change
operators can expect on the dashboard.

## CI integration

Add to your CI pipeline (after the backend container is up):

```yaml
- name: cross-view consistency
  run: pytest tests/integration/test_cross_view_consistency.py -v -m integration
  env:
    POLYBOT_TEST_BASE_URL: http://localhost:8000
```

Recommended: gate this on PRs that touch:

- `src/api/queries.py`
- `src/api/terminal_snapshot.py`
- `src/api/snapshot_builder.py`
- `src/api/reconciliation_queries.py`
- `src/api/pillars_queries.py`
- Any new endpoint under `src/api/main.py`

## Limitations

- **Network-only**. The suite doesn't touch DB/Redis directly — it
  trusts what each endpoint returns. This is intentional: it tests the
  WIRING, not the underlying computations (which have their own unit
  tests in `tests/test_api/` and `tests/test_engine/`).
- **Point-in-time**. The `snapshots` fixture fetches every endpoint
  ONCE at session start. A fast-moving counter could theoretically
  drift in the time between two fetches, which is why every numeric
  comparison uses a tolerance.
- **No write-side coverage**. POST endpoints
  (`/api/control/halt`, `/api/risk/update`) are out of scope —
  they're tested in `tests/test_api/`.
