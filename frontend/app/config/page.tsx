import { Activity, ShieldCheck, TimerReset, Waves } from "lucide-react";

import { RiskSliders } from "@/components/trading/RiskSliders";
import { Badge } from "@/components/ui/badge";

const highlights = [
  {
    icon: TimerReset,
    title: "Auto-save 800ms",
    body: "Chaque mouvement de slider est envoyé sans bouton Save et sans casser le flow.",
    tone: "cyan",
  },
  {
    icon: Activity,
    title: "Live next tick",
    body: "Le moteur récupère la nouvelle configuration au prochain cycle, sans redémarrage.",
    tone: "emerald",
  },
  {
    icon: ShieldCheck,
    title: "Guardrails visibles",
    body: "Les zones sensibles sont marquées directement dans les sliders pour éviter les réglages trop agressifs.",
    tone: "rose",
  },
] as const;

export default function ConfigPage() {
  return (
    <div className="space-y-6 animate-in fade-in duration-500">
      <section className="relative overflow-hidden rounded-[32px] border border-zinc-800/80 bg-zinc-950/60 px-5 py-6 shadow-[0_20px_80px_rgba(2,8,23,0.5)] sm:px-6 sm:py-7">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(34,211,238,0.18),transparent_34%),radial-gradient(circle_at_85%_12%,rgba(16,185,129,0.18),transparent_32%),radial-gradient(circle_at_60%_100%,rgba(244,63,94,0.14),transparent_28%)]" />

        <div className="relative grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className="border-cyan-400/30 bg-cyan-400/10 text-cyan-100">Polymarket config</Badge>
              <Badge className="border-zinc-700 bg-zinc-900/80 text-zinc-300">Dark control surface</Badge>
            </div>

            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <div className="rounded-2xl border border-cyan-400/20 bg-cyan-400/10 p-2 text-cyan-200">
                  <Waves className="size-5" />
                </div>
                <p className="text-xs font-semibold uppercase tracking-[0.24em] text-cyan-100/80">
                  Configuration du bot
                </p>
              </div>

              <h1 className="max-w-3xl text-3xl font-semibold tracking-tight text-zinc-50 sm:text-4xl">
                Ajuste le moteur de risque comme une console live, pas comme un formulaire.
              </h1>

              <p className="max-w-2xl text-sm leading-relaxed text-zinc-400 sm:text-base">
                Cette page orchestre le sizing, l'exposition et les garde-fous du bot en temps réel. Les sliders
                mettent en scène l'impact, les seuils dangereux et la synchronisation backend au même endroit.
              </p>
            </div>
          </div>

          <div className="grid gap-3">
            {highlights.map((item) => (
              <div
                key={item.title}
                className={
                  item.tone === "cyan"
                    ? "rounded-[28px] border border-cyan-400/20 bg-cyan-400/10 p-4"
                    : item.tone === "emerald"
                      ? "rounded-[28px] border border-emerald-400/20 bg-emerald-400/10 p-4"
                      : "rounded-[28px] border border-rose-400/20 bg-rose-400/10 p-4"
                }
              >
                <div className="flex items-center gap-3">
                  <div className="rounded-2xl bg-black/20 p-2">
                    <item.icon
                      className={
                        item.tone === "cyan"
                          ? "size-4 text-cyan-200"
                          : item.tone === "emerald"
                            ? "size-4 text-emerald-200"
                            : "size-4 text-rose-200"
                      }
                    />
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-zinc-50">{item.title}</p>
                    <p className="mt-1 text-sm leading-relaxed text-zinc-300">{item.body}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <RiskSliders />
    </div>
  );
}
