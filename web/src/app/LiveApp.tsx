import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Navigate, Outlet, useNavigate } from "react-router";
import { IncidentRail } from "@/features/incidents/IncidentRail";
import { SourceContext, useLiveUpdates } from "@/lib/queries";
import { LiveSource, tokenStore } from "@/lib/source";
import { LiveStatus, Shell } from "./chrome";

/** The console against the API: a token from the login page, a client, the change stream. */
export default function LiveApp() {
  const navigate = useNavigate();
  const [token] = useState(tokenStore.get);
  const source = useMemo(
    () =>
      token &&
      new LiveSource(token, undefined, () => {
        tokenStore.set(null);
        navigate("/login?expired=1", { replace: true });
      }),
    [token, navigate],
  );
  const client = useMemo(
    () =>
      new QueryClient({
        defaultOptions: {
          // The stream says when something changed; no polling, no refetch on focus.
          queries: { staleTime: Infinity, refetchOnWindowFocus: false, retry: (n, e) => n < 2 && !("status" in e) },
        },
      }),
    [],
  );
  if (!source) return <Navigate to="/login" replace />;
  return (
    <SourceContext.Provider value={source}>
      <QueryClientProvider client={client}>
        <Console />
      </QueryClientProvider>
    </SourceContext.Provider>
  );
}

function Console() {
  const status = useLiveUpdates();
  return (
    <Shell
      home="/incidents"
      status={<LiveStatus status={status} />}
      rail={<IncidentRail linkTo={(id) => `/incidents/${id}`} className="flex-1" />}
    >
      <Outlet />
    </Shell>
  );
}

/** /incidents: the list itself on a phone; on a wide screen the rail is the list. */
export function ListPage() {
  return (
    <>
      <IncidentRail linkTo={(id) => `/incidents/${id}`} className="pt-3 lg:hidden" />
      <div className="hidden h-full items-center justify-center lg:flex">
        <p className="text-sm text-muted">Pick an incident on the left.</p>
      </div>
    </>
  );
}
