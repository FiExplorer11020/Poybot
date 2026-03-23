"use client";

import { useMemo, useState } from "react";
import { Pause, Play, Power, ShieldAlert, TimerReset, TrendingUp, Waves } from "lucide-react";

import { Dialog } from "@/components/ui/dialog";
import { cn, formatMoney, formatPct } from "@/lib/utils";
import { useLiveStore } from "@/store/useLiveStore";

const latencyTone = (latencyMs: number) => {
  if (latencyMs < 100) {
    return {
      text: "text-[#9af4df]",
      dot: "bg-[#00d4aa]",
      badge: "border-[rgba(0,212,170,0.26)] bg-[rgba(0,212,170,0.1)]",
    };
  }
  if (latencyMs < 300) {
    return {
      text: "text-[#ffd89c]",
      dot: "bg-[#ffb84d]",
      badge: "border-[rgba(255,184,77,0.22)] bg-[rgba(255,184,77,0.12)]",
    };
  }
  return {
    text: "text-[#ffb3b3]",
    dot: "bg-[#ff6b6b]",
    badge: "border-[rgba(255,107,107,0.26)] bg-[rgba(255,107,107,0.12)]",
  };
};

const statusTone = (status: string) => {
  if (status === "RUNNING") {
    return {
      text: "text-[#9af4df]",
      dot: "bg-[#00d4aa]",
      badge: "border-[rgba(0,212,170,0.26)] bg-[rgba(0,212,170,0.1)]",
    };
  }
  if (status === "PAUSED") {
    return {
      text: "text-[#ffd89c]",
      dot: "bg-[#ffb84d]",
      badge: "border-[rgba(255,184,77,0.22)] bg-[rgba(255,184,77,0.12)]",
    };
  }
  return {
    text: "text-[#ffb3b3]",
    dot: "bg-[#ff6b6b]",
    badge: "border-[rgba(255,107,107,0.26)] bg-[rgba(255,107,107,0.12)]",
  };
};

const formatUptime = (uptimeSeconds: number) => {
  const hours = Math.floor(uptimeSeconds / 3600);
  const minutes = Math.floor((uptimeSeconds % 3600) / 60);
  const seconds = uptimeSeconds % 60;

  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
};

function MetricLine({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div className="rounded-[18px] border border-white/8 bg-black/20 px-3 py-3">
      <p className="text-[10px] uppercase tracking-[0.24em] text-white/35">{label}</p>
      <span
        key={`${label}-${value}`}
        className={cn("mt-2 inline-flex animate-value-flip font-mono text-base font-medium text-white", accent)}
      >
        {value}
      </span>
    </div>
  );
}

export function StatsSidebar({ mobile = false }: { mobile?: boolean }) {
  const [stopDialogOpen, setStopDialogOpen] = useState(false);

  const status = useLiveStore((state) => state.status);
  const uptimeSeconds = useLiveStore((state) => state.uptimeSeconds);
  const latencyMs = useLiveStore((state) => state.latencyMs);
  const portfolioTotal = useLiveStore((state) => state.portfolioTotal);
  const totalPnl = useLiveStore((state) => state.totalPnl);
  const totalPnlPct = useLiveStore((state) => state.totalPnlPct);
  const capitalInTrade = useLiveStore((state) => state.capitalInTrade);
  const priceHistory = useLiveStore((state) => state.priceHistory);
  const halt = useLiveStore((state) => state.halt);
  const controlPending = useLiveStore((state) => state.controlPending);
  const sendBotCommand = useLiveStore((state) => state.sendBotCommand);
  const clearHalt = useLiveStore((state) => state.clearHalt);

  const liveEquity = priceHistory.at(-1)?.portfolio ?? portfolioTotal + totalPnl;

  const exposurePct = useMemo(
    () => Math.min(100, liveEquity > 0 ? (capitalInTrade / liveEquity) * 100 : 0),
    [capitalInTrade, liveEquity]
  );

  const latency = latencyTone(latencyMs);
  const bot = statusTone(status);

  return (
    <>
      <aside
        className={cn(
          "flex h-full flex-col gap-4 border-white/8 bg-[rgba(18,21,29,0.96)] text-white shadow-[0_24px_80px_rgba(0,0,0,0.35)]",
          mobile ? "rounded-[28px] border p-4" : "h-full border-r px-4 py-5"
        )}
      >
        <div className="space-y-4">
          <div className="border-b border-white/8 pb-4">
            <p className="text-[11px] uppercase tracking-[0.32em] text-white/35">Poybot</p>
            <h2 className="mt-3 text-xl font-semibold tracking-[-0.04em] text-white">Trading control</h2>
            <p className="mt-2 text-sm leading-6 text-white/45">Industrial live monitoring for the Polymarket engine.</p>
          </div>

          <div className={cn("rounded-[22px] border px-4 py-4", bot.badge)}>
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[10px] uppercase tracking-[0.24em] text-white/45">Status bot</p>
                <div className="mt-2 flex items-center gap-2">
                  <span className={cn("h-2.5 w-2.5 rounded-full animate-signal-pulse", bot.dot)} />
                  <span className={cn("font-mono text-sm font-semibold tracking-[0.2em]", bot.text)}>{status}</span>
                </div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/20 p-2 text-white/55">
                <Waves size={16} />
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <MetricLine label="Uptime" value={formatUptime(uptimeSeconds)} />
            <div className={cn("rounded-[18px] border px-3 py-3", latency.badge)}>
              <p className="text-[10px] uppercase tracking-[0.24em] text-white/35">Latency</p>
              <div className="mt-2 flex items-center gap-2">
                <span className={cn("h-2.5 w-2.5 rounded-full animate-signal-pulse", latency.dot)} />
                <span
                  key={`latency-${latencyMs}`}
                  className={cn("inline-flex animate-value-flip font-mono text-base font-medium", latency.text)}
                >
                  {latencyMs}ms
                </span>
              </div>
            </div>
          </div>

          <MetricLine label="Equity totale" value={formatMoney(liveEquity)} />
          <MetricLine
            label="PnL"
            value={`${formatMoney(totalPnl)}  ${formatPct(totalPnlPct)}`}
            accent={totalPnl >= 0 ? "text-[#9af4df]" : "text-[#ffb3b3]"}
          />

          <div className="rounded-[22px] border border-white/8 bg-black/20 p-4">
            <div className="flex items-center justify-between gap-2">
              <div>
                <p className="text-[10px] uppercase tracking-[0.24em] text-white/35">Capital in trade</p>
                <span key={`capital-${capitalInTrade}`} className="mt-2 inline-flex animate-value-flip font-mono text-lg font-semibold text-white">
                  {formatMoney(capitalInTrade)}
                </span>
              </div>
              <TrendingUp size={16} className="text-[#00d4aa]" />
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-white/6">
              <div
                className="h-full rounded-full bg-[linear-gradient(90deg,#00d4aa_0%,#7bf7de_100%)] transition-[width] duration-500"
                style={{ width: `${capitalInTrade > 0 ? Math.max(8, exposurePct) : 0}%` }}
              />
            </div>
            <p className="mt-2 font-mono text-[11px] uppercase tracking-[0.2em] text-white/40">{exposurePct.toFixed(1)}% deployed</p>
          </div>
        </div>

        <div className="mt-auto space-y-4 pt-2">
          <div className="rounded-[22px] border border-white/8 bg-black/20 p-4">
            <div className="flex items-center gap-2 text-white/75">
              <TimerReset size={15} className="text-[#00d4aa]" />
              <p className="text-[11px] uppercase tracking-[0.24em]">Bot controls</p>
            </div>
            <div className="mt-4 grid gap-2">
              <button
                type="button"
                disabled={controlPending || status === "RUNNING"}
                onClick={() => sendBotCommand("start")}
                className="inline-flex items-center justify-center gap-2 rounded-2xl border border-[rgba(0,212,170,0.24)] bg-[rgba(0,212,170,0.1)] px-3 py-3 font-mono text-xs uppercase tracking-[0.22em] text-[#9af4df] transition-colors hover:bg-[rgba(0,212,170,0.14)] disabled:cursor-not-allowed disabled:opacity-45"
              >
                <Play size={14} />
                Start
              </button>
              <button
                type="button"
                disabled={controlPending || status !== "RUNNING"}
                onClick={() => sendBotCommand("pause")}
                className="inline-flex items-center justify-center gap-2 rounded-2xl border border-white/10 bg-white/[0.05] px-3 py-3 font-mono text-xs uppercase tracking-[0.22em] text-white/80 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-45"
              >
                <Pause size={14} />
                Pause
              </button>
              <button
                type="button"
                disabled={controlPending || status === "STOPPED"}
                onClick={() => setStopDialogOpen(true)}
                className="inline-flex items-center justify-center gap-2 rounded-2xl border border-[rgba(255,107,107,0.24)] bg-[rgba(255,107,107,0.1)] px-3 py-3 font-mono text-xs uppercase tracking-[0.22em] text-[#ffb3b3] transition-colors hover:bg-[rgba(255,107,107,0.14)] disabled:cursor-not-allowed disabled:opacity-45"
              >
                <Power size={14} />
                Stop
              </button>
            </div>
          </div>

          {halt.active ? (
            <div className="animate-halt-alert rounded-[22px] border border-[rgba(255,107,107,0.28)] bg-[rgba(255,107,107,0.12)] p-4">
              <div className="flex items-start gap-3">
                <ShieldAlert size={16} className="mt-0.5 shrink-0 text-[#ff6b6b]" />
                <div className="min-w-0">
                  <p className="text-[11px] uppercase tracking-[0.24em] text-[#ffb3b3]">Kill switch active</p>
                  <p className="mt-2 font-mono text-sm text-white">{halt.reason}</p>
                  {halt.details ? <p className="mt-2 text-xs leading-5 text-white/60">{halt.details}</p> : null}
                  <button
                    type="button"
                    onClick={clearHalt}
                    className="mt-3 rounded-full border border-white/12 px-3 py-1.5 text-[10px] uppercase tracking-[0.22em] text-white/75 transition-colors hover:bg-white/[0.06]"
                  >
                    Masquer l’alerte
                  </button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </aside>

      <Dialog open={stopDialogOpen} onClose={() => setStopDialogOpen(false)}>
        <div className="rounded-[28px] border border-[rgba(255,107,107,0.18)] bg-[#141821] p-6">
          <p className="text-[11px] uppercase tracking-[0.3em] text-white/38">Confirmation</p>
          <h3 className="mt-3 text-2xl font-semibold tracking-[-0.04em] text-white">Stop the bot?</h3>
          <p className="mt-3 text-sm leading-6 text-white/55">
            This sends a hard stop command to the backend. Use it when you want the execution loop halted immediately.
          </p>

          <div className="mt-6 flex gap-3">
            <button
              type="button"
              onClick={() => setStopDialogOpen(false)}
              className="flex-1 rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 font-mono text-xs uppercase tracking-[0.22em] text-white/75 transition-colors hover:bg-white/[0.07]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={async () => {
                await sendBotCommand("stop");
                setStopDialogOpen(false);
              }}
              className="flex-1 rounded-2xl border border-[rgba(255,107,107,0.24)] bg-[rgba(255,107,107,0.1)] px-4 py-3 font-mono text-xs uppercase tracking-[0.22em] text-[#ffb3b3] transition-colors hover:bg-[rgba(255,107,107,0.15)]"
            >
              Confirm stop
            </button>
          </div>
        </div>
      </Dialog>
    </>
  );
}
