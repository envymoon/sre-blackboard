import { m } from "motion/react";
import { Fragment, useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { duration, time, tokens, usd } from "@/lib/format";
import type { AgentRun, LlmStep, ToolCall } from "@/lib/types";
import { duration as d, ease } from "@/styles/motion";
import { Chevron } from "./Diagnosis";
import { EvidenceLink } from "./evidence";

/**
 * Every agent's run: its model steps (time, tokens, what it reasoned) and, under each step,
 * the tools it called with the evidence each one produced. A running agent shows a live dot
 * and grows as the stream reports its steps.
 */
export function AgentTree({ runs }: { runs: AgentRun[] | undefined }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (agent: string) =>
    setOpen((s) => {
      const next = new Set(s);
      if (!next.delete(agent)) next.add(agent);
      return next;
    });
  const total = (runs ?? []).reduce(
    (t, r) => ({
      calls: t.calls + r.usage.calls,
      tokens: t.tokens + r.usage.prompt_tokens + r.usage.output_tokens,
      cost: t.cost + r.usage.cost_usd,
    }),
    { calls: 0, tokens: 0, cost: 0 },
  );

  return (
    <section aria-labelledby="agents-h" className="rounded-xl border border-line bg-panel">
      <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-line px-5 py-3.5">
        <h2 id="agents-h" className="text-md font-semibold">
          Agents
        </h2>
        <span className="font-mono text-xs text-muted tabular">
          {total.calls} model calls · {tokens(total.tokens)} tokens · {usd(total.cost)}
        </span>
      </div>
      {runs === undefined ? (
        <div className="flex flex-col gap-2 p-5" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-7" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <p className="px-5 py-4 text-sm text-muted">No agent has started yet.</p>
      ) : (
        <ul>
          {runs.map((run) => (
            <AgentRow key={run.agent} run={run} open={open.has(run.agent)} onToggle={() => toggle(run.agent)} />
          ))}
        </ul>
      )}
    </section>
  );
}

function AgentRow({ run, open, onToggle }: { run: AgentRun; open: boolean; onToggle: () => void }) {
  const running = run.status === "running";
  const n = run.llm_calls.length;
  return (
    <li className="border-b border-line-soft last:border-b-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="grid min-h-11 w-full grid-cols-[12px_minmax(0,1fr)_auto] items-center gap-3 px-5 py-2 text-left hover:bg-raised/50 sm:grid-cols-[12px_minmax(0,1fr)_64px_72px_64px_64px]"
      >
        <Chevron open={open} />
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-sm">{run.agent}</span>
          {running && (
            <span className="inline-flex items-center gap-1.5 text-2xs text-accent-text">
              <span className="live-dot size-1.5 rounded-full bg-mark" />
              running
            </span>
          )}
          {run.status === "failed" && <span className="text-2xs text-danger">failed</span>}
        </span>
        <span className="text-xs text-muted sm:text-left">
          {n} step{n === 1 ? "" : "s"}
        </span>
        <Num className="hidden sm:block">{duration(run.usage.model_time_ms)}</Num>
        <Num className="hidden sm:block">{tokens(run.usage.prompt_tokens + run.usage.output_tokens)}</Num>
        <Num className="hidden sm:block">{usd(run.usage.cost_usd)}</Num>
      </button>
      {open && (
        <m.ol
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: d.base, ease: ease.out }}
          className="flex flex-col gap-3 pr-5 pb-4 pl-5 sm:pl-12"
        >
          <li className="font-mono text-2xs text-faint">
            {run.model} · {run.prompt_version}
          </li>
          {run.llm_calls.map((s, i) => {
            // Step numbers start over when the job was retried: the steps before are from the
            // attempt that failed, kept because they happened (and cost tokens).
            const restarted = i > 0 && s.step_no <= run.llm_calls[i - 1]!.step_no;
            return (
              <Fragment key={`${s.created_at}-${i}`}>
                {restarted && (
                  <li className="flex items-center gap-2 text-xs text-warning">
                    <span className="h-px flex-1 bg-warning-line" aria-hidden="true" />
                    Restarted at {time(s.created_at)} UTC: the job was retried
                    <span className="h-px flex-1 bg-warning-line" aria-hidden="true" />
                  </li>
                )}
                <Step step={s} />
              </Fragment>
            );
          })}
          {run.error && <li className="text-sm text-danger">{run.error}</li>}
        </m.ol>
      )}
    </li>
  );
}

function Step({ step }: { step: LlmStep }) {
  const tok = step.input_tokens + step.cache_read_tokens + step.cache_creation_tokens + step.output_tokens;
  return (
    <m.li
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: d.base, ease: ease.out }}
      className="flex flex-col gap-1.5 border-l border-line-strong pl-3.5"
    >
      <span className="flex flex-wrap gap-x-2.5 text-xs text-muted">
        <span className="text-fg-2">
          Step {step.step_no}
          {step.final && " · final"}
        </span>
        <span className="font-mono tabular">
          {duration(step.latency_ms)} · {tokens(tok)} tok · {usd(step.cost_usd)}
        </span>
        {step.error_code && <span className="text-danger">{step.error_code}</span>}
      </span>
      {step.reasoning && <p className="text-sm text-fg-2">{step.reasoning}</p>}
      {step.tools.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {step.tools.map((t, i) => (
            <Tool key={i} call={t} />
          ))}
        </ul>
      )}
    </m.li>
  );
}

function Tool({ call }: { call: ToolCall }) {
  const ok = call.status === "ok";
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2.5 font-mono text-xs">
      <span className={cn("size-1.5 shrink-0 rounded-full", ok ? "bg-success" : "bg-mark-danger")} aria-hidden="true" />
      <span className="text-fg">{call.tool}</span>
      <span className="min-w-0 flex-1 truncate text-faint" title={JSON.stringify(call.args)}>
        {argsLine(call.args)}
      </span>
      {call.evidence_id ? (
        <EvidenceLink id={call.evidence_id}>{call.evidence_id.slice(0, 11)}</EvidenceLink>
      ) : (
        <span className="text-danger">{call.error_code ?? call.status}</span>
      )}
    </li>
  );
}

function Num({ children, className }: { children: string; className?: string }) {
  return <span className={cn("text-right font-mono text-xs text-muted tabular", className)}>{children}</span>;
}

/** The arguments that were set, as short as they go: `checkout 1→2`, `warnings, 120m`. */
export function argsLine(args: Record<string, unknown>): string {
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== false && v !== "")
    .map(([k, v]) => (typeof v === "string" ? v : `${k}=${JSON.stringify(v)}`))
    .join("  ");
}
