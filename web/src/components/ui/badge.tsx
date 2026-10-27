import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";
import type { Tone } from "@/lib/format";

const tones: Record<Tone, string> = {
  neutral: "text-fg-2 border-line-strong bg-transparent",
  accent: "text-accent-text border-accent-line bg-accent-soft",
  success: "text-success border-success-line bg-success-soft",
  warning: "text-warning border-warning-line bg-warning-soft",
  danger: "text-danger border-danger-line bg-danger-soft",
  critical: "text-critical border-critical-line bg-critical-soft",
};

export function Badge({ tone = "neutral", className, ...props }: ComponentProps<"span"> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-sm border px-1.5 text-xs font-medium whitespace-nowrap",
        tones[tone],
        className,
      )}
      {...props}
    />
  );
}
