import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Props = {
  market: string;
  bid: number;
  ask: number;
  spread: number;
  implied: number;
  profit: number;
  exposure: number;
  detected: boolean;
  risk: "normal" | "high";
};

export function DetectionCard(props: Props) {
  return (
    <Card className={`space-y-2 ${props.risk === "high" ? "border-amber-500/40" : ""}`}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm text-zinc-100">{props.market}</p>
        <Badge className={props.detected ? "border-emerald-400/60 text-emerald-300 neon-glow" : "border-amber-500/50 text-amber-300"}>
          {props.detected ? "DETECTED" : "WATCH"}
        </Badge>
      </div>
      <div className="grid grid-cols-2 gap-2 font-mono text-xs text-zinc-300">
        <div>Bid: {props.bid.toFixed(3)}</div>
        <div>Ask: {props.ask.toFixed(3)}</div>
        <div>Spread: {props.spread.toFixed(3)}</div>
        <div>Implied: {(props.implied * 100).toFixed(2)}%</div>
        <div>Risk Adj. Profit: {props.profit.toFixed(2)}%</div>
        <div>Exposure: {props.exposure.toFixed(2)}%</div>
      </div>
      <Button className="w-full border-emerald-400/40">Simulate Entry</Button>
    </Card>
  );
}
