// Replay of an exported case (app/console/export.py) by its own timestamps, faster than real
// time. Every response is cut back to what existed at the replay clock's "now": the audit rows
// so far, the agent steps and tool calls so far, the plan once proposed, the approval once
// given. Changes are reported like the live stream reports them, so the incident page updates
// through exactly the path it uses against the API.

import { ApiError, type Source, type StreamStatus } from "./source";
import type { AgentRun, ApprovalIn, AuditRow, Change, DiagnosisOut, Incident, LlmStep, Plan, State } from "./types";

export interface CaseBundle {
  format: number;
  incident_id: string;
  exported_at: string;
  responses: Record<string, unknown>;
}

const ms = (iso: string) => Date.parse(iso);
const FINAL: State[] = ["RESOLVED", "ESCALATED"];

export interface ClockState {
  start: number;
  end: number;
  now: number;
  speed: number;
  playing: boolean;
  /** Paused at the approval: the viewer plays the approver. */
  waitingForApproval: boolean;
}

export class Replay implements Source {
  readonly kind = "replay";
  readonly viewer = { subject: "you (replay)", roles: ["approver"], tenant: "lab" };
  readonly id: string;
  private readonly base: string;
  private readonly incident: Incident;
  private readonly audit: AuditRow[];
  private readonly agents: AgentRun[];
  private readonly plans: Plan[];
  private readonly diagnosis: DiagnosisOut;
  /** When the plan started waiting for an approval, and when the recorded approver decided. */
  private readonly holdAt: number | null;
  private readonly approvedAt: number | null;
  private released = false;

  private state: ClockState;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastTick = 0;
  private readonly clockListeners = new Set<() => void>();
  private readonly changeListeners = new Set<(c: Change) => void>();

  constructor(
    private readonly bundle: CaseBundle,
    speed = 10,
  ) {
    this.id = bundle.incident_id;
    this.base = `/api/v1/incidents/${this.id}`;
    const r = bundle.responses;
    this.incident = r[this.base] as Incident;
    this.audit = (r[`${this.base}/audit`] as AuditRow[]) ?? [];
    this.agents = (r[`${this.base}/agents`] as AgentRun[]) ?? [];
    this.plans = (r[`${this.base}/plans`] as Plan[]) ?? [];
    this.diagnosis = r[`${this.base}/diagnosis`] as DiagnosisOut;
    const waiting = this.audit.find((e) => e.event === "incident.transition" && e.payload.to === "AWAITING_APPROVAL");
    const approval = this.plans.find((p) => p.approval)?.approval;
    this.holdAt = waiting ? ms(waiting.ts) + 1500 : null;
    this.approvedAt = approval ? ms(approval.at) : null;
    const start = ms(this.incident.created_at) - 1000;
    const last = Math.max(start, ...this.audit.map((e) => ms(e.ts)));
    this.state = { start, end: last + 2000, now: start, speed, playing: false, waitingForApproval: false };
  }

  // ---- the clock -------------------------------------------------------------------------

  clock = (): ClockState => this.state;

  onClock = (fn: () => void): (() => void) => {
    this.clockListeners.add(fn);
    return () => this.clockListeners.delete(fn);
  };

  private setClock(patch: Partial<ClockState>) {
    this.state = { ...this.state, ...patch };
    for (const fn of this.clockListeners) fn();
  }

  play() {
    if (this.state.now >= this.state.end) this.seek(this.state.start);
    if (this.state.waitingForApproval) return;
    this.setClock({ playing: true });
    this.lastTick = performance.now();
    this.timer ??= setInterval(() => this.tick(), 100);
  }

  pause() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.setClock({ playing: false });
  }

  setSpeed(speed: number) {
    this.setClock({ speed });
  }

  /** Jump to a moment. Going back starts the page over from what existed then. */
  seek(to: number) {
    const now = Math.min(Math.max(to, this.state.start), this.state.end);
    const back = now < this.state.now;
    if (back && (this.holdAt === null || now < this.holdAt)) this.released = false;
    this.advance(now, back);
  }

  dispose() {
    this.pause();
    this.clockListeners.clear();
    this.changeListeners.clear();
  }

  private tick() {
    const t = performance.now();
    const step = (t - this.lastTick) * this.state.speed;
    this.lastTick = t;
    let next = this.state.now + step;
    if (this.holdAt !== null && !this.released && this.state.now < this.holdAt && next >= this.holdAt) {
      next = this.holdAt;
      this.advance(next, false);
      this.pause();
      this.setClock({ waitingForApproval: true });
      return;
    }
    this.advance(next, false);
    if (next >= this.state.end) this.pause();
  }

  private advance(now: number, back: boolean) {
    const prev = this.state.now;
    this.setClock({ now, waitingForApproval: back ? false : this.state.waitingForApproval });
    if (back) {
      this.emit({ kind: "reset" });
      return;
    }
    for (const row of this.audit) {
      const t = ms(row.ts);
      if (t > prev && t <= now) this.emit({ kind: "audit", row: { ...row, subject: this.id } });
    }
    const active = new Set<string>();
    for (const run of this.agents) {
      const marks = [
        run.started_at,
        run.finished_at,
        ...run.llm_calls.flatMap((s) => [s.created_at, ...s.tools.map((x) => x.created_at)]),
      ];
      if (marks.some((m) => m && ms(m) > prev && ms(m) <= now)) active.add(run.agent);
    }
    if (active.size > 0) this.emit({ kind: "activity", incident_id: this.id, agents: [...active].sort() });
  }

  private emit(c: Change) {
    for (const fn of this.changeListeners) fn(c);
  }

  // ---- Source ----------------------------------------------------------------------------

  subscribe(onChange: (c: Change) => void, onStatus: (s: StreamStatus) => void): () => void {
    this.changeListeners.add(onChange);
    onStatus("live");
    return () => this.changeListeners.delete(onChange);
  }

  async decide(_incidentId: string, body: ApprovalIn): Promise<{ state: State }> {
    await new Promise((r) => setTimeout(r, 350)); // a server round trip, so the optimistic state shows
    const plan = this.plans.find((p) => p.approval);
    if (body.decision === "reject" || !plan || this.approvedAt === null) {
      const who = plan?.approval?.approver.replace(/^user:/, "") ?? "the approver";
      throw new ApiError(409, `This replay follows the recorded run, in which ${who} approved the plan.`);
    }
    this.released = true;
    this.setClock({ waitingForApproval: false });
    if (this.state.now < this.approvedAt) this.advance(this.approvedAt + 1, false);
    this.play();
    return { state: "EXECUTING" };
  }

  async get<T>(path: string): Promise<T> {
    const now = this.state.now;
    const found = this.at(path, now);
    if (found === undefined) throw new ApiError(404, `not in this case: ${path}`);
    return structuredClone(found) as T;
  }

  /** The response for ``path`` as it was at ``now``. */
  at(path: string, now: number): unknown {
    const base = this.base;
    const clean = path.split("?")[0] ?? path;
    if (clean === "/api/v1/incidents") return now >= ms(this.incident.created_at) ? [this.incidentAt(now)] : [];
    if (clean === base) return this.incidentAt(now);
    if (clean === `${base}/audit`) return this.audit.filter((e) => ms(e.ts) <= now);
    if (clean === `${base}/agents`) return this.agentsAt(now);
    if (clean === `${base}/plans`) return this.plansAt(now);
    if (clean === `${base}/diagnosis`) return this.diagnosisAt(now);
    return this.bundle.responses[clean];
  }

  private incidentAt(now: number): Incident {
    const moves = this.audit.filter((e) => e.event === "incident.transition" && ms(e.ts) <= now);
    const last = moves.at(-1);
    const state = (last?.payload.to as State | undefined) ?? "RECEIVED";
    return {
      ...this.incident,
      state,
      version: (last?.payload.version as number | undefined) ?? 1,
      updated_at: last?.ts ?? this.incident.created_at,
      closed_at: FINAL.includes(state) ? (last?.ts ?? null) : null,
    };
  }

  private agentsAt(now: number): AgentRun[] {
    return this.agents
      .filter((run) => ms(run.started_at) <= now)
      .map((run) => {
        const done = run.finished_at !== null && ms(run.finished_at) <= now;
        const steps: LlmStep[] = run.llm_calls
          .filter((s) => ms(s.created_at) <= now)
          .map((s) => ({ ...s, tools: s.tools.filter((x) => ms(x.created_at) <= now) }));
        return {
          ...run,
          status: done ? run.status : "running",
          finished_at: done ? run.finished_at : null,
          llm_calls: steps,
          usage: usageOf(steps),
        };
      });
  }

  private plansAt(now: number): Plan[] {
    return this.plans
      .filter((p) => ms(p.created_at) <= now)
      .map((p) => ({
        ...p,
        approval: p.approval && ms(p.approval.at) <= now ? p.approval : null,
        executions: p.executions
          .filter((x) => ms(x.started_at) <= now)
          .map((x) =>
            x.finished_at && ms(x.finished_at) <= now ? x : { ...x, status: "running", finished_at: null, after: null },
          ),
      }));
  }

  private diagnosisAt(now: number): DiagnosisOut {
    const state = this.incidentAt(now).state;
    const diagnosed = this.audit.some(
      (e) => e.event === "incident.transition" && e.payload.to === "DIAGNOSED" && ms(e.ts) <= now,
    );
    const finished = new Set(this.agents.filter((r) => r.finished_at && ms(r.finished_at) <= now).map((r) => r.agent));
    const steps = this.agentsAt(now).flatMap((r) => r.llm_calls);
    const u = usageOf(steps);
    return {
      ...this.diagnosis,
      state,
      diagnosis: diagnosed ? this.diagnosis.diagnosis : null,
      findings: this.diagnosis.findings.filter((f) => finished.has(f.author)),
      usage: {
        ...this.diagnosis.usage,
        calls: u.calls,
        tokens: u.prompt_tokens + u.output_tokens,
        cost_usd: u.cost_usd,
      },
    };
  }
}

function usageOf(steps: LlmStep[]): AgentRun["usage"] {
  const u = { calls: 0, prompt_tokens: 0, output_tokens: 0, cost_usd: 0, model_time_ms: 0, tool_calls: 0 };
  for (const s of steps) {
    u.calls += 1;
    u.prompt_tokens += s.input_tokens + s.cache_read_tokens + s.cache_creation_tokens;
    u.output_tokens += s.output_tokens;
    u.cost_usd += s.cost_usd;
    u.model_time_ms += s.latency_ms;
    u.tool_calls += s.tools.length;
  }
  u.cost_usd = Math.round(u.cost_usd * 1e6) / 1e6;
  return u;
}
