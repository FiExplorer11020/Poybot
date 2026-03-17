import { ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function Button({ className, type, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type={type ?? "button"}
      className={cn(
        "rounded-3xl border border-emerald-400/25 bg-zinc-900 px-4 py-2 text-sm text-zinc-100 transition hover:border-emerald-400/60 hover:shadow-neon disabled:cursor-not-allowed disabled:opacity-60",
        className
      )}
      {...props}
    />
  );
}
