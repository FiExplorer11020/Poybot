"use client";

import { create } from "zustand";

import { apiHeaders, apiUrl } from "@/lib/api";
import type { BotTrade, LiveSnapshot } from "@/lib/types";

export type LiveConnectionState = "connected" | "reconnecting" | "disconnected";
export type BotCommand = "start" | "pause" | "stop";

type LiveStats = LiveSnapshot["stats"] & {
  open_positions?: number;
};

type EquityPoint = LiveSnapshot["price_history"][number];

export interface Market {
  market_id: string;
  title: string;
  end_date?: string;
  token_id_yes: string;
  token_id_no: string;
  best_bid: number;
  best_ask: number;
  mid_price: number;
  spread: number;
  volatility: number;
  liquidity_score: number;
  expected_edge: number;
  entry_threshold: number;
  signal_strength: number;
  direction: string;
  est_profit: number;
  detected: boolean;
  observations: number;
  complement_gap: number;
}

export interface Trade extends BotTrade {}

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
  priceHistory: EquityPoint[];
  recentTrades: Trade[];
  telemetryHistory: TelemetryPoint[];
  riskConfig: LiveSnapshot["risk_config"] | null;
  walletBalance: number;
  walletAddress?: string;
  walletToken?: string;
  walletConnecting: boolean;
  halt: HaltState;
  controlPending: boolean;
  lastEventAt?: string;

  setWalletBalance: (balance: number) => void;
  setWallet: (address?: string, balance?: number, token?: string) => void;
  setWalletConnecting: (connecting: boolean) => void;
  setConnectionState: (state: LiveConnectionState, attempt?: number) => void;
  processBootstrap: (payload: LiveSnapshot | Record<string, unknown>) => void;
  processTick: (payload: Record<string, unknown>) => void;
  processTrade: (payload: BotTrade | Record<string, unknown>) => void;
  processHalt: (payload: { reason?: unknown; details?: unknown } | Record<string, unknown>) => void;
  clearHalt: () => void;
  sendBotCommand: (command: BotCommand) => Promise<void>;
  toggleBot: () => Promise<void>;
}

const MAX_PRICE_POINTS = 1200;
const MAX_RECENT_TRADES = 300;
const MAX_TELEMETRY_POINTS = 1200;
const STARTING_EQUITY = 25_000;

const asNumber = (value: unknown, fallback = 0) => {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
};

const asString = (value: unknown, fallback = "") =>
  typeof value === "string" && value.trim().length > 0 ? value : fallback;

const asTimestamp = (value: unknown) => {
  if (typeof value !== "string") {
    return new Date().toISOString();
  }

  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? new Date().toISOString() : new Date(parsed).toISOString();
};

const dayKey = (timestamp: string) => {
  const date = new Date(timestamp);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
};

const calculateSharpe = (points: EquityPoint[]) => {
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

const normalizeMarket = (market: Record<string, unknown>): Market => ({
  market_id: asString(market.market_id),
  title: asString(market.title, "Untitled market"),
  end_date: typeof market.end_date === "string" ? market.end_date : undefined,
  token_id_yes: asString(market.token_id_yes),
  token_id_no: asString(market.token_id_no),
  best_bid: asNumber(market.best_bid),
  best_ask: asNumber(market.best_ask),
  mid_price: asNumber(market.mid_price),
  spread: asNumber(market.spread),
  volatility: asNumber(market.volatility),
  liquidity_score: asNumber(market.liquidity_score),
  expected_edge: asNumber(market.expected_edge),
  entry_threshold: asNumber(market.entry_threshold),
  signal_strength: asNumber(market.signal_strength),
  direction: asString(market.direction, "HOLD"),
  est_profit: asNumber(market.est_profit),
  detected: Boolean(market.detected),
  observations: asNumber(market.observations),
  complement_gap: asNumber(market.complement_gap),
});

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
  };
};

const normalizePoint = (point: Record<string, unknown>, fallbackEquity = STARTING_EQUITY): EquityPoint => ({
  timestamp: asTimestamp(point.timestamp),
  portfolio: asNumber(point.portfolio, fallbackEquity),
  pnl_pct: asNumber(point.pnl_pct),
});

const normalizeStats = (stats: Record<string, unknown> | undefined, fallbackEquity: number): LiveStats => ({
  total_pnl: asNumber(stats?.total_pnl),
  win_rate: asNumber(stats?.win_rate),
  avg_profit: asNumber(stats?.avg_profit),
  active_markets: asNumber(stats?.active_markets),
  detected_arbs_today: asNumber(stats?.detected_arbs_today),
  portfolio_total: asNumber(stats?.portfolio_total, fallbackEquity),
  capital_in_trade: asNumber(stats?.capital_in_trade),
  pnl_percent: asNumber(stats?.pnl_percent),
  open_positions: asNumber(stats?.open_positions),
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
  priceHistory: EquityPoint[];
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

const uniqueRecentTrades = (trades: Trade[]) => {
  const deduped = new Map<string, Trade>();

  for (const trade of trades) {
    deduped.set(trade.id, trade);
  }

  return Array.from(deduped.values())
    .sort((left, right) => Date.parse(right.timestamp) - Date.parse(left.timestamp))
    .slice(0, MAX_RECENT_TRADES);
};

const bootstrapTelemetry = (snapshot: LiveSnapshot, trades: Trade[]) => {
  const points =
    snapshot.price_history?.map((point) =>
      normalizePoint(point as unknown as Record<string, unknown>, snapshot.stats?.portfolio_total ?? STARTING_EQUITY)
    ) ?? [];

  if (points.length === 0) {
    const timestamp = new Date().toISOString();
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

export const useLiveStore = create<LiveState>((set, get) => ({
  bootstrapped: false,
  connectionState: "disconnected",
  reconnectAttempt: 0,
  status: "PAUSED",
  uptimeSeconds: 0,
  latencyMs: 0,
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
  riskConfig: null,
  walletBalance: STARTING_EQUITY,
  walletAddress: undefined,
  walletToken: undefined,
  walletConnecting: false,
  halt: { active: false, reason: "", details: null, at: null },
  controlPending: false,
  lastEventAt: undefined,

  setWalletBalance: (walletBalance) => set({ walletBalance }),
  setWallet: (walletAddress, walletBalance = 0, walletToken) =>
    set({ walletAddress, walletBalance, walletToken }),
  setWalletConnecting: (walletConnecting) => set({ walletConnecting }),

  setConnectionState: (connectionState, reconnectAttempt = 0) =>
    set({
      connectionState,
      reconnectAttempt,
    }),

  processBootstrap: (payload) =>
    set((state) => {
      const snapshot = payload as LiveSnapshot;
      const normalizedTrades = Array.isArray(snapshot?.recent_trades)
        ? uniqueRecentTrades(snapshot.recent_trades.map((trade) => normalizeTrade(trade as unknown as Record<string, unknown>)))
        : state.recentTrades;
      const normalizedPriceHistory = Array.isArray(snapshot?.price_history)
        ? snapshot.price_history.map((point) =>
            normalizePoint(point as unknown as Record<string, unknown>, state.portfolioTotal)
          )
        : state.priceHistory;
      const normalizedStats = normalizeStats(snapshot?.stats as Record<string, unknown> | undefined, state.portfolioTotal);

      return {
        bootstrapped: true,
        status: asString(snapshot?.bot?.status, state.status),
        uptimeSeconds: asNumber(snapshot?.bot?.uptime_seconds, state.uptimeSeconds),
        latencyMs: asNumber(snapshot?.bot?.latency_ms, state.latencyMs),
        totalPnl: normalizedStats.total_pnl,
        totalPnlPct: normalizedStats.pnl_percent,
        portfolioTotal: normalizedStats.portfolio_total,
        capitalInTrade: normalizedStats.capital_in_trade,
        winRate: normalizedStats.win_rate,
        avgProfit: normalizedStats.avg_profit,
        activeMarkets: normalizedStats.active_markets,
        openPositions: normalizedStats.open_positions ?? state.openPositions,
        detectedArbsToday: normalizedStats.detected_arbs_today,
        markets: Array.isArray(snapshot?.markets)
          ? snapshot.markets.map((market) => normalizeMarket(market as unknown as Record<string, unknown>))
          : state.markets,
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
        riskConfig: snapshot?.risk_config ?? state.riskConfig,
        lastEventAt: new Date().toISOString(),
      };
    }),

  processTick: (payload) =>
    set((state) => {
      const stats = normalizeStats(payload?.stats as Record<string, unknown> | undefined, state.portfolioTotal);
      const nextPriceHistory =
        payload?.point && typeof payload.point === "object"
          ? [...state.priceHistory, normalizePoint(payload.point as Record<string, unknown>, state.portfolioTotal)]
              .slice(-MAX_PRICE_POINTS)
          : state.priceHistory;
      const timestamp =
        typeof payload?.point === "object" && payload?.point
          ? asTimestamp((payload.point as Record<string, unknown>).timestamp)
          : new Date().toISOString();

      const nextTelemetryPoint = buildTelemetryPoint({
        timestamp,
        equity: nextPriceHistory.at(-1)?.portfolio ?? stats.portfolio_total,
        pnlPercent: nextPriceHistory.at(-1)?.pnl_pct ?? stats.pnl_percent,
        winRate: stats.win_rate,
        tradesToday: countTradesForDay(state.recentTrades, timestamp),
        detectedArbsToday: stats.detected_arbs_today,
        latencyMs: asNumber(payload?.latency_ms, state.latencyMs),
        priceHistory: nextPriceHistory,
      });

      return {
        latencyMs: asNumber(payload?.latency_ms, state.latencyMs),
        totalPnl: stats.total_pnl,
        totalPnlPct: stats.pnl_percent,
        portfolioTotal: stats.portfolio_total,
        capitalInTrade: stats.capital_in_trade,
        winRate: stats.win_rate,
        avgProfit: stats.avg_profit,
        activeMarkets: stats.active_markets,
        openPositions: stats.open_positions ?? state.openPositions,
        detectedArbsToday: stats.detected_arbs_today,
        uptimeSeconds: state.status === "RUNNING" ? state.uptimeSeconds + 1 : state.uptimeSeconds,
        markets: Array.isArray(payload?.markets)
          ? payload.markets.map((market) => normalizeMarket(market as Record<string, unknown>))
          : state.markets,
        priceHistory: nextPriceHistory,
        telemetryHistory: [...state.telemetryHistory, nextTelemetryPoint].slice(-MAX_TELEMETRY_POINTS),
        lastEventAt: timestamp,
      };
    }),

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
