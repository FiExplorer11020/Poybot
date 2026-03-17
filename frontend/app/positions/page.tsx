import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { PositionsTable } from "@/components/trading/PositionsTable";
import { RiskSliders } from "@/components/trading/RiskSliders";
import { Card } from "@/components/ui/card";
import { pnlSeries } from "@/lib/mock-data";

export default function PositionsPage() {
  return (
    <div className="grid grid-cols-[20%_55%_25%] gap-4">
      <Card className="space-y-3">
        <p className="text-sm text-zinc-300">Total Portfolio Exposure</p>
        <Gauge value={37.4} label="Exposure" />
        <p className="text-sm text-zinc-300">Total Risk Taken</p>
        <Gauge value={18.9} label="Risk" />
      </Card>

      <div className="space-y-4">
        <Card>
          <p className="mb-2 text-sm text-zinc-300">Open Positions</p>
          <PositionsTable />
        </Card>
        <Card className="h-[270px]">
          <p className="mb-2 text-sm">Live PnL + Drawdown</p>
          <ResponsiveContainer width="100%" height="90%">
            <AreaChart data={pnlSeries}>
              <CartesianGrid stroke="rgba(39,39,42,.6)" />
              <XAxis dataKey="t" />
              <YAxis />
              <Tooltip />
              <Area dataKey="pnl" stroke="#00ff9d" fill="rgba(0,255,157,.14)" />
              <Area dataKey="drawdown" stroke="#fb7185" fill="rgba(244,63,94,.12)" />
            </AreaChart>
          </ResponsiveContainer>
        </Card>
      </div>

      <RiskSliders />
    </div>
  );
}

function Gauge({ value, label }: { value: number; label: string }) {
  return (
    <div className="space-y-1 rounded-3xl border border-zinc-800 p-3">
      <div className="flex items-center justify-between text-xs text-zinc-400"><span>{label}</span><span>{value.toFixed(2)}%</span></div>
      <div className="h-2 rounded-full bg-zinc-800">
        <div className="h-2 rounded-full bg-emerald-400" style={{ width: `${Math.min(100, value)}%` }} />
      </div>
    </div>
  );
}
