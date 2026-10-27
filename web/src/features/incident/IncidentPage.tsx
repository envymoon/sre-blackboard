import { useCallback, useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ago, PRIORITY_TONE, pct, shortId, STATE_LABEL, STATE_TONE, time } from "@/lib/format";
import { useNow } from "@/lib/now";
import { ApiError } from "@/lib/source";
import { useAgents, useAudit, useDiagnosis, useEvidence, useIncident, usePlans } from "@/lib/queries";
import type { AgentRun, DiagnosisOut, Incident, Plan } from "@/lib/types";
import { AgentTree } from "./AgentTree";
import { ApprovalCard } from "./ApprovalCard";
import { RootCauses } from "./Diagnosis";
import { EvidenceContext } from "./evidence";
import { EvidenceDrawer, type Citation } from "./EvidenceDrawer";
import { StateProgress } from "./StateProgress";
import { Timeline } from "./Timeline";

export default function IncidentPage({ id: fixedId, backTo = "/incidents" }: { id?: string; backTo?: string | null }) {
  const params = useParams();
  const id = fixedId ?? params.id ?? "";
  const [search, setSearch] = useSearchParams();
  const evidenceId = search.get("ev");
  const incident = useIncident(id);
  const diagnosis = useDiagnosis(id);
  const agents = useAgents(id);
  const plans = usePlans(id);
  const audit = useAudit(id);

  // The drawer's evidence lives in the URL, so a link can open the page with it showing.
  const openEvidence = useCallback(
    (ev: string) =>
      setSearch((s) => {
        s.set("ev", ev);
        return s;
      }),
    [setSearch],
  );
  const closeEvidence = useCallback(
    () =>
      setSearch((s) => {
        s.delete("ev");
        return s;
      }),
    [setSearch],
  );
  const citedBy = useMemo(() => citations(diagnosis.data, plans.data), [diagnosis.data, plans.data]);
  const fetchedBy = useMemo(() => fetchers(agents.data), [agents.data]);

  if (incident.isError) {
    const notFound = incident.error instanceof ApiError && incident.error.status === 404;
    return (
      <div className="mx-auto flex max-w-[1100px] flex-col gap-2 px-4 py-10 sm:px-8">
        <h1 className="text-lg font-semibold">{notFound ? "No such incident" : "Could not load the incident"}</h1>
        <p className="text-sm text-muted">
          {notFound ? `Nothing with id ${id} in your tenant.` : incident.error.message}
        </p>
      </div>
    );
  }
  const inc = incident.data;
  const plan = plans.data?.at(-1);

  return (
    <EvidenceContext.Provider value={openEvidence}>
      <article className="mx-auto flex w-full max-w-[1100px] flex-col gap-6 px-4 pt-5 pb-12 sm:px-8 sm:pt-6">
        {backTo !== null && (
          <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-sm text-muted">
            <Link to={backTo} className="text-muted hover:text-fg">
              Incidents
            </Link>
            <span aria-hidden="true">/</span>
            <span className="font-mono text-fg-2">{shortId(id)}</span>
          </nav>
        )}
        {inc && audit.data && agents.data && diagnosis.data && plans.data ? (
          <>
            <Header incident={inc} agents={agents.data} />
            <StateProgress state={inc.state} audit={audit.data} createdAt={inc.created_at} />
            {plan && <ApprovalCard plan={plan} incident={inc} />}
            <RootCauses data={diagnosis.data} incident={inc} />
            <AgentTree runs={agents.data} />
            <Timeline audit={audit.data} incident={inc} />
          </>
        ) : (
          // One skeleton for the whole page, replaced at once when every piece has arrived:
          // sections appearing one by one would push each other down.
          <PageSkeleton />
        )}
      </article>
      <EvidenceDrawer id={evidenceId} onClose={closeEvidence} citedBy={citedBy} fetchedBy={fetchedBy} />
    </EvidenceContext.Provider>
  );
}

function Header({ incident, agents }: { incident: Incident; agents: AgentRun[] | undefined }) {
  const now = useNow();
  const l = incident.labels;
  const facts = [l.slo && `${l.slo} SLO`, l.burn && `${l.burn} burn`, l.severity].filter(Boolean).join(" · ");
  return (
    <header className="flex flex-wrap items-end justify-between gap-6">
      <div className="flex min-w-0 flex-col gap-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={PRIORITY_TONE[incident.priority]} className="font-semibold">
            {incident.priority}
          </Badge>
          <Badge tone={STATE_TONE[incident.state]}>{STATE_LABEL[incident.state]}</Badge>
          <span className="text-xs text-muted">{incident.environment}</span>
        </div>
        <h1 className="text-xl font-semibold tracking-[-0.02em]">
          {incident.title} on {incident.service}
        </h1>
        <p className="text-muted">
          {facts && `${facts} · `}opened {time(incident.created_at)} UTC · {ago(incident.created_at, now)}
          {incident.alert_count > 1 && ` · ${incident.alert_count} alerts`}
        </p>
      </div>
      <Sli incident={incident} agents={agents} />
    </header>
  );
}

/** The SLI the alert fired on (error ratio or p95 latency), as the metrics investigator first fetched it. */
function Sli({ incident, agents }: { incident: Incident; agents: AgentRun[] | undefined }) {
  const isRatio = incident.labels.slo !== "latency";
  const metric = isRatio ? "sli_error_ratio" : "sli_latency_p95_ms";
  const call = agents
    ?.flatMap((r) => r.llm_calls.flatMap((s) => s.tools))
    .find(
      (t) =>
        t.tool === "prom_range" &&
        t.evidence_id &&
        typeof t.args.query === "string" &&
        t.args.query.includes(metric) &&
        t.args.query.includes(`"${incident.service}"`),
    );
  const ev = useEvidence(call?.evidence_id ?? null);
  const series = (ev.data?.summary as { series?: { shape: number[]; last: number; max: number }[] } | undefined)
    ?.series?.[0];
  const box = "flex h-[66px] w-[284px] items-center gap-3.5 rounded-lg border border-line bg-panel px-3.5";
  if (!series) {
    return (
      <div className={box} aria-hidden={!call}>
        {call ? (
          <Skeleton className="h-10 w-full" />
        ) : (
          <span className="text-xs text-faint">SLI appears once the metrics agent reads it</span>
        )}
      </div>
    );
  }
  const shape = series.shape;
  const max = Math.max(...shape, 1e-9);
  const points = shape
    .map((v, i) => `${((i * 180) / Math.max(1, shape.length - 1)).toFixed(1)},${(38 - (v / max) * 34).toFixed(1)}`)
    .join(" ");
  const value = isRatio ? pct(series.last) : `${series.last.toFixed(0)} ms`;
  return (
    <figure className={box}>
      <svg
        width="150"
        height="40"
        viewBox="0 0 180 40"
        preserveAspectRatio="none"
        role="img"
        aria-label={`${incident.service} SLI over the last hour, now ${value}, peak ${isRatio ? pct(series.max) : series.max}`}
      >
        <polyline
          points={points}
          fill="none"
          className="stroke-mark-danger"
          strokeWidth="1.5"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <figcaption className="flex flex-col text-xs text-muted">
        <span className="font-mono text-md text-fg tabular">{value}</span>
        <span>{isRatio ? "error ratio · 5m" : "p95 latency"}</span>
      </figcaption>
    </figure>
  );
}

function PageSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading the incident">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="flex flex-col gap-2.5">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-8 w-80 max-w-full" />
          <Skeleton className="h-5 w-96 max-w-full" />
        </div>
        <Skeleton className="h-[66px] w-[284px]" />
      </div>
      <Skeleton className="h-11" />
      <Skeleton className="h-52" />
      <Skeleton className="h-72" />
    </div>
  );
}

function clip(s: string, n = 72): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function citations(dx: DiagnosisOut | undefined, plans: Plan[] | undefined): (id: string) => Citation[] {
  return (id) => {
    const out: Citation[] = [];
    const d = dx?.diagnosis;
    d?.root_causes
      .slice()
      .sort((a, b) => b.confidence - a.confidence)
      .forEach(
        (rc, i) =>
          rc.evidence_ids.includes(id) &&
          out.push({ label: `Root cause #${i + 1} · ${rc.service} ${pct(rc.confidence)}` }),
      );
    for (const p of plans ?? []) {
      p.plan.actions.forEach(
        (a) => a.evidence_ids.includes(id) && out.push({ label: `Plan #${p.seq} · ${a.action} ${a.deployment}` }),
      );
    }
    d?.facts.forEach((f) => f.evidence_ids?.includes(id) && out.push({ label: `Fact · “${clip(f.statement)}”` }));
    d?.inferences.forEach(
      (f) => f.evidence_ids?.includes(id) && out.push({ label: `Inference · “${clip(f.statement)}”` }),
    );
    return out;
  };
}

function fetchers(runs: AgentRun[] | undefined): (id: string) => string | null {
  const by = new Map<string, string>();
  for (const r of runs ?? [])
    for (const s of r.llm_calls) for (const t of s.tools) if (t.evidence_id) by.set(t.evidence_id, r.agent);
  return (id) => by.get(id) ?? null;
}
