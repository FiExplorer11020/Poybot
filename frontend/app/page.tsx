import { DetectionCard } from "@/components/trading/DetectionCard";
import { TradeHistoryTable } from "@/components/trading/TradeHistoryTable";
import { Card } from "@/components/ui/card";
import { pnlSeries, scannerCards } from "@/lib/mock-data";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const rows = Array.from({ length: 14 }).map((_, i) => ({
  timestamp: `${Date.now() - i * 87}ms`,
  market: scannerCards[i % scannerCards.length].market,
  side: i % 2 ? "BUY" : "SELL",
  size: `${(120 + i * 11).toFixed(2)}$`,
  entry: (0.32 + i * 0.01).toFixed(3),
  kelly: (0.3 + (i % 4) * 0.1).toFixed(2),
  risk: (0.8 + (i % 3) * 0.3).toFixed(2),
  status: i % 3 ? "FILLED" : "PENDING",
  latency: `${48 + i * 2}ms`
}));

export default function DashboardPage() {
  return (
    <div className="space-y-4">
      <Card className="h-[430px]">
        <p className="mb-3 text-sm text-zinc-300">PnL Curve</p>
        <ResponsiveContainer width="100%" height="92%">
          <AreaChart data={pnlSeries}>
            <CartesianGrid stroke="rgba(39,39,42,.6)" />
            <XAxis dataKey="t" />
            <YAxis />
            <Tooltip />
            <Area dataKey="pnl" stroke="#00ff9d" fill="rgba(0,255,157,.14)" />
          </AreaChart>
        </ResponsiveContainer>
      </Card>

      <div className="grid grid-cols-4 gap-3">
        {scannerCards.slice(0, 4).map((card) => (
          <DetectionCard key={card.market} {...card} />
        ))}
      </div>

      <Card>
        <p className="mb-2 text-sm text-zinc-300">Trade History</p>
        <TradeHistoryTable rows={rows} />
      </Card>
    </div>
  );
}
