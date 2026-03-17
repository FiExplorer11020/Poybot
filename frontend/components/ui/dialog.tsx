"use client";

import { ReactNode, useEffect } from "react";

import { Card } from "@/components/ui/card";

export function Dialog({ open, children, onClose }: { open: boolean; children: ReactNode; onClose?: () => void }) {
  useEffect(() => {
    if (!open || !onClose) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" onClick={onClose}>
      <Card className="relative w-full max-w-[420px] bg-zinc-950" onClick={(e) => e.stopPropagation()}>
        {onClose ? (
          <button
            type="button"
            aria-label="Close"
            className="absolute right-3 top-3 rounded-full border border-zinc-700 p-1 text-zinc-400 hover:text-zinc-100"
            onClick={onClose}
          >
            ✕
          </button>
        ) : null}
        {children}
      </Card>
    </div>
  );
}
