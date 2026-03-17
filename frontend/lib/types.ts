export type BotTrade = {
  id: string;
  order_id: string;
  tx_hash?: string | null;
  execution_mode: "dry_run" | "live";
  exchange_status: string;
  market_id: string;
  market_title: string;
  token_id: string;
  side: string;
  price: number;
  size: number;
  notional: number;
  risk_pct: number;
  kelly: number;
  slippage: number;
  fees: number;
  pnl_abs: number;
  pnl_pct: number;
  status: string;
  timestamp: string;
};

export type LiveSnapshot = {
  bot: { status: string; uptime_seconds: number; latency_ms: number };
  risk_config: {
    risk_per_trade_pct: number;
    max_total_exposure_pct: number;
    kelly_fraction: number;
    max_drawdown_stop_pct: number;
    fee_bps: number;
  };
  stats: {
    total_pnl: number;
    win_rate: number;
    avg_profit: number;
    active_markets: number;
    detected_arbs_today: number;
    portfolio_total: number;
    capital_in_trade: number;
    pnl_percent: number;
  };
  markets: Array<{
    market_id: string;
    title: string;
    token_id_yes: string;
    token_id_no: string;
    best_bid: number;
    best_ask: number;
    spread: number;
    est_profit: number;
    detected: boolean;
    mid_price: number;
    direction: string;
  }>;
  price_history: Array<{ timestamp: string; portfolio: number; pnl_pct: number }>;
  recent_trades: BotTrade[];
};
