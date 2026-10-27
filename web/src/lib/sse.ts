// Server-Sent Events over fetch. The browser's EventSource cannot send an Authorization
// header, and the API takes a bearer token, so this reads the stream itself: it parses the
// event-stream format, remembers the last event id, and reconnects with Last-Event-ID after
// the server's retry delay, so a dropped connection loses nothing (app/api/stream.py).

export interface SseEvent {
  id: string | null;
  event: string;
  data: string;
}

/** Incremental event-stream parser: feed it text as it arrives, get whole events back. */
export class SseParser {
  private buffer = "";
  private data: string[] = [];
  private event = "";
  private id: string | null = null;
  lastId: string | null = null;
  retryMs: number | null = null;

  feed(chunk: string): SseEvent[] {
    this.buffer += chunk;
    const out: SseEvent[] = [];
    let nl: number;
    while ((nl = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buffer.slice(0, nl);
      const width = this.buffer.startsWith("\r\n", nl) ? 2 : 1;
      this.buffer = this.buffer.slice(nl + width);
      if (line === "") {
        if (this.data.length > 0 || this.event) {
          out.push({ id: this.id, event: this.event || "message", data: this.data.join("\n") });
        }
        this.data = [];
        this.event = "";
        continue;
      }
      if (line.startsWith(":")) continue; // a comment: the server's keep-alive ping
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") this.data.push(value);
      else if (field === "event") this.event = value;
      else if (field === "id" && !value.includes("\0")) this.id = this.lastId = value;
      else if (field === "retry" && /^\d+$/.test(value)) this.retryMs = Number(value);
    }
    return out;
  }
}

export type StreamStatus = "connecting" | "live" | "reconnecting" | "unauthorized";

export interface StreamOptions {
  url: string;
  token: string;
  onEvent: (e: SseEvent) => void;
  onStatus: (s: StreamStatus) => void;
  /** Resume after this event id instead of "from now". */
  lastEventId?: string | null;
  fetchImpl?: typeof fetch;
}

/** Keep a stream open until the returned function is called. */
export function openStream(opts: StreamOptions): () => void {
  const fetchImpl = opts.fetchImpl ?? fetch.bind(globalThis);
  const abort = new AbortController();
  let lastId = opts.lastEventId ?? null;
  let retryMs = 2000;
  let failures = 0;

  const run = async () => {
    while (!abort.signal.aborted) {
      opts.onStatus(failures === 0 ? "connecting" : "reconnecting");
      try {
        const headers: Record<string, string> = { Authorization: `Bearer ${opts.token}`, Accept: "text/event-stream" };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const resp = await fetchImpl(opts.url, { headers, signal: abort.signal, cache: "no-store" });
        if (resp.status === 401 || resp.status === 403) {
          opts.onStatus("unauthorized");
          return;
        }
        if (!resp.ok || !resp.body) throw new Error(`stream: HTTP ${resp.status}`);
        const parser = new SseParser();
        const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const e of parser.feed(value)) {
            if (e.event === "ready") {
              failures = 0;
              opts.onStatus("live");
            }
            opts.onEvent(e);
          }
          if (parser.lastId) lastId = parser.lastId;
          if (parser.retryMs !== null) retryMs = parser.retryMs;
        }
        // The server ends each connection after a few minutes on purpose: reconnect quietly.
      } catch (err) {
        if (abort.signal.aborted) return;
        failures += 1;
      }
      // Back off on repeated failures, up to 30 s; a clean close reconnects after `retry`.
      const wait = Math.min(retryMs * 2 ** Math.max(0, failures - 1), 30_000);
      if (failures > 0) opts.onStatus("reconnecting");
      await sleep(wait, abort.signal);
    }
  };
  void run();
  return () => abort.abort();
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => (clearTimeout(t), resolve()), { once: true });
  });
}
