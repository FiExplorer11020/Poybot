"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDollarSign,
  Info,
  Layers3,
  LoaderCircle,
  Lock,
  RefreshCcw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { apiHeaders, apiUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

type RiskConfig = {
  risk_per_trade_pct: number;
  max_total_exposure_pct: number;
  kelly_fraction: number;
  max_drawdown_stop_pct: number;
  fee_bps: number;
  base_entry_threshold: number;
  spread_cap: number;
};

type WritableRiskConfig = Omit<RiskConfig, "fee_bps">;
type WritableRiskKey = keyof WritableRiskConfig;
type SaveState = "loading" | "saving" | "saved" | "error";
type DangerTone = "warning" | "danger";
type DangerZone = {
  from: number;
  label: string;
  side: "high" | "low";
  tone: DangerTone;
};

type SliderSpec = {
  key: keyof RiskConfig;
  label: string;
  tooltip: string;
  min: number;
  max: number;
  step: number;
  readonly?: boolean;
  valueLabel: (value: number) => string;
  danger?: DangerZone;
};

const ESTIMATED_CAPITAL = 25_000;
const EPSILON = 0.0000001;
const WRITABLE_KEYS: WritableRiskKey[] = [
  "risk_per_trade_pct",
  "max_total_exposure_pct",
  "kelly_fraction",
  "max_drawdown_stop_pct",
  "base_entry_threshold",
  "spread_cap",
];

const DEFAULT_CONFIG: RiskConfig = {
  risk_per_trade_pct: 0.01,
  max_total_exposure_pct: 0.25,
  kelly_fraction: 0.25,
  max_drawdown_stop_pct: 0.1,
  fee_bps: 8,
  base_entry_threshold: 0.005,
  spread_cap: 0.06,
};

const SLIDER_SPECS: SliderSpec[] = [
  {
    key: "risk_per_trade_pct",
    label: "Risk per trade",
    tooltip: "Pourcentage du capital risqué par position. Monter ce seuil augmente vite la taille engagée à chaque entrée.",
    min: 0.001,
    max: 0.05,
    step: 0.001,
    valueLabel: (value) => formatPercent(value, 1),
    danger: { from: 0.03, label: "Agressif", side: "high", tone: "danger" },
  },
  {
    key: "max_total_exposure_pct",
    label: "Max total exposure",
    tooltip: "Part maximale du capital pouvant être engagée simultanément sur le marché.",
    min: 0.05,
    max: 0.8,
    step: 0.01,
    valueLabel: (value) => formatPercent(value, 0),
    danger: { from: 0.6, label: "Exposition lourde", side: "high", tone: "danger" },
  },
  {
    key: "kelly_fraction",
    label: "Kelly fraction",
    tooltip: "Facteur d'agressivité appliqué au sizing Kelly. Au-dessus de 0.8, la stratégie devient nettement plus nerveuse.",
    min: 0.1,
    max: 1,
    step: 0.05,
    valueLabel: (value) => `${value.toFixed(2)}x`,
    danger: { from: 0.8, label: "Agressif", side: "high", tone: "danger" },
  },
  {
    key: "max_drawdown_stop_pct",
    label: "Max drawdown stop",
    tooltip: "Seuil de perte cumulative à partir duquel le bot se met en arrêt automatique.",
    min: 0.02,
    max: 0.25,
    step: 0.005,
    valueLabel: (value) => formatPercent(value, 1),
    danger: { from: 0.18, label: "Tolérance large", side: "high", tone: "danger" },
  },
  {
    key: "fee_bps",
    label: "Fee bps",
    tooltip: "Frais utilisés par le moteur pour ses calculs. Cette valeur est informative et reste en lecture seule.",
    min: 1,
    max: 20,
    step: 1,
    readonly: true,
    valueLabel: (value) => `${value.toFixed(0)} bps`,
  },
  {
    key: "base_entry_threshold",
    label: "Base entry threshold",
    tooltip: "Seuil minimum d'edge attendu avant entrée. Plus la valeur descend, plus le bot devient permissif.",
    min: 0.001,
    max: 0.02,
    step: 0.001,
    valueLabel: (value) => formatPercent(value, 1),
    danger: { from: 0.003, label: "Très permissif", side: "low", tone: "warning" },
  },
  {
    key: "spread_cap",
    label: "Spread cap",
    tooltip: "Spread maximum accepté avant de refuser un trade. Plus il monte, plus la stratégie tolère des marchés moins propres.",
    min: 0.02,
    max: 0.15,
    step: 0.005,
    valueLabel: (value) => formatPercent(value, 1),
    danger: { from: 0.1, label: "Spread large", side: "high", tone: "warning" },
  },
];

export function RiskSliders() {
  const [config, setConfig] = useState<RiskConfig | null>(null);
  const [lastSavedConfig, setLastSavedConfig] = useState<RiskConfig | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("loading");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [loadVersion, setLoadVersion] = useState(0);
  const saveControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function loadConfig() {
      setSaveState("loading");
      setSaveError(null);

      try {
        const response = await fetch(apiUrl("/api/v1/bot/config"), {
          headers: apiHeaders(),
          signal: controller.signal,
          cache: "no-store",
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const payload = await response.json();
        const nextConfig = extractRiskConfig(payload);

        setConfig(nextConfig);
        setLastSavedConfig(nextConfig);
        setSaveState("saved");
      } catch (error) {
        if (controller.signal.aborted) return;

        const message = getErrorMessage(error);
        setSaveState("error");
        setSaveError(message);
        toast.error("Impossible de charger la configuration", {
          description: message,
        });
      }
    }

    void loadConfig();

    return () => {
      controller.abort();
      saveControllerRef.current?.abort();
    };
  }, [loadVersion]);

  useEffect(() => {
    if (!config || !lastSavedConfig) return;

    const patch = buildPatch(config, lastSavedConfig);
    if (Object.keys(patch).length === 0) {
      return;
    }

    setSaveState("saving");
    setSaveError(null);

    const timeout = window.setTimeout(() => {
      void persistPatch(patch);
    }, 800);

    return () => window.clearTimeout(timeout);
  }, [config, lastSavedConfig]);

  async function persistPatch(patch: Partial<WritableRiskConfig>) {
    saveControllerRef.current?.abort();
    const controller = new AbortController();
    saveControllerRef.current = controller;

    try {
      const response = await fetch(apiUrl("/api/v1/bot/config"), {
        method: "PATCH",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(patch),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const payload = await response.json();
      const nextConfig = extractRiskConfig(payload);

      setConfig(nextConfig);
      setLastSavedConfig(nextConfig);
      setSaveState("saved");
    } catch (error) {
      if (controller.signal.aborted) return;

      const message = getErrorMessage(error);
      setSaveState("error");
      setSaveError(message);
      toast.error("Échec de la sauvegarde", {
        description: message,
      });
    }
  }

  const estimatedImpact = useMemo(() => {
    if (!config) return null;

    const maxTrade = Math.min(
      ESTIMATED_CAPITAL * config.max_total_exposure_pct,
      ESTIMATED_CAPITAL * config.risk_per_trade_pct * config.kelly_fraction,
    );

    return {
      maxTrade,
      maxExposure: ESTIMATED_CAPITAL * config.max_total_exposure_pct,
      maxDrawdown: ESTIMATED_CAPITAL * config.max_drawdown_stop_pct,
    };
  }, [config]);

  const activeAlerts = useMemo(() => {
    if (!config) return [];

    return SLIDER_SPECS.flatMap((spec) => {
      const alert = getDangerAlert(spec, config[spec.key]);
      if (!alert) return [];
      return [{ key: spec.key, title: spec.label, label: alert.label, tone: alert.tone }];
    });
  }, [config]);

  const statusMeta = getSaveStatusMeta(saveState, saveError);

  if (!config && saveState === "loading") {
    return (
      <Card className="border-zinc-800/80 bg-zinc-950/50 p-6 shadow-xl">
        <div className="flex items-center gap-3 text-zinc-300">
          <LoaderCircle className="size-5 animate-spin text-cyan-300" />
          <span className="text-sm">Chargement de la configuration de risque...</span>
        </div>
      </Card>
    );
  }

  if (!config) {
    return (
      <Card className="border-rose-500/20 bg-zinc-950/50 p-6 shadow-xl">
        <div className="space-y-4">
          <div className="flex items-center gap-3 text-rose-200">
            <AlertTriangle className="size-5" />
            <p className="text-sm font-medium">La configuration n'a pas pu être chargée.</p>
          </div>
          <p className="text-sm text-zinc-400">
            {saveError ?? "Vérifie que le backend expose bien GET /api/v1/bot/config."}
          </p>
          <Button
            className="border-zinc-700 bg-zinc-900/80 text-zinc-100 hover:border-cyan-400/60 hover:text-cyan-100"
            onClick={() => setLoadVersion((value) => value + 1)}
          >
            Réessayer
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <>
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="overflow-hidden border-zinc-800/80 bg-zinc-950/50 p-0 shadow-[0_18px_60px_rgba(2,8,23,0.45)]">
          <div className="border-b border-zinc-800/80 bg-zinc-900/30 px-5 py-5 sm:px-6">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Badge className="border-cyan-400/30 bg-cyan-400/10 text-cyan-100">Risk controls</Badge>
                  <Badge className="border-emerald-400/20 bg-emerald-400/10 text-emerald-200">
                    Prochain tick, sans redémarrage
                  </Badge>
                </div>
                <div>
                  <h2 className="text-xl font-semibold tracking-tight text-zinc-50">Pilotage du moteur de risque</h2>
                  <p className="mt-1 max-w-2xl text-sm leading-relaxed text-zinc-400">
                    Chaque variation est sauvegardée automatiquement 800 ms après la dernière interaction.
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <div
                  className={cn(
                    "inline-flex items-center gap-2 rounded-full border px-3 py-2 text-sm",
                    statusMeta.className,
                  )}
                >
                  <statusMeta.icon className={cn("size-4", statusMeta.iconClassName)} />
                  <span>{statusMeta.label}</span>
                </div>
                <Button
                  className="border-zinc-700 bg-zinc-900/80 text-zinc-100 hover:border-rose-400/50 hover:text-rose-100"
                  onClick={() => setResetOpen(true)}
                >
                  <RefreshCcw className="mr-2 size-4" />
                  Réinitialiser les défauts
                </Button>
              </div>
            </div>

            {saveError ? (
              <p className="mt-3 text-sm text-rose-300">
                La dernière mise à jour n'a pas été synchronisée. {saveError}
              </p>
            ) : null}
          </div>

          <div className="space-y-5 px-5 py-5 sm:px-6 sm:py-6">
            {SLIDER_SPECS.map((spec) => (
              <RiskField
                key={spec.key}
                spec={spec}
                value={config[spec.key]}
                onChange={(nextValue) => {
                  if (spec.readonly) return;
                  setConfig((current) => (current ? { ...current, [spec.key]: nextValue } : current));
                }}
              />
            ))}
          </div>
        </Card>

        <div className="space-y-4">
          <Card className="border-zinc-800/80 bg-zinc-950/50 p-5 shadow-xl">
            <div className="flex items-center gap-2">
              <Sparkles className="size-4 text-cyan-300" />
              <h3 className="text-sm font-semibold tracking-wide text-zinc-100">Impact estimé</h3>
            </div>

            {estimatedImpact ? (
              <div className="mt-4 space-y-3">
                <ImpactMetric
                  icon={CircleDollarSign}
                  label={`Taille max par trade : ${formatCurrency(estimatedImpact.maxTrade)} (sur capital de ${formatCurrency(ESTIMATED_CAPITAL)})`}
                  tone="cyan"
                  value={Math.min(100, (estimatedImpact.maxTrade / ESTIMATED_CAPITAL) * 100)}
                />
                <ImpactMetric
                  icon={Layers3}
                  label={`Exposition max simultanée : ${formatCurrency(estimatedImpact.maxExposure)}`}
                  tone="emerald"
                  value={config.max_total_exposure_pct * 100}
                />
                <ImpactMetric
                  icon={ShieldAlert}
                  label={`Arrêt automatique si perte > ${formatCurrency(estimatedImpact.maxDrawdown)}`}
                  tone="rose"
                  value={config.max_drawdown_stop_pct * 100}
                />
              </div>
            ) : null}

            <p className="mt-4 text-xs leading-relaxed text-zinc-500">
              Hypothèse de sizing: Kelly pleinement appliqué et cap d'exposition disponible.
            </p>
          </Card>

          <Card className="border-zinc-800/80 bg-zinc-950/50 p-5 shadow-xl">
            <div className="flex items-center gap-2">
              <ShieldCheck className="size-4 text-emerald-300" />
              <h3 className="text-sm font-semibold tracking-wide text-zinc-100">Zones sous surveillance</h3>
            </div>

            <div className="mt-4 space-y-2">
              {activeAlerts.length > 0 ? (
                activeAlerts.map((alert) => (
                  <div
                    key={alert.key}
                    className={cn(
                      "rounded-2xl border px-3 py-3 text-sm",
                      alert.tone === "danger"
                        ? "border-rose-500/30 bg-rose-500/10 text-rose-100"
                        : "border-amber-500/30 bg-amber-500/10 text-amber-100",
                    )}
                  >
                    <span className="font-medium">{alert.title}</span>
                    <span className="text-zinc-300"> · {alert.label}</span>
                  </div>
                ))
              ) : (
                <div className="rounded-2xl border border-emerald-400/20 bg-emerald-400/10 px-3 py-3 text-sm text-emerald-100">
                  Aucun paramètre n'est actuellement dans une zone sensible.
                </div>
              )}
            </div>
          </Card>
        </div>
      </div>

      <Dialog open={resetOpen} onClose={() => setResetOpen(false)}>
        <div className="space-y-4 p-5">
          <div className="flex items-center gap-3">
            <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 p-2 text-rose-200">
              <RefreshCcw className="size-4" />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-zinc-50">Réinitialiser les paramètres</h3>
              <p className="text-sm text-zinc-400">Les valeurs par défaut seront renvoyées au bot automatiquement.</p>
            </div>
          </div>

          <div className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 text-sm text-zinc-300">
            <p>Risk per trade {formatPercent(DEFAULT_CONFIG.risk_per_trade_pct, 1)}</p>
            <p>Max total exposure {formatPercent(DEFAULT_CONFIG.max_total_exposure_pct, 0)}</p>
            <p>Kelly fraction {DEFAULT_CONFIG.kelly_fraction.toFixed(2)}x</p>
            <p>Max drawdown stop {formatPercent(DEFAULT_CONFIG.max_drawdown_stop_pct, 1)}</p>
            <p>Base entry threshold {formatPercent(DEFAULT_CONFIG.base_entry_threshold, 1)}</p>
            <p>Spread cap {formatPercent(DEFAULT_CONFIG.spread_cap, 1)}</p>
          </div>

          <p className="text-sm leading-relaxed text-zinc-500">
            <span className="font-mono text-zinc-300">fee_bps</span> reste informatif ici et n'est pas modifié par la réinitialisation.
          </p>

          <div className="flex flex-wrap justify-end gap-3">
            <Button
              className="border-zinc-700 bg-zinc-900/80 text-zinc-100 hover:border-zinc-500"
              onClick={() => setResetOpen(false)}
            >
              Annuler
            </Button>
            <Button
              className="border-rose-500/40 bg-rose-500/10 text-rose-100 hover:border-rose-400 hover:bg-rose-500/15"
              onClick={() => {
                setConfig((current) => {
                  if (!current) return current;
                  return {
                    ...DEFAULT_CONFIG,
                    fee_bps: current.fee_bps,
                  };
                });
                setResetOpen(false);
              }}
            >
              Confirmer la réinitialisation
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}

function RiskField({
  spec,
  value,
  onChange,
}: {
  spec: SliderSpec;
  value: number;
  onChange: (value: number) => void;
}) {
  const alert = getDangerAlert(spec, value);
  const minLabel = spec.valueLabel(spec.min);
  const maxLabel = spec.valueLabel(spec.max);

  return (
    <section className="rounded-[28px] border border-zinc-800/80 bg-zinc-950/75 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.02)] sm:p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold tracking-wide text-zinc-50">{spec.label}</h3>
            <TooltipChip content={spec.tooltip} />
            {spec.readonly ? (
              <Badge className="border-zinc-700 bg-zinc-900 text-zinc-300">
                <Lock className="mr-1 size-3" />
                Lecture seule
              </Badge>
            ) : null}
            {alert ? <DangerBadge label={alert.label} tone={alert.tone} /> : null}
          </div>
          <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">
            {minLabel} to {maxLabel}
          </p>
        </div>

        <div className="inline-flex min-w-[112px] items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-900/80 px-3 py-2 font-mono text-sm text-zinc-100">
          {spec.valueLabel(value)}
        </div>
      </div>

      <div className="mt-4">
        <CustomSlider
          min={spec.min}
          max={spec.max}
          step={spec.step}
          value={value}
          disabled={spec.readonly}
          danger={spec.danger}
          ariaLabel={spec.label}
          ariaValueText={spec.valueLabel(value)}
          onChange={onChange}
        />
      </div>
    </section>
  );
}

function CustomSlider({
  min,
  max,
  step,
  value,
  disabled,
  danger,
  ariaLabel,
  ariaValueText,
  onChange,
}: {
  min: number;
  max: number;
  step: number;
  value: number;
  disabled?: boolean;
  danger?: DangerZone;
  ariaLabel: string;
  ariaValueText: string;
  onChange: (value: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);

  const progress = ((value - min) / (max - min)) * 100;
  const dangerStart = danger ? ((danger.from - min) / (max - min)) * 100 : null;
  const isDanger = isDangerValue(value, danger);

  useEffect(() => {
    if (!dragging || disabled) return;

    const handleMove = (event: PointerEvent) => {
      updateFromClientX(event.clientX);
    };

    const handleUp = () => {
      setDragging(false);
    };

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);

    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
    };
  }, [disabled, dragging, max, min, step]);

  function updateFromClientX(clientX: number) {
    if (disabled) return;

    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return;

    const ratio = clamp((clientX - rect.left) / rect.width, 0, 1);
    const rawValue = min + ratio * (max - min);
    onChange(snapValue(rawValue, min, max, step));
  }

  return (
    <div className={cn("relative py-3", disabled && "opacity-70")}>
      <div
        ref={trackRef}
        role={disabled ? undefined : "slider"}
        aria-label={disabled ? undefined : ariaLabel}
        aria-valuemin={disabled ? undefined : min}
        aria-valuemax={disabled ? undefined : max}
        aria-valuenow={disabled ? undefined : value}
        aria-valuetext={disabled ? undefined : ariaValueText}
        aria-disabled={disabled ? true : undefined}
        tabIndex={disabled ? -1 : 0}
        className={cn(
          "relative h-8 touch-none select-none outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/40 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950",
          disabled ? "cursor-not-allowed" : "cursor-pointer",
        )}
        onKeyDown={(event) => {
          if (disabled) return;

          let nextValue = value;

          if (event.key === "ArrowRight" || event.key === "ArrowUp") {
            nextValue = snapValue(value + step, min, max, step);
          } else if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
            nextValue = snapValue(value - step, min, max, step);
          } else if (event.key === "Home") {
            nextValue = min;
          } else if (event.key === "End") {
            nextValue = max;
          } else if (event.key === "PageUp") {
            nextValue = snapValue(value + step * 5, min, max, step);
          } else if (event.key === "PageDown") {
            nextValue = snapValue(value - step * 5, min, max, step);
          } else {
            return;
          }

          event.preventDefault();
          onChange(nextValue);
        }}
        onPointerDown={(event) => {
          if (disabled) return;
          setDragging(true);
          updateFromClientX(event.clientX);
        }}
      >
        <div
          className="absolute inset-x-0 top-1/2 h-3 -translate-y-1/2 overflow-hidden rounded-full border border-white/5 shadow-inner"
          style={{ background: getTrackBackground(danger, dangerStart) }}
        />

        {dangerStart !== null ? (
          <div
            className="absolute top-1/2 h-5 w-px -translate-y-1/2 bg-white/20"
            style={{ left: `${dangerStart}%` }}
          />
        ) : null}

        <div
          className={cn(
            "absolute left-0 top-1/2 h-3 -translate-y-1/2 rounded-full transition-[width] duration-200",
            isDanger
              ? "bg-gradient-to-r from-rose-500 via-orange-400 to-amber-300 shadow-[0_0_18px_rgba(251,113,133,0.45)]"
              : "bg-gradient-to-r from-cyan-400 via-emerald-400 to-teal-200 shadow-[0_0_18px_rgba(52,211,153,0.35)]",
          )}
          style={{ width: `${progress}%` }}
        />

        <div
          className={cn(
            "absolute top-1/2 h-5 w-5 -translate-y-1/2 rounded-full border shadow-[0_0_20px_rgba(15,23,42,0.9)] transition-colors",
            isDanger
              ? "border-rose-200 bg-rose-400"
              : "border-cyan-100 bg-cyan-300",
          )}
          style={{ left: `clamp(0px, calc(${progress}% - 10px), calc(100% - 20px))` }}
        />
      </div>
    </div>
  );
}

function ImpactMetric({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof CircleDollarSign;
  label: string;
  value: number;
  tone: "cyan" | "emerald" | "rose";
}) {
  const palette = {
    cyan: {
      panel: "border-cyan-400/20 bg-cyan-400/10",
      icon: "text-cyan-200",
      bar: "bg-gradient-to-r from-cyan-300 to-sky-400",
    },
    emerald: {
      panel: "border-emerald-400/20 bg-emerald-400/10",
      icon: "text-emerald-200",
      bar: "bg-gradient-to-r from-emerald-300 to-green-400",
    },
    rose: {
      panel: "border-rose-400/20 bg-rose-400/10",
      icon: "text-rose-200",
      bar: "bg-gradient-to-r from-rose-300 to-orange-400",
    },
  }[tone];

  return (
    <div className={cn("rounded-3xl border p-4", palette.panel)}>
      <div className="flex items-start gap-3">
        <div className="rounded-2xl bg-black/20 p-2">
          <Icon className={cn("size-4", palette.icon)} />
        </div>
        <p className="text-sm leading-relaxed text-zinc-100">{label}</p>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-black/20">
        <div className={cn("h-full rounded-full", palette.bar)} style={{ width: `${Math.min(100, value)}%` }} />
      </div>
    </div>
  );
}

function DangerBadge({ label, tone }: { label: string; tone: DangerTone }) {
  return (
    <Badge
      className={cn(
        "inline-flex items-center gap-1.5",
        tone === "danger"
          ? "border-rose-500/30 bg-rose-500/10 text-rose-100"
          : "border-amber-500/30 bg-amber-500/10 text-amber-100",
      )}
    >
      <TriangleAlert className="size-3" />
      {label}
    </Badge>
  );
}

function TooltipChip({ content }: { content: string }) {
  return (
    <div className="group relative flex items-center">
      <button
        type="button"
        className="rounded-full border border-zinc-700 bg-zinc-900/80 p-1 text-zinc-400 transition hover:border-cyan-400/40 hover:text-cyan-100"
        aria-label={content}
      >
        <Info className="size-3.5" />
      </button>

      <div className="pointer-events-none absolute left-1/2 top-full z-20 mt-2 hidden w-64 -translate-x-1/2 rounded-2xl border border-zinc-800 bg-zinc-950/95 px-3 py-2 text-xs leading-relaxed text-zinc-300 shadow-2xl group-hover:block group-focus-within:block">
        {content}
      </div>
    </div>
  );
}

function getDangerAlert(spec: SliderSpec, value: number) {
  if (!spec.danger) return null;
  if (!isDangerValue(value, spec.danger)) return null;
  return { label: spec.danger.label, tone: spec.danger.tone };
}

function isDangerValue(value: number, danger?: DangerZone) {
  if (!danger) return false;
  if (danger.side === "high") {
    return value >= danger.from - EPSILON;
  }
  return value <= danger.from + EPSILON;
}

function getTrackBackground(danger?: DangerZone, dangerStart?: number | null) {
  if (!danger || dangerStart === null || dangerStart === undefined) {
    return "linear-gradient(90deg, rgba(14,165,233,0.12) 0%, rgba(24,24,27,0.95) 100%)";
  }

  const safeStart = "rgba(45,212,191,0.14)";
  const safeEnd = "rgba(34,197,94,0.08)";
  const dangerStartColor = "rgba(244,63,94,0.16)";
  const dangerEndColor = "rgba(127,29,29,0.45)";

  if (danger.side === "high") {
    return `linear-gradient(90deg, ${safeStart} 0%, ${safeEnd} ${dangerStart}%, ${dangerStartColor} ${dangerStart}%, ${dangerEndColor} 100%)`;
  }

  return `linear-gradient(90deg, ${dangerEndColor} 0%, ${dangerStartColor} ${dangerStart}%, ${safeStart} ${dangerStart}%, ${safeEnd} 100%)`;
}

function buildPatch(next: RiskConfig, prev: RiskConfig): Partial<WritableRiskConfig> {
  const patch: Partial<WritableRiskConfig> = {};

  for (const key of WRITABLE_KEYS) {
    if (!numbersMatch(next[key], prev[key])) {
      patch[key] = next[key];
    }
  }

  return patch;
}

function extractRiskConfig(payload: unknown): RiskConfig {
  const candidate = isRecord(payload) && "data" in payload ? payload.data : payload;
  return normalizeRiskConfig(candidate);
}

function normalizeRiskConfig(payload: unknown): RiskConfig {
  const source = isRecord(payload) ? payload : {};

  return {
    risk_per_trade_pct: clamp(asNumber(source.risk_per_trade_pct, DEFAULT_CONFIG.risk_per_trade_pct), 0.001, 0.05),
    max_total_exposure_pct: clamp(asNumber(source.max_total_exposure_pct, DEFAULT_CONFIG.max_total_exposure_pct), 0.05, 0.8),
    kelly_fraction: clamp(asNumber(source.kelly_fraction, DEFAULT_CONFIG.kelly_fraction), 0.1, 1),
    max_drawdown_stop_pct: clamp(asNumber(source.max_drawdown_stop_pct, DEFAULT_CONFIG.max_drawdown_stop_pct), 0.02, 0.25),
    fee_bps: clamp(asNumber(source.fee_bps, DEFAULT_CONFIG.fee_bps), 1, 20),
    base_entry_threshold: clamp(asNumber(source.base_entry_threshold, DEFAULT_CONFIG.base_entry_threshold), 0.001, 0.02),
    spread_cap: clamp(asNumber(source.spread_cap, DEFAULT_CONFIG.spread_cap), 0.02, 0.15),
  };
}

function getSaveStatusMeta(state: SaveState, saveError: string | null) {
  if (state === "saving") {
    return {
      label: "Sauvegarde...",
      icon: LoaderCircle,
      className: "border-cyan-400/30 bg-cyan-400/10 text-cyan-100",
      iconClassName: "animate-spin text-cyan-200",
    };
  }

  if (state === "error") {
    return {
      label: "Erreur ✗",
      icon: AlertTriangle,
      className: "border-rose-500/30 bg-rose-500/10 text-rose-100",
      iconClassName: "text-rose-200",
      description: saveError,
    };
  }

  return {
    label: "Sauvegardé ✓",
    icon: CheckCircle2,
    className: "border-emerald-400/30 bg-emerald-400/10 text-emerald-100",
    iconClassName: "text-emerald-200",
  };
}

function formatPercent(value: number, digits: number) {
  return `${(value * 100).toFixed(digits)}%`;
}

function formatCurrency(value: number) {
  const showCents = Math.abs(value) < 1_000;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: showCents ? 2 : 0,
    maximumFractionDigits: showCents ? 2 : 0,
  }).format(value);
}

function snapValue(value: number, min: number, max: number, step: number) {
  const clamped = clamp(value, min, max);
  const stepped = Math.round((clamped - min) / step) * step + min;
  return Number(stepped.toFixed(stepPrecision(step)));
}

function stepPrecision(step: number) {
  const [, decimals = ""] = String(step).split(".");
  return decimals.length;
}

function asNumber(value: unknown, fallback: number) {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function numbersMatch(left: number, right: number) {
  return Math.abs(left - right) <= EPSILON;
}

function getErrorMessage(error: unknown) {
  if (error instanceof Error) {
    return error.message;
  }
  return "Erreur réseau inattendue.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
