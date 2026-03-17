export type LiveSnapshot = {
  bot: { status: string; uptime_seconds: number; latency_ms: number };
  stats: { total_pnl: number; win_rate: number; avg_profit: number; active_markets: number; detected_arbs_today: number };
  markets: Array<{ market_id: string; title: string; best_bid: number; best_ask: number; spread: number; est_profit: number; detected: boolean; mid_price: number }>;
  price_history: Array<{ timestamp: string; value: number }>;
  recent_simulations: Array<{ id: string; market_id: string; side: string; price: number; size: number; pnl: number; timestamp: string }>;
};
