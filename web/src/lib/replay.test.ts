import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { staleFor } from "./queries";
import { Replay, type CaseBundle } from "./replay";
import type { AgentRun, Change, Incident, Plan } from "./types";

const bundle = JSON.parse(readFileSync("public/cases/bad-release.json", "utf8")) as CaseBundle;
const id = bundle.incident_id;
const base = `/api/v1/incidents/${id}`;
const audit = bundle.responses[`${base}/audit`] as { ts: string; event: string; payload: Record<string, unknown> }[];
const at = (to: string) => Date.parse(audit.find((e) => e.payload.to === to)!.ts);

afterEach(() => vi.useRealTimers());

describe("Replay", () => {
  it("shows only what existed at the clock's time", async () => {
    const r = new Replay(bundle);
    expect(await r.get<Incident[]>("/api/v1/incidents")).toEqual([]);
    expect((await r.get<Incident>(base)).state).toBe("RECEIVED");
    expect(await r.get<Plan[]>(`${base}/plans`)).toEqual([]);

    r.seek(at("AWAITING_APPROVAL"));
    const plans = await r.get<Plan[]>(`${base}/plans`);
    expect(plans).toHaveLength(1);
    expect(plans[0]!.approval).toBeNull();
    expect(plans[0]!.executions).toEqual([]);
    const runs = await r.get<AgentRun[]>(`${base}/agents`);
    expect(runs.every((x) => x.status !== "running")).toBe(true);

    r.seek(r.clock().end);
    expect((await r.get<Incident>(base)).state).toBe("RESOLVED");
    expect((await r.get<Plan[]>(`${base}/plans`))[0]!.approval?.decision).toBe("approve");
  });

  it("reports the audit rows it passes, in order, and a reset when it goes back", () => {
    const r = new Replay(bundle);
    const got: Change[] = [];
    r.subscribe(
      (c) => got.push(c),
      () => {},
    );
    r.seek(at("DIAGNOSED"));
    const rows = got.filter((c) => c.kind === "audit");
    expect(rows.map((c) => c.kind === "audit" && c.row.payload.to).filter(Boolean)).toEqual([
      "TRIAGED",
      "INVESTIGATING",
      "DIAGNOSED",
    ]);
    expect(got.some((c) => c.kind === "activity")).toBe(true);
    r.seek(r.clock().start);
    expect(got.at(-1)).toEqual({ kind: "reset" });
  });

  it("waits at the approval, refuses a reject, and goes on after an approve", async () => {
    vi.useFakeTimers();
    const r = new Replay(bundle, 60);
    r.play();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(r.clock().waitingForApproval).toBe(true);
    expect(r.clock().playing).toBe(false);
    const reject = r.decide(id, { plan_hash: "x", decision: "reject", comment: "" });
    const refused = expect(reject).rejects.toThrow(/approved the plan/);
    await vi.advanceTimersByTimeAsync(400);
    await refused;
    const approve = r.decide(id, { plan_hash: "x", decision: "approve", comment: "" });
    await vi.advanceTimersByTimeAsync(400);
    await approve;
    expect(r.clock().playing).toBe(true);
    expect((await r.get<Plan[]>(`${base}/plans`))[0]!.approval).not.toBeNull();
    r.dispose();
  });
});

describe("staleFor", () => {
  it("refetches only what a change touches", () => {
    expect(staleFor({ kind: "activity", incident_id: "i", agents: ["planner"] })).toEqual([
      "/api/v1/incidents/i/agents",
      "/api/v1/incidents/i/diagnosis",
    ]);
    const row = { seq: 1, ts: "", actor: "", hash: "", subject: "i", event: "execution.started", payload: {} };
    expect(staleFor({ kind: "audit", row })).toContain("/api/v1/incidents/i/plans");
    expect(staleFor({ kind: "reset" })).toBe("all");
  });
});
