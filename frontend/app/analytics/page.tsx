"use client";

import { useEffect, useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export default function AnalyticsPage() {
  const [timeframe, setTimeframe] = useState("7d");
  const [points, setPoints] = useState<Array<{ timestamp: string; portfolio: number; pnl_pct: number }>>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    setLoading(true);
    setError(null);

    fetch(`${API_BASE}/api/v1/portfolio/pnl-by-timeframe?timeframe=${timeframe}`, { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((r) => setPoints(r.data ?? []))
      .catch((e: Error) => {
        if (e.name === "AbortError") return;
        setPoints([]);
        setError("Unable to load analytics data right now.");
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [timeframe]);

  const latest = useMemo(() => points[points.length - 1], [points]);

  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg">PnL by timeframe</h2>
          <div className="flex flex-wrap gap-2">
            {(["24h", "7d", "30d", "90d"] as const).map((tf) => (
              <Button key={tf} className={timeframe === tf ? "border-emerald-400/70 text-emerald-300 neon-glow" : ""} onClick={() => setTimeframe(tf)}>{tf}</Button>
            ))}
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
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

      <Card className="h-[520px] space-y-2">
        {loading ? <p className="text-sm text-zinc-400">Loading data…</p> : null}
        {error ? <p className="text-sm text-rose-300">{error}</p> : null}
        {!loading && !error && points.length === 0 ? <p className="text-sm text-zinc-400">No data available for this timeframe.</p> : null}
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
