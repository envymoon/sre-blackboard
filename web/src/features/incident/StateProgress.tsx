import { STATE_SHORT, STATES, time } from "@/lib/format";
import type { AuditRow, State } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * The state machine as a bar of ten steps. A step fills with a scaleX transition when the
 * incident reaches it; the current one is coloured by its meaning (waiting = warning).
 * An escalated incident shows where it stopped.
 */
export function StateProgress({ state, audit, createdAt }: { state: State; audit: AuditRow[]; createdAt: string }) {
  const reached = new Map<string, string>();
  let escalatedFrom: State | null = null;
  for (const e of audit) {
    if (e.event !== "incident.transition") continue;
    const to = e.payload.to as State;
    if (!reached.has(to)) reached.set(to, e.ts);
    if (to === "ESCALATED") escalatedFrom = e.payload.from as State;
  }
  const escalated = state === "ESCALATED";
  const at = STATES.indexOf(escalated ? (escalatedFrom ?? "RECEIVED") : state);

  return (
    <section aria-label="State" className="flex flex-col gap-2">
      <ol className="grid grid-cols-5 gap-x-1 gap-y-3 sm:grid-cols-10">
        {STATES.map((s, i) => {
          const done = i < at || (i === at && s === "RESOLVED");
          const current = i === at && s !== "RESOLVED";
          const fill = done
            ? "bg-success"
            : current
              ? escalated
                ? "bg-mark-danger"
                : s === "AWAITING_APPROVAL"
                  ? "bg-warning"
                  : "bg-mark"
              : "";
          return (
            <li key={s} className="flex min-w-0 flex-col gap-2" aria-current={current ? "step" : undefined}>
              <div className="relative h-1 overflow-hidden rounded-xs bg-mark-track">
                <div
                  className={cn(
                    "absolute inset-0 origin-left rounded-xs transition-transform duration-slow ease-out",
                    fill,
                    done || current ? "scale-x-100" : "scale-x-0",
                  )}
                />
              </div>
              <span className="flex min-w-0 flex-col">
                <span
                  className={cn("truncate text-xs", i <= at ? "text-fg" : "text-faint", current && "font-semibold")}
                >
                  {current && escalated ? `${STATE_SHORT[s]} · escalated` : STATE_SHORT[s]}
                </span>
                <span className="font-mono text-2xs text-faint tabular">
                  {time(s === "RECEIVED" ? createdAt : (reached.get(s) ?? null))}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
