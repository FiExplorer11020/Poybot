"use client";

import { Card } from "@/components/ui/card";
import { formatMoney, formatPct } from "@/lib/utils";
import { useBotStore } from "@/store/useBotStore";

export function StatsSidebar() {
  const { totalPnl, totalPnlPct, activePositions, latencyMs, capitalInTrade, portfolioTotal } = useBotStore();
  const riskGauge = Math.min(100, (capitalInTrade / Math.max(portfolioTotal, 1)) * 100);

  const stats = [
    ["Total P&L", `${formatMoney(totalPnl)} (${formatPct(totalPnlPct)})`],
    ["Win Rate", "63.20%"],
    ["Active Positions", String(activePositions)],
    ["Latency", `${latencyMs}ms`],
    ["Risk Gauge", `${riskGauge.toFixed(2)}%`]
  ];

  return (
    <aside className="w-full">
      <Card className="space-y-3 bg-zinc-950/80">
        {stats.map(([label, value]) => (
          <div key={label} className="rounded-3xl border border-zinc-800 bg-zinc-900/70 p-3">
            <p className="text-[11px] uppercase tracking-wide text-zinc-400">{label}</p>
            <p className="mt-1 font-mono text-lg text-zinc-100">{value}</p>
          </div>
        ))}
      </Card>
    </aside>
  );
}
