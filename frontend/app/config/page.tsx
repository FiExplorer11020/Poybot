import { RiskSliders } from "@/components/trading/RiskSliders";

export default function ConfigPage() {
  return (
    <div className="max-w-[760px]">
      <h2 className="mb-3 text-xl font-semibold tracking-wide">BOT CONFIG</h2>
      <RiskSliders />
    </div>
  );
}
