// Where the pages get their data. The console reads the API; the static site's replay reads
// an exported case (replay.ts). Both give the same responses for the same paths and report
// changes the same way, so the pages cannot tell them apart.

import { openStream, type StreamStatus } from "./sse";
import type { ApprovalIn, AuditRow, Change, State } from "./types";

export type { StreamStatus };

export interface Source {
  kind: "live" | "replay";
  get<T>(path: string): Promise<T>;
  /** Report changes until the returned function is called. */
  subscribe(onChange: (c: Change) => void, onStatus: (s: StreamStatus) => void): () => void;
  decide(incidentId: string, body: ApprovalIn): Promise<{ state: State }>;
  /** Who is looking, for the header and to decide whether to offer the approve buttons. */
  viewer: { subject: string; roles: string[]; tenant: string } | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const TOKEN_KEY = "sre-blackboard.token";

export const tokenStore = {
  get(): string | null {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(token: string | null) {
    try {
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      // Storage blocked: the token lasts as long as the page.
    }
  },
};

/** The claims the console shows. The token is not verified here: the API does that. */
export function readToken(token: string): { subject: string; roles: string[]; tenant: string; exp: number } | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const json = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/")));
    return { subject: json.sub, roles: json.roles ?? [], tenant: json.tenant ?? "", exp: json.exp ?? 0 };
  } catch {
    return null;
  }
}

export class LiveSource implements Source {
  readonly kind = "live";
  readonly viewer: Source["viewer"];

  constructor(
    private readonly token: string,
    private readonly base: string = import.meta.env.VITE_API_BASE ?? "",
    private readonly onUnauthorized: () => void = () => {},
  ) {
    const claims = readToken(token);
    this.viewer = claims && { subject: claims.subject, roles: claims.roles, tenant: claims.tenant };
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(this.base + path, {
      ...init,
      headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json", ...init?.headers },
    });
    if (resp.status === 401) this.onUnauthorized();
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        detail = (await resp.json()).detail ?? detail;
      } catch {
        // not JSON
      }
      throw new ApiError(resp.status, typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return (await resp.json()) as T;
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path);
  }

  decide(incidentId: string, body: ApprovalIn): Promise<{ state: State }> {
    return this.request(`/api/v1/incidents/${incidentId}/approvals`, { method: "POST", body: JSON.stringify(body) });
  }

  subscribe(onChange: (c: Change) => void, onStatus: (s: StreamStatus) => void): () => void {
    return openStream({
      url: `${this.base}/api/v1/stream`,
      token: this.token,
      onStatus: (s) => {
        if (s === "unauthorized") this.onUnauthorized();
        onStatus(s);
      },
      onEvent: (e) => {
        if (e.event === "audit") onChange({ kind: "audit", row: JSON.parse(e.data) as AuditRow & { subject: string } });
        else if (e.event === "activity") {
          const d = JSON.parse(e.data) as { incident_id: string; agents: string[] };
          onChange({ kind: "activity", incident_id: d.incident_id, agents: d.agents });
        }
      },
    });
  }
}
