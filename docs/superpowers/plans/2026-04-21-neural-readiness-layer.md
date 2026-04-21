# Neural Readiness Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first usable V1 Neural Readiness Layer: backend contracts, readiness calculations, `/api/neural-readiness`, dashboard wiring, and persistence schema for future state transitions.

**Architecture:** Add a focused `src/engine/neural_readiness.py` module that owns state enums, readiness caps, belief stability, and API snapshot assembly from existing health/risk/activation data. Expose it through `src/api/main.py`, keep persistence optional through a migration, and render the five V1 progress bars in the existing `Neural System` tab.

**Tech Stack:** Python dataclasses/enums, FastAPI, asyncpg-style query wrappers, Redis health metrics, pytest, vanilla HTML/CSS/JS dashboard.

---

## File Structure

- Create `src/engine/neural_readiness.py`: state enums, dataclasses, readiness math, API snapshot builder.
- Create `tests/test_engine/test_neural_readiness.py`: unit coverage for caps, transitions, oscillation and schema shape.
- Modify `src/api/main.py`: add `/api/neural-readiness`.
- Modify `tests/test_api/test_endpoints.py`: HTTP coverage for the new endpoint and dashboard fetch.
- Modify `templates/dashboard.html`: add Neural Readiness UI and JS renderer, stabilize missing system DOM ids.
- Create `docs/migrations/005_neural_readiness.sql`: tables for future persisted states, transitions, and book quality snapshots.

## Task 1: Backend Neural Readiness Contracts

- [x] Write failing tests in `tests/test_engine/test_neural_readiness.py`:

```python
from src.engine.neural_readiness import (
    DecisionState,
    ReadinessInputs,
    StrategyTrack,
    build_neural_readiness_snapshot,
)


def test_missing_fee_caps_first_position_readiness():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={"fee_snapshot_coverage_pct": None, "token_map_coverage_pct": 100.0},
            activation=[],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )
    assert snapshot["global"]["bars"]["first_position_readiness_pct"] <= 50
    assert "missing_fee_snapshot" in snapshot["global"]["blockers"]


def test_micro_reactive_is_capped_without_book_freshness():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={"book_age_p95_s": None, "fee_snapshot_coverage_pct": 100.0, "token_map_coverage_pct": 100.0},
            activation=[],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )
    assert snapshot["tracks"]["micro_reactive"]["bars"]["data_accumulation_pct"] <= 40
    assert "missing_book_freshness" in snapshot["tracks"]["micro_reactive"]["blockers"]


def test_candidate_market_reaches_candidate_state_from_activation_queue():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={"book_age_p95_s": 3.0, "fee_snapshot_coverage_pct": 100.0, "token_map_coverage_pct": 100.0},
            activation=[{"wallet_address": "0xabc", "follow_readiness_pct": 82, "fade_readiness_pct": 35}],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )
    assert snapshot["markets"][0]["state"] == DecisionState.CANDIDATE_SIGNAL.value
    assert snapshot["tracks"][StrategyTrack.LEADER_SWING.value]["top_candidates"]
```

- [x] Run `python -m pytest tests/test_engine/test_neural_readiness.py -q`; expected failure is `ModuleNotFoundError` or missing symbols.
- [x] Implement `src/engine/neural_readiness.py` with the imported API and deterministic readiness calculations.
- [x] Re-run `python -m pytest tests/test_engine/test_neural_readiness.py -q`; expected pass.

## Task 2: API Endpoint

- [x] Add failing HTTP test in `tests/test_api/test_endpoints.py`:

```python
class TestNeuralReadiness:
    def test_returns_neural_readiness_contract(self, app_client):
        resp = app_client.get("/api/neural-readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) >= {"global", "tracks", "markets", "transitions"}
        assert "leader_swing" in data["tracks"]
        assert "micro_reactive" in data["tracks"]
        assert "data_accumulation_pct" in data["global"]["bars"]
```

- [x] Run `python -m pytest tests/test_api/test_endpoints.py::TestNeuralReadiness -q`; expected 404 before endpoint exists.
- [x] Add `api_neural_readiness()` to `src/api/main.py`, gathering health, activation, risk and ml snapshots.
- [x] Re-run `python -m pytest tests/test_api/test_endpoints.py::TestNeuralReadiness -q`; expected pass.

## Task 3: Persistence Migration

- [x] Create `docs/migrations/005_neural_readiness.sql` with `market_belief_states`, `decision_state_transitions`, and `book_quality_snapshots`.
- [x] Include unique indexes for `(market_id, strategy_track)` and recent transition lookup.
- [x] Keep migration additive only; no existing table rewrite.

## Task 4: Dashboard Wiring

- [x] Add dashboard test to `tests/test_api/test_endpoints.py`:

```python
def test_dashboard_fetches_neural_readiness(self, app_client):
    resp = app_client.get("/")
    assert "/api/neural-readiness" in resp.text
    assert "neural-global-bars" in resp.text
```

- [x] Run the test; expected failure before HTML changes.
- [x] Add Neural Readiness markup to `templates/dashboard.html` under `tab-system`.
- [x] Add `renderNeuralReadiness(neural)` and include `/api/neural-readiness` in `loadAll()`.
- [x] Add null-safe DOM helper or missing containers for existing `renderSystem()` references so the tab does not crash.
- [x] Re-run the dashboard test; expected pass.

## Task 5: Verification

- [x] Run `python -m pytest tests/test_engine/test_neural_readiness.py tests/test_api/test_endpoints.py tests/test_api/test_data_quality_health.py -q`.
- [x] Run `git status --short`.
- [x] Commit implementation if tests pass.
