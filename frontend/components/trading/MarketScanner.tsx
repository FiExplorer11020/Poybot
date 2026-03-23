"use client";

import { useMemo } from "react";
import { Radar, Waves } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Market } from "@/store/useLiveStore";

const percentFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const decimalFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});

const getStatus = (market: Market) => {
  if (market.detected) {
    return "DETECTED";
  }
  if (market.direction !== "HOLD" && market.expected_edge > 0.002) {
    return "READY";
  }
  if (market.spread > 0.04) {
    return "WIDE";
  }
  return "MONITOR";
};

const statusClassName = (status: string) => {
  if (status === "DETECTED") {
    return "border-[rgba(0,212,170,0.32)] bg-[rgba(0,212,170,0.12)] text-[#9af4df]";
  }
  if (status === "READY") {
    return "border-white/10 bg-white/5 text-white";
  }
  if (status === "WIDE") {
    return "border-[rgba(255,107,107,0.26)] bg-[rgba(255,107,107,0.12)] text-[#ffc0c0]";
  }
  return "border-white/10 bg-black/20 text-white/60";
};

const humanizeDirection = (direction: string) => direction.replaceAll("_", " ");

export function MarketScanner({ markets }: { markets: Market[] }) {
  const rows = useMemo(
    () =>
      [...markets].sort(
        (left, right) =>
          Number(right.detected) - Number(left.detected) ||
          right.expected_edge - left.expected_edge ||
          right.signal_strength - left.signal_strength
      ),
    [markets]
  );

  return (
    <section className="overflow-hidden rounded-[28px] border border-white/8 bg-[rgba(22,25,33,0.94)] shadow-[0_24px_80px_rgba(0,0,0,0.35)]">
      <div className="flex items-center justify-between gap-4 border-b border-white/8 px-5 py-4 sm:px-6">
        <div>
          <p className="text-[11px] uppercase tracking-[0.28em] text-white/40">Scanner de marches</p>
          <h2 className="mt-2 text-xl font-semibold text-white">Active opportunity surface</h2>
        </div>
        <div className="hidden items-center gap-2 rounded-full border border-white/8 bg-black/20 px-3 py-2 text-xs text-white/55 sm:flex">
          <Radar size={14} className="text-[#00d4aa]" />
          {rows.length} tracked
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="flex min-h-[360px] flex-col items-center justify-center gap-3 px-6 text-center text-white/45">
          <Waves size={20} className="text-[#00d4aa]" />
          <p className="max-w-sm text-sm">Aucun marche n’est encore alimente par le flux live. Le scanner s’actualisera des reception des prochains ticks.</p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full border-separate border-spacing-0">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-[0.24em] text-white/35">
                <th className="bg-[rgba(13,15,20,0.92)] px-5 py-3 font-medium sm:px-6">Marche</th>
                <th className="bg-[rgba(13,15,20,0.92)] px-4 py-3 font-medium">Mid</th>
                <th className="bg-[rgba(13,15,20,0.92)] px-4 py-3 font-medium">Spread</th>
                <th className="bg-[rgba(13,15,20,0.92)] px-4 py-3 font-medium">Edge</th>
                <th className="bg-[rgba(13,15,20,0.92)] px-4 py-3 font-medium">Direction</th>
                <th className="bg-[rgba(13,15,20,0.92)] px-5 py-3 font-medium sm:px-6">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((market) => {
                const status = getStatus(market);

                return (
                  <tr
                    key={market.market_id}
                    className={cn(
                      "border-t border-white/6 text-sm text-white/85 transition-colors",
                      market.detected
                        ? "animate-detected-row bg-[linear-gradient(90deg,rgba(0,212,170,0.12),rgba(0,212,170,0.03)_35%,transparent_100%)]"
                        : "hover:bg-white/[0.03]"
                    )}
                  >
                    <td className="max-w-[360px] px-5 py-4 sm:px-6">
                      <div className="flex flex-col gap-1">
                        <span className="line-clamp-2 text-sm font-medium text-white">{market.title}</span>
                        <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-white/35">
                          {market.market_id.slice(0, 10)}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-4 font-mono text-[13px] text-[#b8fff1]">
                      {decimalFormatter.format(market.mid_price)}
                    </td>
                    <td className="px-4 py-4 font-mono text-[13px] text-white/70">
                      {decimalFormatter.format(market.spread)}
                    </td>
                    <td className="px-4 py-4">
                      <div className="flex flex-col gap-1 font-mono">
                        <span className={market.expected_edge >= 0 ? "text-[#00d4aa]" : "text-[#ff6b6b]"}>
                          {market.expected_edge >= 0 ? "+" : ""}
                          {percentFormatter.format(market.expected_edge * 100)}%
                        </span>
                        <span className="text-[11px] text-white/35">${market.est_profit.toFixed(2)} est.</span>
                      </div>
                    </td>
                    <td className="px-4 py-4 font-mono text-[12px] uppercase tracking-[0.16em] text-white/65">
                      {humanizeDirection(market.direction)}
                    </td>
                    <td className="px-5 py-4 sm:px-6">
                      <span
                        className={cn(
                          "inline-flex items-center rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.24em]",
                          statusClassName(status)
                        )}
                      >
                        {status}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
