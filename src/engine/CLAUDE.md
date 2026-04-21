# Engine Module — Decision Making + Paper Trading

**Purpose**: Use profiler output to make FOLLOW/FADE/SKIP decisions via Thompson Sampling.
Size positions via Bayesian Kelly. Execute paper trades with full position cycle tracking.
Maintain decision audit log for every action.

See parent [CLAUDE.md](../CLAUDE.md) for full context.

---

## Components

- **confidence_engine.py**: Thompson Sampling for leader credibility. Maintains per-leader (α_follow, β_fade)
  Beta posteriors. Samples from both; higher sampled value → action. Includes exploration floor.
  Readiness checks: FOLLOW needs 50 trades + 5 followers; FADE needs 50 resolved + high error confidence.

- **paper_trader.py**: Simulated portfolio. On FOLLOW/FADE signal, calculates Kelly-sized position,
  opens paper trade in DB. On leader exit (detected by observer module), closes paper trade, calculates PnL
  including fees. Maintains full OPEN → CLOSE cycle. Returns close reason: "leader_exit", "market_resolved", etc.

- **risk_manager.py**: Position sizing via Bayesian Kelly. Shrinkage for uncertainty.
  Hard caps: 2% of bankroll per trade, FADE sizing = 50% of FOLLOW max, no single market > 25% exposure.
  Circuit breakers: if daily loss > 5%, skip all trades next 1h.

- **models.py**: Signal, PaperTrade, Decision dataclasses.

---

## Key Algorithms

### Thompson Sampling (Real-time, <10ms per decision)
Per leader, maintain dual Beta distributions:
```
Beta(α_follow, β_follow)  ← count(profitable FOLLOW trades from this leader)
Beta(α_fade, β_fade)      ← count(profitable FADE trades from this leader)

θ_follow ~ Beta(α_follow, β_follow)  [sample]
θ_fade ~ Beta(α_fade, β_fade)        [sample]

Action = argmax(θ_follow, θ_fade, θ_skip)
θ_skip = fixed constant (e.g., 0.5)  [always available]

Exploration floor: max(0.1, 1/√n_observations)
If any arm's sampled θ < exploration_floor: force uniform random over all arms
```

Update on signal close (FOLLOW resolves win/loss or leader exit):
```
If FOLLOW trade won: α_follow += 1
If FOLLOW trade lost: β_follow += 1
(same for FADE)
```
O(1) per update.

### Readiness Checks (Prevent premature trading)

**FOLLOW readiness**:
```
Trigger only if:
  - Wallets in profiler.trades_observed >= 50 (enough trade history)
  - AND confirmed_followers_count >= 5 (has real follower network)
  - AND profiler.positions_resolved >= 10 (some trades resolved)
  - AND error_model_phase >= 1 (profile available)

If not ready: force SKIP
```

**FADE readiness**:
```
Trigger only if:
  - profiler.positions_resolved >= 50 (substantial resolution history)
  - AND error_model.confidence >= 0.75 (high confidence in error predictions)
  - AND error_model.phase >= 2 (at least Bayesian LogReg)

If not ready: force SKIP
```

### Bayesian Kelly Sizing (O(100) numerical integration)
```
f* = (p·b - q) / b   [Kelly fraction]

where:
  p = P(win | leader, market)  [from error_model: 1 - P(loss)]
  q = 1 - p
  b = odds = (price_exit / price_entry) - 1  [approximated before trade]

Shrinkage for uncertainty:
  shrinkage = 1 - σ²_p / p²  [variance of posterior p]
  f_final = f* × shrinkage

Hard caps:
  max_position_usdc = 2% × PAPER_CAPITAL_USDC
  FADE sizing = 0.50 × FOLLOW sizing (more conservative)
  min_position_usdc = $50 (skip if below)

Circuit breaker:
  if daily_loss_pct > 5%: skip_all_trades_next_1h
```

### Decision Log Audit Trail
Every decision (FOLLOW, FADE, SKIP) recorded in `decision_log` table:
```sql
time, leader_wallet, market_id, action, thompson_follow, thompson_fade,
kelly_fraction, confidence, reason (human-readable), outcome (pending/win/loss)
```
Enables backtesting: replay all decisions, verify PnL calculations.

### Paper Trade Full Cycle
On FOLLOW/FADE signal → open paper trade:
```
paper_trades table INSERT:
  opened_at, market_id, token_id, direction, entry_price, size_usdc,
  strategy ('follow' or 'fade'), leader_wallet, leader_context (JSON),
  confidence, status='open'
```

On leader exit (observer detects position_reconstructed.close_time) → close paper trade:
```
UPDATE paper_trades SET
  closed_at = now(),
  exit_price = price_at_leader_exit,
  pnl_usdc = (exit_price - entry_price) * size / entry_price - fee_paid,
  fee_paid_usdc = estimated_fee,
  status = 'closed',
  close_reason = 'leader_exit' | 'market_resolved' | 'timeout'
```

---

## Critical Pitfalls

1. **FOLLOW vs FADE readiness DIFFER**: FOLLOW is "following", requires high data volume (50 trades).
   FADE is "betting against", requires RESOLVED positions (50 resolved) + high error confidence.
   Don't conflate the two. Code two separate readiness checks.

2. **Kelly sizing with uncertainty**: Raw Kelly can be aggressive (f* = 20%+ of bankroll).
   MUST apply shrinkage for parameter uncertainty. Without it, you will be over-leveraged.

3. **FADE sizing = 50% of FOLLOW**: If FOLLOW position = $200, FADE position = $100 (not same).
   This is intentional: fading is riskier (betting against crowd). Hard-cap in risk_manager.

4. **Don't execute live trades yet**: Paper trader is simulation only. Entry price, exit price are
   estimates. Don't connect to real broker API during this phase.

5. **Decision log must be complete**: Every action must log to `decision_log`, including SKIP.
   Without this, you can't debug decisions later. Include reason in human-readable form.

6. **Thompson posterior updates are ONE-WAY**: Once you increment α_follow, you can't undo it.
   Only update on RESOLUTION (trade closes), not on open trades. Avoid premature updates.

---

## Testing Approach

- **Unit tests**:
  - Thompson Sampling: mock α_follow=10, β_follow=5, α_fade=3, β_fade=7. Sample 1000x, verify mode near 0.67.
  - Readiness checks: mock profiler with 30 trades (insufficient), verify FOLLOW returns SKIP.
  - Mock profiler with 50 trades + 3 followers (insufficient), verify FOLLOW returns SKIP.
  - Kelly sizing: p=0.60, b=1.0, no shrinkage → f* ≈ 20%. Apply shrinkage=0.8 → f_final=16%.
  - Verify hard caps: cap_pct=2%, capital=$10k → max_trade=$200 (not Kelly f_final * capital).

- **Integration tests**:
  - Real DB: insert 60 resolved positions (40 wins, 20 losses) for leader L1.
  - Trigger FOLLOW readiness: verify no error (sufficient history).
  - Trigger FADE readiness: verify error if error_model_phase < 2 (not ready).
  - Simulate 10 FOLLOW decisions, verify `decision_log` table populated (10 rows).
  - Simulate leader exit: verify paper_trades updated with exit_price, close_reason, pnl_usdc.

---

## References
- Profiler output: behavior_profiler, error_model from profiler module
- Graph output: follower edges, follower count per leader from graph module
- Database: `decision_log`, `paper_trades` tables (master CLAUDE.md § 6)
- Constants: `FOLLOW_MIN_TRADES`, `FOLLOW_MIN_FOLLOWERS`, `FADE_MIN_RESOLVED`, `FADE_MIN_CONFIDENCE`,
  `THOMPSON_EXPLORATION_FLOOR`, `MAX_POSITION_PCT`, `FADE_SIZE_RATIO` from config.py
- Libraries: scipy.stats.beta (sampling), numpy (Kelly integration)
- Observer integration: subscribe to position_reconstructed close events (via Redis pub/sub)
