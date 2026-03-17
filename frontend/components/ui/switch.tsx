import { InputHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function Switch({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input type="checkbox" className={cn("h-4 w-4 accent-emerald-400", className)} {...props} />;
}
