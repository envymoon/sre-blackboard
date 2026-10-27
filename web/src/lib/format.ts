// Display helpers. Times are shown in UTC, as the API and the audit trail record them.

import type { Priority, State } from "./types";

export const STATES: State[] = [
  "RECEIVED",
  "TRIAGED",
  "INVESTIGATING",
  "DIAGNOSED",
  "PLANNED",
  "SAFETY_REVIEWED",
  "AWAITING_APPROVAL",
  "EXECUTING",
  "VERIFYING",
  "RESOLVED",
];

export const STATE_LABEL: Record<State, string> = {
  RECEIVED: "Received",
  TRIAGED: "Triaged",
  INVESTIGATING: "Investigating",
  DIAGNOSED: "Diagnosed",
  PLANNED: "Planned",
  SAFETY_REVIEWED: "Reviewed",
  AWAITING_APPROVAL: "Awaiting approval",
  EXECUTING: "Executing",
  VERIFYING: "Verifying",
  RESOLVED: "Resolved",
  ESCALATED: "Escalated",
};

/** Shorter names for the progress bar, where ten states share one row. */
export const STATE_SHORT: Record<State, string> = {
  ...STATE_LABEL,
  SAFETY_REVIEWED: "Reviewed",
  AWAITING_APPROVAL: "Approval",
};

/** The semantic tone of each state: one colour family per meaning, the same everywhere. */
export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "critical";

export const STATE_TONE: Record<State, Tone> = {
  RECEIVED: "neutral",
  TRIAGED: "accent",
  INVESTIGATING: "accent",
  DIAGNOSED: "accent",
  PLANNED: "accent",
  SAFETY_REVIEWED: "accent",
  AWAITING_APPROVAL: "warning",
  EXECUTING: "accent",
  VERIFYING: "accent",
  RESOLVED: "success",
  ESCALATED: "danger",
};

export const PRIORITY_TONE: Record<Priority, Tone> = {
  P0: "danger",
  P1: "critical",
  P2: "neutral",
  P3: "neutral",
};

export const OPEN_STATES = new Set<State>(STATES.filter((s) => s !== "RESOLVED"));

export function time(iso: string | null | undefined, seconds = true): string {
  if (!iso) return "—";
  return new Date(iso).toISOString().slice(11, seconds ? 19 : 16);
}

export function dateTime(iso: string): string {
  const d = new Date(iso).toISOString();
  return `${d.slice(0, 10)} ${d.slice(11, 19)} UTC`;
}

export function duration(msValue: number): string {
  if (msValue < 1000) return `${Math.round(msValue)} ms`;
  const s = msValue / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

export function tokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function usd(n: number): string {
  return `$${n < 0.01 ? n.toFixed(4) : n.toFixed(3)}`;
}

export function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

/** "12 min ago" from a fixed "now", so a replay can pass its own clock. */
export function ago(iso: string, now: number): string {
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
