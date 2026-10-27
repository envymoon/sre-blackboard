import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AnimatePresence, m } from "motion/react";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useLocation, useParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import IncidentPage from "@/features/incident/IncidentPage";
import { IncidentRail } from "@/features/incidents/IncidentRail";
import { cn } from "@/lib/cn";
import { duration, time } from "@/lib/format";
import { ClockContext, type Clock } from "@/lib/now";
import { SourceContext, useLiveUpdates } from "@/lib/queries";
import { Replay, type CaseBundle } from "@/lib/replay";
import { duration as d, ease } from "@/styles/motion";
import { LiveStatus, Shell } from "./chrome";

const SPEEDS = [1, 10, 30, 60];

/**
 * A real incident from the lab, replayed in the browser from its exported case. No server:
 * the incident page reads the case through the same queries and the same change events it
 * uses against the API, cut to the replay clock.
 */
export default function ReplayApp() {
  const name = useParams().case ?? "bad-release";
  const [bundle, setBundle] = useState<{ name: string; data?: CaseBundle; error?: string } | null>(null);
  useEffect(() => {
    let live = true;
    fetch(`${import.meta.env.BASE_URL}cases/${encodeURIComponent(name)}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`No case called ${name}.`))))
      .then(
        (data: CaseBundle) => live && setBundle({ name, data }),
        (e: Error) => live && setBundle({ name, error: e.message }),
      );
    return () => {
      live = false;
    };
  }, [name]);
  if (bundle?.name === name && bundle.error) return <p className="p-8 text-danger">{bundle.error}</p>;
  if (bundle?.name !== name || !bundle.data) return <LoadingShell />;
  return <Player key={name} bundle={bundle.data} name={name} />;
}

function Player({ bundle, name }: { bundle: CaseBundle; name: string }) {
  const [replay] = useState(() => new Replay(bundle, 10));
  // Plays by itself when the viewer came from a "replay" link; a direct load waits for Play.
  const autoplay = Boolean((useLocation().state as { autoplay?: boolean } | null)?.autoplay);
  const client = useMemo(
    () =>
      new QueryClient({
        defaultOptions: { queries: { staleTime: Infinity, refetchOnWindowFocus: false, retry: false } },
      }),
    [],
  );
  const clock = useMemo<Clock>(() => ({ subscribe: replay.onClock, now: () => replay.clock().now }), [replay]);
  useEffect(() => {
    const t = autoplay ? setTimeout(() => replay.play(), 600) : undefined;
    return () => {
      clearTimeout(t);
      replay.dispose();
    };
  }, [replay, autoplay]);

  return (
    <SourceContext.Provider value={replay}>
      <ClockContext.Provider value={clock}>
        <QueryClientProvider client={client}>
          <ReplayShell replay={replay} name={name} />
        </QueryClientProvider>
      </ClockContext.Provider>
    </SourceContext.Provider>
  );
}

function ReplayShell({ replay, name }: { replay: Replay; name: string }) {
  useLiveUpdates();
  return (
    <Shell
      home="/"
      status={<LiveStatus status="live" label="Replay" />}
      rail={<IncidentRail linkTo={() => `/replay/${name}`} className="flex-1" initialFilter="all" />}
      banner={<ReplayBar replay={replay} />}
    >
      <IncidentPage id={replay.id} backTo={null} />
    </Shell>
  );
}

function ReplayBar({ replay }: { replay: Replay }) {
  const c = useSyncExternalStore(replay.onClock, replay.clock);
  const elapsed = c.now - c.start;
  const total = c.end - c.start;
  return (
    <div className="border-b border-line bg-panel" role="region" aria-label="Replay controls">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 sm:px-5">
        <Button
          variant="primary"
          size="sm"
          className="w-[72px]"
          onClick={() => (c.playing ? replay.pause() : replay.play())}
          disabled={c.waitingForApproval}
        >
          {c.playing ? "Pause" : c.now >= c.end ? "Replay" : "Play"}
        </Button>
        <div className="flex gap-1" role="group" aria-label="Speed">
          {SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              aria-pressed={c.speed === s}
              onClick={() => replay.setSpeed(s)}
              className={cn(
                "h-7 min-w-9 rounded-md border px-1.5 font-mono text-xs transition-colors duration-fast",
                c.speed === s ? "border-line-strong bg-raised text-fg" : "border-transparent text-muted hover:text-fg",
              )}
            >
              {s}×
            </button>
          ))}
        </div>
        <input
          type="range"
          min={0}
          max={total}
          step={500}
          value={elapsed}
          onChange={(e) => replay.seek(c.start + Number(e.target.value))}
          aria-label="Replay position"
          aria-valuetext={`${duration(elapsed)} of ${duration(total)}`}
          className="h-7 min-w-40 flex-1 accent-[var(--c-accent)]"
        />
        <span className="font-mono text-xs text-muted tabular">
          {time(new Date(c.now).toISOString())} UTC · T+{clockText(elapsed)} / {clockText(total)}
        </span>
      </div>
      <AnimatePresence>
        {c.waitingForApproval && (
          <m.p
            role="status"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 12 }}
            transition={{ duration: d.slow, ease: ease.out }}
            className="fixed inset-x-4 bottom-4 z-30 mx-auto max-w-lg rounded-lg border border-warning-line bg-panel px-4 py-3 text-sm shadow-pop"
          >
            <span className="font-medium text-warning">Paused where the real run waited for a human.</span>{" "}
            <span className="text-fg-2">You are the approver: review the plan and approve it to continue.</span>
          </m.p>
        )}
      </AnimatePresence>
    </div>
  );
}

function clockText(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function LoadingShell() {
  return (
    <div className="flex h-dvh flex-col">
      <div className="h-12 border-b border-line bg-panel" />
      <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-6 px-4 pt-6 sm:px-8" aria-busy="true">
        <Skeleton className="h-24" />
        <Skeleton className="h-11" />
        <Skeleton className="h-72" />
      </div>
    </div>
  );
}
