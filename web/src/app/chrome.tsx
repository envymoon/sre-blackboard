import type { ReactNode } from "react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import type { StreamStatus } from "@/lib/source";
import { setTheme, useTheme, type ThemeChoice } from "./theme";

const NEXT: Record<ThemeChoice, ThemeChoice> = { system: "dark", dark: "light", light: "system" };

export function ThemeToggle() {
  const theme = useTheme();
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(NEXT[theme])}
      aria-label={`Theme: ${theme}. Switch to ${NEXT[theme]}`}
      title={`Theme: ${theme}`}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        aria-hidden="true"
        className="fill-none stroke-current"
        strokeWidth="1.4"
      >
        {theme === "dark" ? (
          <path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z" strokeLinejoin="round" />
        ) : theme === "light" ? (
          <>
            <circle cx="8" cy="8" r="3" />
            <path
              d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1"
              strokeLinecap="round"
            />
          </>
        ) : (
          <>
            <rect x="2" y="3" width="12" height="8.5" rx="1.5" />
            <path d="M5.5 14h5" strokeLinecap="round" />
          </>
        )}
      </svg>
    </Button>
  );
}

const STATUS: Record<StreamStatus, { label: string; dot: string }> = {
  connecting: { label: "Connecting", dot: "bg-mark-muted" },
  live: { label: "Live", dot: "bg-success live-dot" },
  reconnecting: { label: "Reconnecting", dot: "bg-warning" },
  unauthorized: { label: "Signed out", dot: "bg-mark-danger" },
};

export function LiveStatus({ status, label }: { status: StreamStatus; label?: string }) {
  const s = STATUS[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted" role="status">
      <span className={cn("size-1.5 rounded-full", s.dot)} aria-hidden="true" />
      {label ?? s.label}
    </span>
  );
}

export function Brand({ to = "/" }: { to?: string }) {
  return (
    <Link to={to} className="flex items-center gap-2 font-semibold tracking-[-0.01em] text-fg">
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
        <rect x="1" y="1" width="16" height="16" rx="4" className="fill-accent" />
        <path d="M5 6h8M5 9h5M5 12h6.5" className="stroke-accent-fg" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      SRE Blackboard
    </Link>
  );
}

/** Rail on the left from lg up; on a phone the page has a top bar and the rail is its own page. */
export function Shell({
  rail,
  status,
  banner,
  children,
  home,
}: {
  rail: ReactNode;
  status: ReactNode;
  banner?: ReactNode;
  children: ReactNode;
  home?: string;
}) {
  return (
    <div className="flex min-h-dvh flex-col lg:h-dvh">
      {banner}
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-[300px] shrink-0 flex-col border-r border-line bg-rail pt-5 lg:flex">
          <div className="flex items-center justify-between px-5 pb-4">
            <Brand to={home} />
            {status}
          </div>
          {rail}
          <div className="flex items-center justify-end border-t border-line px-3 py-2">
            <ThemeToggle />
          </div>
        </aside>
        <main id="main" className="min-w-0 flex-1 lg:overflow-y-auto">
          <div className="sticky top-0 z-10 flex h-12 items-center justify-between border-b border-line bg-bg/90 px-4 backdrop-blur lg:hidden">
            <Brand to={home} />
            <span className="flex items-center gap-2">
              {status}
              <ThemeToggle />
            </span>
          </div>
          {children}
        </main>
      </div>
    </div>
  );
}
