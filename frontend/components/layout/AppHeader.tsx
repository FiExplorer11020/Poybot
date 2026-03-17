"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { WalletButton } from "@/components/trading/WalletButton";
import { cn } from "@/lib/utils";
import { useBotStore } from "@/store/useBotStore";

const links = [
  { href: "/", label: "Dashboard" },
  { href: "/scanner", label: "Scanner" },
  { href: "/positions", label: "Positions" },
  { href: "/history", label: "History" },
  { href: "/config", label: "Config" }
];

export function AppHeader() {
  const pathname = usePathname();
  const { status, uptime, latencyMs } = useBotStore();

  return (
    <header className="sticky top-0 z-40 flex flex-col gap-3 rounded-3xl border border-zinc-800 bg-zinc-950/90 px-4 py-4 backdrop-blur lg:h-20 lg:flex-row lg:items-center lg:justify-between lg:px-6 lg:py-0">
      <div className="flex min-w-0 flex-col gap-3 lg:flex-row lg:items-center lg:gap-6">
        <h1 className="shrink-0 font-sans text-xl font-semibold tracking-wide text-zinc-100 lg:text-2xl">FRONT-RUN BOT</h1>
        <nav className="flex flex-wrap gap-2 text-sm">
          {links.map((link) => {
            const isActive = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "rounded-full border px-3 py-1.5 transition",
                  isActive
                    ? "border-emerald-400/70 text-emerald-300 neon-glow"
                    : "border-zinc-800 hover:border-emerald-400/50 hover:text-emerald-300"
                )}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
      </div>

      <div className="flex flex-wrap items-center gap-2 lg:gap-3">
        <Badge className={status === "LIVE" ? "border-emerald-400/50 text-emerald-300 neon-glow" : "border-amber-500/50 text-amber-300"}>
          {status}
        </Badge>
        <Badge className="font-mono text-zinc-300">{uptime}</Badge>
        <Badge className="font-mono text-zinc-300">{latencyMs}ms</Badge>
        <WalletButton />
      </div>
    </header>
  );
}
