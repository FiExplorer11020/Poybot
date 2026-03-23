"use client";

import type { ReactNode } from "react";
import { useMemo } from "react";
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
import {
  Activity,
  Bot,
  Clock3,
  Radar,
  ShieldCheck,
  Signal,
  TimerReset,
  TrendingUp,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { useRealtimeClock } from "@/lib/useRealtimeClock";
import { cn, formatMoney, formatPct } from "@/lib/utils";
import { useBotStore } from "@/store/useBotStore";

const axisTickStyle = {
  fill: "rgba(255,255,255,0.38)",
  fontSize: 11,
  fontFamily: "var(--font-jetbrains-mono)",
};

const formatShortTime = (timestamp: string) =>
  new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const formatDelay = (ms: number) => {
  if (ms >= 1000) {
    return `${(ms / 1000).toFixed(1)}s`;
  }
  return `${Math.round(ms)}ms`;
};

const statusTone = (status: string) => {
  if (status === "RUNNING") {
    return "border-emerald-400/25 bg-emerald-400/10 text-emerald-100";
  }
  if (status === "PAUSED") {
    return "border-amber-400/25 bg-amber-400/10 text-amber-100";
  }
  return "border-rose-400/25 bg-rose-400/10 text-rose-100";
};

const connectionTone = (state: string) => {
  if (state === "connected") {
    return "border-cyan-400/25 bg-cyan-400/10 text-cyan-100";
  }
  if (state === "reconnecting") {
    return "border-amber-400/25 bg-amber-400/10 text-amber-100";
  }
  return "border-rose-400/25 bg-rose-400/10 text-rose-100";
};

const sourceTone = (status: string) => {
  if (status === "live") {
    return "bg-emerald-400";
  }
  if (status === "connecting") {
    return "bg-amber-400";
  }
  return "bg-rose-400";
};

function SectionTitle({
  icon,
  eyebrow,
  title,
  note,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  note: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-white/8 px-5 py-4 sm:px-6">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.28em] text-white/40">
          {icon}
          <span>{eyebrow}</span>
        </div>
        <h2 className="mt-2 text-xl font-semibold tracking-[-0.03em] text-white">{title}</h2>
      </div>
      <p className="max-w-xs text-right text-sm leading-6 text-white/45">{note}</p>
    </div>
  );
}

export default function ScannerPage() {
  const nowMs = useRealtimeClock(500);

  const status = useBotStore((state) => state.status);
  const connectionState = useBotStore((state) => state.connectionState);
  const snapshotTime = useBotStore((state) => state.snapshotTime);
  const uptimeSeconds = useBotStore((state) => state.uptimeSeconds);
  const latencyMs = useBotStore((state) => state.latencyMs);
  const cycleLatencyMs = useBotStore((state) => state.cycleLatencyMs);
  const totalPnl = useBotStore((state) => state.totalPnl);
  const totalPnlPct = useBotStore((state) => state.totalPnlPct);
  const portfolioTotal = useBotStore((state) => state.portfolioTotal);
  const capitalInTrade = useBotStore((state) => state.capitalInTrade);
  const openPositions = useBotStore((state) => state.openPositions);
  const markets = useBotStore((state) => state.markets);
  const signalHistory = useBotStore((state) => state.signalHistory);
  const priceHistory = useBotStore((state) => state.priceHistory);
  const decisionRanked = useBotStore((state) => state.decisionRanked);
  const positions = useBotStore((state) => state.positions);
  const logs = useBotStore((state) => state.logs);
  const sources = useBotStore((state) => state.sources);
  const analyticsSummary = useBotStore((state) => state.analyticsSummary);
  const decisionSummary = useBotStore((state) => state.decisionSummary);
  const ingestion = useBotStore((state) => state.ingestion);

  const snapshotAgeMs = useMemo(() => {
    if (!snapshotTime) {
      return 0;
    }
    const parsed = Date.parse(snapshotTime);
    if (Number.isNaN(parsed)) {
      return 0;
    }
    return Math.max(0, nowMs - parsed);
  }, [nowMs, snapshotTime]);

  const liveUptimeSeconds = useMemo(() => {
    if (status !== "RUNNING") {
      return uptimeSeconds;
    }
    return uptimeSeconds + Math.floor(snapshotAgeMs / 1000);
  }, [snapshotAgeMs, status, uptimeSeconds]);

  const liveDelayMs = useMemo(() => {
    if (status !== "RUNNING") {
      return latencyMs;
    }
    return latencyMs + snapshotAgeMs;
  }, [latencyMs, snapshotAgeMs, status]);

  const opportunityRows = useMemo(
    () =>
      [...markets]
        .filter((market) => market.decision_action === "OPEN" || market.detected)
        .sort(
          (left, right) =>
            right.signal_strength - left.signal_strength ||
            right.expected_edge - left.expected_edge ||
            left.freshness_ms! - right.freshness_ms!
        )
        .slice(0, 12),
    [markets]
  );

  const rankedDecisions = useMemo(() => decisionRanked.slice(0, 10), [decisionRanked]);
  const marketRows = useMemo(() => markets.slice(0, 10), [markets]);

  const equitySeries = useMemo(
    () =>
      (priceHistory.length > 0 ? priceHistory : [{ timestamp: new Date().toISOString(), portfolio: portfolioTotal, pnl_pct: totalPnlPct }]).slice(-120),
    [portfolioTotal, priceHistory, totalPnlPct]
  );

  const signalSeries = useMemo(
    () =>
      (signalHistory.length > 0
        ? signalHistory
        : [
            {
              timestamp: new Date().toISOString(),
              opportunity_count: analyticsSummary?.opportunity_count ?? 0,
              top_signal_score: analyticsSummary?.top_signal_score ?? 0,
              avg_freshness_ms: ingestion?.avg_freshness_ms ?? 0,
              data_latency_ms: ingestion?.avg_source_delay_ms ?? 0,
            },
          ]).slice(-120),
    [analyticsSummary, ingestion, signalHistory]
  );

  return (
    <div className="space-y-6 animate-in fade-in duration-500">
      <section className="relative overflow-hidden rounded-[32px] border border-white/8 bg-[linear-gradient(180deg,rgba(21,25,34,0.96),rgba(12,15,21,0.98))] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(0,212,170,0.12),transparent_36%),radial-gradient(circle_at_78%_12%,rgba(56,189,248,0.10),transparent_28%),linear-gradient(180deg,transparent,rgba(0,0,0,0.18))]" />
        <div className="relative grid gap-6 px-5 py-6 sm:px-6 xl:grid-cols-[minmax(0,1.35fr)_340px]">
          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className={cn("border px-3 py-1 text-[11px] uppercase tracking-[0.24em]", statusTone(status))}>
                {status}
              </Badge>
              <Badge className={cn("border px-3 py-1 text-[11px] uppercase tracking-[0.24em]", connectionTone(connectionState))}>
                {connectionState}
              </Badge>
              <Badge className="border-white/10 bg-black/20 px-3 py-1 text-[11px] uppercase tracking-[0.24em] text-white/70">
                {ingestion?.status ?? "idle"} ingestion
              </Badge>
            </div>

            <div>
              <p className="text-[11px] uppercase tracking-[0.32em] text-white/38">Quant scanner</p>
              <h1 className="mt-3 max-w-4xl text-3xl font-semibold tracking-[-0.05em] text-white sm:text-4xl">
                Derived analytics, ranked opportunities, and traceable decision state.
              </h1>
              <p className="mt-3 max-w-3xl text-sm leading-7 text-white/52 sm:text-base">
                The raw feed stays in ingestion. This surface only shows freshness, reusable analytics, decision rationale, and live position context.
              </p>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <MetricBlock label="Uptime" value={`${liveUptimeSeconds}s`} note="Independent real-time clock" accent="text-white" />
              <MetricBlock label="Data delay" value={formatDelay(liveDelayMs)} note={`Cycle ${cycleLatencyMs}ms`} accent="text-cyan-100" />
              <MetricBlock label="Top signal" value={(analyticsSummary?.top_signal_score ?? 0).toFixed(2)} note={`${analyticsSummary?.opportunity_count ?? 0} actionable opportunities`} accent="text-emerald-100" />
              <MetricBlock label="PnL" value={`${formatMoney(totalPnl)} ${formatPct(totalPnlPct)}`} note={`${openPositions} open positions`} accent={totalPnl >= 0 ? "text-emerald-100" : "text-rose-100"} />
            </div>
          </div>

          <div className="rounded-[28px] border border-white/8 bg-black/20 p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[11px] uppercase tracking-[0.28em] text-white/38">Decision engine</p>
                <p className="mt-2 text-lg font-semibold text-white">Current execution posture</p>
              </div>
              <div className="rounded-2xl border border-white/8 bg-white/[0.04] p-2 text-white/60">
                <Bot size={18} />
              </div>
            </div>
            <div className="mt-5 space-y-3">
              <AsideMetric label="Open intents" value={decisionSummary?.open_count ?? 0} />
              <AsideMetric label="Close intents" value={decisionSummary?.close_count ?? 0} />
              <AsideMetric label="Reduce intents" value={decisionSummary?.reduce_count ?? 0} />
              <AsideMetric label="Exposure" value={`${((capitalInTrade / Math.max(portfolioTotal + totalPnl, 1)) * 100).toFixed(1)}%`} />
            </div>
            <div className="mt-5 rounded-[22px] border border-white/8 bg-white/[0.03] p-4">
              <p className="text-[11px] uppercase tracking-[0.24em] text-white/38">Latest operator log</p>
              {logs.length > 0 ? (
                <>
                  <p className="mt-3 text-sm font-medium text-white">{logs[logs.length - 1]?.message}</p>
                  <p className="mt-2 text-xs leading-5 text-white/45">
                    {logs[logs.length - 1]?.category} · {formatShortTime(logs[logs.length - 1]?.timestamp ?? new Date().toISOString())}
                  </p>
                </>
              ) : (
                <p className="mt-3 text-sm text-white/45">No controlled logs yet.</p>
              )}
            </div>
          </div>
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)]">
        <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
          <SectionTitle
            icon={<Signal size={14} className="text-[#00d4aa]" />}
            eyebrow="Signal canvas"
            title="Opportunity intensity and freshness"
            note="Signal score, opportunity count, and ingestion drift stay visible in one timeseries surface."
          />
          <div className="grid gap-4 p-5 sm:p-6">
            <div className="grid gap-3 sm:grid-cols-3">
              <MiniStrip label="Actionable" value={analyticsSummary?.opportunity_count ?? 0} tone="emerald" />
              <MiniStrip label="Freshness avg" value={formatDelay(ingestion?.avg_freshness_ms ?? 0)} tone="cyan" />
              <MiniStrip label="Worst lag" value={formatDelay(ingestion?.max_freshness_ms ?? 0)} tone="rose" />
            </div>
            <div className="h-[320px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={signalSeries}>
                  <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                  <XAxis dataKey="timestamp" tickFormatter={formatShortTime} tick={axisTickStyle} minTickGap={32} />
                  <YAxis yAxisId="left" tick={axisTickStyle} width={48} />
                  <YAxis yAxisId="right" orientation="right" tick={axisTickStyle} width={48} />
                  <Tooltip
                    contentStyle={{
                      background: "rgba(11,14,20,0.96)",
                      border: "1px solid rgba(255,255,255,0.08)",
                      borderRadius: 16,
                      color: "#fff",
                    }}
                  />
                  <Line
                    yAxisId="left"
                    type="monotone"
                    dataKey="top_signal_score"
                    stroke="#00d4aa"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                    name="Top signal"
                  />
                  <Line
                    yAxisId="right"
                    type="monotone"
                    dataKey="avg_freshness_ms"
                    stroke="#60a5fa"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                    name="Avg freshness"
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </Card>

        <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
          <SectionTitle
            icon={<TrendingUp size={14} className="text-[#7bf7de]" />}
            eyebrow="Equity"
            title="Portfolio and exposure"
            note="Equity remains separate from the live decision signal so portfolio state stays legible."
          />
          <div className="grid gap-4 p-5 sm:p-6">
            <div className="grid gap-3 sm:grid-cols-3">
              <MiniStrip label="Portfolio" value={formatMoney(portfolioTotal + totalPnl)} tone="emerald" />
              <MiniStrip label="Capital deployed" value={formatMoney(capitalInTrade)} tone="cyan" />
              <MiniStrip label="Open positions" value={positions.length} tone="rose" />
            </div>
            <div className="h-[320px]">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equitySeries}>
                  <defs>
                    <linearGradient id="scannerEquity" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#00d4aa" stopOpacity={0.28} />
                      <stop offset="100%" stopColor="#00d4aa" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                  <XAxis dataKey="timestamp" tickFormatter={formatShortTime} tick={axisTickStyle} minTickGap={32} />
                  <YAxis tick={axisTickStyle} width={56} tickFormatter={(value) => `$${Math.round(value / 1000)}k`} />
                  <Tooltip
                    formatter={(value: number) => formatMoney(Number(value))}
                    contentStyle={{
                      background: "rgba(11,14,20,0.96)",
                      border: "1px solid rgba(255,255,255,0.08)",
                      borderRadius: 16,
                      color: "#fff",
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="portfolio"
                    stroke="#7bf7de"
                    fill="url(#scannerEquity)"
                    strokeWidth={2}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
        <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
          <SectionTitle
            icon={<Radar size={14} className="text-[#00d4aa]" />}
            eyebrow="Opportunity ladder"
            title="Ranked opportunities"
            note="Signals are sorted by edge and score, with freshness and rationale visible in the same row."
          />
          <div className="overflow-x-auto">
            <table className="min-w-full border-separate border-spacing-0">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-[0.24em] text-white/35">
                  <th className="px-5 py-3 font-medium sm:px-6">Market</th>
                  <th className="px-4 py-3 font-medium">Freshness</th>
                  <th className="px-4 py-3 font-medium">Edge</th>
                  <th className="px-4 py-3 font-medium">Z-score</th>
                  <th className="px-4 py-3 font-medium">Action</th>
                  <th className="px-5 py-3 font-medium sm:px-6">Rationale</th>
                </tr>
              </thead>
              <tbody>
                {opportunityRows.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-5 py-12 text-center text-sm text-white/45 sm:px-6">
                      No open-grade opportunities right now. The ladder updates from analytics only, not raw message volume.
                    </td>
                  </tr>
                ) : (
                  opportunityRows.map((market) => (
                    <tr key={market.market_id} className="border-t border-white/6 text-sm text-white/85 hover:bg-white/[0.03]">
                      <td className="max-w-[360px] px-5 py-4 sm:px-6">
                        <div className="space-y-1">
                          <p className="line-clamp-2 font-medium text-white">{market.title}</p>
                          <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-white/35">
                            {market.direction.replaceAll("_", " ")}
                          </p>
                        </div>
                      </td>
                      <td className="px-4 py-4 font-mono text-[13px] text-cyan-100">
                        {formatDelay((market.freshness_ms ?? 0) + snapshotAgeMs)}
                      </td>
                      <td className="px-4 py-4 font-mono text-[13px] text-emerald-100">
                        {(market.expected_edge * 100).toFixed(2)}%
                      </td>
                      <td className="px-4 py-4 font-mono text-[13px] text-white/72">
                        {(market.z_score ?? 0).toFixed(2)}
                      </td>
                      <td className="px-4 py-4">
                        <span
                          className={cn(
                            "inline-flex rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.24em]",
                            market.decision_action === "OPEN"
                              ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-100"
                              : "border-white/10 bg-white/[0.04] text-white/72"
                          )}
                        >
                          {market.decision_action ?? "HOLD"}
                        </span>
                      </td>
                      <td className="px-5 py-4 text-sm leading-6 text-white/55 sm:px-6">
                        {market.decision_summary || market.explain?.[0] || "Waiting for clearer analytics."}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <div className="space-y-6">
          <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
            <SectionTitle
              icon={<ShieldCheck size={14} className="text-[#7bf7de]" />}
              eyebrow="Decision trace"
              title="Why the engine acts or waits"
              note="Every open, hold, reduce, and reject state is shown with the first rationale or filter that fired."
            />
            <div className="space-y-3 p-5 sm:p-6">
              {rankedDecisions.length === 0 ? (
                <p className="text-sm text-white/45">No decision rows yet.</p>
              ) : (
                rankedDecisions.map((decision) => (
                  <div key={decision.market_id} className="rounded-[22px] border border-white/8 bg-black/20 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-white">{decision.title}</p>
                        <p className="mt-1 text-xs leading-5 text-white/45">
                          {decision.summary}
                        </p>
                      </div>
                      <span
                        className={cn(
                          "rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.24em]",
                          decision.action === "OPEN"
                            ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-100"
                            : decision.action === "CLOSE"
                              ? "border-rose-400/25 bg-rose-400/10 text-rose-100"
                              : "border-white/10 bg-white/[0.04] text-white/72"
                        )}
                      >
                        {decision.action}
                      </span>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2 text-[11px] uppercase tracking-[0.18em] text-white/38">
                      <span>Confidence {(decision.confidence * 100).toFixed(0)}%</span>
                      <span>Signal {Number(decision.analytics_refs.signal_strength ?? 0).toFixed(2)}</span>
                      <span>Freshness {formatDelay(Number(decision.analytics_refs.freshness_ms ?? 0) + snapshotAgeMs)}</span>
                    </div>
                  </div>
                ))
              )}
            </div>
          </Card>

          <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
            <SectionTitle
              icon={<Activity size={14} className="text-[#00d4aa]" />}
              eyebrow="Position state"
              title="Open positions"
              note="Position rows show unrealized PnL and the current maintenance action suggested by the decision engine."
            />
            <div className="space-y-3 p-5 sm:p-6">
              {positions.length === 0 ? (
                <p className="text-sm text-white/45">No open positions.</p>
              ) : (
                positions.map((position) => (
                  <div key={position.trade_id} className="rounded-[22px] border border-white/8 bg-black/20 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-white">{position.market_title}</p>
                        <p className="mt-1 text-xs text-white/45">
                          {position.side.replaceAll("_", " ")} · {formatMoney(position.notional)}
                        </p>
                      </div>
                      <p className={cn("font-mono text-sm", position.unrealized_pnl_abs >= 0 ? "text-emerald-100" : "text-rose-100")}>
                        {formatMoney(position.unrealized_pnl_abs)}
                      </p>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-white/55">{position.decision_summary || "No position rationale available."}</p>
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
        <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
          <SectionTitle
            icon={<TimerReset size={14} className="text-[#60a5fa]" />}
            eyebrow="Ingestion health"
            title="Sources and freshness"
            note="The scanner watches source lag and market freshness separately from the decision layer."
          />
          <div className="space-y-4 p-5 sm:p-6">
            <div className="grid gap-3 sm:grid-cols-2">
              <MiniStrip label="Updates/min" value={ingestion?.updates_last_minute ?? 0} tone="cyan" />
              <MiniStrip label="Stale markets" value={ingestion?.stale_market_count ?? 0} tone="rose" />
            </div>
            <div className="space-y-3">
              {sources.length === 0 ? (
                <p className="text-sm text-white/45">No source health rows yet.</p>
              ) : (
                sources.map((source) => (
                  <div key={source.name} className="rounded-[22px] border border-white/8 bg-black/20 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex items-center gap-3">
                        <span className={cn("h-2.5 w-2.5 rounded-full", sourceTone(source.status))} />
                        <div>
                          <p className="text-sm font-medium text-white">{source.name}</p>
                          <p className="mt-1 text-xs text-white/45">{source.status}</p>
                        </div>
                      </div>
                      <p className="font-mono text-xs text-white/55">
                        {source.lag_ms == null ? "n/a" : formatDelay(source.lag_ms + snapshotAgeMs)}
                      </p>
                    </div>
                    {source.note ? <p className="mt-3 text-sm leading-6 text-white/45">{source.note}</p> : null}
                  </div>
                ))
              )}
            </div>
            <div className="rounded-[22px] border border-white/8 bg-black/20 p-4">
              <p className="text-[11px] uppercase tracking-[0.24em] text-white/38">Hot markets</p>
              <div className="mt-3 space-y-3">
                {marketRows.length === 0 ? (
                  <p className="text-sm text-white/45">No market health rows yet.</p>
                ) : (
                  marketRows.map((market) => (
                    <div key={market.market_id} className="flex items-center justify-between gap-3 border-b border-white/6 pb-3 last:border-none last:pb-0">
                      <div className="min-w-0">
                        <p className="truncate text-sm text-white">{market.title}</p>
                        <p className="mt-1 text-xs text-white/38">{market.quote_source} · {market.regime}</p>
                      </div>
                      <p className="font-mono text-xs text-cyan-100">{formatDelay((market.freshness_ms ?? 0) + snapshotAgeMs)}</p>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </Card>

        <Card className="overflow-hidden border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
          <SectionTitle
            icon={<Clock3 size={14} className="text-[#fda4af]" />}
            eyebrow="Controlled logs"
            title="Readable operator log"
            note="Only decision, control, startup, and risk events are shown here so the log stays explainable."
          />
          <div className="max-h-[520px] space-y-3 overflow-y-auto p-5 sm:p-6">
            {logs.length === 0 ? (
              <p className="text-sm text-white/45">No controlled logs yet.</p>
            ) : (
              [...logs].reverse().map((entry) => (
                <div key={`${entry.timestamp}-${entry.message}`} className="rounded-[22px] border border-white/8 bg-black/20 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge className="border-white/10 bg-white/[0.04] text-[10px] uppercase tracking-[0.22em] text-white/70">
                      {entry.category}
                    </Badge>
                    <Badge
                      className={cn(
                        "text-[10px] uppercase tracking-[0.22em]",
                        entry.level === "error"
                          ? "border-rose-400/25 bg-rose-400/10 text-rose-100"
                          : entry.level === "warning"
                            ? "border-amber-400/25 bg-amber-400/10 text-amber-100"
                            : "border-cyan-400/25 bg-cyan-400/10 text-cyan-100"
                      )}
                    >
                      {entry.level}
                    </Badge>
                    <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-white/35">
                      {formatShortTime(entry.timestamp)}
                    </span>
                  </div>
                  <p className="mt-3 text-sm leading-6 text-white">{entry.message}</p>
                  {entry.market_id ? (
                    <p className="mt-2 font-mono text-[11px] uppercase tracking-[0.18em] text-white/38">
                      {entry.market_id}
                    </p>
                  ) : null}
                </div>
              ))
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

function MetricBlock({
  label,
  value,
  note,
  accent,
}: {
  label: string;
  value: string;
  note: string;
  accent: string;
}) {
  return (
    <div className="rounded-[22px] border border-white/8 bg-black/20 px-4 py-4">
      <p className="text-[10px] uppercase tracking-[0.24em] text-white/35">{label}</p>
      <p className={cn("mt-3 font-mono text-2xl font-semibold tracking-[-0.04em]", accent)}>{value}</p>
      <p className="mt-2 text-xs leading-5 text-white/42">{note}</p>
    </div>
  );
}

function AsideMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-white/8 pb-3 last:border-none last:pb-0">
      <p className="text-sm text-white/52">{label}</p>
      <p className="font-mono text-sm text-white">{value}</p>
    </div>
  );
}

function MiniStrip({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone: "emerald" | "cyan" | "rose";
}) {
  return (
    <div
      className={cn(
        "rounded-[20px] border px-4 py-3",
        tone === "emerald"
          ? "border-emerald-400/15 bg-emerald-400/10"
          : tone === "cyan"
            ? "border-cyan-400/15 bg-cyan-400/10"
            : "border-rose-400/15 bg-rose-400/10"
      )}
    >
      <p className="text-[10px] uppercase tracking-[0.24em] text-white/38">{label}</p>
      <p className="mt-2 font-mono text-lg font-semibold text-white">{value}</p>
    </div>
  );
}
