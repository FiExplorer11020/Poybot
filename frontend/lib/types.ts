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
  slippage?: number;
  fees: number;
  pnl_abs: number;
  pnl_pct: number;
  status: string;
  timestamp: string;
  closed_at?: string | null;
  unrealized_pnl_abs?: number;
  unrealized_pnl_pct?: number;
};

export type SourceHealth = {
  name: string;
  status: string;
  last_seen_at?: string | null;
  last_message_at?: string | null;
  lag_ms?: number | null;
  messages_last_minute: number;
  note?: string | null;
};

export type IngestionMarketHealth = {
  market_id: string;
  title: string;
  quote_source: string;
  bootstrap_only: boolean;
  last_message_at?: string | null;
  last_quote_at?: string | null;
  freshness_ms?: number | null;
  observations: number;
  messages_last_minute: number;
  source_delay_ms?: number | null;
};

export type MarketSnapshot = {
  market_id: string;
  title: string;
  end_date?: string;
  token_id_yes: string;
  token_id_no: string;
  best_bid: number;
  best_ask: number;
  mid_price: number;
  no_mid_price?: number;
  spread: number;
  no_spread?: number;
  volatility: number;
  rolling_mean?: number;
  rolling_std?: number;
  z_score?: number;
  liquidity_score: number;
  expected_edge: number;
  entry_threshold: number;
  signal_strength: number;
  rank_score?: number;
  direction: string;
  est_profit: number;
  detected: boolean;
  observations: number;
  complement_gap: number;
  price_delta: number;
  momentum: number;
  pressure?: number;
  imbalance?: number;
  freshness_ms?: number;
  source_delay_ms?: number;
  open_trade_id?: string | null;
  open_position?: boolean;
  bootstrap_only?: boolean;
  quote_source?: string;
  regime?: string;
  decision_action?: string;
  decision_summary?: string;
  decision_rejections?: string[];
  decision_reasons?: string[];
  explain?: string[];
};

export type DecisionRow = {
  market_id: string;
  title: string;
  action: string;
  executable: boolean;
  side: string;
  confidence: number;
  cooldown_remaining_ms: number;
  reasons: string[];
  rejections: string[];
  summary: string;
  analytics_refs: Record<string, number | string>;
};

export type PositionRow = {
  trade_id: string;
  market_id: string;
  market_title: string;
  side: string;
  entry_price: number;
  size: number;
  notional: number;
  unrealized_pnl_abs: number;
  unrealized_pnl_pct: number;
  decision_action: string;
  decision_summary: string;
};

export type SignalHistoryPoint = {
  timestamp: string;
  opportunity_count: number;
  top_signal_score: number;
  avg_freshness_ms: number;
  data_latency_ms: number;
};

export type LogEntry = {
  timestamp: string;
  level: string;
  category: string;
  message: string;
  market_id?: string | null;
  details?: Record<string, unknown>;
};

export type LiveSnapshot = {
  clock: {
    server_time: string;
    cycle_interval_ms: number;
  };
  bot: {
    status: string;
    uptime_seconds: number;
    latency_ms: number;
    cycle_latency_ms: number;
    started_at: string;
    active_run_started_at?: string | null;
    accumulated_run_seconds: number;
    last_command_at: string;
    stopped_at?: string | null;
    execution_enabled: boolean;
  };
  risk_config: {
    risk_per_trade_pct: number;
    max_total_exposure_pct: number;
    kelly_fraction: number;
    max_drawdown_stop_pct: number;
    fee_bps: number;
    base_entry_threshold: number;
    spread_cap: number;
    allocation_mode?: string;
    manual_notional_amount?: number;
    min_observations?: number;
    min_signal_strength?: number;
    max_concurrent_positions?: number;
    max_positions_per_tick?: number;
    cooldown_seconds?: number;
    signal_staleness_seconds?: number;
    max_holding_seconds?: number;
    display_market_limit?: number;
  };
  stats: {
    total_pnl: number;
    win_rate: number;
    avg_profit: number;
    active_markets: number;
    detected_arbs_today: number;
    open_positions: number;
    portfolio_total: number;
    capital_in_trade: number;
    pnl_percent: number;
  };
  ingestion: {
    status: string;
    total_markets: number;
    live_markets: number;
    stale_market_count: number;
    updates_last_minute: number;
    raw_buffer_size: number;
    avg_freshness_ms: number;
    max_freshness_ms: number;
    avg_source_delay_ms: number;
    last_message_at?: string | null;
    sources: SourceHealth[];
    markets: IngestionMarketHealth[];
    recent_raw?: Array<Record<string, unknown>>;
  };
  analytics: {
    summary: {
      tracked_markets: number;
      opportunity_count: number;
      top_signal_score: number;
      top_edge: number;
      avg_freshness_ms: number;
      avg_volatility: number;
    };
    opportunities: MarketSnapshot[];
    leaderboard: MarketSnapshot[];
    history: SignalHistoryPoint[];
  };
  decision_engine: {
    summary: {
      actionable_count: number;
      open_count: number;
      close_count: number;
      reduce_count: number;
      reject_count: number;
      slots_remaining: number;
      exposure_remaining: number;
    };
    ranked: DecisionRow[];
  };
  positions: {
    open_count: number;
    capital_in_trade: number;
    exposure_pct: number;
    items: PositionRow[];
  };
  markets: MarketSnapshot[];
  price_history: Array<{ timestamp: string; portfolio: number; pnl_pct: number }>;
  recent_trades: BotTrade[];
  logs: LogEntry[];
  timestamp: string;
};
