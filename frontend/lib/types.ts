export type LiveSnapshot = {
  bot: { status: string; uptime_seconds: number; latency_ms: number };
  stats: { total_pnl: number; win_rate: number; avg_profit: number; active_markets: number; detected_arbs_today: number };
  markets: Array<{
    market_id: string;
    condition_id: string;
    title: string;
    best_bid_yes: number;
    best_ask_yes: number;
    best_bid_no: number;
    best_ask_no: number;
    spread: number;
    est_profit: number;
    detected: boolean;
    decision: string;
    decision_reason: string;
    yes_mid: number;
  }>;
  price_history: Array<{ timestamp: string; value: number }>;
  recent_simulations: Array<{ id: string; market_id: string; side: string; price: number; size: number; pnl: number; timestamp: string; decision: string; reason: string }>;
  risk: {
    config: {
      risk_per_trade_pct: number;
      max_total_exposure_pct: number;
      kelly_fraction_multiplier: number;
      max_drawdown_auto_stop_pct: number;
    };
    toggles: {
      risk_managed_sizing: boolean;
      use_kelly_on_sum_positions: boolean;
      auto_close_on_resolution: boolean;
      pause_on_high_latency: boolean;
    };
    gauges: {
      total_portfolio_exposure_pct: number;
      total_risk_taken_pct: number;
      current_drawdown_pct: number;
    };
    preview: string;
  };
};
