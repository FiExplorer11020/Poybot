"use client";

import { create } from "zustand";

import { apiHeaders, apiUrl } from "@/lib/api";
import type {
  BotTrade,
  DecisionRow,
  LiveSnapshot,
  LogEntry,
  MarketSnapshot,
  PositionRow,
  SignalHistoryPoint,
  SourceHealth,
} from "@/lib/types";

export type LiveConnectionState = "connected" | "reconnecting" | "disconnected";
export type BotCommand = "start" | "pause" | "stop";
export type Market = MarketSnapshot;
export type Trade = BotTrade;

export interface HaltState {
  active: boolean;
  reason: string;
  details?: string | null;
  at?: string | null;
}

export interface TelemetryPoint {
  timestamp: string;
  equity: number;
  pnlPercent: number;
  winRate: number;
  tradesToday: number;
  detectedArbsToday: number;
  sharpe: number;
  latencyMs: number;
}

export interface LiveState {
  bootstrapped: boolean;
  connectionState: LiveConnectionState;
  reconnectAttempt: number;
  status: string;
  uptimeSeconds: number;
  latencyMs: number;
  cycleLatencyMs: number;
  totalPnl: number;
  totalPnlPct: number;
  portfolioTotal: number;
  capitalInTrade: number;
  winRate: number;
  avgProfit: number;
  activeMarkets: number;
  openPositions: number;
  detectedArbsToday: number;
  markets: Market[];
  priceHistory: Array<{ timestamp: string; portfolio: number; pnl_pct: number }>;
  recentTrades: Trade[];
  telemetryHistory: TelemetryPoint[];
  signalHistory: SignalHistoryPoint[];
  decisionRanked: DecisionRow[];
  positions: PositionRow[];
  logs: LogEntry[];
  sources: SourceHealth[];
  riskConfig: LiveSnapshot["risk_config"] | null;
  walletBalance: number;
  walletAddress?: string;
  walletToken?: string;
  walletConnecting: boolean;
  halt: HaltState;
  controlPending: boolean;
  lastEventAt?: string;
  snapshotTime?: string;
  analyticsSummary: LiveSnapshot["analytics"]["summary"] | null;
  decisionSummary: LiveSnapshot["decision_engine"]["summary"] | null;
  ingestion: LiveSnapshot["ingestion"] | null;
  positionsSummary: LiveSnapshot["positions"] | null;

  setWalletBalance: (balance: number) => void;
  setWallet: (address?: string, balance?: number, token?: string) => void;
  setWalletConnecting: (connecting: boolean) => void;
  setConnectionState: (state: LiveConnectionState, attempt?: number) => void;
  processBootstrap: (payload: LiveSnapshot | Record<string, unknown>) => void;
  processTick: (payload: LiveSnapshot | Record<string, unknown>) => void;
  processTrade: (payload: BotTrade | Record<string, unknown>) => void;
  processHalt: (payload: { reason?: unknown; details?: unknown } | Record<string, unknown>) => void;
  clearHalt: () => void;
  sendBotCommand: (command: BotCommand) => Promise<void>;
  toggleBot: () => Promise<void>;
}

const MAX_PRICE_POINTS = 1200;
const MAX_RECENT_TRADES = 300;
const MAX_TELEMETRY_POINTS = 1200;
const MAX_SIGNAL_POINTS = 1200;
const STARTING_EQUITY = 25_000;

const asNumber = (value: unknown, fallback = 0) => {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
};

const asString = (value: unknown, fallback = "") =>
  typeof value === "string" && value.trim().length > 0 ? value : fallback;

const asTimestamp = (value: unknown, fallback = new Date().toISOString()) => {
  if (typeof value !== "string") {
    return fallback;
  }

  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? fallback : new Date(parsed).toISOString();
};

const dayKey = (timestamp: string) => {
  const date = new Date(timestamp);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
};

const calculateSharpe = (points: Array<{ portfolio: number }>) => {
  if (points.length < 3) {
    return 0;
  }

  const returns: number[] = [];
  for (let index = 1; index < points.length; index += 1) {
    const previous = asNumber(points[index - 1]?.portfolio);
    const current = asNumber(points[index]?.portfolio);
    if (previous <= 0 || current <= 0) {
      continue;
    }
    returns.push((current - previous) / previous);
  }

  if (returns.length < 2) {
    return 0;
  }

  const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance =
    returns.reduce((sum, value) => sum + (value - mean) ** 2, 0) / Math.max(returns.length - 1, 1);
  const stdDev = Math.sqrt(variance);

  if (!Number.isFinite(stdDev) || stdDev === 0) {
    return 0;
  }

  return Number(((mean / stdDev) * Math.sqrt(Math.min(returns.length, 1440))).toFixed(2));
};

const countTradesForDay = (trades: Trade[], timestamp: string) => {
  const targetDay = dayKey(timestamp);
  return trades.filter((trade) => dayKey(trade.timestamp) === targetDay).length;
};

const normalizeTrade = (trade: Record<string, unknown>): Trade => {
  const price = asNumber(trade.price);
  const size = asNumber(trade.size);

  return {
    id: asString(trade.id, crypto.randomUUID()),
    order_id: asString(trade.order_id),
    tx_hash: typeof trade.tx_hash === "string" ? trade.tx_hash : null,
    execution_mode: asString(trade.execution_mode, "dry_run") as Trade["execution_mode"],
    exchange_status: asString(trade.exchange_status, "unknown"),
    market_id: asString(trade.market_id),
    market_title: asString(trade.market_title, "Unknown market"),
    token_id: asString(trade.token_id),
    side: asString(trade.side, "BUY_YES"),
    price,
    size,
    notional: asNumber(trade.notional, size * price),
    risk_pct: asNumber(trade.risk_pct),
    kelly: asNumber(trade.kelly),
    slippage:
      trade.slippage === null || trade.slippage === undefined ? undefined : asNumber(trade.slippage),
    fees: asNumber(trade.fees),
    pnl_abs: asNumber(trade.pnl_abs),
    pnl_pct: asNumber(trade.pnl_pct),
    status: asString(trade.status, "OPEN"),
    timestamp: asTimestamp(trade.timestamp),
    closed_at:
      trade.closed_at === null || trade.closed_at === undefined ? null : asTimestamp(trade.closed_at),
    unrealized_pnl_abs: asNumber(trade.unrealized_pnl_abs),
    unrealized_pnl_pct: asNumber(trade.unrealized_pnl_pct),
  };
};

const uniqueRecentTrades = (trades: Trade[]) => {
  const deduped = new Map<string, Trade>();

  for (const trade of trades) {
    deduped.set(trade.id, trade);
  }

  return Array.from(deduped.values())
    .sort((left, right) => Date.parse(right.timestamp) - Date.parse(left.timestamp))
    .slice(0, MAX_RECENT_TRADES);
};

const normalizeMarket = (market: Record<string, unknown>): Market => ({
  market_id: asString(market.market_id),
  title: asString(market.title, "Untitled market"),
  end_date: typeof market.end_date === "string" ? market.end_date : undefined,
  token_id_yes: asString(market.token_id_yes),
  token_id_no: asString(market.token_id_no),
  best_bid: asNumber(market.best_bid),
  best_ask: asNumber(market.best_ask),
  mid_price: asNumber(market.mid_price),
  no_mid_price: asNumber(market.no_mid_price),
  spread: asNumber(market.spread),
  no_spread: asNumber(market.no_spread),
  volatility: asNumber(market.volatility),
  rolling_mean: asNumber(market.rolling_mean),
  rolling_std: asNumber(market.rolling_std),
  z_score: asNumber(market.z_score),
  liquidity_score: asNumber(market.liquidity_score),
  expected_edge: asNumber(market.expected_edge),
  entry_threshold: asNumber(market.entry_threshold),
  signal_strength: asNumber(market.signal_strength),
  rank_score: asNumber(market.rank_score),
  direction: asString(market.direction, "HOLD"),
  est_profit: asNumber(market.est_profit),
  detected: Boolean(market.detected),
  observations: asNumber(market.observations),
  complement_gap: asNumber(market.complement_gap),
  price_delta: asNumber(market.price_delta),
  momentum: asNumber(market.momentum),
  pressure: asNumber(market.pressure),
  imbalance: asNumber(market.imbalance),
  freshness_ms: asNumber(market.freshness_ms),
  source_delay_ms: asNumber(market.source_delay_ms),
  open_trade_id: typeof market.open_trade_id === "string" ? market.open_trade_id : null,
  open_position: Boolean(market.open_position),
  bootstrap_only: Boolean(market.bootstrap_only),
  quote_source: asString(market.quote_source, "seed"),
  regime: asString(market.regime, "normal"),
  decision_action: asString(market.decision_action, "HOLD"),
  decision_summary: asString(market.decision_summary),
  decision_rejections: Array.isArray(market.decision_rejections)
    ? market.decision_rejections.map((item) => asString(item))
    : [],
  decision_reasons: Array.isArray(market.decision_reasons)
    ? market.decision_reasons.map((item) => asString(item))
    : [],
  explain: Array.isArray(market.explain) ? market.explain.map((item) => asString(item)) : [],
});

const normalizePoint = (
  point: Record<string, unknown>,
  fallbackEquity = STARTING_EQUITY
) => ({
  timestamp: asTimestamp(point.timestamp),
  portfolio: asNumber(point.portfolio, fallbackEquity),
  pnl_pct: asNumber(point.pnl_pct),
});

const normalizeStats = (
  stats: LiveSnapshot["stats"] | Record<string, unknown> | undefined,
  fallbackEquity: number
) => ({
  total_pnl: asNumber(stats?.total_pnl),
  win_rate: asNumber(stats?.win_rate),
  avg_profit: asNumber(stats?.avg_profit),
  active_markets: asNumber(stats?.active_markets),
  detected_arbs_today: asNumber(stats?.detected_arbs_today),
  open_positions: asNumber(stats?.open_positions),
  portfolio_total: asNumber(stats?.portfolio_total, fallbackEquity),
  capital_in_trade: asNumber(stats?.capital_in_trade),
  pnl_percent: asNumber(stats?.pnl_percent),
});

const buildTelemetryPoint = ({
  timestamp,
  equity,
  pnlPercent,
  winRate,
  tradesToday,
  detectedArbsToday,
  latencyMs,
  priceHistory,
}: {
  timestamp: string;
  equity: number;
  pnlPercent: number;
  winRate: number;
  tradesToday: number;
  detectedArbsToday: number;
  latencyMs: number;
  priceHistory: Array<{ timestamp: string; portfolio: number; pnl_pct: number }>;
}): TelemetryPoint => ({
  timestamp,
  equity,
  pnlPercent,
  winRate,
  tradesToday,
  detectedArbsToday,
  sharpe: calculateSharpe(priceHistory),
  latencyMs,
});

const bootstrapTelemetry = (snapshot: LiveSnapshot, trades: Trade[]) => {
  const points =
    snapshot.price_history?.map((point) =>
      normalizePoint(point as unknown as Record<string, unknown>, snapshot.stats?.portfolio_total ?? STARTING_EQUITY)
    ) ?? [];

  if (points.length === 0) {
    const timestamp = snapshot.clock?.server_time ?? new Date().toISOString();
    return [
      buildTelemetryPoint({
        timestamp,
        equity: asNumber(snapshot.stats?.portfolio_total, STARTING_EQUITY),
        pnlPercent: asNumber(snapshot.stats?.pnl_percent),
        winRate: asNumber(snapshot.stats?.win_rate),
        tradesToday: countTradesForDay(trades, timestamp),
        detectedArbsToday: asNumber(snapshot.stats?.detected_arbs_today),
        latencyMs: asNumber(snapshot.bot?.latency_ms),
        priceHistory: [],
      }),
    ];
  }

  return points.map((point, index) =>
    buildTelemetryPoint({
      timestamp: point.timestamp,
      equity: point.portfolio,
      pnlPercent: point.pnl_pct,
      winRate: asNumber(snapshot.stats?.win_rate),
      tradesToday: countTradesForDay(trades, point.timestamp),
      detectedArbsToday: asNumber(snapshot.stats?.detected_arbs_today),
      latencyMs: index === points.length - 1 ? asNumber(snapshot.bot?.latency_ms) : 0,
      priceHistory: points.slice(0, index + 1),
    })
  );
};

const asSnapshot = (payload: LiveSnapshot | Record<string, unknown>) => payload as LiveSnapshot;

const applySnapshotState = (
  state: LiveState,
  payload: LiveSnapshot | Record<string, unknown>
): Partial<LiveState> => {
  const snapshot = asSnapshot(payload);
  const normalizedTrades = Array.isArray(snapshot?.recent_trades)
    ? uniqueRecentTrades(
        snapshot.recent_trades.map((trade) => normalizeTrade(trade as unknown as Record<string, unknown>))
      )
    : state.recentTrades;
  const normalizedPriceHistory = Array.isArray(snapshot?.price_history)
    ? snapshot.price_history.map((point) =>
        normalizePoint(point as unknown as Record<string, unknown>, state.portfolioTotal)
      )
    : state.priceHistory;
  const normalizedStats = normalizeStats(snapshot?.stats, state.portfolioTotal);
  const normalizedMarkets = Array.isArray(snapshot?.markets)
    ? snapshot.markets.map((market) => normalizeMarket(market as unknown as Record<string, unknown>))
    : state.markets;
  const signalHistory = Array.isArray(snapshot?.analytics?.history)
    ? snapshot.analytics.history.map((item) => ({
        timestamp: asTimestamp(item.timestamp),
        opportunity_count: asNumber(item.opportunity_count),
        top_signal_score: asNumber(item.top_signal_score),
        avg_freshness_ms: asNumber(item.avg_freshness_ms),
        data_latency_ms: asNumber(item.data_latency_ms),
      }))
    : state.signalHistory;

  return {
    bootstrapped: true,
    status: asString(snapshot?.bot?.status, state.status),
    uptimeSeconds: asNumber(snapshot?.bot?.uptime_seconds, state.uptimeSeconds),
    latencyMs: asNumber(snapshot?.bot?.latency_ms, state.latencyMs),
    cycleLatencyMs: asNumber(snapshot?.bot?.cycle_latency_ms, state.cycleLatencyMs),
    totalPnl: normalizedStats.total_pnl,
    totalPnlPct: normalizedStats.pnl_percent,
    portfolioTotal: normalizedStats.portfolio_total,
    capitalInTrade: normalizedStats.capital_in_trade,
    winRate: normalizedStats.win_rate,
    avgProfit: normalizedStats.avg_profit,
    activeMarkets: normalizedStats.active_markets,
    openPositions: normalizedStats.open_positions,
    detectedArbsToday: normalizedStats.detected_arbs_today,
    markets: normalizedMarkets,
    priceHistory: normalizedPriceHistory.slice(-MAX_PRICE_POINTS),
    recentTrades: normalizedTrades,
    telemetryHistory: bootstrapTelemetry(
      {
        ...snapshot,
        price_history: normalizedPriceHistory,
        recent_trades: normalizedTrades,
        stats: {
          ...snapshot?.stats,
          ...normalizedStats,
        },
      } as LiveSnapshot,
      normalizedTrades
    ).slice(-MAX_TELEMETRY_POINTS),
    signalHistory: signalHistory.slice(-MAX_SIGNAL_POINTS),
    decisionRanked: Array.isArray(snapshot?.decision_engine?.ranked) ? snapshot.decision_engine.ranked : state.decisionRanked,
    positions: Array.isArray(snapshot?.positions?.items) ? snapshot.positions.items : state.positions,
    logs: Array.isArray(snapshot?.logs) ? snapshot.logs : state.logs,
    sources: Array.isArray(snapshot?.ingestion?.sources) ? snapshot.ingestion.sources : state.sources,
    riskConfig: snapshot?.risk_config ?? state.riskConfig,
    analyticsSummary: snapshot?.analytics?.summary ?? state.analyticsSummary,
    decisionSummary: snapshot?.decision_engine?.summary ?? state.decisionSummary,
    ingestion: snapshot?.ingestion ?? state.ingestion,
    positionsSummary: snapshot?.positions ?? state.positionsSummary,
    snapshotTime: asTimestamp(snapshot?.clock?.server_time ?? snapshot?.timestamp, state.snapshotTime),
    lastEventAt: asTimestamp(snapshot?.timestamp ?? snapshot?.clock?.server_time, state.lastEventAt),
  };
};

export const useLiveStore = create<LiveState>((set, get) => ({
  bootstrapped: false,
  connectionState: "disconnected",
  reconnectAttempt: 0,
  status: "PAUSED",
  uptimeSeconds: 0,
  latencyMs: 0,
  cycleLatencyMs: 0,
  totalPnl: 0,
  totalPnlPct: 0,
  portfolioTotal: STARTING_EQUITY,
  capitalInTrade: 0,
  winRate: 0,
  avgProfit: 0,
  activeMarkets: 0,
  openPositions: 0,
  detectedArbsToday: 0,
  markets: [],
  priceHistory: [],
  recentTrades: [],
  telemetryHistory: [],
  signalHistory: [],
  decisionRanked: [],
  positions: [],
  logs: [],
  sources: [],
  riskConfig: null,
  walletBalance: STARTING_EQUITY,
  walletAddress: undefined,
  walletToken: undefined,
  walletConnecting: false,
  halt: { active: false, reason: "", details: null, at: null },
  controlPending: false,
  lastEventAt: undefined,
  snapshotTime: undefined,
  analyticsSummary: null,
  decisionSummary: null,
  ingestion: null,
  positionsSummary: null,

  setWalletBalance: (walletBalance) => set({ walletBalance }),
  setWallet: (walletAddress, walletBalance = 0, walletToken) =>
    set({ walletAddress, walletBalance, walletToken }),
  setWalletConnecting: (walletConnecting) => set({ walletConnecting }),

  setConnectionState: (connectionState, reconnectAttempt = 0) =>
    set({
      connectionState,
      reconnectAttempt,
    }),

  processBootstrap: (payload) => set((state) => applySnapshotState(state, payload)),

  processTick: (payload) => set((state) => applySnapshotState(state, payload)),

  processTrade: (payload) =>
    set((state) => {
      const trade = normalizeTrade(payload as Record<string, unknown>);
      const nextTrades = uniqueRecentTrades([trade, ...state.recentTrades]);
      const timestamp = trade.timestamp;
      const nextTelemetry = buildTelemetryPoint({
        timestamp,
        equity: state.priceHistory.at(-1)?.portfolio ?? state.portfolioTotal,
        pnlPercent: state.totalPnlPct,
        winRate: state.winRate,
        tradesToday: countTradesForDay(nextTrades, timestamp),
        detectedArbsToday: state.detectedArbsToday,
        latencyMs: state.latencyMs,
        priceHistory: state.priceHistory,
      });

      return {
        recentTrades: nextTrades,
        openPositions: nextTrades.filter((entry) => entry.status === "OPEN").length,
        telemetryHistory: [...state.telemetryHistory, nextTelemetry].slice(-MAX_TELEMETRY_POINTS),
        lastEventAt: timestamp,
      };
    }),

  processHalt: (payload) =>
    set({
      status: "STOPPED",
      halt: {
        active: true,
        reason: asString(payload?.reason, "halt"),
        details:
          payload?.details === null || payload?.details === undefined
            ? null
            : typeof payload.details === "string"
              ? payload.details
              : JSON.stringify(payload.details),
        at: new Date().toISOString(),
      },
      lastEventAt: new Date().toISOString(),
    }),

  clearHalt: () =>
    set((state) => ({
      halt: {
        ...state.halt,
        active: false,
      },
    })),

  sendBotCommand: async (command) => {
    set({ controlPending: true });

    try {
      const response = await fetch(apiUrl("/api/v1/bot/control"), {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ command }),
      });

      if (!response.ok) {
        throw new Error(`Bot control failed with status ${response.status}`);
      }

      const data = await response.json();
      if (data?.data) {
        get().processBootstrap(data.data);
      }

      if (command === "start") {
        get().clearHalt();
      }
    } finally {
      set({ controlPending: false });
    }
  },

  toggleBot: async () => {
    const state = get();
    await state.sendBotCommand(state.status === "RUNNING" ? "pause" : "start");
  },
}));
