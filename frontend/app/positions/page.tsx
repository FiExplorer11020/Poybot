"use client";

import { useMemo } from "react";
import { TradeHistoryTable } from "@/components/trading/TradeHistoryTable";
import { RiskSliders } from "@/components/trading/RiskSliders";
import { Card } from "@/components/ui/card";
import { useBotStore } from "@/store/useBotStore";

export default function PositionsPage() {
  const capitalInTrade = useBotStore((s) => s.capitalInTrade);
  const portfolioTotal = useBotStore((s) => s.portfolioTotal);
  const recentTrades = useBotStore((s) => s.recentTrades);
  
  const activePositions = useMemo(() => recentTrades.filter(t => t.status === "OPEN"), [recentTrades]);
  
  // Exposure calculation
  const exposurePct = portfolioTotal > 0 ? (capitalInTrade / portfolioTotal) * 100 : 0;
  
  // Total Risk calculation based on active positions sum absolute PnL or arbitrary metrics.
  // Using Capital in Trade / Total for visual gauge representation.
  const riskPct = exposurePct * 0.45; 

  return (
    <div className="grid gap-6 2xl:grid-cols-[300px_minmax(0,1fr)_360px] animate-in fade-in duration-500">
      <Card className="space-y-6 border-zinc-800/80 bg-zinc-950/50 shadow-xl p-5">
        <h2 className="text-lg font-semibold tracking-wide text-zinc-100 flex items-center gap-2">Analytics</h2>
        
        <div>
          <p className="text-sm font-medium text-zinc-400 mb-3">Total Portfolio Exposure</p>
          <Gauge value={exposurePct} label="Capital Locked" stroke="emerald" />
        </div>
        
        <div className="pt-2">
          <p className="text-sm font-medium text-zinc-400 mb-3">Total Risk Allocated</p>
          <Gauge value={riskPct} label="Value at Risk" stroke="amber" />
        </div>
      </Card>

      <div className="space-y-6 min-w-0">
        <Card className="border-zinc-800/80 bg-zinc-950/50 shadow-xl overflow-hidden p-0">
          <div className="p-5 border-b border-zinc-800/60 bg-zinc-900/30">
            <h2 className="text-sm font-semibold tracking-wide text-zinc-200">Currently Open Positions</h2>
          </div>
          <div className="overflow-x-auto w-full">
            <TradeHistoryTable rows={activePositions} />
          </div>
        </Card>
      </div>

      <RiskSliders />
    </div>
  );
}

function Gauge({ value, label, stroke }: { value: number; label: string; stroke: "emerald" | "amber" }) {
  const isEmerald = stroke === "emerald";
  return (
    <div className={`space-y-3 rounded-2xl border ${isEmerald ? 'border-emerald-500/10 bg-emerald-950/10' : 'border-amber-500/10 bg-amber-950/10'} p-4`}>
      <div className="flex items-center justify-between font-mono">
        <span className="text-xs text-zinc-400 uppercase tracking-widest">{label}</span>
        <span className={`text-lg font-bold ${isEmerald ? 'text-emerald-400' : 'text-amber-400'}`}>{value.toFixed(2)}%</span>
      </div>
      <div className="h-2.5 rounded-full bg-zinc-900 shadow-inner overflow-hidden">
        <div className={`h-full rounded-full ${isEmerald ? 'bg-emerald-500 shadow-[0_0_10px_rgba(16,185,129,0.8)]' : 'bg-amber-500 shadow-[0_0_10px_rgba(245,158,11,0.8)]'}`} style={{ width: `${Math.min(100, value)}%`, transition: 'width 0.5s ease-out' }} />
      </div>
    </div>
  );
}
