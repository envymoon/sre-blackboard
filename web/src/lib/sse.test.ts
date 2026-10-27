import { describe, expect, it } from "vitest";
import { openStream, SseParser, type SseEvent, type StreamStatus } from "./sse";

describe("SseParser", () => {
  it("splits events across chunk boundaries and remembers the id and retry", () => {
    const p = new SseParser();
    expect(p.feed("retry: 2000\nid: 0.0.0\nevent: ready\nda")).toEqual([]);
    expect(p.feed('ta: {}\n\n: ping\n\nid: 3.1.0\r\nevent: audit\r\ndata: {"seq":3}\r\n\r\n')).toEqual([
      { id: "0.0.0", event: "ready", data: "{}" },
      { id: "3.1.0", event: "audit", data: '{"seq":3}' },
    ]);
    expect(p.lastId).toBe("3.1.0");
    expect(p.retryMs).toBe(2000);
  });

  it("joins multi-line data and defaults the event name", () => {
    expect(new SseParser().feed("data: a\ndata: b\n\n")).toEqual([{ id: null, event: "message", data: "a\nb" }]);
  });
});

function body(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(text));
      c.close();
    },
  });
}

describe("openStream", () => {
  it("sends the token, and resumes from the last id after the server closes", async () => {
    const seen: (string | null)[] = [];
    const events: SseEvent[] = [];
    const statuses: StreamStatus[] = [];
    let n = 0;
    const fetchImpl = (async (_url: string, init: RequestInit) => {
      const h = init.headers as Record<string, string>;
      expect(h.Authorization).toBe("Bearer t0k");
      seen.push(h["Last-Event-ID"] ?? null);
      n += 1;
      const text =
        n === 1
          ? "retry: 1\nid: 0.0.0\nevent: ready\ndata: {}\n\nid: 5.2.1\nevent: activity\ndata: {}\n\n"
          : "retry: 1\nid: 5.2.1\nevent: ready\ndata: {}\n\n";
      return new Response(body(text), { status: 200 });
    }) as typeof fetch;
    const stop = openStream({
      url: "/s",
      token: "t0k",
      fetchImpl,
      onEvent: (e) => events.push(e),
      onStatus: (s) => statuses.push(s),
    });
    await new Promise((r) => setTimeout(r, 60));
    stop();
    expect(seen.slice(0, 2)).toEqual([null, "5.2.1"]);
    expect(events.map((e) => e.event).slice(0, 3)).toEqual(["ready", "activity", "ready"]);
    expect(statuses).toContain("live");
  });

  it("stops on 401", async () => {
    const statuses: StreamStatus[] = [];
    const fetchImpl = (async () => new Response("", { status: 401 })) as typeof fetch;
    openStream({ url: "/s", token: "x", fetchImpl, onEvent: () => {}, onStatus: (s) => statuses.push(s) });
    await new Promise((r) => setTimeout(r, 10));
    expect(statuses.at(-1)).toBe("unauthorized");
  });
});
