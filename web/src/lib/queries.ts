// Data hooks. Every query is keyed by its API path; the stream says which paths changed and
// only those refetch, so a new agent step re-renders the call tree and nothing else.

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState } from "react";
import type { Source, StreamStatus } from "./source";
import type { AgentRun, ApprovalIn, AuditRow, Change, DiagnosisOut, Evidence, Incident, Plan } from "./types";

export const SourceContext = createContext<Source | null>(null);

export function useSource(): Source {
  const s = useContext(SourceContext);
  if (!s) throw new Error("no data source");
  return s;
}

export const paths = {
  list: "/api/v1/incidents",
  incident: (id: string) => `/api/v1/incidents/${id}`,
  diagnosis: (id: string) => `/api/v1/incidents/${id}/diagnosis`,
  agents: (id: string) => `/api/v1/incidents/${id}/agents`,
  plans: (id: string) => `/api/v1/incidents/${id}/plans`,
  audit: (id: string) => `/api/v1/incidents/${id}/audit`,
  evidence: (id: string) => `/api/v1/evidence/${id}`,
};

function usePath<T>(path: string | null) {
  const source = useSource();
  return useQuery({
    queryKey: [path],
    queryFn: () => source.get<T>(path as string),
    enabled: path !== null,
  });
}

export const useIncidents = () => usePath<Incident[]>(paths.list);
export const useIncident = (id: string) => usePath<Incident>(paths.incident(id));
export const useDiagnosis = (id: string) => usePath<DiagnosisOut>(paths.diagnosis(id));
export const useAgents = (id: string) => usePath<AgentRun[]>(paths.agents(id));
export const usePlans = (id: string) => usePath<Plan[]>(paths.plans(id));
export const useAudit = (id: string) => usePath<AuditRow[]>(paths.audit(id));
export const useEvidence = (id: string | null) => usePath<Evidence>(id && paths.evidence(id));

/** Which queries a change makes stale. */
export function staleFor(c: Change): string[] | "all" {
  if (c.kind === "reset") return "all";
  if (c.kind === "activity") return [paths.agents(c.incident_id), paths.diagnosis(c.incident_id)];
  const id = c.row.subject;
  const out = [paths.list, paths.incident(id), paths.audit(id)];
  const e = c.row.event;
  if (e.startsWith("remediation.") || e.startsWith("execution.") || e.startsWith("verification.")) {
    out.push(paths.plans(id));
  }
  if (e === "incident.transition") out.push(paths.diagnosis(id), paths.plans(id));
  return out;
}

function apply(client: QueryClient, c: Change) {
  const stale = staleFor(c);
  if (stale === "all") {
    void client.invalidateQueries();
    return;
  }
  // cancelRefetch: false lets a refetch already on its way finish instead of restarting it,
  // so a burst of agent steps costs one request per query, not one per step.
  for (const p of stale) void client.invalidateQueries({ queryKey: [p] }, { cancelRefetch: false });
}

/** Follow the source's changes for as long as the page is open. */
export function useLiveUpdates(): StreamStatus {
  const source = useSource();
  const client = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>("connecting");
  useEffect(() => source.subscribe((c) => apply(client, c), setStatus), [source, client]);
  return status;
}

/**
 * Approve or reject the waiting plan. The page shows the decision at once; if the API refuses
 * it (the plan was revised, it expired, someone else decided first), the page goes back and
 * says why.
 */
export function useDecide(incidentId: string) {
  const source = useSource();
  const client = useQueryClient();
  const plansKey = [paths.plans(incidentId)];
  const incidentKey = [paths.incident(incidentId)];
  return useMutation({
    mutationFn: (body: ApprovalIn) => source.decide(incidentId, body),
    onMutate: async (body) => {
      await Promise.all([
        client.cancelQueries({ queryKey: plansKey }),
        client.cancelQueries({ queryKey: incidentKey }),
      ]);
      const before = {
        plans: client.getQueryData<Plan[]>(plansKey),
        incident: client.getQueryData<Incident>(incidentKey),
      };
      const approver = `user:${source.viewer?.subject ?? "you"}`;
      client.setQueryData<Plan[]>(plansKey, (plans) =>
        plans?.map((p) =>
          p.plan_hash === body.plan_hash
            ? {
                ...p,
                approval: { approver, decision: body.decision, comment: body.comment, at: new Date().toISOString() },
              }
            : p,
        ),
      );
      client.setQueryData<Incident>(
        incidentKey,
        (i) => i && { ...i, state: body.decision === "approve" ? "EXECUTING" : "ESCALATED" },
      );
      return before;
    },
    onError: (_err, _body, before) => {
      if (!before) return;
      client.setQueryData(plansKey, before.plans);
      client.setQueryData(incidentKey, before.incident);
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: plansKey });
      void client.invalidateQueries({ queryKey: incidentKey });
    },
  });
}
