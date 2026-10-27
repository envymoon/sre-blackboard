import { lazy, Suspense } from "react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { bytes, time } from "@/lib/format";
import { useEvidence } from "@/lib/queries";
import { argsLine } from "./AgentTree";

// The tool-specific views (and Recharts, for the metric sparklines) load on the first open.
const EvidenceBody = lazy(() => import("./EvidenceBody"));

export interface Citation {
  label: string;
}

/**
 * One piece of evidence: what the tool was asked, what it found (as the model read it), and
 * the size and SHA-256 of the raw result, which the evidence store keeps unchanged.
 */
export function EvidenceDrawer({
  id,
  onClose,
  citedBy,
  fetchedBy,
}: {
  id: string | null;
  onClose: () => void;
  citedBy: (id: string) => Citation[];
  fetchedBy: (id: string) => string | null;
}) {
  const { data: ev, isError, error } = useEvidence(id);
  const cites = id ? citedBy(id) : [];
  return (
    <Sheet open={id !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent aria-describedby={undefined}>
        <div className="flex items-center justify-between gap-3 border-b border-line px-6 py-4">
          <SheetTitle className="truncate font-mono text-sm font-normal text-fg-2">{id}</SheetTitle>
          <SheetClose asChild>
            <Button variant="ghost" size="icon" aria-label="Close evidence">
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M2 2l8 8M10 2l-8 8" className="stroke-current" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </Button>
          </SheetClose>
        </div>
        <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto overscroll-contain px-6 py-5">
          {isError ? (
            <p role="alert" className="text-sm text-danger">
              {error.message}
            </p>
          ) : !ev ? (
            <div className="flex flex-col gap-3" aria-busy="true">
              <Skeleton className="h-6 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-40" />
            </div>
          ) : (
            <>
              <div className="flex flex-col gap-1">
                <h2 className="text-md font-semibold">{ev.tool}</h2>
                <SheetDescription className="text-xs text-muted">
                  Fetched {time(ev.fetched_at)} UTC
                  {fetchedBy(ev.id) && ` by ${fetchedBy(ev.id)}`}
                </SheetDescription>
                <p className="font-mono text-xs break-words text-faint">{argsLine(ev.args)}</p>
              </div>
              <Suspense fallback={<Skeleton className="h-40" />}>
                <EvidenceBody ev={ev} />
              </Suspense>
              <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-y-2 text-sm">
                <dt className="text-muted">Raw result</dt>
                <dd>
                  {bytes(ev.result_bytes)}
                  {ev.truncated && " · truncated by the tool"}
                  {ev.result_omitted && <span className="text-muted"> · left out of this export</span>}
                </dd>
                <dt className="text-muted">SHA-256</dt>
                <dd className="font-mono text-xs break-all text-fg-2">{ev.result_hash}</dd>
              </dl>
              {cites.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <span className="text-xs text-muted">Cited by</span>
                  <ul className="flex flex-col gap-1">
                    {cites.map((c, i) => (
                      <li key={i} className="text-sm">
                        {c.label}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
