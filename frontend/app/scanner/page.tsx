"use client";

import { useMemo, useState } from "react";
import { DetectionCard } from "@/components/trading/DetectionCard";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { useBotStore } from "@/store/useBotStore";

const tabs = ["All", "Detected", "High-Risk"];

export default function ScannerPage() {
  const [tab, setTab] = useState("All");
  const markets = useBotStore((s) => s.markets);

  const filtered = useMemo(() => {
    if (tab === "All") return markets;
    if (tab === "High-Risk") return markets.filter((m) => m.spread > 0.05 || m.volatility > 0.05);
    if (tab === "Detected") return markets.filter((m) => m.detected);
    return markets;
  }, [tab, markets]);

  return (
    <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_320px] animate-in fade-in duration-500">
      <div className="space-y-4 min-w-0">
        <Card className="border-zinc-800/80 bg-zinc-950/50 shadow-xl">
          <div className="mb-4 flex flex-wrap gap-2">
            {tabs.map((item) => (
              <button
                key={item}
                className={`rounded-full border px-4 py-1.5 text-sm transition ${tab === item ? "border-emerald-400/70 text-emerald-300 neon-glow" : "border-zinc-800 text-zinc-400 hover:text-zinc-200 hover:border-zinc-700"}`}
                onClick={() => setTab(item)}
              >
                {item}
              </button>
            ))}
          </div>
          {filtered.length === 0 ? <p className="text-sm text-zinc-500 py-10 text-center font-mono">NO MARKETS MATCHING CRITERIA.</p> : null}
          <div className="grid gap-4 md:grid-cols-2">
            {filtered.map((market) => (
              <DetectionCard key={market.market_id} market={market} />
            ))}
          </div>
        </Card>
      </div>

      <Card className="border-zinc-800/80 bg-zinc-950/50 shadow-xl">
        <p className="mb-4 text-sm font-semibold tracking-wide text-zinc-200">Live Alerts</p>
        <div className="space-y-3 overflow-y-auto pr-1 font-mono text-xs max-h-[50vh] 2xl:max-h-[730px]">
          {markets.filter(m => m.detected || m.spread > 0.05).slice(0, 25).map((m, i) => (
            <div key={`${m.market_id}-${i}`} className="rounded-xl border border-zinc-800/60 bg-zinc-900/40 p-3">
              <Badge className={m.detected ? "border-emerald-400/50 text-emerald-300" : "border-amber-500/50 text-amber-300"}>
                {m.detected ? "DETECTED EDGE" : "HIGH SPREAD"}
              </Badge>
              <p className="mt-2 text-zinc-300 leading-relaxed truncate">{m.title}</p>
              <p className="mt-1 text-zinc-500">Value: {m.expected_edge > 0 ? `+${(m.expected_edge * 100).toFixed(2)}%` : 'Wait'}</p>
            </div>
          ))}
          {markets.filter(m => m.detected || m.spread > 0.05).length === 0 && (
            <div className="text-zinc-500 text-center py-6">No anomalies detected.</div>
          )}
        </div>
      </Card>
    </div>
  );
}
