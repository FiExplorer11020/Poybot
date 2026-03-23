"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight } from "lucide-react";

import { WalletButton } from "@/components/trading/WalletButton";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Dashboard" },
  { href: "/scanner", label: "Scanner" },
  { href: "/positions", label: "Positions" },
  { href: "/history", label: "History" },
  { href: "/config", label: "Config" },
];

export function AppHeader() {
  const pathname = usePathname();
  const activeLink =
    links.find((link) => (link.href === "/" ? pathname === "/" : pathname.startsWith(link.href))) ?? links[0];

  return (
    <header className="sticky top-0 z-30 border-b border-white/8 bg-[rgba(13,15,20,0.84)] px-4 py-4 backdrop-blur-xl sm:px-6 lg:px-8">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.3em] text-white/32">
            <span>Control surface</span>
            <ChevronRight size={12} className="text-white/18" />
            <span>{activeLink.label}</span>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {links.map((link) => {
              const isActive = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);

              return (
                <Link
                  key={link.href}
                  href={link.href}
                  aria-current={isActive ? "page" : undefined}
                  className={cn(
                    "rounded-full border px-3 py-1.5 text-xs uppercase tracking-[0.22em] transition-colors",
                    isActive
                      ? "border-[rgba(0,212,170,0.26)] bg-[rgba(0,212,170,0.1)] text-[#9af4df]"
                      : "border-white/10 bg-black/15 text-white/48 hover:border-white/16 hover:text-white/75"
                  )}
                >
                  {link.label}
                </Link>
              );
            })}
          </div>
        </div>

        <div className="flex items-center gap-3 self-start xl:self-auto">
          <div className="hidden rounded-full border border-white/8 bg-black/20 px-3 py-2 text-[11px] uppercase tracking-[0.24em] text-white/45 md:inline-flex">
            Polymarket bot
          </div>
          <WalletButton />
        </div>
      </div>
    </header>
  );
}
