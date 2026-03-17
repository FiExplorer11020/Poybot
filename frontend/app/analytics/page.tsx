"use client";

import { useEffect, useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export default function AnalyticsPage() {
  const [timeframe, setTimeframe] = useState("7d");
  const [points, setPoints] = useState<Array<{ timestamp: string; portfolio: number; pnl_pct: number }>>([]);

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/portfolio/pnl-by-timeframe?timeframe=${timeframe}`)
      .then((r) => r.json())
      .then((r) => setPoints(r.data ?? []));
  }, [timeframe]);

  const latest = useMemo(() => points[points.length - 1], [points]);

  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg">PnL by timeframe</h2>
          <div className="flex gap-2">
            {(["24h", "7d", "30d", "90d"] as const).map((tf) => (
              <Button key={tf} className={timeframe === tf ? "border-emerald-400/70 text-emerald-300 neon-glow" : ""} onClick={() => setTimeframe(tf)}>{tf}</Button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Card>
            <p className="text-xs text-zinc-400">Latest Portfolio Value</p>
            <p className="mt-1 font-mono text-2xl text-zinc-100">${latest?.portfolio?.toFixed?.(2) ?? "0.00"}</p>
          </Card>
          <Card>
            <p className="text-xs text-zinc-400">Latest PnL %</p>
            <p className="mt-1 font-mono text-2xl text-emerald-300">{latest?.pnl_pct?.toFixed?.(2) ?? "0.00"}%</p>
          </Card>
        </div>
      </Card>

      <Card className="h-[520px]">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={points}>
            <CartesianGrid stroke="rgba(39,39,42,.6)" />
            <XAxis dataKey="timestamp" hide />
            <YAxis yAxisId="left" />
            <YAxis yAxisId="right" orientation="right" />
            <Tooltip />
            <Area yAxisId="left" dataKey="portfolio" stroke="#00ff9d" fill="rgba(0,255,157,.14)" />
            <Area yAxisId="right" dataKey="pnl_pct" stroke="#60a5fa" fill="rgba(96,165,250,.14)" />
          </AreaChart>
        </ResponsiveContainer>
      </Card>
    </div>
  );
}
