"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { TradeHistoryTable } from "@/components/trading/TradeHistoryTable";
import { useBotStore } from "@/store/useBotStore";

export default function HistoryPage() {
  const [side, setSide] = useState("ALL");
  const [status, setStatus] = useState("ALL");
  const recentTrades = useBotStore((s) => s.recentTrades);

  const rows = useMemo(() => {
    return recentTrades.filter((r) => {
      const matchSide = side === "ALL" ? true : side === "BUY" ? r.side.includes("BUY") : r.side.includes("SELL");
      const matchStatus = status === "ALL" ? true : r.status.toUpperCase() === status.toUpperCase();
      return matchSide && matchStatus;
    });
  }, [recentTrades, side, status]);

  return (
    <div className="rounded-3xl border border-zinc-800/80 bg-zinc-950/50 shadow-xl p-5 animate-in fade-in duration-500">
      <div className="mb-6 flex flex-wrap items-center gap-4">
        <label className="text-xs text-zinc-400 flex items-center">
          <span className="mr-3 font-semibold tracking-wider uppercase">Side Filter</span>
          <select className="rounded-lg border border-zinc-800 bg-zinc-900/80 px-4 py-2 text-xs focus:ring-1 focus:ring-emerald-500/50 outline-none transition" value={side} onChange={(e) => setSide(e.target.value)}>
            <option>ALL</option><option>BUY</option><option>SELL</option>
          </select>
        </label>
        <label className="text-xs text-zinc-400 flex items-center">
          <span className="mr-3 font-semibold tracking-wider uppercase">Status Filter</span>
          <select className="rounded-lg border border-zinc-800 bg-zinc-900/80 px-4 py-2 text-xs focus:ring-1 focus:ring-emerald-500/50 outline-none transition" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option>ALL</option><option>OPEN</option><option>CLOSED</option>
          </select>
        </label>
      </div>
      
      <div className="rounded-xl border border-zinc-800/40 bg-zinc-900/20 overflow-hidden">
        <TradeHistoryTable rows={rows} />
      </div>
    </div>
  );
}
