import { Link } from "react-router";
import { buttonVariants } from "@/components/ui/button";
import { Brand, ThemeToggle } from "@/app/chrome";
import { cn } from "@/lib/cn";

const REPO = "https://github.com/envymoon/sre-blackboard";

// Each number is from a lab run or an evaluation in the repository; the link goes to where it
// is recorded.
const RESULTS = [
  {
    value: "3 / 3",
    label: "acceptance faults with the right top root cause",
    detail: "In the last 2 of 3 lab runs (2 / 3 in the first), about $0.05 per investigation on a small model.",
    href: `${REPO}/blob/main/docs/learn/m4-agent.md`,
  },
  {
    value: "130 s",
    label: "from the approval to RESOLVED",
    detail:
      "The bad release: diagnosed, rollback proposed (R3), approved, rolled back, SLO back to 0 errors, audit chain intact.",
    href: `${REPO}/blob/main/docs/learn/m5-safety.md`,
  },
  {
    value: "6 / 7",
    label: "unfixable incidents where the planner proposed nothing",
    detail: "For flag and network faults no catalogue action repairs, it wrote down what a human should check instead.",
    href: `${REPO}/blob/main/docs/learn/m5-safety.md`,
  },
  {
    value: "0",
    label: "safety violations in the prompt-injection evaluation",
    detail:
      "Instructions planted in a rollout's change cause and a log line, run against the real model on every model check.",
    href: `${REPO}/blob/main/scripts/injection_eval.py`,
  },
];

const FLOW = [
  { n: "1", title: "Alert", body: "Alertmanager posts an SLO burn; the API deduplicates it into an incident." },
  {
    n: "2",
    title: "Investigate",
    body: "A coordinator briefs three investigators (metrics, logs and traces, changes) that run in parallel, each with only its own read-only tools.",
  },
  {
    n: "3",
    title: "Diagnose",
    body: "A synthesizer ranks root causes. Every cited evidence id is checked without a model.",
  },
  {
    n: "4",
    title: "Plan and review",
    body: "A planner proposes a catalogue action; a deterministic policy checks it, item by item.",
  },
  { n: "5", title: "Approve", body: "Risky actions wait for a human, bound to the plan's hash." },
  {
    n: "6",
    title: "Execute and verify",
    body: "A separate executor with its own credentials acts once; the SLO decides RESOLVED or ESCALATED.",
  },
];

export default function Landing() {
  return (
    <div className="min-h-dvh">
      <header className="mx-auto flex h-14 max-w-[1100px] items-center justify-between px-4 sm:px-8">
        <Brand />
        <nav className="flex items-center gap-1" aria-label="Site">
          <a href={REPO} className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}>
            GitHub
          </a>
          <ThemeToggle />
        </nav>
      </header>

      <main className="mx-auto flex max-w-[1100px] flex-col gap-20 px-4 pt-14 pb-24 sm:px-8 sm:pt-20">
        <section className="flex max-w-[760px] flex-col gap-6">
          <h1 className="text-2xl font-semibold tracking-[-0.025em] text-balance sm:text-[44px] sm:leading-[52px]">
            An on-call agent that shows its evidence, and acts only when policy and a human agree.
          </h1>
          <p className="max-w-[62ch] text-md text-muted">
            SRE Blackboard investigates an alert with read-only tools, cites every claim, proposes a fix from a fixed
            catalogue, and leaves an audit trail you can verify. This page replays a real incident from the lab: a bad
            checkout release on the OpenTelemetry Demo, from the alert to the rollback.
          </p>
          <div className="flex flex-wrap gap-3">
            <Link
              to="/replay/bad-release"
              state={{ autoplay: true }}
              className={cn(buttonVariants({ variant: "primary" }), "h-10 px-5")}
            >
              Replay a real incident
            </Link>
            <a href={REPO} className={cn(buttonVariants({ variant: "secondary" }), "h-10 px-5")}>
              Read the code
            </a>
          </div>
        </section>

        <section aria-labelledby="arch-h" className="flex flex-col gap-6">
          <div className="flex flex-col gap-2">
            <h2 id="arch-h" className="text-lg font-semibold">
              How an incident moves
            </h2>
            <p className="max-w-[68ch] text-sm text-muted">
              Stateless API and worker processes around one Postgres database, which holds the state machine, the job
              queue, the agents' blackboard, the evidence store and a hash-chained audit log. The console follows it
              over Server-Sent Events.
            </p>
          </div>
          <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {FLOW.map((s) => (
              <li key={s.n} className="flex gap-3 rounded-xl border border-line bg-panel p-4">
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent-soft font-mono text-xs text-accent-text">
                  {s.n}
                </span>
                <span className="flex flex-col gap-1">
                  <span className="text-sm font-semibold">{s.title}</span>
                  <span className="text-sm text-muted">{s.body}</span>
                </span>
              </li>
            ))}
          </ol>
          <Architecture />
        </section>

        <section aria-labelledby="eval-h" className="flex flex-col gap-6">
          <div className="flex flex-col gap-2">
            <h2 id="eval-h" className="text-lg font-semibold">
              Results so far
            </h2>
            <p className="max-w-[68ch] text-sm text-muted">
              From the fault lab on GitHub Actions (kind and the OpenTelemetry Demo, rebuilt from scratch every run) and
              the evaluations in the repository.
            </p>
          </div>
          <ul className="grid gap-3 sm:grid-cols-2">
            {RESULTS.map((r) => (
              <li key={r.label} className="flex flex-col gap-2 rounded-xl border border-line bg-panel p-5">
                <span className="font-mono text-[28px] leading-9 font-medium tabular">{r.value}</span>
                <span className="text-sm font-medium">{r.label}</span>
                <span className="text-sm text-muted">{r.detail}</span>
                <a href={r.href} className="mt-auto text-xs">
                  Where this comes from →
                </a>
              </li>
            ))}
          </ul>
        </section>
      </main>
      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-[1100px] flex-wrap justify-between gap-2 px-4 py-6 text-xs text-muted sm:px-8">
          <span>SRE Blackboard · a learning project, not a product</span>
          <a href={REPO}>{REPO.replace("https://", "")}</a>
        </div>
      </footer>
    </div>
  );
}

/** Processes and what they talk to. Boxes are drawn with the theme's tokens. */
function Architecture() {
  const box = "fill-panel stroke-line-strong";
  const label = "fill-fg text-[13px] font-medium";
  const sub = "fill-muted text-[11px]";
  const line = "stroke-mark-muted";
  return (
    <figure className="overflow-x-auto rounded-xl border border-line bg-sunken p-4">
      <svg
        viewBox="0 0 960 300"
        className="h-auto w-full min-w-[720px]"
        role="img"
        aria-labelledby="arch-title arch-desc"
      >
        <title id="arch-title">Architecture</title>
        <desc id="arch-desc">
          Alertmanager posts to the API. The API and the workers share Postgres. Workers run the agents, which call
          read-only tools on Prometheus, Loki, Tempo and Kubernetes. The executor applies approved plans to Kubernetes
          with its own credentials. The console reads the API and its event stream.
        </desc>
        <defs>
          <marker
            id="arr"
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M0 0L8 4L0 8z" className="fill-mark-muted" />
          </marker>
        </defs>
        {/* row 1 */}
        <rect x="20" y="30" width="150" height="56" rx="8" className={box} />
        <text x="95" y="55" textAnchor="middle" className={label}>
          Alertmanager
        </text>
        <text x="95" y="73" textAnchor="middle" className={sub}>
          SLO burn alerts
        </text>

        <rect x="240" y="30" width="170" height="56" rx="8" className={box} />
        <text x="325" y="55" textAnchor="middle" className={label}>
          API × 2
        </text>
        <text x="325" y="73" textAnchor="middle" className={sub}>
          ingest · approvals · SSE
        </text>

        <rect x="480" y="30" width="200" height="56" rx="8" className="fill-accent-soft stroke-accent-line" />
        <text x="580" y="55" textAnchor="middle" className={label}>
          Console
        </text>
        <text x="580" y="73" textAnchor="middle" className={sub}>
          this page, against the API
        </text>

        {/* row 2 */}
        <rect x="240" y="130" width="440" height="56" rx="8" className={box} />
        <text x="460" y="155" textAnchor="middle" className={label}>
          Postgres
        </text>
        <text x="460" y="173" textAnchor="middle" className={sub}>
          incidents · job queue · blackboard · evidence · hash-chained audit log
        </text>

        <rect x="750" y="130" width="190" height="56" rx="8" className={box} />
        <text x="845" y="155" textAnchor="middle" className={label}>
          Policy engine
        </text>
        <text x="845" y="173" textAnchor="middle" className={sub}>
          deterministic checks
        </text>

        {/* row 3 */}
        <rect x="20" y="230" width="300" height="56" rx="8" className={box} />
        <text x="170" y="255" textAnchor="middle" className={label}>
          Workers × 2: agents
        </text>
        <text x="170" y="273" textAnchor="middle" className={sub}>
          coordinator · 3 investigators · synthesizer · planner
        </text>

        <rect x="390" y="230" width="230" height="56" rx="8" className={box} />
        <text x="505" y="255" textAnchor="middle" className={label}>
          Read-only tools
        </text>
        <text x="505" y="273" textAnchor="middle" className={sub}>
          Prometheus · Loki · Tempo · K8s
        </text>

        <rect x="690" y="230" width="250" height="56" rx="8" className={box} />
        <text x="815" y="255" textAnchor="middle" className={label}>
          Executor → Kubernetes
        </text>
        <text x="815" y="273" textAnchor="middle" className={sub}>
          own service account · once per plan
        </text>

        {/* arrows */}
        <path d="M170 58H236" className={line} strokeWidth="1.5" markerEnd="url(#arr)" />
        <path d="M476 58H414" className={line} strokeWidth="1.5" markerEnd="url(#arr)" />
        <path d="M325 86V126" className={line} strokeWidth="1.5" markerEnd="url(#arr)" />
        <path
          d="M170 230V200Q170 186 184 186H236"
          className={line}
          strokeWidth="1.5"
          markerEnd="url(#arr)"
          fill="none"
        />
        <path d="M320 258H386" className={line} strokeWidth="1.5" markerEnd="url(#arr)" />
        <path d="M680 158H746" className={line} strokeWidth="1.5" markerEnd="url(#arr)" />
        <path
          d="M815 230V200Q815 192 760 192H684"
          className={line}
          strokeWidth="1.5"
          markerEnd="url(#arr)"
          fill="none"
        />
      </svg>
    </figure>
  );
}
