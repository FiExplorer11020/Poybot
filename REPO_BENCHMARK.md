# Repo Benchmark and Recovery Plan

## 1. Mission
Stabilize and incrementally improve the existing Polymarket trading bot/platform without replacing its useful structure.

This repo should be handled as a fragile but valuable system. Every future change should be reviewed against:
- the intended product objective,
- current runtime truth,
- integration impact,
- validation coverage,
- cleanup side effects.

## 2. Benchmark Product Objective
The intended product is a Polymarket market-intelligence and trading platform that:
- acquires live binary-market data from Polymarket,
- normalizes and serves that data reliably,
- computes strategy signals from executable market conditions,
- sizes positions under portfolio controls,
- simulates or routes execution,
- exposes live state and controls in a frontend dashboard,
- supports validation and backtesting that are meaningfully connected to runtime behavior.

## 3. Current Repo Reality

### Backend runtime path
- `backend/app/main.py`
  - FastAPI entrypoint, startup/shutdown, `/ws/live`, REST routers.
- `backend/app/live/state.py`
  - central live runtime hub used by the UI and bot control paths.
- `backend/app/services/adaptive_strategy.py`
  - current signal/risk/sizing logic.
- `backend/app/services/trade_executor.py`
  - dry-run vs live execution bridge.
- `backend/app/api/v1/live_routes.py`
  - live summary, control, config, execute, close-position routes.

### Data/ingestion path
- `backend/app/clients/gamma.py`
  - Gamma market metadata client.
- `backend/app/clients/clob.py`
  - CLOB REST client.
- `backend/app/ingestion/universe.py`
  - active market universe builder.
- `backend/app/ingestion/stream_manager.py`
  - token sharding plan for large subscription sets.
- `backend/app/ingestion/ws_ingestor.py`
  - separate repository-backed websocket ingestion path.

### Database/repository path
- `backend/app/models/*`
  - SQLAlchemy models for events, markets, trades, books, snapshots.
- `backend/app/repositories/*`
  - persistence and query layer.
- `backend/app/services/market_sync_service.py`
  - metadata sync service.

### Frontend path
- `frontend/app/*`
  - dashboard and pages.
- `frontend/components/layout/LiveSocketBridge.tsx`
  - live summary bootstrap + websocket integration.
- `frontend/store/useBotStore.ts`
  - frontend runtime state normalization and storage.
- `frontend/components/trading/*`
  - dashboard scanner, risk sliders, trade table, wallet scaffold.

## 4. High-Confidence Findings

### Intended architecture exists, but the repo has drift
- The repo is not greenfield.
- There is a usable backend/frontend/runtime skeleton.
- The instability mainly comes from drift between:
  - spec vs implementation,
  - backend payloads vs frontend assumptions,
  - live runtime vs backtest assumptions,
  - security/tests vs actual runtime policy.

### The financial engine is still a prototype
- The current signal model is a single-market momentum heuristic, not true arbitrage logic.
- `TRADING_SPEC.md` and implementation are already partially out of sync.
- Synthetic fallback quotes and synthetic backtesting can create false confidence.

### The live hub is currently the operational truth
- The frontend relies on `live_hub.snapshot()` and websocket events.
- Any change touching live bot behavior must treat `backend/app/live/state.py` as a central contract file.

### Validation is too thin for a fragile system
- Current tests cover basic routes and unit logic.
- They do not adequately protect:
  - startup degradation,
  - config contract changes,
  - trade lifecycle changes,
  - websocket payload semantics,
  - frontend/backend live contract drift.

## 5. Module Classification

### Keep as core runtime
- `backend/app/main.py`
- `backend/app/live/state.py`
- `backend/app/services/adaptive_strategy.py`
- `backend/app/services/trade_executor.py`
- `backend/app/api/v1/live_routes.py`
- `frontend/store/useBotStore.ts`
- `frontend/components/layout/LiveSocketBridge.tsx`

### Keep but refactor carefully
- `backend/TRADING_SPEC.md`
  - good target document, but must be realigned with runtime truth.
- `backend/scripts/backtest.py`
  - useful only as a toy sanity script today; should evolve into replay-based validation.
- `backend/app/ingestion/ws_ingestor.py`
  - valuable as a persistent ingestion path, but currently separate from the live hub path.
- `frontend/lib/types.ts`
  - should become the shared frontend contract source, aligned with backend payloads.

### Isolate or de-emphasize until aligned
- fallback quote synthesis inside `backend/app/live/state.py`
  - analysis-friendly, but should not be confused with executable book truth.
- wallet scaffold files
  - useful UI scaffolding, but not the repo’s current stability bottleneck.

### Cleanup candidates
- generated artifacts and local runtime files should stay out of repo reasoning:
  - `frontend/.next/`
  - `frontend/node_modules/`
  - `frontend/frontend.log`
  - `frontend/tsconfig.tsbuildinfo`
  - `backend/poybot.db`
  - `backend/poybot_backend.egg-info/`
- mixed-language or drifted docs should be treated as documentation debt, not architecture truth.

## 6. Immediate Failure Points
1. Spec drift between `backend/TRADING_SPEC.md` and runtime logic.
2. No single typed live payload contract shared across backend and frontend.
3. Startup and degradation behavior are not strongly validated.
4. Security policy and tests are inconsistent around websocket auth.
5. Backtest script is disconnected from actual live runtime semantics.
6. Synthetic fallback pricing can be mistaken for tradable reality.

## 7. Required Review Framework
Every meaningful change should be reviewed through these lenses:

### Orchestrator
- Does the change improve repo stability and product coherence?

### Quant / Strategy
- Does it improve financially meaningful behavior, or only UI/engineering appearance?

### Data / Pipeline
- Does it improve data truth, freshness, schema coherence, and service reliability?

### Reliability / Security
- Does it reduce regressions, startup fragility, and hidden integration failure?

### Frontend Alignment
- Does the UI reflect real backend truth rather than stale assumptions?

### Backtest / Validation
- Is there a practical verification path showing the change is safe?

## 8. Mandatory Change Loop
For future edits:
1. Identify affected runtime contract files.
2. Compare intended behavior vs actual behavior.
3. Apply the minimum coherent fix.
4. Check related frontend/backend/spec/test files.
5. Run validation.
6. Record what changed, what remains risky, and what should be reviewed next.

## 9. Immediate Stabilization Priorities

### P0
- Align `TRADING_SPEC.md` with current runtime or explicitly mark current runtime as a prototype mode.
- Introduce a single live payload contract shared by:
  - `backend/app/live/state.py`
  - `backend/app/api/v1/live_routes.py`
  - `frontend/store/useBotStore.ts`
  - `frontend/lib/types.ts`
- Add tests for:
  - `POST /api/v1/strategy/config`
  - `POST /api/v1/trades/{trade_id}/close`
  - startup degradation of the live hub
  - websocket/live payload semantics

### P1
- Decide the financial identity of the bot:
  - momentum scanner,
  - complement-dislocation scanner,
  - or true arbitrage engine.
- Replace or explicitly downgrade synthetic fallback price assumptions.
- Separate scanner logic from exit/execution policy inside the live loop.

### P2
- Replace synthetic backtest confidence with recorded quote replay or a runtime-connected smoke validator.
- Add event-level and related-market exposure controls.
- Clean or archive obsolete/confusing docs and experimental code paths once replacements are in place.

## 10. Validation Surface To Preserve

### Minimum validation after runtime changes
- `cd backend && PYTHONPATH=. python -m py_compile app/main.py app/live/state.py app/api/v1/live_routes.py app/services/adaptive_strategy.py`
- `cd frontend && npm run lint`

### Minimum validation after route/contract changes
- backend live route tests
- frontend typecheck
- a smoke check that `/api/v1/live-summary` still loads and the UI can bootstrap from it

## 11. Repo Continuity Rules
- Do not replace existing modules without first mapping their downstream consumers.
- Do not leave spec drift unresolved after behavior changes.
- Do not add new strategy ideas until runtime truth, validation, and product identity are clearer.
- Prefer incremental refactors over sweeping rewrites.
- Treat generated files and local logs as noise, not architecture.

## 12. What To Review Next
1. `backend/TRADING_SPEC.md` against `backend/app/services/adaptive_strategy.py`
2. live payload schema across backend/frontend
3. websocket auth policy vs security tests
4. `backend/scripts/backtest.py` vs actual live runtime assumptions
5. generated/local artifacts and stale docs that increase repo confusion
