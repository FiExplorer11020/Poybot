# Audit 05 — ML Data-Pipeline (Model ↔ Data Interface)

Auditor: ML data-pipeline auditor
Scope: how features flow from raw observations into every model the bot trains
or samples from, and where freshness, leakage, eval, or coverage gaps cap
accuracy. Written in support of the "evolved form" goal — smarter models,
fresher inputs, tighter training cadence.

Cross-references: `polymarket-bot/CLAUDE.md` §7 (statistical models),
`src/profiler/CLAUDE.md`, `src/graph/CLAUDE.md`, `src/engine/CLAUDE.md`,
`src/observer/CLAUDE.md`, `src/registry/CLAUDE.md`.

---

## 1. Per-model data dependency table

For every component, "Refresh cadence" is the freshness of the upstream
*signal that actually changes the model state*, NOT the I/O frequency.

### 1.1 `behavior_profiler.py` — Dirichlet / EWMA / KDE / accuracy Beta

| Model element | Source table / Redis key | Refresh cadence | Update path | Staleness window |
|---|---|---|---|---|
| Dirichlet `α_category` (size-weighted) | event from Redis `positions:closed` ; persisted to `leader_profiles.profile_json.preferred_categories` | per closed position | online, O(1) (`_update_dirichlet`, `behavior_profiler.py:891`) | bounded by close-detection lag (often hours: SELLs not always seen the second they fire; merge-exit detection windowed at 600 s in `position_tracker.py:21`) |
| EWMA position size `μ_size_ewma`, λ=0.94 | `leader_profiles.profile_json.sizing.ewma_size` | per closed position (`_update_sizing`, `behavior_profiler.py:911`) | online, O(1), runs BEFORE the size-weighted posteriors so the same trade's weight is consistent | same as Dirichlet |
| Per-category accuracy Beta `(beta_a, beta_b)`, size-weighted | `leader_profiles.profile_json.accuracy.by_category[cat]` | per closed position (`_update_accuracy`, `behavior_profiler.py:940`) | online, O(1) | same as Dirichlet |
| `decision_process` order-flow stats (flip_rate, scale_in_rate, EWMA interarrival, process_score_ewma, category_counts/last_seen) | `trades:observed` Redis pub/sub → `leader_profiles.profile_json.decision_process` (`behavior_profiler.py:206` → `_update_decision_process`, `behavior_profiler.py:1146`) | per leader trade (no need to wait for resolution) | online, O(1) | bounded by trade-observer lag (≤30 s for `api_market`/`api_wallet` polls; WS messages do not carry wallet, see §2 MG-2) |
| `decision_learning.{follow,fade}` Beta + reason_stats | written by `record_decision_outcome` (`behavior_profiler.py:253`) on paper-trade close | per paper-trade close | online, O(1) | bounded by paper close detection (leader_exit detection, market_resolved detection) |
| `loss_analysis.recent_losses[]` (rolling 25) | written on paper-trade close + position close | per close | online | same as above |
| `_compute_maturity` (resolved/100 × followers/5) | `leader_profiles.positions_resolved` × `follower_edges` count where `co_occurrences>=5` AND `same_direction_rate>=0.7` | every position close (`behavior_profiler.py:182`) | online | trades count comes from a `SELECT COUNT(*) FROM trades_observed` — bounded by trade ingestion lag |
| **KDE for time-of-day distribution** | DOCUMENTED in `src/profiler/CLAUDE.md:56-62` ("Update weekly (batch job)") | **NOT IMPLEMENTED** — see §2 MG-1 | n/a | n/a |

### 1.2 `error_model.py` — 3-phase progression

Feature vector built in `_build_features` (`error_model.py:211`). 18 floats:
`[cat_code, is_contrarian, deviation_score, size_ratio, liquidity_score,
process_score, flip_rate, scale_in_rate, hours_since_last_trade,
hours_since_category_last_trade, hours_since_last_loss, category_accuracy,
profile_maturity, confirmed_followers, hour_sin, hour_cos, dow_sin, dow_cos]`.

| Phase | Trigger (current effective settings) | Model | Per-feature provenance | Refresh cadence | Staleness |
|---|---|---|---|---|---|
| **1** Beta-Binomial per-category | `< 30` resolved (was 100, lowered in `config.py:93` for cold start) | `_predict_phase1` reads `accuracy.by_category[cat].beta_a/b` | needs only `category` from `markets.category` and the leader's `profile_json.accuracy` | per resolved position | bounded by close detection (same as profiler) |
| **2** BayesianRidge | `[30, 150)` resolved | `sklearn.linear_model.BayesianRidge` fitted in `_upgrade_phase` (`error_model.py:319`) | uses ALL 18 features. `liquidity_score` from `markets.liquidity_score` (refreshed at most every 24h via `sync_markets`); `recent_avg_price` (used to derive `is_contrarian`) from `trades_observed` ≤ trade time | nightly batch via `step_refit_error_models` (`scripts/batch_runner.py:76`) AND time-gated via `_retrain_if_needed` (`error_model.py:355`, refit every 24h) | features are only as fresh as `markets.liquidity_score` (24h cadence at best — see §2 MG-3) and the rolling profile — no online retrain |
| **3** LightGBM + Platt | `≥ 150` resolved (was 500, lowered in `config.py:94`) | `LGBMClassifier(n_estimators=50, max_depth=3)` wrapped in `CalibratedClassifierCV(method='sigmoid', cv='prefit')` (`error_model.py:335-341`) | same 18 features | nightly batch + 7-day refit interval (`error_model.py:698-700`) | 7 days. Hyperparameters static (`n_estimators=50, max_depth=3`) — no search |
| CUSUM drift | computed in `update()` after every closed position when phase ≥ 2 (`error_model.py:131-146`) | `S = max(0, S + |p_pred - actual| - 0.15 - 0.05)` ; threshold 2.0 | uses `pred.p_error` from `predict()` and the actual loss flag | per close | in-memory `_cusum_state` dict ALSO mirrored to `profile_json.error_model_runtime.cusum_state` (`error_model.py:138-141`) — survives restart but dual-store creates risk of desync (the in-memory dict is the authoritative read, see §2 MG-7) |

### 1.3 `graph_engine.py` — Beta-Binomial follower edges

| Element | Source | Refresh cadence | Update path | Staleness |
|---|---|---|---|---|
| `co_occurrences`, `follow_beta_a/b`, `same_direction_rate`, `avg_delay_s` (EWMA), `follow_probability` | Redis pub/sub `trades:observed` (`graph_engine.py:39-54`); written to `follower_edges` | per leader/follower trade pair within 300 s window | online, O(1) UPSERT (`_update_edge`, `graph_engine.py:207`) | bounded by trade-observer lag. Hot path uses an in-memory `deque(maxlen=1000)` per market that is warm-started from the last 1200 s of `trades_observed` on startup (`_hydrate_recent_trades`, `graph_engine.py:56`). Loses any unflushed state on crash. |
| `trapped_rate` | DOCUMENTED in `src/graph/CLAUDE.md` (line 86) and master `CLAUDE.md` §6 schema (column exists) | **NEVER WRITTEN** — see §2 MG-4 | n/a | column stays NULL |

### 1.4 `hawkes_fitter.py` — Hawkes MLE

| Element | Source | Refresh cadence | Update path | Staleness |
|---|---|---|---|---|
| `hawkes_alpha_mu` per edge | `trades_observed` time series (last 30 days) for both `leader_wallet` and `follower_wallet`, fitted in `fit_edge` (`hawkes_fitter.py:62`) | nightly only via `step_refit_hawkes` → `run_batch` (`hawkes_fitter.py:145`) — capped at `BATCH_HAWKES_LEADERS=200` confirmed edges | batch. scipy L-BFGS-B with 5 random restarts | up to 24h old. Fitted on the FOLLOWER's own marginal time series (univariate self-exciting), NOT on the (leader→follower) bivariate / cross-excitation process — the alpha/mu printed in the schema is therefore an upper bound, not a true causal coupling. See §2 MG-5. |

### 1.5 `confidence_engine.py` — Thompson sampling + Bayesian Kelly

| Element | Source | Refresh cadence | Update path | Staleness |
|---|---|---|---|---|
| `(α_follow, β_follow)`, `(α_fade, β_fade)` per wallet | in-memory `self._thompson` dict, seeded from Redis cache (`CACHE_PREFIX=confidence:leader:{wallet}`) or from `leader_profiles.profile_json.decision_learning` | seeded at first sample (`_seed_thompson_from_cache` then `_seed_thompson_from_profile`, `confidence_engine.py:177`); updated on paper-trade close via `update_thompson` (`confidence_engine.py:415`) | online for in-memory; persisted only at paper close + via nightly `precompute_redis_cache` (`confidence_engine.py:712`) | Redis cache TTL = `max(3600, FALCON_CACHE_TTL_S=172800)` ≈ 48 h. Process restart that pre-empts the next nightly batch keeps reading 48h-old Beta state from cache — **Thompson update lost between restart and next batch unless paper close already happened** — see §2 MG-6. |
| `trade_context` features for downstream error model | computed live in `_build_trade_context` (`confidence_engine.py:574`) per leader trade. DB lookups: `markets` for `category`+`liquidity_score`; `leader_profiles` for `profile_maturity`; last-10-trade avg price for `is_contrarian` | per trade — synchronous DB reads on the hot path | inline | features always at most ~30 s old, **except `liquidity_score`** which is at best 24h stale (see MG-3). |
| `kelly_fraction` shrinkage | computed from `(α, β)` of the chosen action; uses Beta variance | per trade | inline | inherits any staleness in α/β |
| `process_score` penalty (`<0.25` ⇒ SKIP, contributes to `context_penalty`) | `profile.decision_process.process_score_ewma` updated per leader trade | per leader trade | online | very fresh (≤ trade-observer lag) |
| `p_error` from error model | called in `evaluate()` (`confidence_engine.py:213`) | per trade | inline call to `ErrorModel.predict()` | fresh prediction over potentially stale model + stale features |

### 1.6 `neural_readiness.py` — readiness scoring

Pure function `build_neural_readiness_snapshot` (`neural_readiness.py:262`). No
model state of its own. Inputs:

| Bar | Inputs | Provenance | Cadence |
|---|---|---|---|
| `data_accumulation_pct` | `health.fee_snapshot_coverage_pct`, `health.token_map_coverage_pct`, `health.book_age_p95_s`, max activation_pct | API `terminal_snapshot` queries (`api/queries.py`) over `book_quality_snapshots`, `markets`, runtime metrics | computed on every `/api/terminal/snapshot` (1 s TTL cache) |
| `first_position_readiness_pct`, `belief_stability_pct`, `portfolio_accumulation_pct` | activation, fee/token coverage, book score, drawdown_pct, ml.{drift_alerts, follow.samples, fade.samples, win_rate} | aggregated from `leader_profiles`, `paper_trades`, `portfolio_state` | same |
| `v1_go_no_go_pct` | weighted blend, hard-capped at 35 if any of `missing_fee_snapshot / missing_token_map / missing_book_freshness / stale_book / risk_drawdown_high` is in blockers | same | same |

This file is deterministic and stateless ("intentionally a control plane and
gate explainer, not a hidden trading model" — line 5-7). It has no training
loop, so it does not directly belong to the ML pipeline; it inherits all its
freshness from the underlying tables. **It does not consume the error model's
predicted probability or the Thompson posterior at all** — that is a missed
opportunity for the dashboard story (see §5).

---

## 2. Freshness gaps

### MG-1: KDE timing model is documented but never implemented
- **Model**: `behavior_profiler.py` time-of-day KDE
- **Required freshness**: weekly refit (per profiler/CLAUDE.md:62)
- **Actual freshness**: NEVER — searched for `gaussian_kde`, `scipy.stats.gaussian_kde`, `kde` across `src/`; only doc references exist (`src/profiler/CLAUDE.md:14, 56`)
- **Impact**: the `cyclical_time_features` (hour_sin/cos, dow_sin/cos) the error model receives at `error_model.py:262-265` are raw clock features. There is no per-leader timing density. A leader who trades exclusively in the 06:00–10:00 UTC window looks identical to a 24/7 trader to the model. Misses an obvious "out-of-character timing → likely error" signal.
- **Cause**: stub never landed
- **Fix**: add `_update_timing_kde(profile, trade_time)` on every leader trade — store `(hour, dow)` samples (rolling 200) and an EWMA over a 24-bin histogram; expose `time_anomaly_score = 1 - density(current_hour) / max_density` as a feature in `_build_error_trade_context` (`behavior_profiler.py:607`). No scipy fit needed at trade-time — the histogram is the KDE's lazy approximation.
- **Effort**: S

### MG-2: Trade attribution lag is 30 s minimum (no realtime wallet stream)
- **Model**: every model that depends on `trades:observed` (graph_engine, behavior_profiler, confidence_engine)
- **Required freshness**: per trade. Confidence engine is sensitive — `LIVE_DECISION_MAX_TRADE_AGE_S=120` (`config.py:108`), so a leader trade older than 2 min is dropped at `confidence_engine.py:142`.
- **Actual freshness**: 30 s polling on `data-api.polymarket.com/trades` (`config.py:59`, `trade_observer.py:606`). The CLOB WS market channel (`trade_observer.py:355` and observer/CLAUDE.md:18-21) explicitly does NOT carry wallet addresses, so WS messages cannot be wallet-attributed in real time.
- **Impact**: median attribution lag is ~15 s and p99 is 30+ s plus any data-api propagation delay. Combined with the 120 s SKIP gate, a meaningful tail of leader signals is dropped (paper trades that "almost made it" are silently lost). Hawkes also degrades — the cross-excitation kernel decay timescale is seconds-to-minutes, but the time series we feed it has 30 s quantization noise.
- **Cause**: data-api is REST, not streaming. No feed available that broadcasts (wallet, market, trade) in real time without authenticating each wallet's user channel.
- **Fix**: subscribe to per-wallet authenticated USER channels for the top ~50 highest-value leaders (CLOB WS supports `user` channel). Drops attribution lag to <1 s for those wallets. Also: drop polling interval to 10 s for the per-wallet REST endpoint (cheap — N≤200) and keep market-wide polling at 30 s.
- **Effort**: M

### MG-3: `liquidity_score` is at best 24h fresh and is stamped from agent 574 not agent 575
- **Model**: error model phase 2/3 (feature `[4]`); confidence engine signal gating (the `LIVE_FILTER` conditions don't currently use it but they should)
- **Required freshness**: minutes — liquidity moves intraday around news events
- **Actual freshness**: written by `sync_markets` (`leader_registry.py:289`) which runs once per registry cycle (`FALCON_REFRESH_INTERVAL_S=1800` = 30 min) but `sync_markets` itself only re-fetches markets whose `updated_at` is > 24h old (line 305-308). So a market refreshed once will not be refreshed again for 24 h, regardless of how stale its liquidity is.
- **Also**: docs (`profiler/CLAUDE.md:172`, `error_model.py:83/220`, master `CLAUDE.md:160`) consistently claim `liquidity_score` comes from Falcon agent 575 (Market Insights). It actually comes from agent 574 field `liquidity` (`leader_registry.py:348` — `m.get("liquidity")`). Agent 575 is **never called anywhere in `src/`**. The scoring methodology in agent 575 (concentration, depth, trend) is therefore never reaching the model.
- **Impact**: Phase 2/3 model is reading a feature that, on average, lags reality by 12 h and is methodologically the wrong field. Predictions for high-volatility markets (sudden liquidity drains around news) are systematically blind.
- **Cause**: agent 575 integration was scoped but never landed; the doc/code drifted.
- **Fix**: (a) wire agent 575 in `falcon_client.py` and replace the `liquidity` field write at `leader_registry.py:348` with a normalized 0–1 score from agent 575; (b) drop the 24h staleness gate inside `sync_markets` to 1h for active markets (`end_date > NOW() + 24h`); (c) optionally store `liquidity_score` in a small TimescaleDB-style `market_liquidity_history(market_id, ts, score)` to feed time-series features in Phase 3.
- **Effort**: M

### MG-4: `trapped_rate` schema column is documented + reserved but never populated
- **Model**: graph_engine (would feed FADE confidence — "follower trapped at exit" is a strong fade signal)
- **Required freshness**: per leader-close event
- **Actual freshness**: NULL forever. `master CLAUDE.md` §6 line 246 specifies the column; `src/graph/CLAUDE.md` line 87 ("Trapped Rate") describes the formula; `graph_engine.py` never writes it. No `UPDATE follower_edges SET trapped_rate` exists in the codebase.
- **Impact**: a strong, low-cost FADE signal ("the leader exits, followers are still in, price will overshoot down") is unavailable to the confidence engine. FADE today only uses error_model p_error and Thompson posterior.
- **Cause**: dropped scope from the original cold-path implementation.
- **Fix**: add a hook in `position_tracker._close_position` that for each confirmed edge of the closing leader, COUNTs the followers with an open position in the same `(market_id, token_id)` and increments two counters per edge (`closes_seen`, `trapped_count`); persist `trapped_rate = trapped_count / closes_seen` to `follower_edges`. Then add it to confidence_engine `trade_context` and to the error model feature vector (`_build_features` slot 18+).
- **Effort**: S

### MG-5: Hawkes fit is univariate, not bivariate
- **Model**: graph_engine confirmation pipeline
- **Required behavior**: `λ_F(t) = μ_F + α_FL Σ_{leader trades} exp(-β·(t - t_i))` — i.e. follower's intensity excited BY THE LEADER's history.
- **Actual behavior**: `hawkes_fitter.py:101-104` extracts the FOLLOWER's own timestamps (`follower_times`) and fits a univariate self-exciting process on them. The leader timestamps are fetched (`leader_times` at line 81) but never used. `alpha_mu_ratio` reflects how clustered the follower's own trades are, NOT how strongly leader trades excite follower trades.
- **Impact**: every confirmed edge whose follower happens to be a clustered/burst trader will look "causal" even with zero relation to the leader. False-positive rate on edge confirmation is unconstrained.
- **Cause**: implementation shortcut — true bivariate Hawkes MLE is a small step harder.
- **Fix**: switch to bivariate. Use `tick.hawkes.HawkesExpKernEstimator` or a custom log-likelihood with two intensities; the cross-excitation parameter `α_FL / μ_F` is what we want. As a stop-gap that does not pull in `tick`, restrict the leader timestamp set to the 5-minute window before each follower timestamp and compute Granger-style cross-correlation strength via permutation test.
- **Effort**: L for full bivariate, M for the cross-correlation stop-gap.

### MG-6: Thompson posteriors can be lost on engine restart (Redis cache acts as authoritative store between batches)
- **Model**: confidence_engine
- **Required behavior**: every paper-trade outcome must durably increment `α/β`.
- **Actual behavior**: `update_thompson` (`confidence_engine.py:415`) updates only the in-memory dict. Persistence to `decision_learning` happens via `record_decision_outcome` → `_save_profile` (`behavior_profiler.py:307`) — that path is fine. BUT: at next process restart, the seed comes from the Redis cache (`_seed_thompson_from_cache`, `confidence_engine.py:538`) BEFORE falling back to the DB. The Redis cache is rewritten only by `precompute_redis_cache` (nightly, `batch_runner.py:101`). If a restart happens after the nightly batch but before the next paper close, Thompson reads from Redis (which reflects last-night's profile) — fine. If a restart happens AFTER several paper closes have updated `decision_learning` in the DB but BEFORE the next nightly precompute, **the Redis cache is stale and the engine will Thompson-sample from yesterday's Beta until either a new paper close arrives for that wallet or the next nightly batch runs.**
- **Impact**: small but non-zero — most active leaders see ≥1 paper close per day. Still, the inversion of authority (cache > DB on cold start) is an architectural smell.
- **Cause**: cache was added for hot-path speed, but the seeding precedence wasn't re-checked.
- **Fix**: in `_seed_thompson_from_cache`, also fetch `last_updated` from the cache payload and compare to `leader_profiles.last_updated`; if DB is newer, fall through to `_seed_thompson_from_profile`. Cheaper alternative: invalidate the cache key on every `record_decision_outcome` write.
- **Effort**: S

### MG-7: CUSUM state is dual-stored with stale read precedence
- **Model**: error_model drift detection
- **Required behavior**: `cusum_state` is per-wallet running sum, must be persistent.
- **Actual behavior**: `error_model.py:67-70` keeps `self._cusum_state: dict[str, float]` in memory. On `_load_state` (line 393), it `setdefault`s the in-memory value from `profile_json.error_model_runtime.cusum_state` only if the key is not already present. After a restart that clears the in-memory dict, the first call repopulates from the DB; subsequent updates write back to BOTH stores. The `setdefault` semantics mean: if two ErrorModel instances ever ran in parallel (they shouldn't, but `batch_runner.py:104` constructs a fresh one), they race on the writeback.
- **Impact**: low in practice. A clean architectural fix is cheap.
- **Fix**: drop the in-memory dict; use `profile_json.error_model_runtime.cusum_state` as the only source of truth; load fresh on every `predict`/`update`.
- **Effort**: S

### MG-8: Behavior profile depends on positions resolution detection, which is itself slow
- **Model**: error_model phase 1 + accuracy Beta
- **Required freshness**: per close
- **Actual freshness**: a position closes via SELL, MERGE (10-min window in `position_tracker.py:21`), or RESOLUTION. Resolution events are not subscribed to anywhere — a market that resolves on Polymarket only updates `positions_reconstructed.close_method='resolution'` if a manual reconciliation runs. Search confirms there is no `step_resolve_open_positions` step in `batch_runner.py`. So a market that resolves with a leader still holding ends up with `close_method=NULL` and never feeds the profiler.
- **Impact**: the error model's training data is biased toward leaders who exit via SELL or MERGE; "hold to resolution" outcomes (a non-trivial fraction even for swing traders) are dropped — both wins AND losses, so direction of bias is hard to predict, but variance is real.
- **Cause**: no scheduled resolution-reconciliation job. Master `CLAUDE.md` §16 changelog notes `sync_markets` skips expired markets but does NOT close their positions.
- **Fix**: add a nightly `step_resolve_open_positions` that for every `markets.end_date < NOW()` market, looks up the resolution outcome via Gamma API and closes any open `positions_reconstructed` rows accordingly with `close_method='resolution'` and the appropriate `pnl_usdc`.
- **Effort**: M

---

## 3. Training-data quality issues

### 3.1 Train/serve skew on `is_contrarian`, `process_score`, `confirmed_followers` — partial leakage during training reconstruction

`error_model._fetch_training_data` (`error_model.py:463`) reconstructs the
profile state at each historical position's `open_time` by replaying observed
trades up to that timestamp (`error_model.py:570-585`). Good intent, BUT:

- `is_contrarian` is computed from `AVG(price)` over the **last 10 trades_observed before pr.open_time** (line 491-499). This is correct point-in-time. ✓
- `process_score`, `flip_rate`, `scale_in_rate` come from `_compute_process_insights(rolling_profile, trade)` where `rolling_profile` was built only from `trades_observed WHERE wallet_address = $1 AND is_leader = TRUE` (line 535-539). The flag `is_leader` itself is set at trade-ingestion time based on the CURRENT `leaders` table state — a wallet that became a leader on 2026-04-01 will have `is_leader=TRUE` for trades on 2026-02-15 too once the registry first marks them. **This is leakage**: at the historical timestamp, the wallet was not yet known to be a leader, so its trades would not have been routed through the same processing path. The `confirmed_followers` count at line 610 has the same issue — it counts edges with `first_observed <= open_time`, which is fine, but the underlying `follower_edges` table only exists for wallets that were leaders at SOME point.
- `liquidity_score` for the historical row is fetched from `markets.liquidity_score` AS OF NOW (`error_model.py:488`), not as of `open_time`. **Direct leakage of post-trade information.** A market that became liquid two weeks after the leader entered will look liquid in training but was illiquid at decision time.
- `category_accuracy` and `_get_category_accuracy(rolling_profile, ...)` is fed AFTER `_update_accuracy` is called for the prior rows — the rolling profile gets the label for the PREVIOUS row updated before the NEXT row's features are built (line 644-651), which is correct. ✓ ordering.

Net: at minimum `liquidity_score` and the `is_leader` flag carry future
information into training. This is the classic train/serve skew killer.

**Fix**:
1. Snapshot `liquidity_score` per `(market_id, hour_bucket)` to a small history table (`market_liquidity_history`) — already proposed in MG-3. Reconstructions then read the closest snapshot ≤ `open_time`.
2. Persist `is_leader_at_time(wallet, ts) → bool` (or use `leader_first_seen` per wallet and gate on it). Easier alternative: for training, **only include positions whose `open_time > leaders.first_seen`** for that wallet.

### 3.2 Class imbalance for the error model is unhandled

Targets in `error_model._fetch_training_data` (line 644): `y = 1 if pnl<0 else 0`.
Polymarket leaders are leaders precisely because they win more than they lose — the
typical `wins/losses` ratio for a watchlisted wallet is 60/40 to 70/30 (visible
in `leader_profiles.profile_json.accuracy.overall` aggregations). The training
loop does **no** `class_weight`, no `scale_pos_weight`, no SMOTE, and no
stratified split (`profiler/CLAUDE.md:108` documents stratified 80/20 — not
implemented). The `LGBMClassifier(n_estimators=50, max_depth=3, verbose=-1)`
call at `error_model.py:338` uses defaults.

**Impact**: phase 3 model under-weights the minority class (losses), which is
the class we actually need to predict for FADE decisions. The shrinkage in
Bayesian Kelly partially papers over this, but the FADE edge is systematically
weaker than it should be.

**Fix**:
- `LGBMClassifier(..., class_weight='balanced', scale_pos_weight=neg/pos)` or pass `sample_weight=`
- Stratified 80/20 split; train on 80, calibrate `CalibratedClassifierCV` on the held-out 20 (currently `cv='prefit'` is invoked on the SAME data the base model was just fitted on — `error_model.py:339-341` — which produces an over-confident calibration).
- Track per-class metrics (recall on losses) in a `model_eval` table (see §6).

### 3.3 Cold-start handling

- `falcon_no_data` wallets are stamped `excluded=TRUE` (master `CLAUDE.md` §16 — confirmed at `leader_registry.py:117+` `enrich_leaders`). This is a HARD exclusion, not a "delay until you have data" — once a wallet is `falcon_no_data` it is permanently out unless manually un-stamped via `cleanup_falcon_no_data_leaders.sql`. For new wallets that simply haven't been picked up by Falcon yet (registry/CLAUDE.md:65), this is too aggressive — a Polymarket wallet active for 3 days will be `falcon_no_data` and never recover until Falcon's pipeline re-indexes.
- Adaptive thresholds (`config.py:336+` `BOUNDS`, `eff()` lookup) lower the phase-2/3 promotion gates to 30/150 from 100/500 during cold start. ✓ implemented.
- Default Beta priors `(α=1, β=1)` for new leaders mean Thompson sampling will explore a brand-new leader at ~50/50 follow vs fade — fine for paper, but no Bayesian shrinkage toward the global "leaders win 60%" prior. **Fix**: seed the Beta prior with the population mean (e.g. `α=6, β=4`) when a new leader appears, so the first few decisions don't look like pure noise.

### 3.4 Model artifact versioning

- `economic_model_version='v1.0.0'` is stamped on every write (`leader_profiles.economic_model_version`, `error_model.py:434`, `behavior_profiler.py:534`). ✓
- `error_model_blob` is a pickled `BayesianRidge` or `CalibratedClassifierCV(LGBMClassifier)` — there is **no record of**: (a) the lookback window the blob was fit on, (b) the count of training samples, (c) the timestamp of the earliest/latest training row, (d) the feature schema version. `_upgrade_phase` does write `runtime["last_fit_at"]`, `runtime["last_fit_phase"]`, `runtime["training_samples"]` to `profile_json.error_model_runtime` (`error_model.py:345-348`), which is a start, but the WINDOW (`open_time` min/max) and the feature-vector layout are NOT pinned. If `_build_features` ever changes the feature order, every existing blob silently misaligns.
- **Fix**: emit a sidecar `error_model_metadata` JSONB column with `{trained_at, training_samples, training_window: [t_min, t_max], feature_schema_version: int, base_estimator: 'BayesianRidge|LightGBM', hparams: {...}}`. Bump `feature_schema_version` whenever `_build_features` changes; refuse to load blobs whose schema doesn't match.

---

## 4. The "evolved form" — proposed architecture

### 4.1 Feature store with point-in-time correctness

A `feature_store` schema would consolidate every input feature with an `as_of`
timestamp. Minimum tables:

```
market_liquidity_history (market_id, ts, liquidity_score)
market_volatility_history (market_id, ts, sigma_24h, vol_24h)
leader_state_snapshot     (wallet, ts, ewma_size, process_score, flip_rate,
                           scale_in_rate, contrarian_rate, top_categories[],
                           accuracy_overall, positions_resolved)
edge_state_snapshot       (leader, follower, ts, follow_probability,
                           hawkes_alpha_mu, trapped_rate)
book_quality_history      already exists as book_quality_snapshots
```

For training reconstruction: `SELECT … FROM <table> WHERE ts <= position.open_time
ORDER BY ts DESC LIMIT 1` — the standard "as-of join" pattern. This kills
MG-3 leakage and makes online/offline parity testable (§4.4).

### 4.2 Online learning vs nightly retrain — phase mapping

| Path | Phase | Cadence |
|---|---|---|
| Online (per event) | Phase 1 Beta-Binomial; profile state; graph edges; Thompson | already correct |
| Time-gated online (per N events) | Phase 2 BayesianRidge incremental partial_fit on a streaming buffer of 1000 most-recent samples | currently nightly only; the underlying `BayesianRidge` model in sklearn does NOT support `partial_fit`, so move Phase 2 to `sklearn.linear_model.SGDClassifier(loss='log_loss')` with `partial_fit` for true online learning, OR keep BayesianRidge but trigger refit every 50 new resolved positions instead of every 24 h. |
| Nightly batch | Phase 3 LightGBM + Platt; Hawkes refit; resolution reconciliation | already correct cadence; calibration set should be the held-out 20% (§3.2). |
| Weekly | hyperparameter search for Phase 3 (currently fixed at `n_estimators=50, max_depth=3`); KDE refit (once it exists) | not implemented |

### 4.3 Streaming features: Redis Streams over LISTEN/NOTIFY

The current pub/sub channels (`trades:observed`, `positions:closed`,
`market:price_changes`) are ephemeral — a slow consumer drops messages. For the
self-learning loop, switch to Redis Streams (`XADD trades:observed` +
`XREADGROUP profiler-1`) with consumer groups. This gives:
- replay on restart (no need for `_hydrate_recent_trades`-style warm-starts)
- per-consumer lag metrics (already partly done via `metrics:book_age_p95_s`)
- dead-letter queue for malformed messages

Polled features (Falcon agent 575 liquidity, market metadata) stay polled but
move to Redis Streams as well so the consumer cadence is decoupled from the
producer's HTTP-poll cadence.

### 4.4 Backtesting parity (train/serve skew)

The honest answer: **NO, current online and offline features do not match.**

- Online (`confidence_engine._build_trade_context`, line 574): reads
  `markets.liquidity_score` AS OF NOW; reads `leader_profiles.profile_maturity`
  AS OF NOW; reads `recent_avg_price` from last 10 trades before the live trade
  time.
- Offline (`error_model._fetch_training_data`, line 463): rebuilds a
  `rolling_profile` by replaying `trades_observed`, but reads
  `markets.liquidity_score` and `markets.category` AS OF NOW (the JOIN at
  line 511), and uses TODAY's `leader_profiles` for all profile-derived features.
- **Process insights** (`process_score`, `flip_rate`, etc.): online uses the
  persisted `decision_process` from the live profile (today's state, including
  ALL trades up to now); offline rebuilds it incrementally. THESE DO NOT MATCH
  for the same `(wallet, t)` — online evaluation at past time t would see "all
  trades up to now", offline reconstruction sees "trades up to t". The offline
  is point-in-time correct; online uses future information when a backtest
  replay runs against today's profile state. This is a real bug for any backtest
  harness that exercises `evaluate()` against historical data.

A simple parity test: pick 100 closed positions, compute `_build_features`
online via the live profile and offline via the reconstructed rolling profile
at `open_time`, and assert L∞ distance < 0.05. **This test does not exist.**

### 4.5 Promotion criteria — encoded but not auditable

| Transition | Encoded where | Auditable? |
|---|---|---|
| Phase 1 → 2 | `error_model._determine_phase`, line 292; threshold from `eff("MIN_RESOLVED_FOR_ERROR_P2")` (default 30) | partially — `last_fit_phase` written but not the count at promotion time |
| Phase 2 → 3 | same; threshold `MIN_RESOLVED_FOR_ERROR_P3` (default 150); also requires `_phase3_supported()` (lightgbm installed) | partially |
| Phase 3 → 2 (downgrade) | CUSUM > 2.0 in `update()` (line 144) | yes — `last_downgraded_at` is written |
| Phase 2 → 1 (downgrade) | same | yes |

What is missing:
- Promotion does NOT check the held-out validation Brier score / log-loss before swapping the production model. A new LightGBM that overfits on 150 samples can ship without any quality gate other than "we have 150 samples now".
- No A/B between the candidate and incumbent. A clean pattern: train candidate, run it in shadow against the next 50 closes, only promote if `brier_score(candidate) < brier_score(incumbent) - 0.01`.
- No record of WHY a downgrade fired beyond `last_downgraded_at`. CUSUM crossing is logged as a warning (`error_model.py:145`), not stored. Downstream dashboards cannot show a drift timeline.

**Fix**: introduce a `model_promotion_log(wallet, from_phase, to_phase,
brier_before, brier_after, sample_count, decided_at, reason)` table; gate
promotions on Brier improvement.

---

## 5. Acquisition gaps the models WOULD use if collected

Highest first by my estimate of accuracy lift:

1. **Per-market order-book imbalance time series** (top-of-book bid/ask depth, midprice, spread, top-5 levels)
   - `book_quality_snapshots` already captures this (`trade_observer.py:487`) but only as a one-shot per WS book event. There is no rollup to per-minute imbalance. **Build a `book_imbalance_minute(market_id, ts, bid_depth_5, ask_depth_5, imbalance, spread_bps, microprice)` table.**
   - Models that gain: error_model phase 3 (microprice deviation from market_price = a strong "leader entering at the wrong price" signal); FADE confidence (wide spread + thin book = fade-aware sizing); the still-hypothetical micro_reactive track in `neural_readiness.py:334`.
   - **Highest ROI** of this list — the data is already partly captured, the work is the rollup + a feature column.

2. **Per-market price + volume time series at 1-minute granularity** (Falcon agent 568 candlesticks, currently never queried)
   - Adds true volatility features (`sigma_5m`, `sigma_1h`, return-since-open) that the error model currently approximates with `_get_market_liquidity()` alone.
   - Enables a `momentum_score` feature: was the market trending in the leader's direction in the 5-min window before the trade?
   - Models that gain: error model (volatility regime conditioning), confidence engine (better `is_contrarian` than the "last 10 trades" heuristic at `confidence_engine.py:620-637`).

3. **Leader on-chain wallet state**: USDC balance over time, # of open positions, total exposure, recent inflow/outflow.
   - Today, the bot has no idea whether a leader is sized-up (high conviction) or just deploying excess balance.
   - Models that gain: behavior_profiler — a "size relative to current bankroll" feature is much more predictive than absolute size; error_model — leaders trading near max-exposure tend to lose more (over-leveraged signal).
   - Source: Polygon RPC (free) + The Graph subgraph.

4. **Real bivariate Hawkes input** — per-edge log of `(leader_trade_time, follower_trade_time, market_id, side_match)` materialized to a `hawkes_input_log` table.
   - Without this, MG-5 stays blocked even when we switch to `tick`.

5. **Leader social metadata** — Falcon agent 585 (Social Pulse) is documented in master `CLAUDE.md:163` but never queried. A leader who tweeted about a market 5 minutes before trading is qualitatively different from one who didn't.
   - Models that gain: behavior_profiler `entry_patterns` (add `social_pre_trade_rate`); confidence_engine FADE confidence.
   - Effort: M. ROI uncertain — depends how many top wallets are even on X.

6. **Market resolution outcomes** — currently no explicit feed. Required for MG-8. Sources: Polymarket Gamma API or UMA optimistic oracle.

---

## 6. Missing eval / monitoring

| Model | Out-of-sample validation | Calibration tracking | Drift detection | Notes |
|---|---|---|---|---|
| Behavior profiler (Dirichlet, EWMA, accuracy Beta) | n/a (descriptive, not predictive) | n/a | none | could track `category_accuracy` rolling drift |
| Error model Phase 1 | none | none | covered by per-leader CUSUM (`error_model.py:131-146`) ✓ | |
| Error model Phase 2 (BayesianRidge) | NONE — `model.fit(features, y)` on all data; no holdout | NONE | CUSUM ✓ | |
| Error model Phase 3 (LightGBM + Platt) | NONE — `CalibratedClassifierCV(base, cv='prefit')` calibrates on the SAME data the base just trained on (`error_model.py:339-341`) — the calibration is meaningless | NONE — Brier/log-loss never computed | CUSUM ✓ | calibration is broken (overconfident) |
| Graph engine (Beta-Binomial) | n/a | n/a | none — confirmed edges decay only by Hawkes batch | |
| Hawkes fitter | none | n/a | none — refit silently overwrites previous α/μ | should track AIC/BIC and log when fit deteriorates |
| Confidence engine Thompson | implicit via paper PnL | none | none | the `decision_log` table is the audit trail (`engine/CLAUDE.md:130`) but no eval is run on it |

**Concrete plan**: add a `model_eval(wallet, model_phase, evaluated_at,
brier, log_loss, accuracy_at_05, n_samples, slice='last_30d')` table; populate
nightly by running the current production blob against the last 30 days of
resolved positions held out from the next training run. Alert when
`brier_now > brier_7d_ago + 0.02`.

---

# 250-word summary

The three highest-impact freshness gaps:

**MG-3 — `liquidity_score` is 24 h stale and sourced from the wrong Falcon
agent.** `markets.liquidity_score` is written by `sync_markets`
(`leader_registry.py:289`) using agent 574's `liquidity` field, but every
docstring claims agent 575 (Market Insights). `sync_markets` only re-fetches
markets whose row is older than 24 h, so the error model's phase-2/3
`liquidity_score` feature lags reality by ~12 h on average and is
methodologically the wrong field. This also creates leakage in training
(`error_model._fetch_training_data` reads liquidity AS-OF-NOW for historical
positions).

**MG-2 — Trade attribution lag is bounded below by 30 s REST polling.** The
CLOB WebSocket feed does not carry wallet addresses, so leader-trade attribution
runs through `data-api.polymarket.com/trades` polled every 30 s
(`config.py:59`). Combined with `LIVE_DECISION_MAX_TRADE_AGE_S=120`, a
non-trivial tail of leader signals is silently SKIPped at
`confidence_engine.py:142`. Hawkes is also fed 30 s-quantized timestamps,
killing sub-minute excitation kernels.

**MG-5 — Hawkes fit is univariate, not bivariate.** `hawkes_fitter.py:101-104`
fetches leader timestamps but discards them and fits a self-exciting process
on the follower's own trade times. The published `alpha_mu_ratio` measures
follower burstiness, not leader→follower causality, so every clustered retail
trader gets confirmed as a follower.

**Highest-ROI new data source to start collecting now: a per-minute order-book
imbalance + microprice time series** (rolled up from the existing
`book_quality_snapshots`). The data is already partially captured — the work
is a per-minute rollup table feeding three features (depth imbalance, spread
bps, microprice deviation) directly into the error model and FADE confidence.
Cheapest single change with the highest expected accuracy lift.

