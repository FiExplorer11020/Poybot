"use client";

import Link from "next/link";
import { Activity, Clock3, Gauge, Wallet } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { WalletButton } from "@/components/trading/WalletButton";
import { useBotStore } from "@/store/useBotStore";

export function AppHeader() {
  const { status, uptime, latencyMs } = useBotStore();

  return (
    <header className="sticky top-0 z-40 flex h-20 items-center justify-between rounded-3xl border border-zinc-800 bg-zinc-950/90 px-6 backdrop-blur">
      <div className="flex items-center gap-8">
        <h1 className="font-sans text-2xl font-semibold tracking-wide text-zinc-100">FRONT-RUN BOT</h1>
        <nav className="flex gap-2 text-sm">
          {["/", "/scanner", "/positions", "/history", "/config"].map((href) => (
            <Link key={href} href={href} className="rounded-full border border-zinc-800 px-3 py-1.5 hover:border-emerald-400/50 hover:text-emerald-300">
              {href === "/" ? "Dashboard" : href.replace("/", "").toUpperCase()}
            </Link>
          ))}
        </nav>
      </div>

      <div className="flex items-center gap-3">
        <Badge className={status === "LIVE" ? "border-emerald-400/50 text-emerald-300 neon-glow" : "border-amber-500/50 text-amber-300"}>
          <Activity className="mr-1 inline h-3 w-3" />
          {status}
        </Badge>
        <Badge className="font-mono text-zinc-300">
          <Clock3 className="mr-1 inline h-3 w-3" />
          {uptime}
        </Badge>
        <Badge className="font-mono text-zinc-300">
          <Gauge className="mr-1 inline h-3 w-3" />
          {latencyMs}ms
        </Badge>
        <Wallet className="h-4 w-4 text-zinc-400" />
        <WalletButton />
      </div>
    </header>
  );
}
