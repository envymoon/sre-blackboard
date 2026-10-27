import { useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { duration, time } from "@/lib/format";
import { useNow } from "@/lib/now";
import { useDecide, useSource } from "@/lib/queries";
import type { Check, Incident, Plan, PlanAction } from "@/lib/types";
import { EvidenceLink } from "./evidence";

export function describe(a: PlanAction): string {
  if (a.action === "rollout_undo")
    return `Roll ${a.deployment} back to revision ${a.to_revision ?? "the previous one"}`;
  if (a.action === "restart_deployment") return `Restart ${a.deployment}`;
  if (a.action === "scale_deployment") return `Scale ${a.deployment} to ${a.replicas} replicas`;
  return `${a.action} ${a.deployment}`;
}

function actionLine(a: PlanAction): string {
  if (a.action === "rollout_undo") return `rollout_undo ${a.deployment} → rev ${a.to_revision ?? "prev"}`;
  if (a.action === "scale_deployment") return `scale_deployment ${a.deployment} → ${a.replicas}`;
  return `${a.action} ${a.deployment}`;
}

const VERDICT: Record<Plan["verdict"], { label: string; tone: "warning" | "success" | "danger" }> = {
  require_approval: { label: "Policy: needs an approver", tone: "warning" },
  allow: { label: "Policy: allowed", tone: "success" },
  deny: { label: "Policy: denied", tone: "danger" },
};

/**
 * The plan the policy reviewed, its check-by-check reasons, and the decision. Approve and
 * Reject are bound to the plan's hash, so a revised plan cannot ride on an old approval;
 * the page shows the decision before the API answers and takes it back if the API refuses.
 */
export function ApprovalCard({ plan, incident }: { plan: Plan; incident: Incident }) {
  const source = useSource();
  const decide = useDecide(incident.id);
  const [comment, setComment] = useState("");
  const commentId = useId();
  const now = useNow();
  const waiting =
    incident.state === "AWAITING_APPROVAL" && plan.approval === null && plan.verdict === "require_approval";
  const canDecide = source.viewer?.roles.some((r) => r === "approver" || r === "admin") ?? false;
  const proposedByViewer = plan.proposed_by === `user:${source.viewer?.subject}`;
  const actions = plan.plan.actions;
  const title = actions.length ? actions.map(describe).join(", then ") : "No action";
  const verdict = VERDICT[plan.verdict];
  const expires = plan.expires_at ? Date.parse(plan.expires_at) : null;
  const left = expires !== null ? expires - now : null;
  const send = (decision: "approve" | "reject") =>
    decide.mutate({ plan_hash: plan.plan_hash, decision, comment: comment.trim() });

  return (
    <section
      aria-labelledby="plan-h"
      className={cn("overflow-hidden rounded-xl border bg-panel", waiting ? "border-warning-line" : "border-line")}
    >
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-5 py-4">
        <div className="flex min-w-0 flex-[1_1_360px] flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="plan-h" className="text-md font-semibold">
              {title}
            </h2>
            <Badge tone={verdict.tone}>{verdict.label}</Badge>
          </div>
          <p className="text-sm text-muted">{plan.plan.summary}</p>
        </div>
        <Decision plan={plan} pending={decide.isPending} />
      </div>

      {waiting && (
        <div className="flex flex-wrap items-end gap-3 border-b border-line bg-warning-soft/40 px-5 py-3">
          {canDecide && !proposedByViewer ? (
            <>
              <label htmlFor={commentId} className="flex min-w-0 flex-[1_1_240px] flex-col gap-1 text-xs text-muted">
                Comment for the audit trail (optional)
                <input
                  id={commentId}
                  value={comment}
                  maxLength={500}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="e.g. rollback matches the diff"
                  className="h-9 rounded-md border border-line-strong bg-panel px-3 text-sm text-fg placeholder:text-faint"
                />
              </label>
              <div className="flex gap-2">
                <Button variant="secondary" disabled={decide.isPending} onClick={() => send("reject")}>
                  Reject
                </Button>
                <Button variant="primary" disabled={decide.isPending} onClick={() => send("approve")}>
                  Approve plan <span className="font-mono">{plan.plan_hash.slice(0, 8)}</span>
                </Button>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted">
              {proposedByViewer
                ? "You proposed this plan, so someone else has to approve it."
                : "Waiting for an approver. Your token has no approver role."}
            </p>
          )}
        </div>
      )}
      {decide.isError && (
        <p role="alert" className="border-b border-danger-line bg-danger-soft px-5 py-2 text-sm text-danger">
          Not recorded: {decide.error.message}
        </p>
      )}

      <div className="flex flex-wrap">
        <div className="flex min-w-0 flex-[1_1_300px] flex-col gap-4 border-line px-5 py-4 md:border-r">
          <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-y-1.5 text-sm">
            {actions.map((a, i) => (
              <Row key={i} label={actions.length > 1 ? `Action ${i + 1}` : "Action"}>
                <span className="font-mono">{actionLine(a)}</span>
              </Row>
            ))}
            <Row label="Risk">
              <span className="font-semibold text-critical">{plan.risk}</span>
              <span className="text-muted">
                {" "}
                · {plan.verdict === "require_approval" ? "needs an approver" : plan.verdict}
              </span>
            </Row>
            <Row label="Proposed by">
              <span className="font-mono">{plan.proposed_by}</span>
            </Row>
            <Row label="Plan hash">
              <span className="font-mono break-all" title={plan.plan_hash}>
                {plan.plan_hash.slice(0, 12)}…{plan.plan_hash.slice(-4)}
              </span>
            </Row>
            {expires !== null && (
              <Row label="Expires">
                {time(plan.expires_at, false)} UTC
                {waiting && left !== null && (
                  <span className="text-muted"> · {left > 0 ? `${duration(left)} left` : "expired"}</span>
                )}
              </Row>
            )}
          </dl>
          {actions.some((a) => a.evidence_ids.length) && (
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted">Evidence cited by the plan</span>
              <div className="flex flex-wrap gap-x-3">
                {[...new Set(actions.flatMap((a) => a.evidence_ids))].map((id) => (
                  <EvidenceLink key={id} id={id} />
                ))}
              </div>
            </div>
          )}
          {plan.plan.alternatives.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-xs text-muted">Not chosen</span>
              {plan.plan.alternatives.map((alt) => (
                <p key={alt.action} className="text-sm">
                  <span className="font-mono text-fg-2">{alt.action}</span>
                  <span className="text-muted"> — {alt.why_not}</span>
                </p>
              ))}
            </div>
          )}
        </div>
        <div className="flex min-w-0 flex-[1_1_300px] flex-col gap-1 px-5 py-3">
          <span className="pt-1 text-xs text-muted">Policy checks</span>
          <ul className="flex flex-col">
            {plan.checks.map((c, i) => (
              <CheckRow key={i} check={c} />
            ))}
          </ul>
        </div>
      </div>
      {plan.executions.length > 0 && (
        <div className="flex flex-col gap-1 border-t border-line px-5 py-3">
          <span className="text-xs text-muted">Execution</span>
          {plan.executions.map((x) => (
            <p key={x.action_index} className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={x.status === "succeeded" ? "success" : x.status === "running" ? "accent" : "danger"}>
                {x.status}
              </Badge>
              <span className="font-mono text-xs">{actionLine(actions[x.action_index] ?? actions[0]!)}</span>
              <span className="font-mono text-xs text-muted">
                rev {String(x.before?.revision ?? "?")} → {String(x.after?.revision ?? "…")}
                {x.finished_at && ` · ${duration(Date.parse(x.finished_at) - Date.parse(x.started_at))}`}
              </span>
              <span className="font-mono text-xs text-faint">{x.executor}</span>
              {x.error && <span className="text-danger">{x.error}</span>}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </>
  );
}

function CheckRow({ check }: { check: Check }) {
  return (
    <li className="flex items-start gap-2.5 py-1.5">
      <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" className="mt-0.5 shrink-0">
        {check.ok ? (
          <path
            d="M3.5 8.5l3 3 6-7"
            fill="none"
            className="stroke-success"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ) : (
          <path d="M4 4l8 8M12 4l-8 8" fill="none" className="stroke-danger" strokeWidth="1.6" strokeLinecap="round" />
        )}
      </svg>
      <span className="flex min-w-0 flex-col">
        <span className="font-mono text-xs text-fg-2">
          <span className="sr-only">{check.ok ? "passed: " : "failed: "}</span>
          {check.name}
          {!check.blocking && <span className="text-faint"> · advisory</span>}
        </span>
        <span className="text-sm text-muted">{check.detail}</span>
      </span>
    </li>
  );
}

function Decision({ plan, pending }: { plan: Plan; pending: boolean }) {
  const a = plan.approval;
  if (!a) return null;
  const approved = a.decision === "approve";
  return (
    <div className="flex flex-col items-end gap-1 text-right" aria-live="polite">
      <Badge tone={approved ? "success" : "danger"}>{pending ? "Sending…" : approved ? "Approved" : "Rejected"}</Badge>
      <span className="text-xs text-muted">
        <span className="font-mono">{a.approver}</span> · {time(a.at)}
      </span>
      {a.comment && <span className="max-w-72 text-xs text-fg-2">“{a.comment}”</span>}
    </div>
  );
}
