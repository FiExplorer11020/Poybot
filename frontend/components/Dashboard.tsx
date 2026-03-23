"use client";

import { useMemo, useState } from "react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  BellRing,
  CandlestickChart,
  Gauge,
  ShieldAlert,
  TrendingUp,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { MarketScanner } from "@/components/trading/MarketScanner";
import { TradeFeed } from "@/components/trading/TradeFeed";
import { cn, formatMoney, formatPct } from "@/lib/utils";
import { useLiveStore } from "@/store/useLiveStore";

type TimeframeKey = "1h" | "24h" | "7d" | "30d";

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const TIMEFRAME_MS: Record<TimeframeKey, number> = {
  "1h": HOUR_MS,
  "24h": DAY_MS,
  "7d": 7 * DAY_MS,
  "30d": 30 * DAY_MS,
};

const connectionTone: Record<string, string> = {
  connected: "border-[rgba(0,212,170,0.26)] bg-[rgba(0,212,170,0.1)] text-[#9af4df]",
  reconnecting: "border-[rgba(255,184,77,0.26)] bg-[rgba(255,184,77,0.12)] text-[#ffd89c]",
  disconnected: "border-[rgba(255,107,107,0.24)] bg-[rgba(255,107,107,0.12)] text-[#ffb3b3]",
};

const connectionDotTone: Record<string, string> = {
  connected: "bg-[#00d4aa]",
  reconnecting: "bg-[#ffb84d]",
  disconnected: "bg-[#ff6b6b]",
};

const axisTickStyle = { fill: "rgba(255,255,255,0.38)", fontSize: 11, fontFamily: "var(--font-jetbrains-mono)" };

const chartValueFormatter = (value: number) => formatMoney(Number(value ?? 0));

const sharpeFromEquity = (points: Array<{ portfolio: number }>) => {
  if (points.length < 3) {
    return 0;
  }

  const returns: number[] = [];
  for (let index = 1; index < points.length; index += 1) {
    const previous = Number(points[index - 1]?.portfolio ?? 0);
    const current = Number(points[index]?.portfolio ?? 0);
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
  const deviation = Math.sqrt(variance);

  if (!Number.isFinite(deviation) || deviation === 0) {
    return 0;
  }

  return Number(((mean / deviation) * Math.sqrt(Math.min(returns.length, 1440))).toFixed(2));
};

const formatXAxisLabel = (timestamp: string, timeframe: TimeframeKey) => {
  const date = new Date(timestamp);

  if (timeframe === "1h" || timeframe === "24h") {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  return date.toLocaleDateString([], { month: "short", day: "numeric" });
};

const isToday = (timestamp: string) => {
  const tradeDate = new Date(timestamp);
  const now = new Date();
  return (
    tradeDate.getFullYear() === now.getFullYear() &&
    tradeDate.getMonth() === now.getMonth() &&
    tradeDate.getDate() === now.getDate()
  );
};

const buildTradeMetricSeries = (timestamps: string[], values: number[]) =>
  timestamps.map((timestamp, index) => ({
    timestamp,
    value: values[index] ?? 0,
  }));

const countArbCandidates = (markets: ReturnType<typeof useLiveStore.getState>["markets"]) =>
  markets.filter((market) => market.detected).length;

function Sparkline({ data, color }: { data: Array<{ timestamp: string; value: number }>; color: string }) {
  const points = data.length > 0 ? data : [{ timestamp: new Date().toISOString(), value: 0 }];

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={points}>
        <Line
          type="monotone"
          dataKey="value"
          stroke={color}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

function KpiCard({
  label,
  value,
  note,
  sparkline,
  accent,
  icon,
}: {
  label: string;
  value: string;
  note: string;
  sparkline: Array<{ timestamp: string; value: number }>;
  accent: string;
  icon: React.ReactNode;
}) {
  return (
    <div className="rounded-[24px] border border-white/8 bg-[linear-gradient(180deg,rgba(255,255,255,0.05),rgba(255,255,255,0.02))] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] uppercase tracking-[0.24em] text-white/40">{label}</p>
        <div className="flex h-9 w-9 items-center justify-center rounded-2xl border border-white/8 bg-black/20 text-white/70">
          {icon}
        </div>
      </div>
      <div className="mt-4">
        <span key={value} className="inline-flex animate-value-flip font-mono text-[28px] font-semibold tracking-[-0.04em] text-white">
          {value}
        </span>
        <p className="mt-2 text-xs text-white/45">{note}</p>
      </div>
      <div className="mt-4 h-12">
        <Sparkline data={sparkline} color={accent} />
      </div>
    </div>
  );
}

export function Dashboard() {
  const [timeframe, setTimeframe] = useState<TimeframeKey>("24h");

  const connectionState = useLiveStore((state) => state.connectionState);
  const reconnectAttempt = useLiveStore((state) => state.reconnectAttempt);
  const halt = useLiveStore((state) => state.halt);
  const priceHistory = useLiveStore((state) => state.priceHistory);
  const recentTrades = useLiveStore((state) => state.recentTrades);
  const telemetryHistory = useLiveStore((state) => state.telemetryHistory);
  const markets = useLiveStore((state) => state.markets);
  const winRate = useLiveStore((state) => state.winRate);
  const detectedArbsToday = useLiveStore((state) => state.detectedArbsToday);
  const totalPnl = useLiveStore((state) => state.totalPnl);
  const totalPnlPct = useLiveStore((state) => state.totalPnlPct);
  const portfolioTotal = useLiveStore((state) => state.portfolioTotal);

  const tradesToday = useMemo(() => recentTrades.filter((trade) => isToday(trade.timestamp)).length, [recentTrades]);

  const hourlyTradeMetrics = useMemo(() => {
    const start = Date.now() - DAY_MS;
    const allTrades = [...recentTrades]
      .filter((trade) => Date.parse(trade.timestamp) >= start)
      .sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
    const closedTrades = allTrades.filter((trade) => trade.status === "CLOSED");

    let tradeIndex = 0;
    let closedIndex = 0;
    let wins = 0;
    let closedCount = 0;

    const timestamps: string[] = [];
    const tradeCounts: number[] = [];
    const winRates: number[] = [];

    for (let bucket = 0; bucket < 24; bucket += 1) {
      const bucketStart = start + bucket * HOUR_MS;
      const bucketEnd = bucketStart + HOUR_MS;

      let bucketCount = 0;
      while (tradeIndex < allTrades.length && Date.parse(allTrades[tradeIndex].timestamp) < bucketEnd) {
        bucketCount += 1;
        tradeIndex += 1;
      }

      while (closedIndex < closedTrades.length && Date.parse(closedTrades[closedIndex].timestamp) < bucketEnd) {
        closedCount += 1;
        if (closedTrades[closedIndex].pnl_abs > 0) {
          wins += 1;
        }
        closedIndex += 1;
      }

      timestamps.push(new Date(bucketEnd).toISOString());
      tradeCounts.push(bucketCount);
      winRates.push(closedCount > 0 ? Number(((wins / closedCount) * 100).toFixed(2)) : 0);
    }

    return {
      trades: buildTradeMetricSeries(timestamps, tradeCounts),
      winRate: buildTradeMetricSeries(timestamps, winRates),
    };
  }, [recentTrades]);

  const telemetry24h = useMemo(() => {
    const cutoff = Date.now() - DAY_MS;
    return telemetryHistory.filter((point) => Date.parse(point.timestamp) >= cutoff);
  }, [telemetryHistory]);

  const arbSparkline = useMemo(
    () =>
      (telemetry24h.length > 0 ? telemetry24h : telemetryHistory.slice(-24)).map((point) => ({
        timestamp: point.timestamp,
        value: point.detectedArbsToday,
      })),
    [telemetry24h, telemetryHistory]
  );

  const sharpeSparkline = useMemo(
    () =>
      (telemetry24h.length > 0 ? telemetry24h : telemetryHistory.slice(-24)).map((point) => ({
        timestamp: point.timestamp,
        value: point.sharpe,
      })),
    [telemetry24h, telemetryHistory]
  );

  const sharpeEstimate = useMemo(() => {
    const telemetryValue = telemetryHistory.at(-1)?.sharpe;
    if (typeof telemetryValue === "number" && Number.isFinite(telemetryValue)) {
      return telemetryValue;
    }
    return sharpeFromEquity(priceHistory);
  }, [telemetryHistory, priceHistory]);

  const equitySeries = useMemo(() => {
    const fullSeries = [...priceHistory].sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
    const cutoff = Date.now() - TIMEFRAME_MS[timeframe];
    const filtered = fullSeries.filter((point) => Date.parse(point.timestamp) >= cutoff);
    const source = filtered.length > 1 ? filtered : fullSeries.slice(-Math.min(fullSeries.length, 60));

    let peak = Number.NEGATIVE_INFINITY;

    return source.map((point) => {
      peak = Math.max(peak, Number(point.portfolio ?? 0));
      const drawdownGap = Math.max(peak - Number(point.portfolio ?? 0), 0);
      return {
        ...point,
        drawdownGap,
        peak,
      };
    });
  }, [priceHistory, timeframe]);

  const drawdownNow = equitySeries.at(-1)?.drawdownGap ?? 0;
  const maxDrawdown = useMemo(
    () => equitySeries.reduce((worst, point) => Math.max(worst, point.drawdownGap), 0),
    [equitySeries]
  );
  const liveEquity = equitySeries.at(-1)?.portfolio ?? portfolioTotal + totalPnl;

  const kpis = [
    {
      label: "Win rate",
      value: `${winRate.toFixed(1)}%`,
      note: "Closed trades converted to winners.",
      sparkline: hourlyTradeMetrics.winRate,
      accent: "#00d4aa",
      icon: <TrendingUp size={16} />,
    },
    {
      label: "Trades today",
      value: String(tradesToday),
      note: "Executed since local midnight.",
      sparkline: hourlyTradeMetrics.trades,
      accent: "#9fd5ff",
      icon: <CandlestickChart size={16} />,
    },
    {
      label: "Arbs detectes",
      value: String(detectedArbsToday),
      note: `${countArbCandidates(markets)} live signals currently highlighted.`,
      sparkline: arbSparkline,
      accent: "#ffd166",
      icon: <BellRing size={16} />,
    },
    {
      label: "Sharpe estime",
      value: sharpeEstimate.toFixed(2),
      note: "Estimated from rolling equity returns.",
      sparkline: sharpeSparkline,
      accent: "#ff6b6b",
      icon: <Gauge size={16} />,
    },
  ];

  return (
    <div className="space-y-6 pb-8">
      <section className="rounded-[30px] border border-white/8 bg-[rgba(22,25,33,0.94)] px-5 py-5 shadow-[0_24px_80px_rgba(0,0,0,0.35)] sm:px-6">
        <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-3">
            <p className="text-[11px] uppercase tracking-[0.3em] text-white/38">Polymarket live operations</p>
            <div>
              <h1 className="text-3xl font-semibold tracking-[-0.05em] text-white sm:text-[40px]">
                Live execution surface for the Polymarket bot
              </h1>
              <p className="mt-3 max-w-2xl text-sm leading-6 text-white/52">
                Watch latency, equity, detected edges and the execution tape in one dense surface optimized for fast operational scans.
              </p>
            </div>
          </div>

          <div className="flex flex-col items-start gap-3 xl:items-end">
            <div
              className={cn(
                "inline-flex items-center gap-3 rounded-full border px-4 py-2 text-xs uppercase tracking-[0.24em]",
                connectionTone[connectionState]
              )}
            >
              <span className={cn("h-2.5 w-2.5 rounded-full animate-signal-pulse", connectionDotTone[connectionState])} />
              {connectionState}
              {connectionState === "reconnecting" ? ` #${reconnectAttempt}` : ""}
            </div>

            {halt.active ? (
              <div className="animate-halt-alert rounded-full border border-[rgba(255,107,107,0.32)] bg-[rgba(255,107,107,0.12)] px-4 py-2 text-xs font-semibold uppercase tracking-[0.24em] text-[#ffb3b3]">
                <ShieldAlert size={14} className="mr-2 inline-block" />
                Halt active: {halt.reason}
              </div>
            ) : (
              <div className="rounded-full border border-white/8 bg-black/20 px-4 py-2 text-xs uppercase tracking-[0.24em] text-white/45">
                Equity {formatMoney(liveEquity)}
              </div>
            )}
          </div>
        </div>

        <div className="mt-6 grid gap-4 lg:grid-cols-2 2xl:grid-cols-4">
          {kpis.map((metric) => (
            <KpiCard key={metric.label} {...metric} />
          ))}
        </div>
      </section>

      <section className="rounded-[30px] border border-white/8 bg-[rgba(22,25,33,0.94)] px-5 py-5 shadow-[0_24px_80px_rgba(0,0,0,0.35)] sm:px-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-[11px] uppercase tracking-[0.28em] text-white/40">Equity curve</p>
            <h2 className="mt-2 text-2xl font-semibold tracking-[-0.04em] text-white">Real-time portfolio trajectory</h2>
            <div className="mt-3 flex flex-wrap gap-3 text-sm text-white/45">
              <span className="inline-flex items-center gap-2 rounded-full border border-white/8 bg-black/20 px-3 py-1.5">
                <Activity size={14} className="text-[#00d4aa]" />
                PnL {formatPct(totalPnlPct)}
              </span>
              <span className="inline-flex items-center gap-2 rounded-full border border-white/8 bg-black/20 px-3 py-1.5">
                {drawdownNow > 0 ? <ArrowDownRight size={14} className="text-[#ff6b6b]" /> : <ArrowUpRight size={14} className="text-[#00d4aa]" />}
                Max drawdown {formatMoney(maxDrawdown)}
              </span>
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            {(Object.keys(TIMEFRAME_MS) as TimeframeKey[]).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setTimeframe(option)}
                className={cn(
                  "rounded-full border px-4 py-2 font-mono text-xs uppercase tracking-[0.24em] transition-colors",
                  timeframe === option
                    ? "border-[rgba(0,212,170,0.28)] bg-[rgba(0,212,170,0.1)] text-[#9af4df]"
                    : "border-white/8 bg-black/15 text-white/48 hover:border-white/16 hover:text-white/75"
                )}
              >
                {option}
              </button>
            ))}
          </div>
        </div>

        <div className="mt-6 h-[380px]">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={equitySeries}>
              <defs>
                <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#00d4aa" stopOpacity={0.28} />
                  <stop offset="100%" stopColor="#00d4aa" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="drawdownFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#ff6b6b" stopOpacity={0.18} />
                  <stop offset="100%" stopColor="#ff6b6b" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(255,255,255,0.08)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="timestamp"
                minTickGap={24}
                tickFormatter={(value) => formatXAxisLabel(String(value), timeframe)}
                axisLine={false}
                tickLine={false}
                tick={axisTickStyle}
              />
              <YAxis
                width={88}
                tickFormatter={(value) => `$${Number(value).toLocaleString("en-US")}`}
                axisLine={false}
                tickLine={false}
                tick={axisTickStyle}
              />
              <Tooltip
                cursor={{ stroke: "rgba(255,255,255,0.12)", strokeWidth: 1 }}
                contentStyle={{
                  background: "rgba(13, 15, 20, 0.92)",
                  borderColor: "rgba(255,255,255,0.12)",
                  borderRadius: "18px",
                  color: "#ffffff",
                  boxShadow: "0 20px 80px rgba(0,0,0,0.35)",
                }}
                formatter={(value: number, key: string) => {
                  if (key === "drawdownGap") {
                    return [chartValueFormatter(value), "Drawdown"];
                  }
                  return [chartValueFormatter(value), "Equity"];
                }}
                labelFormatter={(value) => new Date(String(value)).toLocaleString()}
              />
              <Area type="monotone" dataKey="portfolio" stackId="drawdown" stroke="none" fill="transparent" />
              <Area type="monotone" dataKey="drawdownGap" stackId="drawdown" stroke="none" fill="url(#drawdownFill)" />
              <Area
                type="monotone"
                dataKey="portfolio"
                stroke="#00d4aa"
                strokeWidth={2.6}
                fill="url(#equityFill)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.35fr)_minmax(360px,0.95fr)]">
        <MarketScanner markets={markets} />
        <TradeFeed trades={recentTrades} />
      </div>
    </div>
  );
}
