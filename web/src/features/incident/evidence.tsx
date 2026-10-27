import { createContext, useContext, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/** Opens the evidence drawer from anywhere on the incident page. */
export const EvidenceContext = createContext<(id: string) => void>(() => {});

export function EvidenceLink({ id, className, children }: { id: string; className?: string; children?: ReactNode }) {
  const open = useContext(EvidenceContext);
  return (
    <button
      type="button"
      onClick={() => open(id)}
      className={cn(
        "inline-flex min-h-6 items-center rounded-xs font-mono text-xs text-accent-text underline-offset-2 hover:underline",
        className,
      )}
    >
      {children ?? id}
    </button>
  );
}
