import { AnimatePresence, m } from "motion/react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { STATE_LABEL, time } from "@/lib/format";
import type { AuditRow, Incident, State } from "@/lib/types";
import { duration as d, ease } from "@/styles/motion";

interface Entry {
  key: string;
  ts: string;
  what: string;
  actor: string;
  detail: string;
  tone: "accent" | "success" | "warning" | "danger";
}

function entry(e: AuditRow): Entry {
  const p = e.payload;
  const base = { key: String(e.seq), ts: e.ts, actor: e.actor };
  switch (e.event) {
    case "incident.created":
      return { ...base, what: "Alert received", detail: String(p.title ?? p.alertname ?? ""), tone: "accent" };
    case "incident.transition": {
      const to = p.to as State;
      const tone =
        to === "ESCALATED"
          ? "danger"
          : to === "RESOLVED"
            ? "success"
            : to === "AWAITING_APPROVAL"
              ? "warning"
              : "accent";
      return { ...base, what: STATE_LABEL[to] ?? to, detail: String(p.reason ?? ""), tone };
    }
    case "remediation.plan": {
      const actions = (p.actions as { action: string; deployment: string }[] | undefined) ?? [];
      return {
        ...base,
        what: "Plan proposed",
        detail: actions.map((a) => `${a.action} ${a.deployment}`).join(", ") || "no action",
        tone: "accent",
      };
    }
    case "remediation.approved":
    case "remediation.rejected":
      return {
        ...base,
        what: e.event.endsWith("approved") ? "Approved" : "Rejected",
        detail: p.comment ? `“${p.comment}”` : `plan ${String(p.plan_hash).slice(0, 12)}`,
        tone: e.event.endsWith("approved") ? "success" : "danger",
      };
    default: {
      const [kind, outcome] = e.event.split(".");
      const what =
        `${kind === "verification" ? "SLO check" : kind === "execution" ? "Execution" : kind} ${outcome ?? ""}`.trim();
      const failed = outcome === "failed";
      return {
        ...base,
        what,
        detail: String(p.reason ?? p.error ?? ""),
        tone: failed ? "danger" : outcome === "started" ? "accent" : "success",
      };
    }
  }
}

const DOT = { accent: "bg-mark", success: "bg-success", warning: "bg-warning", danger: "bg-mark-danger" };

/** The incident's audit trail, as it happens. */
export function Timeline({ audit, incident }: { audit: AuditRow[] | undefined; incident: Incident }) {
  const entries = (audit ?? []).map(entry);
  if (audit && !audit.some((e) => e.event === "incident.created")) {
    entries.unshift({
      key: "created",
      ts: incident.created_at,
      what: "Alert received",
      actor: "alertmanager",
      detail: `${incident.title} on ${incident.service}`,
      tone: "accent",
    });
  }
  return (
    <section aria-labelledby="tl-h" className="flex flex-col gap-2.5">
      <h2 id="tl-h" className="text-md font-semibold">
        Timeline
      </h2>
      {audit === undefined ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      ) : (
        <ol className="flex flex-col">
          <AnimatePresence initial={false}>
            {entries.map((e) => (
              <m.li
                key={e.key}
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: d.slow, ease: ease.out }}
                className="grid grid-cols-[64px_12px_minmax(0,1fr)] items-baseline gap-3 py-1.5"
              >
                <time dateTime={e.ts} className="font-mono text-xs text-muted tabular">
                  {time(e.ts)}
                </time>
                <span className={cn("size-[7px] self-center rounded-full", DOT[e.tone])} aria-hidden="true" />
                <span className="flex min-w-0 flex-col">
                  <span className="text-sm">
                    <span className="font-medium">{e.what}</span>{" "}
                    <span className="font-mono text-xs text-faint">{e.actor}</span>
                  </span>
                  {e.detail && <span className="text-sm break-words text-muted">{e.detail}</span>}
                </span>
              </m.li>
            ))}
          </AnimatePresence>
        </ol>
      )}
    </section>
  );
}
