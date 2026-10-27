import { useId, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { LiveSource, readToken, tokenStore } from "@/lib/source";
import { Brand, ThemeToggle } from "./chrome";

/** Paste an access token. It stays in this tab's session storage and goes only to the API. */
export default function Login() {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(
    search.get("expired") ? "Your token expired or was refused. Paste a new one." : null,
  );
  const [busy, setBusy] = useState(false);
  const fieldId = useId();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const t = token.trim();
    const claims = readToken(t);
    if (!claims) return setError("That is not a JWT.");
    if (claims.exp * 1000 < Date.now()) return setError("That token has expired.");
    setBusy(true);
    try {
      await new LiveSource(t).get("/api/v1/incidents?limit=1");
      tokenStore.set(t);
      navigate("/incidents", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex h-14 items-center justify-between px-4 sm:px-8">
        <Brand />
        <ThemeToggle />
      </header>
      <main className="flex flex-1 items-start justify-center px-4 pt-[12vh]">
        <form
          onSubmit={submit}
          className="flex w-full max-w-md flex-col gap-4 rounded-xl border border-line bg-panel p-6"
        >
          <div className="flex flex-col gap-1">
            <h1 className="text-lg font-semibold">Sign in to the console</h1>
            <p className="text-sm text-muted">
              Paste an access token for your tenant. A viewer can read; an approver can also approve plans.
            </p>
          </div>
          <label htmlFor={fieldId} className="flex flex-col gap-1.5 text-sm">
            Access token
            <textarea
              id={fieldId}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              rows={4}
              spellCheck={false}
              autoComplete="off"
              className="rounded-md border border-line-strong bg-sunken p-2.5 font-mono text-xs break-all text-fg"
            />
          </label>
          <p className="text-xs text-muted">
            For a local stack: <code className="font-mono text-fg-2">make token SUB=you ROLES=approver</code>
          </p>
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
          <Button type="submit" variant="primary" disabled={busy || !token.trim()}>
            {busy ? "Checking…" : "Open the console"}
          </Button>
        </form>
      </main>
    </div>
  );
}
