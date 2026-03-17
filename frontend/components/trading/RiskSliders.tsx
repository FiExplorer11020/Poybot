"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";

export function RiskSliders() {
  const [riskManaged, setRiskManaged] = useState(true);
  const [kellyOnSum, setKellyOnSum] = useState(true);
  const [riskPerTrade, setRiskPerTrade] = useState(1.2);
  const [maxExposure, setMaxExposure] = useState(35);
  const [kellyFraction, setKellyFraction] = useState(0.6);
  const [maxDrawdown, setMaxDrawdown] = useState(8);

  const preview = useMemo(() => {
    const projected = maxExposure + riskPerTrade * kellyFraction * 2;
    return { projected, safe: projected < 75 };
  }, [maxExposure, riskPerTrade, kellyFraction]);

  return (
    <Card className="space-y-4">
      <h3 className="font-sans text-lg">Arbitrage Risk Engine</h3>

      <label className="flex items-center justify-between text-sm">Risk-Managed Sizing <Switch checked={riskManaged} onChange={(e) => setRiskManaged(e.target.checked)} /></label>
      <label className="flex items-center justify-between text-sm">Use Kelly on position sums <Switch checked={kellyOnSum} onChange={(e) => setKellyOnSum(e.target.checked)} /></label>

      <Range label={`Risk per trade (${riskPerTrade.toFixed(1)}%)`} min={0.1} max={5} step={0.1} value={riskPerTrade} onChange={setRiskPerTrade} />
      <Range label={`Max simultaneous exposure (${maxExposure.toFixed(0)}%)`} min={1} max={100} step={1} value={maxExposure} onChange={setMaxExposure} />
      <Range label={`Kelly Fraction (${kellyFraction.toFixed(2)})`} min={0.1} max={1} step={0.01} value={kellyFraction} onChange={setKellyFraction} />
      <Range label={`Max Drawdown Auto-Stop (${maxDrawdown.toFixed(0)}%)`} min={3} max={20} step={1} value={maxDrawdown} onChange={setMaxDrawdown} />

      <Card className={`text-sm ${preview.safe ? "border-emerald-400/40" : "border-amber-500/40"}`}>
        If take this arb → new total exposure = {preview.projected.toFixed(2)}% ({preview.safe ? "safe" : "unsafe"})
      </Card>

      <Button className="w-full border-emerald-400/50 text-emerald-300 neon-glow">Save</Button>
    </Card>
  );
}

function Range({ label, min, max, step, value, onChange }: { label: string; min: number; max: number; step: number; value: number; onChange: (v: number) => void }) {
  return (
    <div className="space-y-2">
      <p className="text-xs text-zinc-300">{label}</p>
      <Slider min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.currentTarget.value))} />
    </div>
  );
}
