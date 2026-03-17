import { ReactNode } from "react";

import { Card } from "@/components/ui/card";

export function Dialog({ open, children }: { open: boolean; children: ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <Card className="w-[380px] bg-zinc-950">{children}</Card>
    </div>
  );
}
