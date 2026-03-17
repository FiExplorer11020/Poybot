import { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function Badge({ className, ...props }: HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn("rounded-full border border-zinc-700 px-3 py-1 text-xs font-medium", className)} {...props} />;
}
