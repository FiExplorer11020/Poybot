# Polymarket Adaptive Bot Spec (MVP v1)

## 1. Scope
This specification defines the **trading rules, risk controls, and execution simulation constraints** used by the backend bot logic.
It is designed for Polymarket-style binary outcome markets where token prices represent probabilities.

## 2. Market model assumptions (Polymarket-compatible)
1. Binary markets are represented by YES and NO shares.
2. Price domain is bounded: `0.0 <= price <= 1.0`.
3. Approximate complement consistency: `P(YES) + P(NO) ~= 1`.
4. Mid-price is interpreted as implied probability for YES token in binary contexts.
5. Tradability quality depends on spread and micro-volatility; wider spreads imply higher execution risk.

## 3. Signal model
A market receives a candidate signal when all conditions are met:
- price in valid domain
- spread positive and finite
- estimated edge (risk-adjusted) above adaptive threshold

### 3.1 Derived features
For each market tick:
- `mid` = `(best_bid + best_ask)/2`
- `spread` = `best_ask - best_bid`
- `volatility` = rolling stdev of mid-price deltas over lookback
- `liquidity_score` = `1 - min(1, spread / spread_cap)`

### 3.2 Adaptive threshold
`entry_threshold` is dynamic:
- base threshold
- plus spread penalty
- plus volatility penalty
- minus liquidity benefit

The bot only marks DETECTED if:
`expected_edge >= entry_threshold` and risk controls allow entry.

## 4. Risk model (portfolio-level)
All entries must pass:
1. `risk_per_trade_pct` cap
2. `max_total_exposure_pct` cap
3. `max_drawdown_stop_pct` cap
4. optional Kelly scaling factor

### 4.1 Position size
`notional = min(max_notional_by_risk, max_notional_by_exposure)`

Where:
- `max_notional_by_risk = portfolio_equity * risk_per_trade_pct`
- `max_notional_by_exposure = max(0, portfolio_equity*max_total_exposure_pct - capital_in_trade)`

If Kelly enabled:
- `notional *= clamp(kelly_fraction * kelly_score, min_kelly, max_kelly)`

## 5. Execution simulation rules
Execution output is deterministic from market + risk state (no pure random PnL draw):
1. Side selection:
   - BUY_YES if mid <= 0.5
   - BUY_NO if mid > 0.5
2. Entry price uses `best_ask` for buys.
3. Slippage is proportional to spread and volatility.
4. Fee model uses notional * `fee_bps`.
5. Estimated PnL is computed from edge minus costs.

## 6. Polymarket safety constraints
Before emitting trade:
- price and implied probabilities clipped to [0.01, 0.99]
- spread clipped non-negative
- reject markets with invalid orderbook (`best_ask < best_bid`)

## 7. Runtime operating modes
- LIVE: evaluate signals + allow simulated entries
- PAUSED: evaluate market stats only, no new entries
- STOPPED: no evaluations and no entries

## 8. Required observability outputs
Each cycle publishes:
- latency
- market-level edge, spread, detected status
- portfolio equity, capital in trade, pnl abs, pnl %
- per-trade: market title, side, size, entry, slippage, fees, risk %, kelly, expected pnl

## 9. Configuration defaults
- `risk_per_trade_pct = 1.0%`
- `max_total_exposure_pct = 40%`
- `kelly_fraction = 0.5`
- `max_drawdown_stop_pct = 10%`
- `fee_bps = 8`
- `base_entry_threshold = 0.004`
- `spread_cap = 0.08`

## 10. Future extension points
- cross-market arbitrage consistency graph
- event-level concentration limits
- regime classifier (high vol / low vol)
- real fill bridge (CLOB orders + tx hash tracking)
