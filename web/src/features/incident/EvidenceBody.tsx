import { Line, LineChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { Badge } from "@/components/ui/badge";
import { time } from "@/lib/format";
import type { Evidence } from "@/lib/types";

// What each tool found, drawn from the evidence summary: the compressed result the model
// read (app/tools/*). Unknown tools fall back to the summary as JSON.

type S = Record<string, any>;

export default function EvidenceBody({ ev }: { ev: Evidence }) {
  const s = ev.summary as S;
  switch (ev.tool) {
    case "prom_range":
      return <Metrics s={s} />;
    case "logs_search":
      return <Logs s={s} />;
    case "traces_search":
      return <Traces s={s} />;
    case "trace_get":
      return <Trace s={s} />;
    case "deploy_diff":
      return <Diff s={s} />;
    case "rollout_history":
      return <Rollouts s={s} />;
    case "k8s_events":
      return <Events s={s} />;
    case "flag_state":
      return <Flags s={s} />;
    default:
      return <Json value={s} />;
  }
}

function fmt(v: number): string {
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 0.01) return v.toFixed(3);
  return v.toExponential(1);
}

function Metrics({ s }: { s: S }) {
  const series: S[] = s.series ?? [];
  if (!series.length) return <Note>{s.note ?? "No series in this window."}</Note>;
  return (
    <ul className="flex flex-col gap-3">
      {series.slice(0, 8).map((x, i) => {
        const label =
          Object.entries(x.labels ?? {})
            .filter(([k]) => k !== "__name__")
            .map(([k, v]) => `${k}=${v}`)
            .join(", ") || String(x.labels?.__name__ ?? "series");
        const data = (x.shape as number[]).map((v, j) => ({ j, v }));
        return (
          <li key={i} className="flex flex-col gap-1.5 rounded-lg border border-line bg-panel p-3">
            <span className="truncate font-mono text-xs text-fg-2" title={label}>
              {label}
            </span>
            <div
              className="h-12"
              role="img"
              aria-label={`${label}: from ${fmt(x.first)} to ${fmt(x.last)}, peak ${fmt(x.max)} at ${time(x.peak_at, false)}`}
            >
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={data} margin={{ top: 4, right: 2, bottom: 4, left: 2 }}>
                  <YAxis hide domain={["dataMin", "dataMax"]} />
                  <Tooltip
                    cursor={{ stroke: "var(--c-line-strong)" }}
                    contentStyle={{
                      background: "var(--c-panel)",
                      border: "1px solid var(--c-line)",
                      borderRadius: 6,
                      fontSize: 12,
                      color: "var(--c-fg)",
                    }}
                    labelFormatter={() => ""}
                    formatter={(v: number) => [fmt(v), "value"]}
                  />
                  <Line
                    type="monotone"
                    dataKey="v"
                    stroke="var(--c-mark)"
                    strokeWidth={1.5}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <span className="flex flex-wrap gap-x-3 font-mono text-2xs text-muted tabular">
              <span>last {fmt(x.last)}</span>
              <span>
                peak {fmt(x.max)} @ {time(x.peak_at, false)}
              </span>
              <span>mean {fmt(x.mean)}</span>
              <span>
                {time(x.from, false)}–{time(x.to, false)}
              </span>
            </span>
          </li>
        );
      })}
      {series.length > 8 && <Note>and {series.length - 8} more series</Note>}
    </ul>
  );
}

function Logs({ s }: { s: S }) {
  const groups: S[] = s.groups ?? [];
  return (
    <div className="flex flex-col gap-3">
      <p className="font-mono text-xs break-words text-faint">{s.query}</p>
      <p className="text-sm text-muted">
        {s.lines} lines · {s.distinct_messages} distinct messages
        {s.hit_limit && " · hit the limit"}
      </p>
      {groups.length === 0 ? (
        <Note>{s.note ?? "No matching lines."}</Note>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {groups.slice(0, 12).map((g, i) => (
            <li key={i} className="rounded-md border border-line bg-panel px-3 py-2">
              <span className="flex items-center gap-2 text-2xs text-muted">
                <Badge tone={g.level === "error" ? "danger" : g.level === "warn" ? "warning" : "neutral"}>
                  {g.level || "—"}
                </Badge>
                <span className="font-mono tabular">×{g.count}</span>
                <span className="font-mono">
                  {time(g.first_seen)}–{time(g.last_seen)}
                </span>
              </span>
              <pre className="mt-1 font-mono text-xs break-words whitespace-pre-wrap text-fg-2">{g.example}</pre>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Traces({ s }: { s: S }) {
  const errs: S[] = s.error_spans ?? [];
  const slow: S[] = s.slowest ?? [];
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        {s.traces} traces · {s.with_errors} with errors · p50 {s.duration_ms?.p50} ms · p95 {s.duration_ms?.p95} ms
      </p>
      {errs.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <h3 className="text-xs text-muted">Error spans</h3>
          {errs.slice(0, 6).map((e, i) => (
            <SpanError key={i} e={e} />
          ))}
        </div>
      )}
      {slow.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-xs text-muted">Slowest traces</h3>
          <table className="w-full text-xs">
            <tbody>
              {slow.slice(0, 6).map((t) => (
                <tr key={t.trace_id} className="border-b border-line-soft last:border-0">
                  <td className="py-1 font-mono text-fg-2">{t.trace_id.slice(0, 12)}</td>
                  <td className="py-1 text-muted">
                    {t.root?.service} {t.root?.operation}
                  </td>
                  <td className="py-1 text-right font-mono tabular">{t.duration_ms} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SpanError({ e }: { e: S }) {
  return (
    <div className="rounded-md border border-danger-line bg-danger-soft px-3 py-2">
      <span className="font-mono text-xs text-fg">
        {e.service} · {e.operation}
        {e.count ? <span className="text-muted"> ×{e.count}</span> : null}
      </span>
      {e.message && <p className="mt-0.5 font-mono text-xs break-words text-danger">{e.message}</p>}
      {e.status && <p className="font-mono text-2xs text-muted">{e.status}</p>}
    </div>
  );
}

function Trace({ s }: { s: S }) {
  const errs: S[] = s.errors ?? [];
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        <span className="font-mono">{String(s.trace_id).slice(0, 16)}</span> · {s.duration_ms} ms · {s.spans} spans ·{" "}
        {(s.services ?? []).length} services · root {s.root?.service} {s.root?.operation}
      </p>
      {errs.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <h3 className="text-xs text-muted">Errors, from the leaf up</h3>
          {[...errs]
            .reverse()
            .slice(0, 8)
            .map((e, i) => (
              <SpanError key={i} e={e} />
            ))}
        </div>
      ) : (
        <Note>No error spans.</Note>
      )}
    </div>
  );
}

function Diff({ s }: { s: S }) {
  const changes: S[] = s.changes ?? [];
  return (
    <div className="flex flex-col gap-3">
      {changes.length === 0 ? (
        <Note>No pod-template differences.</Note>
      ) : (
        changes.map((c, i) => (
          <div key={i} className="overflow-hidden rounded-lg border border-line font-mono text-xs">
            <div className="bg-raised px-3 py-2 break-all text-muted">{c.path}</div>
            <div className="bg-del px-3 py-1.5 break-all text-del-fg">
              <span aria-hidden="true">− </span>
              <span className="sr-only">before: </span>
              {String(c.from ?? "∅")}
            </div>
            <div className="bg-add px-3 py-1.5 break-all text-add-fg">
              <span aria-hidden="true">+ </span>
              <span className="sr-only">after: </span>
              {String(c.to ?? "∅")}
            </div>
          </div>
        ))
      )}
      <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-y-2 text-sm">
        {(["from", "to"] as const).map((k) => (
          <Rev key={k} r={s[k]} />
        ))}
      </dl>
    </div>
  );
}

function Rev({ r }: { r: S | undefined }) {
  if (!r) return null;
  return (
    <>
      <dt className="text-muted">Revision {r.revision}</dt>
      <dd>
        {time(r.created)}Z ·{" "}
        {r.change_cause ? `“${r.change_cause}”` : <span className="text-muted">no change cause</span>}
      </dd>
    </>
  );
}

function Rollouts({ s }: { s: S }) {
  const revs: S[] = s.revisions ?? [];
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-muted">
        {s.deployment} · {s.replicas?.readyReplicas ?? "?"}/{s.replicas?.replicas ?? "?"} ready
      </p>
      <ol className="flex flex-col gap-1.5">
        {revs.map((r) => (
          <li key={r.revision} className="rounded-md border border-line bg-panel px-3 py-2 text-sm">
            <span className="flex items-center gap-2">
              <span className="font-mono">rev {r.revision}</span>
              {r.current && <Badge tone="accent">current</Badge>}
              <span className="font-mono text-xs text-muted">{time(r.created)}Z</span>
            </span>
            <span className="block text-muted">{r.change_cause ?? "no change cause"}</span>
            <span className="block truncate font-mono text-2xs text-faint">{(r.images ?? []).join(", ")}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Events({ s }: { s: S }) {
  const groups: S[] = s.groups ?? [];
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-muted">
        {s.events} events · {s.warnings} warnings in {s.namespace}
      </p>
      {groups.length === 0 ? (
        <Note>No events.</Note>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {groups.slice(0, 10).map((g, i) => (
            <li key={i} className="rounded-md border border-line bg-panel px-3 py-2">
              <span className="flex flex-wrap items-center gap-2 text-2xs text-muted">
                <Badge tone={g.type === "Warning" ? "warning" : "neutral"}>{g.reason}</Badge>
                <span className="font-mono">{g.object}</span>
                <span className="font-mono tabular">
                  ×{g.count} · {time(g.last_seen)}
                </span>
              </span>
              <p className="mt-1 font-mono text-xs break-words text-fg-2">{g.message}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Flags({ s }: { s: S }) {
  const on: S[] = s.on ?? [];
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-muted">
        {s.flags} flags · {on.length} on
      </p>
      {on.length ? <Json value={on} /> : <Note>{s.note ?? "Every flag is off."}</Note>}
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="rounded-md border border-dashed border-line-strong px-3 py-2 text-sm text-muted">{children}</p>;
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-96 overflow-auto rounded-md border border-line bg-panel p-3 font-mono text-xs text-fg-2">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
