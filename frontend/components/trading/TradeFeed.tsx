"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownRight, ArrowUpRight, Clock3, ReceiptText } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Trade } from "@/store/useLiveStore";

const moneyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const sideClassName = (side: string) =>
  side.includes("NO")
    ? "border-[rgba(255,107,107,0.24)] bg-[rgba(255,107,107,0.1)] text-[#ffb3b3]"
    : "border-[rgba(0,212,170,0.24)] bg-[rgba(0,212,170,0.1)] text-[#9af4df]";

const modeLabel = (trade: Trade) => (trade.execution_mode === "live" ? "LIVE" : "DRY");

export function TradeFeed({ trades }: { trades: Trade[] }) {
  const previousIdsRef = useRef<string[]>([]);
  const [freshIds, setFreshIds] = useState<string[]>([]);

  const rows = useMemo(() => trades.slice(0, 18), [trades]);

  useEffect(() => {
    const nextIds = rows.map((trade) => trade.id);
    const previousIds = new Set(previousIdsRef.current);
    const additions = nextIds.filter((id) => !previousIds.has(id));
    previousIdsRef.current = nextIds;

    if (additions.length === 0) {
      return undefined;
    }

    setFreshIds((current) => Array.from(new Set([...current, ...additions])));

    const timer = setTimeout(() => {
      setFreshIds((current) => current.filter((id) => !additions.includes(id)));
    }, 900);

    return () => clearTimeout(timer);
  }, [rows]);

  return (
    <section className="overflow-hidden rounded-[28px] border border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
      <div className="flex items-center justify-between gap-4 border-b border-white/8 px-5 py-4 sm:px-6">
        <div>
          <p className="text-[11px] uppercase tracking-[0.28em] text-white/40">Trade feed</p>
          <h2 className="mt-2 text-xl font-semibold text-white">Execution tape</h2>
        </div>
        <div className="hidden items-center gap-2 rounded-full border border-white/8 bg-black/20 px-3 py-2 text-xs text-white/55 sm:flex">
          <ReceiptText size={14} className="text-[#00d4aa]" />
          {rows.length} recent
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="flex min-h-[360px] flex-col items-center justify-center gap-3 px-6 text-center text-white/45">
          <ReceiptText size={20} className="text-[#00d4aa]" />
          <p className="max-w-sm text-sm">Le feed s’animera automatiquement au premier trade execute par le bot.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1">
          <div className="hidden grid-cols-[92px_minmax(0,1fr)_96px_108px_92px_82px] gap-4 border-b border-white/8 bg-[rgba(13,15,20,0.9)] px-5 py-3 text-[11px] uppercase tracking-[0.24em] text-white/35 sm:grid sm:px-6">
            <span>Temps</span>
            <span>Marche</span>
            <span>Side</span>
            <span>Notional</span>
            <span>PnL</span>
            <span>Mode</span>
          </div>

          <div className="max-h-[560px] overflow-y-auto">
            {rows.map((trade) => {
              const isProfit = trade.pnl_abs >= 0;
              const pnlLabel = `${trade.pnl_abs >= 0 ? "+" : "-"}${moneyFormatter.format(Math.abs(trade.pnl_abs))}`;

              return (
                <div
                  key={trade.id}
                  className={cn(
                    "grid gap-3 border-b border-white/6 px-5 py-4 transition-colors hover:bg-white/[0.03] sm:grid-cols-[92px_minmax(0,1fr)_96px_108px_92px_82px] sm:gap-4 sm:px-6",
                    freshIds.includes(trade.id) && "animate-trade-slide bg-[rgba(0,212,170,0.06)]"
                  )}
                >
                  <div className="flex items-center gap-2 font-mono text-[12px] text-white/55">
                    <Clock3 size={14} className="text-white/30" />
                    {new Date(trade.timestamp).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}
                  </div>

                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-white">{trade.market_title}</p>
                    <p className="mt-1 font-mono text-[11px] uppercase tracking-[0.18em] text-white/30">
                      {trade.status}
                    </p>
                  </div>

                  <div>
                    <span
                      className={cn(
                        "inline-flex items-center rounded-full border px-3 py-1 font-mono text-[10px] uppercase tracking-[0.24em]",
                        sideClassName(trade.side)
                      )}
                    >
                      {trade.side.replaceAll("_", " ")}
                    </span>
                  </div>

                  <div className="font-mono text-sm text-white/78">{moneyFormatter.format(trade.notional)}</div>

                  <div
                    className={cn(
                      "flex items-center gap-1 font-mono text-sm",
                      isProfit ? "text-[#00d4aa]" : "text-[#ff6b6b]"
                    )}
                  >
                    {isProfit ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
                    {pnlLabel}
                  </div>

                  <div>
                    <span className="inline-flex rounded-full border border-white/10 bg-black/20 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.24em] text-white/62">
                      {modeLabel(trade)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
