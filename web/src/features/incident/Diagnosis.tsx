import { m } from "motion/react";
import { useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { pct } from "@/lib/format";
import type { DiagnosisOut, Incident } from "@/lib/types";
import { duration as d, ease } from "@/styles/motion";
import { EvidenceLink } from "./evidence";

/**
 * Root cause candidates, most likely first. Each names its problem host (where the cause
 * is); the event host is where the alert fired. They differ when a fault in one service
 * shows up as errors in its caller, and the remediation must target the problem host.
 */
export function RootCauses({ data, incident }: { data: DiagnosisOut | undefined; incident: Incident }) {
  const diagnosis = data?.diagnosis;
  const causes = [...(diagnosis?.root_causes ?? [])].sort((a, b) => b.confidence - a.confidence);
  const investigating = !diagnosis && incident.state !== "ESCALATED";

  return (
    <section aria-labelledby="rc-h" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="rc-h" className="text-md font-semibold">
          Root cause candidates
        </h2>
        <span className="text-xs text-muted">
          Event host: <span className="font-mono text-fg-2">{incident.service}</span> — where the alert fired
        </span>
      </div>
      {diagnosis?.summary && <p className="max-w-[72ch] text-sm text-fg-2">{diagnosis.summary}</p>}
      {causes.length > 0 ? (
        <ol className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-3">
          {causes.map((rc, i) => (
            <m.li
              key={rc.service + i}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: d.slow, ease: ease.out, delay: i * 0.05 }}
              className={cn(
                "flex flex-col gap-2.5 rounded-lg border bg-panel p-3.5",
                i === 0 ? "border-accent-line" : "border-line",
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono font-medium">{rc.service}</span>
                <span className="font-mono text-lg font-medium tabular">{pct(rc.confidence)}</span>
              </div>
              <div
                className="h-1 overflow-hidden rounded-xs bg-mark-track"
                role="meter"
                aria-label={`confidence for ${rc.service}`}
                aria-valuenow={Math.round(rc.confidence * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
              >
                <div
                  className={cn(
                    "h-full origin-left rounded-xs transition-transform duration-slow ease-out",
                    i === 0 ? "bg-mark" : "bg-mark-muted",
                  )}
                  style={{ transform: `scaleX(${rc.confidence})` }}
                />
              </div>
              <div className="flex flex-wrap gap-1.5">
                <HostTag>problem host</HostTag>
                {rc.service === incident.service && <HostTag>event host</HostTag>}
              </div>
              <p className="text-sm text-fg-2">{rc.cause}</p>
              <div className="mt-auto flex flex-wrap gap-x-3">
                {rc.evidence_ids.map((id) => (
                  <EvidenceLink key={id} id={id} />
                ))}
              </div>
            </m.li>
          ))}
        </ol>
      ) : investigating ? (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-3" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-44" />
          ))}
          <span className="sr-only">The agents are still investigating.</span>
        </div>
      ) : (
        <p className="text-sm text-muted">No root cause: the investigation ended without one.</p>
      )}
      {diagnosis && <Findings data={data} />}
    </section>
  );
}

function HostTag({ children }: { children: string }) {
  return <span className="rounded-sm border border-line-strong px-1.5 text-2xs text-fg-2">{children}</span>;
}

function Findings({ data }: { data: DiagnosisOut | undefined }) {
  const [open, setOpen] = useState(false);
  const dx = data?.diagnosis;
  if (!dx) return null;
  const groups = [
    { label: "Facts", items: dx.facts },
    { label: "Inferences", items: dx.inferences },
    {
      label: "Open hypotheses",
      items: dx.hypotheses.map((h) => ({ statement: h.statement, verify: h.how_to_verify })),
    },
  ].filter((g) => g.items.length);
  return (
    <div className="rounded-lg border border-line bg-panel">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex min-h-11 w-full items-center gap-3 px-4 text-left text-sm"
      >
        <Chevron open={open} />
        <span className="font-medium">What the agents established</span>
        <span className="text-muted">
          {groups.map((g) => `${g.items.length} ${g.label.toLowerCase()}`).join(" · ")}
        </span>
      </button>
      {open && (
        <m.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: d.base, ease: ease.out }}
          className="flex flex-col gap-4 border-t border-line px-4 py-3"
        >
          {groups.map((g) => (
            <div key={g.label} className="flex flex-col gap-2">
              <h3 className="text-xs text-muted">{g.label}</h3>
              <ul className="flex flex-col gap-2">
                {g.items.map((it, i) => (
                  <li key={i} className="text-sm">
                    {it.statement}
                    {"verify" in it && it.verify && <span className="block text-muted">To verify: {it.verify}</span>}
                    {"evidence_ids" in it && it.evidence_ids && (
                      <span className="ml-1 inline-flex flex-wrap gap-x-2">
                        {it.evidence_ids.map((id) => (
                          <EvidenceLink key={id} id={id} />
                        ))}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </m.div>
      )}
    </div>
  );
}

export function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      aria-hidden="true"
      className={cn("shrink-0 stroke-muted transition-transform duration-base ease-out", open && "rotate-90")}
    >
      <path d="M4.5 2.5l3.5 3.5-3.5 3.5" fill="none" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
