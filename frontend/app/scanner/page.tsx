"use client";

import { useMemo, useState } from "react";

import { DetectionCard } from "@/components/trading/DetectionCard";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { scannerCards } from "@/lib/mock-data";

const tabs = ["All", "Crypto", "Politics", "High-Risk"];

export default function ScannerPage() {
  const [tab, setTab] = useState("All");

  const filtered = useMemo(() => {
    if (tab === "All") return scannerCards;
    if (tab === "High-Risk") return scannerCards.filter((m) => m.risk === "high");
    return scannerCards.filter((m) => m.category === tab);
  }, [tab]);

  return (
    <div className="grid grid-cols-[75%_25%] gap-4">
      <div className="space-y-4">
        <Card>
          <div className="mb-3 flex gap-2">
            {tabs.map((item) => (
              <button
                key={item}
                className={`rounded-full border px-3 py-1 text-sm ${tab === item ? "border-emerald-400/70 text-emerald-300 neon-glow" : "border-zinc-700 text-zinc-300"}`}
                onClick={() => setTab(item)}
              >
                {item}
              </button>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {filtered.map((card) => (
              <DetectionCard key={card.market} {...card} />
            ))}
          </div>
        </Card>

        <Card>
          <p className="mb-2 text-sm">Recent mini-log</p>
          <div className="space-y-2 font-mono text-xs">
            {filtered.slice(0, 8).map((x) => (
              <div key={x.market} className="rounded-2xl border border-zinc-800 p-2">
                {x.market} • spread {x.spread.toFixed(3)} • {x.detected ? "detected" : "watch"}
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Card>
        <p className="mb-3 text-sm">Live Alerts</p>
        <div className="space-y-2 overflow-y-auto pr-1 font-mono text-xs" style={{ maxHeight: 730 }}>
          {Array.from({ length: 25 }).map((_, i) => (
            <div key={i} className="rounded-2xl border border-zinc-800 p-2">
              <Badge className={i % 2 ? "border-emerald-400/50 text-emerald-300" : "border-amber-500/50 text-amber-300"}>
                {i % 2 ? "DETECTED" : "HIGH RISK"}
              </Badge>
              <p className="mt-1">Signal #{100 + i} latency {48 + i}ms</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
