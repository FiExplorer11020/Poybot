"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import {
  Activity,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  Download,
  Filter,
  LoaderCircle,
  Play,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { Area, Bar, CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";
import { apiHeaders, apiUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;

const STRATEGIES = [
  { value: "adaptive", label: "Adaptive", description: "Default runtime-aligned strategy." },
  { value: "latency_arb", label: "Latency Arb", description: "Exploit delayed quote reactions." },
  { value: "spread_arb", label: "Spread Arb", description: "Capture YES/NO spread dislocations." },
] as const;

const KPI_CONFIG = [
  { key: "winRate", label: "Win Rate", benchmark: 50, type: "percent", better: "higher" },
  { key: "totalPnl", label: "Total PnL $", benchmark: 0, type: "currency", better: "higher" },
  { key: "sharpeRatio", label: "Sharpe Ratio", benchmark: 1, type: "ratio", better: "higher" },
  { key: "maxDrawdown", label: "Max Drawdown", benchmark: 10, type: "percent", better: "lower" },
  { key: "profitFactor", label: "Profit Factor", benchmark: 1.4, type: "ratio", better: "higher" },
  { key: "tradeCount", label: "Nb Trades", benchmark: 30, type: "integer", better: "higher" },
] as const;

type StrategyValue = (typeof STRATEGIES)[number]["value"];
type SideFilterValue = "ALL" | "BUY_YES" | "BUY_NO";
type StatusFilterValue = "ALL" | "WIN" | "LOSS";
type RawRecord = Record<string, unknown>;

type BacktestMetrics = {
  winRate: number;
  totalPnl: number;
  sharpeRatio: number;
  maxDrawdown: number;
  profitFactor: number;
  tradeCount: number;
};

type NormalizedEquityPoint = {
  id: string;
  timestamp: string;
  equity: number;
  drawdown: number;
};

type NormalizedTrade = {
  id: string;
  date: string;
  market: string;
  side: string;
  entry: number | null;
  size: number | null;
  pnl: number;
  pnlPct: number | null;
  status: string;
  slippage: number | null;
  fees: number | null;
  notional: number | null;
  exitPrice: number | null;
  orderId: string | null;
  tokenId: string | null;
  notes: string | null;
  raw: RawRecord;
};

type NormalizedBacktestResult = {
  id: string | null;
  metrics: BacktestMetrics;
  equityCurve: NormalizedEquityPoint[];
  trades: NormalizedTrade[];
};

type KpiType = (typeof KPI_CONFIG)[number]["type"];
type KpiKey = keyof BacktestMetrics;

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const integerFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

const decimalFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const percentageFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function isRecord(value: unknown): value is RawRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }

  if (typeof value === "string") {
    const cleaned = value.replace(/[$,%\s,]/g, "");
    if (!cleaned) {
      return null;
    }

    const parsed = Number(cleaned);
    return Number.isFinite(parsed) ? parsed : null;
  }

  return null;
}

function toText(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }

  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }

  return null;
}

function pickNumber(record: RawRecord | null | undefined, keys: string[]): number | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const value = toNumber(record[key]);
    if (value !== null) {
      return value;
    }
  }

  return null;
}

function pickText(record: RawRecord | null | undefined, keys: string[]): string | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const value = toText(record[key]);
    if (value) {
      return value;
    }
  }

  return null;
}

function normalizePercent(value: number | null | undefined, { absolute = false }: { absolute?: boolean } = {}) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return 0;
  }

  const scaled = Math.abs(value) <= 1.5 ? value * 100 : value;
  return absolute ? Math.abs(scaled) : scaled;
}

function formatDateInput(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftDate(baseDate: Date, deltaDays: number) {
  const nextDate = new Date(baseDate);
  nextDate.setDate(nextDate.getDate() + deltaDays);
  return nextDate;
}

function createDefaultRange() {
  const today = new Date();
  return {
    startDate: formatDateInput(shiftDate(today, -30)),
    endDate: formatDateInput(today),
  };
}

function parseDateInput(value: string) {
  return new Date(`${value}T00:00:00`);
}

function isValidDate(date: Date) {
  return !Number.isNaN(date.getTime());
}

function formatTimestamp(value: string, { withTime = false }: { withTime?: boolean } = {}) {
  const parsed = /^\d{4}-\d{2}-\d{2}$/.test(value) ? parseDateInput(value) : new Date(value);

  if (!isValidDate(parsed)) {
    return value;
  }

  if (withTime) {
    return parsed.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  return parsed.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

function formatCurrency(value: number, { signed = false, absolute = false }: { signed?: boolean; absolute?: boolean } = {}) {
  if (!Number.isFinite(value)) {
    return "∞";
  }

  const base = absolute ? Math.abs(value) : value;
  const formatted = currencyFormatter.format(base);

  if (signed && base > 0) {
    return `+${formatted}`;
  }

  return formatted;
}

function formatPercent(value: number, { signed = false, absolute = false }: { signed?: boolean; absolute?: boolean } = {}) {
  if (!Number.isFinite(value)) {
    return "∞";
  }

  const base = absolute ? Math.abs(value) : value;
  const formatted = `${percentageFormatter.format(base)}%`;

  if (signed && base > 0) {
    return `+${formatted}`;
  }

  return formatted;
}

function formatRatio(value: number) {
  if (!Number.isFinite(value)) {
    return "∞";
  }

  return decimalFormatter.format(value);
}

function formatKpiValue(value: number, type: KpiType, { signed = false }: { signed?: boolean } = {}) {
  switch (type) {
    case "currency":
      return formatCurrency(value, { signed });
    case "percent":
      return formatPercent(value, { signed, absolute: false });
    case "integer":
      return integerFormatter.format(value);
    case "ratio":
      return formatRatio(value);
    default:
      return String(value);
  }
}

function benchmarkLabel(type: KpiType, benchmark: number, better: "higher" | "lower") {
  if (better === "lower") {
    return `Target <= ${formatKpiValue(benchmark, type)}`;
  }

  return `Benchmark ${formatKpiValue(benchmark, type)}`;
}

function getBenchmarkState(value: number, benchmark: number, better: "higher" | "lower") {
  const meetsTarget = better === "higher" ? value >= benchmark : value <= benchmark;

  return {
    meetsTarget,
    label: meetsTarget ? "Above benchmark" : "Below benchmark",
  };
}

function computeProfitFactor(trades: NormalizedTrade[]) {
  const grossProfit = trades.filter((trade) => trade.pnl > 0).reduce((sum, trade) => sum + trade.pnl, 0);
  const grossLoss = Math.abs(trades.filter((trade) => trade.pnl < 0).reduce((sum, trade) => sum + trade.pnl, 0));

  if (grossLoss === 0) {
    return grossProfit > 0 ? Number.POSITIVE_INFINITY : 0;
  }

  return grossProfit / grossLoss;
}

function computeMaxDrawdown(equityCurve: NormalizedEquityPoint[]) {
  if (equityCurve.length === 0) {
    return 0;
  }

  return equityCurve.reduce((maxValue, point) => Math.max(maxValue, Math.abs(point.drawdown)), 0);
}

function normalizeStatus(status: string | null, pnl: number) {
  const normalized = status?.toUpperCase() ?? "";

  if (normalized.includes("WIN")) {
    return "WIN";
  }

  if (normalized.includes("LOSS")) {
    return "LOSS";
  }

  if (normalized.includes("FLAT") || normalized.includes("BREAKEVEN")) {
    return "FLAT";
  }

  return pnl > 0 ? "WIN" : pnl < 0 ? "LOSS" : "FLAT";
}

function normalizeTrade(entry: unknown, index: number): NormalizedTrade | null {
  if (!isRecord(entry)) {
    return null;
  }

  const pnl = pickNumber(entry, ["pnl_abs", "pnl", "net_pnl", "total_pnl", "profit_loss"]) ?? 0;
  const entryPrice = pickNumber(entry, ["entry_price", "price", "entry", "avg_entry_price"]);
  const size = pickNumber(entry, ["size", "shares", "quantity", "qty"]);
  const notional = pickNumber(entry, ["notional", "size_usd", "position_value"]);
  const pnlPctFromPayload = pickNumber(entry, ["pnl_pct", "pnl_percent", "return_pct", "return_percentage"]);
  const derivedNotional = notional ?? (entryPrice !== null && size !== null ? entryPrice * size : null);
  const pnlPct =
    pnlPctFromPayload !== null
      ? normalizePercent(pnlPctFromPayload)
      : derivedNotional && derivedNotional !== 0
        ? (pnl / derivedNotional) * 100
        : null;

  return {
    id: pickText(entry, ["id", "trade_id", "order_id"]) ?? `trade-${index}`,
    date: pickText(entry, ["timestamp", "date", "entry_time", "executed_at", "created_at"]) ?? `Trade ${index + 1}`,
    market: pickText(entry, ["market_title", "market", "market_name", "symbol", "title"]) ?? "Unknown market",
    side: (pickText(entry, ["side", "direction"]) ?? "BUY_YES").toUpperCase(),
    entry: entryPrice,
    size,
    pnl,
    pnlPct,
    status: normalizeStatus(pickText(entry, ["status", "result", "trade_status"]), pnl),
    slippage: pickNumber(entry, ["slippage", "slippage_bps", "avg_slippage"]),
    fees: pickNumber(entry, ["fees", "fee", "fee_paid"]),
    notional: derivedNotional,
    exitPrice: pickNumber(entry, ["exit_price", "close_price", "exit"]),
    orderId: pickText(entry, ["order_id", "orderId"]),
    tokenId: pickText(entry, ["token_id", "tokenId"]),
    notes: pickText(entry, ["notes", "reason", "trigger", "exit_reason"]),
    raw: entry,
  };
}

function normalizeEquityCurve(curveInput: unknown, fallbackInitialEquity: number) {
  if (!Array.isArray(curveInput)) {
    return [] as NormalizedEquityPoint[];
  }

  let runningPeak = fallbackInitialEquity;

  return curveInput.flatMap((item, index) => {
    if (!isRecord(item)) {
      return [];
    }

    const timestamp = pickText(item, ["timestamp", "time", "date", "x"]) ?? `Point ${index + 1}`;
    const equity = pickNumber(item, ["equity", "portfolio", "value", "balance", "y"]) ?? fallbackInitialEquity;

    runningPeak = Math.max(runningPeak, equity);

    const rawDrawdown = pickNumber(item, ["drawdown", "drawdown_pct", "dd"]);
    const fallbackDrawdown = runningPeak > 0 ? ((equity - runningPeak) / runningPeak) * 100 : 0;
    let drawdown = rawDrawdown !== null ? normalizePercent(rawDrawdown) : fallbackDrawdown;

    if (drawdown > 0) {
      drawdown *= -1;
    }

    return [
      {
        id: `${timestamp}-${index}`,
        timestamp,
        equity,
        drawdown,
      },
    ];
  });
}

function normalizeBacktestResult(payload: unknown, fallbackInitialEquity: number): NormalizedBacktestResult | null {
  if (!isRecord(payload)) {
    return null;
  }

  const root = isRecord(payload.data) ? payload.data : payload;
  const metricsRoot = isRecord(root.metrics) ? root.metrics : root;

  const trades = Array.isArray(root.trades) ? root.trades.map(normalizeTrade).filter(Boolean) as NormalizedTrade[] : [];
  const equityCurve = normalizeEquityCurve(root.equity_curve ?? root.equityCurve, fallbackInitialEquity);
  const winCount = trades.filter((trade) => trade.status === "WIN").length;
  const derivedWinRate = trades.length > 0 ? (winCount / trades.length) * 100 : 0;
  const totalPnl =
    pickNumber(metricsRoot, ["total_pnl", "net_pnl", "pnl", "profit_loss"]) ??
    (equityCurve.length > 0 ? equityCurve[equityCurve.length - 1].equity - fallbackInitialEquity : 0);

  const normalizedResult: NormalizedBacktestResult = {
    id: pickText(root, ["id", "backtest_id", "run_id", "result_id"]),
    metrics: {
      winRate: normalizePercent(pickNumber(metricsRoot, ["win_rate", "win_rate_pct"]) ?? derivedWinRate),
      totalPnl,
      sharpeRatio: pickNumber(metricsRoot, ["sharpe_ratio", "sharpe"]) ?? 0,
      maxDrawdown:
        normalizePercent(pickNumber(metricsRoot, ["max_drawdown", "max_drawdown_pct", "drawdown"]), { absolute: true }) ||
        computeMaxDrawdown(equityCurve),
      profitFactor: pickNumber(metricsRoot, ["profit_factor"]) ?? computeProfitFactor(trades),
      tradeCount: pickNumber(metricsRoot, ["num_trades", "trade_count", "nb_trades"]) ?? trades.length,
    },
    equityCurve,
    trades,
  };

  return normalizedResult;
}

function sideBadgeClass(side: string) {
  return side.includes("NO")
    ? "border-rose-500/30 bg-rose-500/10 text-rose-200"
    : "border-emerald-500/30 bg-emerald-500/10 text-emerald-200";
}

function statusBadgeClass(status: string) {
  if (status === "WIN") {
    return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200";
  }

  if (status === "LOSS") {
    return "border-rose-500/30 bg-rose-500/10 text-rose-200";
  }

  return "border-zinc-700 bg-zinc-800/70 text-zinc-300";
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-zinc-800/70 bg-zinc-950/60 px-4 py-3">
      <p className="text-[11px] uppercase tracking-[0.2em] text-zinc-500">{label}</p>
      <p className="mt-1 break-all text-sm text-zinc-200">{value}</p>
    </div>
  );
}

function DateRangePicker({
  startDate,
  endDate,
  disabled,
  onStartDateChange,
  onEndDateChange,
}: {
  startDate: string;
  endDate: string;
  disabled?: boolean;
  onStartDateChange: (value: string) => void;
  onEndDateChange: (value: string) => void;
}) {
  const today = formatDateInput(new Date());

  const applyPreset = (days: number) => {
    const end = new Date();
    const start = shiftDate(end, -(days - 1));
    onStartDateChange(formatDateInput(start));
    onEndDateChange(formatDateInput(end));
  };

  const applyYtd = () => {
    const end = new Date();
    const start = new Date(end.getFullYear(), 0, 1);
    onStartDateChange(formatDateInput(start));
    onEndDateChange(formatDateInput(end));
  };

  const daySpan = (() => {
    const start = parseDateInput(startDate);
    const end = parseDateInput(endDate);

    if (!isValidDate(start) || !isValidDate(end)) {
      return null;
    }

    return Math.max(1, Math.round((end.getTime() - start.getTime()) / 86400000) + 1);
  })();

  return (
    <div className="space-y-4">
      <div className="grid gap-3 lg:grid-cols-2">
        <label className="space-y-2">
          <span className="text-xs font-semibold uppercase tracking-[0.18em] text-zinc-400">Start date</span>
          <div className="flex items-center gap-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 px-4 py-3">
            <CalendarDays size={16} className="text-zinc-500" />
            <input
              type="date"
              value={startDate}
              max={endDate || today}
              onChange={(event) => onStartDateChange(event.target.value)}
              disabled={disabled}
              className="w-full bg-transparent text-sm text-zinc-100 outline-none [color-scheme:dark]"
            />
          </div>
        </label>

        <label className="space-y-2">
          <span className="text-xs font-semibold uppercase tracking-[0.18em] text-zinc-400">End date</span>
          <div className="flex items-center gap-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 px-4 py-3">
            <CalendarDays size={16} className="text-zinc-500" />
            <input
              type="date"
              value={endDate}
              min={startDate}
              max={today}
              onChange={(event) => onEndDateChange(event.target.value)}
              disabled={disabled}
              className="w-full bg-transparent text-sm text-zinc-100 outline-none [color-scheme:dark]"
            />
          </div>
        </label>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-2">
          {[
            { label: "7D", days: 7 },
            { label: "30D", days: 30 },
            { label: "90D", days: 90 },
          ].map((preset) => (
            <button
              key={preset.label}
              type="button"
              onClick={() => applyPreset(preset.days)}
              disabled={disabled}
              className="rounded-full border border-zinc-800 bg-zinc-900/70 px-3 py-1.5 text-xs text-zinc-300 transition hover:border-emerald-400/40 hover:text-emerald-200 disabled:opacity-50"
            >
              {preset.label}
            </button>
          ))}

          <button
            type="button"
            onClick={applyYtd}
            disabled={disabled}
            className="rounded-full border border-zinc-800 bg-zinc-900/70 px-3 py-1.5 text-xs text-zinc-300 transition hover:border-emerald-400/40 hover:text-emerald-200 disabled:opacity-50"
          >
            YTD
          </button>
        </div>

        <div className="rounded-full border border-zinc-800/80 bg-zinc-950/70 px-3 py-1.5 text-xs text-zinc-400">
          {daySpan ? `${daySpan} calendar days selected` : "Pick a valid backtest window"}
        </div>
      </div>
    </div>
  );
}

function EquityTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: NormalizedEquityPoint }>;
}) {
  const point = payload?.[0]?.payload;

  if (!active || !point) {
    return null;
  }

  return (
    <div className="rounded-2xl border border-zinc-800/90 bg-zinc-950/95 px-4 py-3 shadow-2xl backdrop-blur">
      <p className="text-xs text-zinc-400">{formatTimestamp(point.timestamp, { withTime: true })}</p>
      <div className="mt-2 space-y-1.5 text-sm">
        <div className="flex items-center justify-between gap-6">
          <span className="text-zinc-400">Equity</span>
          <span className="font-medium text-emerald-200">{formatCurrency(point.equity)}</span>
        </div>
        <div className="flex items-center justify-between gap-6">
          <span className="text-zinc-400">Drawdown</span>
          <span className="font-medium text-rose-200">{formatPercent(point.drawdown)}</span>
        </div>
      </div>
    </div>
  );
}

export default function BacktestPage() {
  const defaults = useMemo(() => createDefaultRange(), []);
  const [startDate, setStartDate] = useState(defaults.startDate);
  const [endDate, setEndDate] = useState(defaults.endDate);
  const [strategy, setStrategy] = useState<StrategyValue>("adaptive");
  const [initialEquity, setInitialEquity] = useState("25000");
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<NormalizedBacktestResult | null>(null);
  const [sideFilter, setSideFilter] = useState<SideFilterValue>("ALL");
  const [statusFilter, setStatusFilter] = useState<StatusFilterValue>("ALL");
  const [currentPage, setCurrentPage] = useState(1);
  const [expandedTradeId, setExpandedTradeId] = useState<string | null>(null);

  const selectedStrategy = STRATEGIES.find((item) => item.value === strategy) ?? STRATEGIES[0];

  const filteredTrades = useMemo(() => {
    if (!result) {
      return [] as NormalizedTrade[];
    }

    return result.trades.filter((trade) => {
      const matchesSide = sideFilter === "ALL" ? true : trade.side === sideFilter;
      const matchesStatus = statusFilter === "ALL" ? true : trade.status === statusFilter;
      return matchesSide && matchesStatus;
    });
  }, [result, sideFilter, statusFilter]);

  const totalPages = Math.max(1, Math.ceil(filteredTrades.length / PAGE_SIZE));

  const paginatedTrades = useMemo(() => {
    const startIndex = (currentPage - 1) * PAGE_SIZE;
    return filteredTrades.slice(startIndex, startIndex + PAGE_SIZE);
  }, [currentPage, filteredTrades]);

  useEffect(() => {
    setCurrentPage(1);
  }, [sideFilter, statusFilter, result?.id, result?.trades.length]);

  useEffect(() => {
    setExpandedTradeId(null);
  }, [currentPage, sideFilter, statusFilter, result?.id]);

  useEffect(() => {
    if (currentPage > totalPages) {
      setCurrentPage(totalPages);
    }
  }, [currentPage, totalPages]);

  const handleRunBacktest = async () => {
    const parsedEquity = Number(initialEquity);

    if (!startDate || !endDate) {
      setError("Select a valid date window before launching the simulation.");
      return;
    }

    if (parseDateInput(startDate) > parseDateInput(endDate)) {
      setError("Start date must be earlier than end date.");
      return;
    }

    if (!Number.isFinite(parsedEquity) || parsedEquity <= 0) {
      setError("Initial equity must be a positive number.");
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const response = await fetch(apiUrl("/api/v1/backtest"), {
        method: "POST",
        headers: apiHeaders({
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          start_date: startDate,
          end_date: endDate,
          strategy,
          initial_equity: parsedEquity,
        }),
      });

      const payload = await response.json().catch(() => null);

      if (!response.ok) {
        const message =
          (isRecord(payload) && (pickText(payload, ["detail", "message", "error"]) ?? pickText(isRecord(payload.data) ? payload.data : null, ["detail", "message", "error"]))) ||
          `Backtest request failed with HTTP ${response.status}.`;
        throw new Error(message);
      }

      const normalized = normalizeBacktestResult(payload, parsedEquity);

      if (!normalized) {
        throw new Error("The backtest response could not be parsed.");
      }

      setResult(normalized);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "Unable to run the backtest right now.";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async () => {
    if (!result?.id) {
      toast.error("Excel export is unavailable because this result has no export id.");
      return;
    }

    setExporting(true);

    try {
      const response = await fetch(apiUrl(`/api/v1/backtest/${result.id}/export`), {
        method: "GET",
        headers: apiHeaders(),
      });

      if (!response.ok) {
        throw new Error(`Export failed with HTTP ${response.status}.`);
      }

      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `backtest-${result.id}.xlsx`;
      anchor.click();
      URL.revokeObjectURL(objectUrl);
    } catch (exportError) {
      const message = exportError instanceof Error ? exportError.message : "Unable to download the Excel export.";
      toast.error(message);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-6 animate-in fade-in duration-500">
      <div className="space-y-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-emerald-300/80">Research Surface</p>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-100">Backtest</h1>
            <p className="mt-1 max-w-2xl text-sm text-zinc-400">
              Configure la fenetre, lance la simulation, puis inspecte l&apos;equity curve et chaque trade du run.
            </p>
          </div>
          <div className="rounded-2xl border border-zinc-800/80 bg-zinc-950/50 px-4 py-3 text-sm text-zinc-400">
            Strategy profile: <span className="font-medium text-zinc-100">{selectedStrategy.label}</span>
          </div>
        </div>
      </div>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(300px,0.7fr)]">
        <Card className="border-zinc-800/80 bg-zinc-950/50 p-6 shadow-xl">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-zinc-100">Parametres</h2>
              <p className="text-sm text-zinc-400">Choisis la fenetre de simulation, la strategie et le capital de depart.</p>
            </div>
            <Badge className="w-fit border-emerald-500/30 bg-emerald-500/10 text-emerald-200">POST /api/v1/backtest</Badge>
          </div>

          <div className="mt-6 grid gap-6">
            <DateRangePicker
              startDate={startDate}
              endDate={endDate}
              onStartDateChange={setStartDate}
              onEndDateChange={setEndDate}
              disabled={loading}
            />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_240px]">
              <label className="space-y-2">
                <span className="text-xs font-semibold uppercase tracking-[0.18em] text-zinc-400">Strategie</span>
                <div className="rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-1">
                  <select
                    value={strategy}
                    onChange={(event) => setStrategy(event.target.value as StrategyValue)}
                    disabled={loading}
                    className="w-full rounded-xl bg-transparent px-3 py-3 text-sm text-zinc-100 outline-none"
                  >
                    {STRATEGIES.map((option) => (
                      <option key={option.value} value={option.value} className="bg-zinc-950 text-zinc-100">
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <p className="text-xs text-zinc-500">{selectedStrategy.description}</p>
              </label>

              <label className="space-y-2">
                <span className="text-xs font-semibold uppercase tracking-[0.18em] text-zinc-400">Equity initiale</span>
                <div className="flex items-center rounded-2xl border border-zinc-800/80 bg-zinc-950/60 px-4 py-3">
                  <span className="text-zinc-500">$</span>
                  <input
                    type="number"
                    inputMode="decimal"
                    min="1"
                    step="100"
                    value={initialEquity}
                    onChange={(event) => setInitialEquity(event.target.value)}
                    disabled={loading}
                    className="w-full bg-transparent px-3 text-sm text-zinc-100 outline-none"
                    placeholder="25000"
                  />
                </div>
              </label>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-zinc-800/80 bg-zinc-950/40 px-4 py-4">
              <div className="space-y-1">
                <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">Execution summary</p>
                <p className="text-sm text-zinc-300">
                  {startDate} to {endDate} with <span className="font-medium text-zinc-100">{selectedStrategy.label}</span> on{" "}
                  <span className="font-medium text-zinc-100">
                    {Number.isFinite(Number(initialEquity)) ? formatCurrency(Number(initialEquity)) : "$0.00"}
                  </span>
                  .
                </p>
              </div>

              <Button
                onClick={handleRunBacktest}
                disabled={loading}
                className="min-w-[220px] border-emerald-400/40 bg-emerald-500/10 px-5 py-3 text-sm text-emerald-100 hover:bg-emerald-500/15"
              >
                <span className="flex items-center justify-center gap-2">
                  {loading ? <LoaderCircle size={16} className="animate-spin" /> : <Play size={16} />}
                  {loading ? "Simulation en cours..." : "Lancer le backtest"}
                </span>
              </Button>
            </div>
          </div>
        </Card>

        <Card className="relative overflow-hidden border-zinc-800/80 bg-zinc-950/50 p-6 shadow-xl">
          <div className="absolute right-0 top-0 h-56 w-56 translate-x-1/3 -translate-y-1/3 rounded-full bg-emerald-500/10 blur-3xl" />
          <div className="relative space-y-5">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.2em] text-zinc-500">Run framing</p>
              <h2 className="mt-2 text-lg font-semibold text-zinc-100">Scenario profile</h2>
            </div>

            <div className="space-y-3">
              <div className="rounded-2xl border border-zinc-800/80 bg-zinc-950/70 px-4 py-4">
                <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">Strategy</p>
                <p className="mt-1 text-sm font-medium text-zinc-100">{selectedStrategy.label}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800/80 bg-zinc-950/70 px-4 py-4">
                <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">Window</p>
                <p className="mt-1 text-sm font-medium text-zinc-100">
                  {formatTimestamp(startDate)} to {formatTimestamp(endDate)}
                </p>
              </div>
              <div className="rounded-2xl border border-zinc-800/80 bg-zinc-950/70 px-4 py-4">
                <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">Capital</p>
                <p className="mt-1 text-sm font-medium text-zinc-100">
                  {Number.isFinite(Number(initialEquity)) ? formatCurrency(Number(initialEquity)) : "$0.00"}
                </p>
              </div>
            </div>

            <p className="text-sm leading-6 text-zinc-400">
              Once the run completes, the dashboard compares the key ratios against operational benchmarks and exposes every
              fill for review or export.
            </p>
          </div>
        </Card>
      </section>

      {loading ? (
        <Card className="border-zinc-800/80 bg-zinc-950/50 px-6 py-12 shadow-xl">
          <div className="flex flex-col items-center justify-center gap-4 text-center">
            <div className="rounded-full border border-emerald-500/30 bg-emerald-500/10 p-4 text-emerald-300">
              <LoaderCircle size={24} className="animate-spin" />
            </div>
            <div className="space-y-1">
              <h3 className="text-lg font-semibold text-zinc-100">Simulation en cours...</h3>
              <p className="text-sm text-zinc-400">Le moteur rejoue les conditions de marche et calcule la courbe d&apos;equity.</p>
            </div>
          </div>
        </Card>
      ) : null}

      {!loading && error ? (
        <Card className="border-rose-500/30 bg-rose-950/20 px-6 py-5 shadow-xl">
          <p className="text-sm font-medium text-rose-100">Backtest error</p>
          <p className="mt-1 text-sm text-rose-200/80">{error}</p>
        </Card>
      ) : null}

      {!loading && !error && !result ? (
        <Card className="border-zinc-800/80 bg-zinc-950/50 px-6 py-12 shadow-xl">
          <div className="mx-auto max-w-xl text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full border border-zinc-800 bg-zinc-900/80 text-zinc-300">
              <Activity size={22} />
            </div>
            <h3 className="mt-4 text-lg font-semibold text-zinc-100">No backtest loaded</h3>
            <p className="mt-2 text-sm leading-6 text-zinc-400">
              Launch a run to unlock KPI cards, the equity curve, trade-level drilldown, and Excel export.
            </p>
          </div>
        </Card>
      ) : null}

      {result ? (
        <>
          <section className="space-y-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold tracking-tight text-zinc-100">KPIs resultat</h2>
                <p className="text-sm text-zinc-400">Benchmark comparison against a neutral operational baseline.</p>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {KPI_CONFIG.map((item) => {
                const value = result.metrics[item.key as KpiKey];
                const state = getBenchmarkState(value, item.benchmark, item.better);

                return (
                  <Card
                    key={item.key}
                    className={cn(
                      "p-5 shadow-xl transition",
                      state.meetsTarget
                        ? "border-emerald-500/30 bg-emerald-950/15"
                        : "border-rose-500/25 bg-rose-950/15"
                    )}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <p className="text-sm font-medium text-zinc-300">{item.label}</p>
                        <p className="mt-2 text-3xl font-semibold tracking-tight text-zinc-50">
                          {item.key === "maxDrawdown" ? formatPercent(-value) : formatKpiValue(value, item.type)}
                        </p>
                      </div>

                      <div
                        className={cn(
                          "rounded-2xl p-3",
                          state.meetsTarget ? "bg-emerald-500/10 text-emerald-300" : "bg-rose-500/10 text-rose-300"
                        )}
                      >
                        {state.meetsTarget ? <TrendingUp size={18} /> : <TrendingDown size={18} />}
                      </div>
                    </div>

                    <div className="mt-5 flex items-center justify-between gap-3">
                      <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">
                        {benchmarkLabel(item.type, item.benchmark, item.better)}
                      </p>
                      <span
                        className={cn(
                          "rounded-full border px-3 py-1 text-xs font-medium",
                          state.meetsTarget
                            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                            : "border-rose-500/30 bg-rose-500/10 text-rose-200"
                        )}
                      >
                        {state.label}
                      </span>
                    </div>
                  </Card>
                );
              })}
            </div>
          </section>

          <section className="space-y-4">
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-zinc-100">Equity curve</h2>
              <p className="text-sm text-zinc-400">Portfolio equity on the primary axis with negative drawdown bars below.</p>
            </div>

            <Card className="h-[460px] border-zinc-800/80 bg-zinc-950/50 p-5 shadow-xl">
              {result.equityCurve.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={result.equityCurve} margin={{ top: 16, right: 8, left: 0, bottom: 8 }}>
                    <defs>
                      <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#10b981" stopOpacity={0.32} />
                        <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="drawdown-fill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#fb7185" stopOpacity={0.9} />
                        <stop offset="100%" stopColor="#7f1d1d" stopOpacity={0.45} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgba(63,63,70,0.4)" strokeDasharray="3 3" vertical={false} />
                    <XAxis
                      dataKey="timestamp"
                      minTickGap={32}
                      tickMargin={10}
                      axisLine={false}
                      tickLine={false}
                      stroke="#71717a"
                      fontSize={11}
                      tickFormatter={(value) => formatTimestamp(String(value))}
                    />
                    <YAxis
                      yAxisId="equity"
                      axisLine={false}
                      tickLine={false}
                      tickMargin={10}
                      width={86}
                      stroke="#a1a1aa"
                      fontSize={11}
                      tickFormatter={(value) => formatCurrency(Number(value))}
                    />
                    <YAxis
                      yAxisId="drawdown"
                      orientation="right"
                      axisLine={false}
                      tickLine={false}
                      tickMargin={10}
                      width={72}
                      stroke="#a1a1aa"
                      fontSize={11}
                      domain={["dataMin", 0]}
                      tickFormatter={(value) => formatPercent(Number(value))}
                    />
                    <Tooltip content={<EquityTooltip />} cursor={{ stroke: "rgba(16,185,129,0.25)", strokeWidth: 1 }} />
                    <Bar yAxisId="drawdown" dataKey="drawdown" barSize={12} radius={[6, 6, 0, 0]} fill="url(#drawdown-fill)" />
                    <Area
                      yAxisId="equity"
                      type="monotone"
                      dataKey="equity"
                      stroke="#10b981"
                      strokeWidth={3}
                      fill="url(#equity-fill)"
                      dot={false}
                      activeDot={{ r: 4, fill: "#10b981", stroke: "#052e16" }}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center rounded-2xl border border-dashed border-zinc-800 bg-zinc-950/50 text-sm text-zinc-500">
                  No equity curve was returned for this run.
                </div>
              )}
            </Card>
          </section>

          <section className="space-y-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold tracking-tight text-zinc-100">Trades</h2>
                <p className="text-sm text-zinc-400">Filter fills by side and outcome, then expand any row for execution details.</p>
              </div>

              <Button
                onClick={handleExport}
                disabled={exporting || !result.id}
                className="border-blue-400/30 bg-blue-500/10 text-blue-100 hover:bg-blue-500/15 disabled:opacity-50"
              >
                <span className="flex items-center gap-2">
                  {exporting ? <LoaderCircle size={16} className="animate-spin" /> : <Download size={16} />}
                  {exporting ? "Exporting..." : "Export Excel"}
                </span>
              </Button>
            </div>

            <Card className="border-zinc-800/80 bg-zinc-950/50 p-0 shadow-xl">
              <div className="flex flex-col gap-4 border-b border-zinc-800/70 px-5 py-5 lg:flex-row lg:items-center lg:justify-between">
                <div className="flex items-center gap-3">
                  <div className="rounded-2xl border border-zinc-800 bg-zinc-900/80 p-2 text-zinc-300">
                    <Filter size={16} />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-zinc-100">Trade filters</p>
                    <p className="text-xs text-zinc-500">{filteredTrades.length} rows match the current selection.</p>
                  </div>
                </div>

                <div className="flex flex-wrap gap-3">
                  <label className="space-y-2">
                    <span className="text-[11px] uppercase tracking-[0.18em] text-zinc-500">Side</span>
                    <select
                      value={sideFilter}
                      onChange={(event) => setSideFilter(event.target.value as SideFilterValue)}
                      className="rounded-2xl border border-zinc-800 bg-zinc-950/80 px-4 py-2.5 text-sm text-zinc-100 outline-none"
                    >
                      <option value="ALL">All sides</option>
                      <option value="BUY_YES">BUY_YES</option>
                      <option value="BUY_NO">BUY_NO</option>
                    </select>
                  </label>

                  <label className="space-y-2">
                    <span className="text-[11px] uppercase tracking-[0.18em] text-zinc-500">Status</span>
                    <select
                      value={statusFilter}
                      onChange={(event) => setStatusFilter(event.target.value as StatusFilterValue)}
                      className="rounded-2xl border border-zinc-800 bg-zinc-950/80 px-4 py-2.5 text-sm text-zinc-100 outline-none"
                    >
                      <option value="ALL">All outcomes</option>
                      <option value="WIN">WIN</option>
                      <option value="LOSS">LOSS</option>
                    </select>
                  </label>
                </div>
              </div>

              {filteredTrades.length === 0 ? (
                <div className="px-6 py-12 text-center">
                  <p className="text-sm font-medium text-zinc-200">No trades match the current filters.</p>
                  <p className="mt-2 text-sm text-zinc-500">Adjust side/status filters or rerun the backtest with another setup.</p>
                </div>
              ) : (
                <>
                  <div className="overflow-x-auto">
                    <Table className="min-w-[980px] text-sm">
                      <THead className="bg-zinc-900/40 text-[11px] uppercase tracking-[0.18em] text-zinc-500">
                        <Tr className="border-b border-zinc-800/70">
                          <Th className="w-14 px-5 py-4" />
                          <Th className="px-3 py-4">Date</Th>
                          <Th className="px-3 py-4">Marche</Th>
                          <Th className="px-3 py-4">Side</Th>
                          <Th className="px-3 py-4 text-right">Entree $</Th>
                          <Th className="px-3 py-4 text-right">Taille</Th>
                          <Th className="px-3 py-4 text-right">PnL $</Th>
                          <Th className="px-3 py-4 text-right">PnL %</Th>
                          <Th className="px-5 py-4">Status</Th>
                        </Tr>
                      </THead>
                      <TBody>
                        {paginatedTrades.map((trade) => {
                          const isExpanded = expandedTradeId === trade.id;
                          const detailItems = [
                            trade.entry !== null ? { label: "Entry", value: formatCurrency(trade.entry) } : null,
                            trade.exitPrice !== null ? { label: "Exit", value: formatCurrency(trade.exitPrice) } : null,
                            trade.notional !== null ? { label: "Notional", value: formatCurrency(trade.notional) } : null,
                            trade.slippage !== null ? { label: "Slippage", value: formatKpiValue(trade.slippage, "ratio") } : null,
                            trade.fees !== null ? { label: "Fees", value: formatCurrency(trade.fees) } : null,
                            trade.orderId ? { label: "Order ID", value: trade.orderId } : null,
                            trade.tokenId ? { label: "Token ID", value: trade.tokenId } : null,
                            trade.notes ? { label: "Notes", value: trade.notes } : null,
                          ].filter(Boolean) as Array<{ label: string; value: string }>;

                          return (
                            <Fragment key={trade.id}>
                              <Tr
                                className="border-b border-zinc-800/50 transition-colors hover:bg-zinc-900/30"
                              >
                                <Td className="px-5 py-4">
                                  <button
                                    type="button"
                                    onClick={() => setExpandedTradeId(isExpanded ? null : trade.id)}
                                    className="flex h-8 w-8 items-center justify-center rounded-full border border-zinc-800 bg-zinc-950/70 text-zinc-300 transition hover:border-emerald-400/40 hover:text-emerald-200"
                                    aria-label={isExpanded ? "Collapse row" : "Expand row"}
                                  >
                                    {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                                  </button>
                                </Td>
                                <Td className="px-3 py-4 text-zinc-300">{formatTimestamp(trade.date, { withTime: true })}</Td>
                                <Td className="max-w-[340px] px-3 py-4 text-zinc-100">
                                  <div className="truncate" title={trade.market}>
                                    {trade.market}
                                  </div>
                                </Td>
                                <Td className="px-3 py-4">
                                  <Badge className={cn("font-mono text-[11px]", sideBadgeClass(trade.side))}>{trade.side}</Badge>
                                </Td>
                                <Td className="px-3 py-4 text-right font-mono text-zinc-200">
                                  {trade.entry !== null ? formatCurrency(trade.entry) : "—"}
                                </Td>
                                <Td className="px-3 py-4 text-right font-mono text-zinc-200">
                                  {trade.size !== null ? decimalFormatter.format(trade.size) : "—"}
                                </Td>
                                <Td
                                  className={cn(
                                    "px-3 py-4 text-right font-mono font-medium",
                                    trade.pnl >= 0 ? "text-emerald-300" : "text-rose-300"
                                  )}
                                >
                                  {formatCurrency(trade.pnl, { signed: true })}
                                </Td>
                                <Td
                                  className={cn(
                                    "px-3 py-4 text-right font-mono font-medium",
                                    (trade.pnlPct ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"
                                  )}
                                >
                                  {trade.pnlPct !== null ? formatPercent(trade.pnlPct, { signed: true }) : "—"}
                                </Td>
                                <Td className="px-5 py-4">
                                  <Badge className={cn("font-mono text-[11px]", statusBadgeClass(trade.status))}>{trade.status}</Badge>
                                </Td>
                              </Tr>

                              {isExpanded ? (
                                <Tr className="border-b border-zinc-800/70 bg-zinc-950/80">
                                  <Td colSpan={9} className="px-5 py-5">
                                    {detailItems.length > 0 ? (
                                      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                                        {detailItems.map((item) => (
                                          <DetailField key={`${trade.id}-${item.label}`} label={item.label} value={item.value} />
                                        ))}
                                      </div>
                                    ) : (
                                      <div className="rounded-2xl border border-dashed border-zinc-800 bg-zinc-950/40 px-4 py-5 text-sm text-zinc-500">
                                        No extra execution details were returned for this trade.
                                      </div>
                                    )}
                                  </Td>
                                </Tr>
                              ) : null}
                            </Fragment>
                          );
                        })}
                      </TBody>
                    </Table>
                  </div>

                  <div className="flex flex-col gap-3 border-t border-zinc-800/70 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                    <p className="text-sm text-zinc-500">
                      Showing {(currentPage - 1) * PAGE_SIZE + 1}-{Math.min(currentPage * PAGE_SIZE, filteredTrades.length)} of{" "}
                      {filteredTrades.length} trades
                    </p>

                    <div className="flex items-center gap-2">
                      <Button
                        onClick={() => setCurrentPage((page) => Math.max(1, page - 1))}
                        disabled={currentPage === 1}
                        className="rounded-full border-zinc-800 bg-zinc-900/80 px-4 py-2 text-zinc-200"
                      >
                        Previous
                      </Button>
                      <div className="rounded-full border border-zinc-800 bg-zinc-950 px-4 py-2 text-sm text-zinc-300">
                        Page {currentPage} / {totalPages}
                      </div>
                      <Button
                        onClick={() => setCurrentPage((page) => Math.min(totalPages, page + 1))}
                        disabled={currentPage === totalPages}
                        className="rounded-full border-zinc-800 bg-zinc-900/80 px-4 py-2 text-zinc-200"
                      >
                        Next
                      </Button>
                    </div>
                  </div>
                </>
              )}
            </Card>
          </section>
        </>
      ) : null}
    </div>
  );
}
