import { useVirtualizer } from "@tanstack/react-virtual";
import { m } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useParams } from "react-router";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { OPEN_STATES, PRIORITY_TONE, STATE_LABEL, STATE_TONE, time } from "@/lib/format";
import { useIncidents } from "@/lib/queries";
import type { Incident } from "@/lib/types";
import { duration as d, ease } from "@/styles/motion";

const FILTERS = [
  { key: "open", label: "Open", test: (i: Incident) => OPEN_STATES.has(i.state) },
  { key: "waiting", label: "Waiting", test: (i: Incident) => i.state === "AWAITING_APPROVAL" },
  { key: "escalated", label: "Escalated", test: (i: Incident) => i.state === "ESCALATED" },
  { key: "all", label: "All", test: () => true },
] as const;
type FilterKey = (typeof FILTERS)[number]["key"];

const ROW = 60;

/**
 * The incident list. Long lists are virtualised (only the visible rows exist in the DOM);
 * an incident that arrives while the page is open slides in, the ones already there do not.
 */
export function IncidentRail({
  linkTo,
  className,
  initialFilter = "open",
}: {
  linkTo: (id: string) => string;
  className?: string;
  initialFilter?: FilterKey;
}) {
  const { data, isPending, isError, error } = useIncidents();
  const [filter, setFilter] = useState<FilterKey>(initialFilter);
  const [query, setQuery] = useState("");
  const params = useParams();
  const all = data ?? [];
  const counts = useMemo(() => Object.fromEntries(FILTERS.map((f) => [f.key, all.filter(f.test).length])), [all]);
  const q = query.trim().toLowerCase();
  const rows = useMemo(() => {
    const test = FILTERS.find((f) => f.key === filter)!.test;
    return all
      .filter(
        (i) => (test(i) || i.id === params.id) && (!q || `${i.service} ${i.title} ${i.id}`.toLowerCase().includes(q)),
      )
      .sort((a, b) => a.priority.localeCompare(b.priority) || b.created_at.localeCompare(a.created_at));
  }, [all, filter, q, params.id]);

  // Ids on screen before the latest update: anything else is new and animates in.
  const seen = useRef<Set<string> | null>(null);
  const fresh = new Set<string>();
  if (data) {
    if (seen.current === null) seen.current = new Set(data.map((i) => i.id));
    else for (const i of data) if (!seen.current.has(i.id)) fresh.add(i.id);
  }
  useEffect(() => {
    if (data && seen.current) for (const i of data) seen.current.add(i.id);
  }, [data]);

  const scroller = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW,
    overscan: 8,
  });

  return (
    <nav aria-label="Incidents" className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex flex-col gap-3 px-3 pt-1 pb-3">
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              aria-pressed={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={cn(
                "h-7 rounded-md border px-2.5 text-xs transition-colors duration-fast",
                filter === f.key ? "border-line-strong bg-raised text-fg" : "border-line text-muted hover:text-fg",
              )}
            >
              {f.label}
              {data && <span className="ml-1 text-faint tabular">{counts[f.key]}</span>}
            </button>
          ))}
        </div>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by service or title"
          aria-label="Filter by service or title"
          className="h-8 rounded-md border border-line bg-panel px-2.5 text-sm text-fg placeholder:text-faint"
        />
      </div>
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {isPending ? (
          <div className="flex flex-col gap-1 px-1" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-[52px]" />
            ))}
          </div>
        ) : isError ? (
          <p role="alert" className="px-2 text-sm text-danger">
            {error.message}
          </p>
        ) : rows.length === 0 ? (
          <p className="px-2 py-3 text-sm text-muted">
            {all.length ? "Nothing matches this filter." : "No incidents yet."}
          </p>
        ) : (
          <ul className="relative" style={{ height: virtual.getTotalSize() }}>
            {virtual.getVirtualItems().map((v) => {
              const inc = rows[v.index]!;
              return (
                <li
                  key={inc.id}
                  className="absolute inset-x-0 top-0"
                  style={{ transform: `translateY(${v.start}px)`, height: ROW }}
                >
                  <m.div
                    className="h-full py-0.5"
                    initial={fresh.has(inc.id) ? { opacity: 0, x: -12 } : false}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ duration: d.slow, ease: ease.out }}
                  >
                    <Row inc={inc} to={linkTo(inc.id)} selected={params.id === inc.id} />
                  </m.div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </nav>
  );
}

function Row({ inc, to, selected }: { inc: Incident; to: string; selected: boolean }) {
  return (
    <NavLink
      to={to}
      aria-current={selected ? "page" : undefined}
      className={cn(
        "flex h-full flex-col justify-center gap-0.5 rounded-md px-2 text-fg transition-colors duration-fast",
        selected ? "bg-raised" : "hover:bg-raised/60",
      )}
    >
      <span className="flex items-center gap-2">
        <Badge tone={PRIORITY_TONE[inc.priority]} className="w-7 justify-center px-0 font-mono text-2xs font-semibold">
          {inc.priority}
        </Badge>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{inc.service}</span>
        <time dateTime={inc.created_at} className="font-mono text-2xs text-muted tabular">
          {time(inc.created_at, false)}
        </time>
      </span>
      <span className="flex min-w-0 items-center gap-2 pl-9 text-xs text-muted">
        <span className="min-w-0 truncate">{inc.title}</span>
        <Badge tone={STATE_TONE[inc.state]} className="ml-auto px-1 text-2xs font-normal">
          {STATE_LABEL[inc.state].toLowerCase()}
        </Badge>
      </span>
    </NavLink>
  );
}
