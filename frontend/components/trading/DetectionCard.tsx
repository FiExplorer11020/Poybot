import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Market } from "@/store/useBotStore";
import { Zap } from "lucide-react";

export function DetectionCard({ market }: { market: Market }) {
  const isHighRisk = market.spread > 0.05;

  return (
    <Card className={`relative overflow-hidden flex flex-col justify-between space-y-4 p-5 border ${isHighRisk ? "border-amber-500/30 bg-amber-950/10" : "border-emerald-500/20 bg-emerald-950/10"} transition-all duration-300 hover:border-emerald-500/50 hover:shadow-[0_0_20px_rgba(16,185,129,0.1)] rounded-2xl`}>
      {market.detected && (
        <div className="absolute -top-10 -right-10 w-32 h-32 bg-emerald-500/10 blur-3xl rounded-full" />
      )}
      <div className="flex items-start justify-between gap-3 relative z-10">
        <p className="text-[13px] font-semibold text-zinc-100 line-clamp-2 leading-tight tracking-wide">{market.title}</p>
        <Badge className={market.detected ? "border-emerald-400/60 bg-emerald-500/10 text-emerald-300 neon-glow whitespace-nowrap shadow-sm" : "border-zinc-700 bg-zinc-800/50 text-zinc-400 whitespace-nowrap"}>
          {market.detected ? <span className="flex items-center gap-1"><Zap size={10}/> Detected</span> : "Watching"}
        </Badge>
      </div>
      
      <div className="grid grid-cols-2 gap-y-3 gap-x-2 font-mono text-[11px] text-zinc-400 relative z-10 bg-black/40 p-3 rounded-xl border border-white/5">
        <div className="flex flex-col"><span className="text-[9px] font-sans text-zinc-500 uppercase tracking-widest mb-1">Bid / Ask</span><span className="text-zinc-200">{market.best_bid?.toFixed(3) ?? "N/A"} / {market.best_ask?.toFixed(3) ?? "N/A"}</span></div>
        <div className="flex flex-col"><span className="text-[9px] font-sans text-zinc-500 uppercase tracking-widest mb-1">Spread</span><span className={`${isHighRisk ? "text-amber-400" : "text-zinc-200"}`}>{market.spread?.toFixed(3) ?? "N/A"}</span></div>
        <div className="flex flex-col"><span className="text-[9px] font-sans text-zinc-500 uppercase tracking-widest mb-1">Exp. Edge</span><span className="text-emerald-400">+{(market.expected_edge * 100).toFixed(2)}%</span></div>
        <div className="flex flex-col"><span className="text-[9px] font-sans text-zinc-500 uppercase tracking-widest mb-1">Est. Profit</span><span className="text-emerald-400">${market.est_profit?.toFixed(2) ?? "0.00"}</span></div>
      </div>
    </Card>
  );
}
