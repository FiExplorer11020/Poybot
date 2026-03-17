export type BotTrade = {
  id: string;
  market_id: string;
  market_title: string;
  side: string;
  price: number;
  size: number;
  notional: number;
  pnl_abs: number;
  pnl_pct: number;
  timestamp: string;
};

export type LiveSnapshot = {
  bot: { status: string; uptime_seconds: number; latency_ms: number };
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
    best_bid: number;
    best_ask: number;
    spread: number;
    est_profit: number;
    detected: boolean;
    mid_price: number;
  }>;
  price_history: Array<{ timestamp: string; portfolio: number; pnl_pct: number }>;
  recent_trades: BotTrade[];
};
