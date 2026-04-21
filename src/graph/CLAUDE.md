# Graph Module — Leader → Follower Social Graph

**Purpose**: Build a weighted directed graph of influence: when a follower trades the SAME token
in the SAME market within 5 minutes AFTER a leader trade, that's a potential causal link.
Validate causality via Hawkes process (batch daily), update edge confidence probabilistically.

See parent [CLAUDE.md](../CLAUDE.md) for full context.

---

## Components

- **graph_engine.py**: Real-time graph updates. On each leader trade, scan all other wallets for
  trades in same (market, token) within +5m window. Update `follower_edges` table with co-occurrence count,
  follow probability (Beta posterior), same_direction_rate, trapped_rate.

- **hawkes_fitter.py**: Batch job (1x/24h, 3 AM UTC). Fit Hawkes process on 30 days of
  (leader_trade_time, follower_trade_time) pairs per edge. Compute α/μ ratio. Confirm edge only if α/μ > 1
  AND co_occurrences ≥ 5 AND same_direction_rate ≥ 0.7.

- **models.py**: Edge, HawkesResult dataclasses.

---

## Key Algorithms

### Real-time Co-occurrence Tracking (O(1) update)
On leader trade (wallet_L, market_M, token_T, time_t):
1. Query all trades in (market_M, token_T) from (t, t+300s) by OTHER wallets → follower candidates
2. For each candidate (wallet_F, direction_F):
   - If direction_F = direction_L → co_occurrences += 1, same_direction_rate += 1
   - Else → co_occurrences += 1
3. Update Beta posterior: follow_beta_a += 1 or follow_beta_b += 1 depending on direction match
4. Recalculate follow_probability = α / (α + β) [Beta posterior mean]
5. Upsert to `follower_edges` table: UNIQUE(leader_wallet, follower_wallet)

### Beta-Binomial Follower Probability
State per edge: (α_follow, β_follow) ∈ Beta distribution
```
P(follower) = α / (α + β)

On same-direction co-occurrence: α += 1
On opposite-direction co-occurrence: β += 1
Uninformed prior: α = β = 1 (uniform)
```
O(1) per update. No re-fitting needed in hot path.

### Hawkes Process Validation (batch, daily)
Given 30 days of trade times for leader_L and follower_F in same market:
```
λ(t) = μ + α · Σ exp(-β · (t - t_i))  for all t_i < t (leader trades before t)

MLE fit: estimate α (excitation), μ (baseline)
α/μ ratio interpretation:
  - α/μ > 1   → each leader trade excites ~1+ follower trades → CAUSAL (confirmed follower)
  - 0.3 < α/μ ≤ 1 → weak correlation
  - α/μ ≤ 0.3 → coincidental (background noise)
```

Use scipy.optimize for MLE (or library `tick` if installed).

### Trapped Rate (P(follower still in when leader exits))
For each confirmed edge, track:
```
trapped_rate = Σ(follower OPEN when leader CLOSE) / count(leader CLOSE)
```
Follower is "trapped" if leader exits but follower's position is still open in same (market, token).
High trapped_rate → follower may be panic-holding or may not have read the signal in time.

---

## Critical Pitfalls

1. **Direction matters**: Same token ≠ same bet. If leader BUY YES + follower SELL YES (covering short),
   that's opposite direction. Track direction_match rate separately. Only count SAME direction as evidence.

2. **5-minute window is RIGID**: Use (t_leader, t_leader + 300s]. Not 5-minute rolling average.
   Tighten to 1m if needed; widen to 10m only after careful backtesting.

3. **Don't double-count**: If leader trades twice in 5 minutes, each trade independently triggers
   the follower window. Don't merge windows or average.

4. **Hawkes fit noise**: With < 30 days or < 10 (leader, follower) pairs in a market, MLE fit is unstable.
   Only validate edges that have:
   - co_occurrences ≥ 5 (minimum confidence for fit)
   - Data span ≥ 7 days (enough temporal variation)

5. **False positives from retail**: If 1000 retail traders all buy BTC rally on same day, α/μ might appear
   high even though no causal link exists. Require ALSO:
   - same_direction_rate ≥ 0.7 (at least 70% of co-occurrences are same direction)
   - avg_delay_s < 60 (follower trades within 60 seconds, not just 5 minutes)

---

## Testing Approach

- **Unit tests**:
  - Mock leader + follower trades in same market, 2 min apart. Verify co_occurrence += 1.
  - Mock opposite-direction trade. Verify same_direction_rate = 0.5, Beta updated correctly.
  - Verify Beta posterior mean: α=3, β=1 → P = 0.75.
  - Mock 30-day trade sequence. Verify Hawkes fit returns α > 0, μ > 0, α/μ in [0, 2].

- **Integration tests**:
  - Real DB: insert 100 trades (10 leaders, 10 followers each), run graph_engine.
  - Verify `follower_edges` table populated with correct co_occurrence counts.
  - Run Hawkes batch on small dataset, verify edges confirmed/rejected by α/μ threshold.
  - Verify trapped_rate calculated correctly (follower open positions when leader exits).

---

## References
- Falcon agent 556: Polymarket Trades (backfill trade history)
- Database: `follower_edges`, `trades_observed`, `positions_reconstructed` tables (master CLAUDE.md § 6)
- Constants: `FOLLOWER_WINDOW_S` (300), `MIN_CO_OCCURRENCES` (5), `MIN_SAME_DIRECTION_RATE` (0.7) from config.py
- Hawkes: scipy.optimize + scipy.special for MLE
- Batch schedule: `BATCH_HOUR_UTC` (3 AM) from config.py
