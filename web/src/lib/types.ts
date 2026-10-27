// Shapes of the API responses the console reads (app/api/*.py). Only the fields the pages use.

export type State =
  | "RECEIVED"
  | "TRIAGED"
  | "INVESTIGATING"
  | "DIAGNOSED"
  | "PLANNED"
  | "SAFETY_REVIEWED"
  | "AWAITING_APPROVAL"
  | "EXECUTING"
  | "VERIFYING"
  | "RESOLVED"
  | "ESCALATED";

export type Priority = "P0" | "P1" | "P2" | "P3";

export interface Incident {
  id: string;
  title: string;
  service: string;
  environment: string;
  priority: Priority;
  state: State;
  version: number;
  alert_count: number;
  labels: Record<string, string>;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface Statement {
  statement: string;
  evidence_ids?: string[];
}

export interface RootCause {
  service: string;
  cause: string;
  confidence: number;
  evidence_ids: string[];
}

export interface Hypothesis {
  statement: string;
  how_to_verify?: string;
}

export interface Diagnosis {
  summary: string;
  facts: Statement[];
  inferences: Statement[];
  hypotheses: Hypothesis[];
  root_causes: RootCause[];
}

export interface Finding {
  author: string;
  kind: string;
  service: string;
  signal: string;
  statement: string;
  confidence: number;
  evidence_ids: string[];
}

export interface DiagnosisOut {
  incident_id: string;
  state: State;
  diagnosis: Diagnosis | null;
  findings: Finding[];
  usage: { calls: number; tokens: number; cost_usd: number; errors: number };
}

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
  status: string;
  error_code: string | null;
  attempts: number;
  latency_ms: number;
  evidence_id: string | null;
  created_at: string;
}

export interface LlmStep {
  step_no: number;
  status: string;
  model: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  cost_usd: number;
  reasoning: string | null;
  final: boolean;
  error_code: string | null;
  created_at: string;
  tools: ToolCall[];
}

export interface AgentRun {
  agent: string;
  status: string;
  model: string;
  prompt_version: string;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  llm_calls: LlmStep[];
  usage: {
    calls: number;
    prompt_tokens: number;
    output_tokens: number;
    cost_usd: number;
    model_time_ms: number;
    tool_calls: number;
  };
}

export interface Check {
  name: string;
  ok: boolean;
  blocking: boolean;
  detail: string;
  action: number | null;
}

export interface PlanAction {
  action: string;
  deployment: string;
  reason: string;
  evidence_ids: string[];
  to_revision?: number;
  replicas?: number;
}

export interface Execution {
  action_index: number;
  status: string;
  executor: string;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface Plan {
  id: number;
  seq: number;
  plan_hash: string;
  proposed_by: string;
  risk: string;
  verdict: "allow" | "require_approval" | "deny";
  created_at: string;
  expires_at: string | null;
  checks: Check[];
  plan: {
    summary: string;
    actions: PlanAction[];
    alternatives: { action: string; why_not: string }[];
    no_action_reason: string | null;
  };
  approval: { approver: string; decision: "approve" | "reject"; comment: string; at: string } | null;
  executions: Execution[];
}

export interface AuditRow {
  seq: number;
  ts: string;
  actor: string;
  event: string;
  payload: Record<string, unknown>;
  hash: string;
  subject?: string;
}

export interface Evidence {
  id: string;
  incident_id: string;
  tool: string;
  args: Record<string, unknown>;
  summary: Record<string, unknown>;
  result: unknown;
  result_bytes: number;
  result_hash: string;
  result_omitted?: boolean;
  truncated: boolean;
  fetched_at: string;
  created_at: string;
}

/** A change the stream reports. The page refetches only the piece it names. */
export type Change =
  | { kind: "audit"; row: AuditRow & { subject: string } }
  | { kind: "activity"; incident_id: string; agents: string[] }
  /** The replay jumped back: everything on the page is out of date. */
  | { kind: "reset" };

export interface ApprovalIn {
  plan_hash: string;
  decision: "approve" | "reject";
  comment: string;
}
