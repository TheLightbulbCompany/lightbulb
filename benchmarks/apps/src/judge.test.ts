import { describe, expect, it } from "vitest";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Task } from "./adapters/types";
import { judge, judgeRequest, parseJudgeScore } from "./judge";

const task = (prompt = "Build a to-do app."): Task => ({ id: "b01-todo", kind: "internal-tool", type: "build", prompt, fixture: "blank", journeys: "j", probes: [], timeoutMin: 45 });
const png = Buffer.from("fake-png");

describe("judge", () => {
  it("parses a score from the screens rubric's JSON, clamps it, and gives NaN for junk", () => {
    expect(parseJudgeScore('{"fit": 0.8, "design": 0.6, "clarity": 0.7, "completeness": 0.9, "rationale": "x"}')).toBeCloseTo(0.75, 6);
    expect(parseJudgeScore('Sure.\n```json\n{"fit": 1, "design": 1, "clarity": 1, "completeness": 1, "score": 0.9}\n```')).toBeCloseTo(0.9, 6);
    expect(parseJudgeScore('{"score": 1.4}')).toBe(1);
    expect(parseJudgeScore("I cannot judge this")).toBeNaN();
    expect(parseJudgeScore('{"rationale": "no numbers"}')).toBeNaN();
  });

  it("uses the screens rubric, not Dyad's code rubric", () => {
    const text = judgeRequest(task(), readFileSync(join(import.meta.dirname, "../judge/rubric-screens.md"), "utf8"), [png]).messages[0].content.at(-1).text;
    expect(text).toMatch(/completeness/);
    expect(text).not.toMatch(/schema_quality/);
  });

  it("builds a request with at most 6 images, the rubric, the prompt, and capped text", () => {
    const body = judgeRequest(task("P".repeat(500_000)), "RUBRIC", Array(9).fill(png));
    expect(body.model).toBe("claude-opus-5-5");
    const content = body.messages[0].content;
    expect(content.filter((c: any) => c.type === "image")).toHaveLength(6);
    const text = content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("");
    expect(text).toContain("RUBRIC");
    expect(text).toContain("PPPP");
    expect(text.length).toBeLessThanOrEqual(80_000);
  });

  it("calls the Messages API with the key and returns the parsed score", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fake = (async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ content: [{ type: "text", text: '{"score": 0.5}' }] }), { status: 200 });
    }) as unknown as typeof fetch;
    const score = await judge("http://x", "/nowhere", task(), { fetch: fake, shots: [png, png], apiKey: "k-test" });
    expect(score).toBeCloseTo(0.5, 6);
    expect(calls[0].url).toBe("https://api.anthropic.com/v1/messages");
    const h = calls[0].init.headers as Record<string, string>;
    expect(h["x-api-key"]).toBe("k-test");
    expect(h["anthropic-version"]).toBe("2023-06-01");
    expect(JSON.parse(String(calls[0].init.body)).messages[0].content.filter((c: any) => c.type === "image")).toHaveLength(2);
  });

  it("an API error, a missing key or a screenshot crash gives NaN (no score) instead of crashing the run", async () => {
    let n = 0;
    const fail = (async () => { n++; return new Response("overloaded", { status: 529 }); }) as unknown as typeof fetch;
    expect(await judge("http://x", "/nowhere", task(), { fetch: fail, shots: [png], apiKey: "k", retryMs: 1 })).toBeNaN();
    expect(n).toBe(2); // one retry on 5xx
    n = 0;
    const bad = (async () => { n++; return new Response("bad", { status: 400 }); }) as unknown as typeof fetch;
    expect(await judge("http://x", "/nowhere", task(), { fetch: bad, shots: [png], apiKey: "k" })).toBeNaN();
    expect(n).toBe(1); // no retry on 4xx other than 429
    expect(await judge("http://x", "/nowhere", task(), { fetch: fail, shots: [png], apiKey: "" })).toBeNaN();
    expect(await judge("http://127.0.0.1:1", "/nowhere", task(), { fetch: fail, apiKey: "k", retryMs: 1 })).toBeNaN();
  }, 60_000);

  it("retries once on 429 and then scores", async () => {
    let n = 0;
    const flaky = (async () => (n++ === 0 ? new Response("slow down", { status: 429 }) : new Response(JSON.stringify({ content: [{ type: "text", text: '{"score": 0.7}' }] })))) as unknown as typeof fetch;
    expect(await judge("http://x", "/nowhere", task(), { fetch: flaky, shots: [png], apiKey: "k", retryMs: 1 })).toBeCloseTo(0.7, 6);
  });

  it("screenshots the contract's static pages, not dynamic or API routes", async () => {
    const srv = createServer((_q, r) => { r.writeHead(200, { "content-type": "text/html" }); r.end("<h1>hi</h1>"); });
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
    const dir = mkdtempSync(join(tmpdir(), "judge-"));
    writeFileSync(join(dir, "contract.json"), JSON.stringify({ routes: ["/a", "/b/[id]", "/api/x"], testids: [] }));
    let images = -1;
    const fake = (async (_u: string, init: RequestInit) => {
      images = JSON.parse(String(init.body)).messages[0].content.filter((c: any) => c.type === "image").length;
      return new Response(JSON.stringify({ content: [{ type: "text", text: '{"score": 1}' }] }));
    }) as unknown as typeof fetch;
    try {
      expect(await judge(`http://127.0.0.1:${(srv.address() as any).port}`, dir, task(), { fetch: fake, apiKey: "k" })).toBe(1);
      expect(images).toBe(2); // "/" and "/a"
    } finally { srv.close(); }
  }, 60_000);

  it("signs up a judge user first so protected pages are not just the sign-in screen", async () => {
    const seen: string[] = [];
    const srv = createServer((q, r) => {
      const signedIn = /sid=judge/.test(q.headers.cookie ?? "");
      if (q.url === "/auth/sign-up" && q.method === "POST") { r.writeHead(303, { location: "/a", "set-cookie": "sid=judge; Path=/" }); return r.end(); }
      if (q.url === "/auth/sign-up") { r.writeHead(200, { "content-type": "text/html" }); return r.end(`<form method=post><input data-testid=signup-name><input data-testid=signup-email><input data-testid=signup-password><button data-testid=signup-submit>x</button></form>`); }
      if (q.url === "/a") seen.push(signedIn ? "a-in" : "a-out");
      r.writeHead(200, { "content-type": "text/html" }); r.end("<h1>page</h1>");
    });
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
    const dir = mkdtempSync(join(tmpdir(), "judge-"));
    writeFileSync(join(dir, "contract.json"), JSON.stringify({ routes: ["/auth/sign-up", "/a"], testids: [] }));
    const fake = (async () => new Response(JSON.stringify({ content: [{ type: "text", text: '{"score": 1}' }] }))) as unknown as typeof fetch;
    try {
      expect(await judge(`http://127.0.0.1:${(srv.address() as any).port}`, dir, task(), { fetch: fake, apiKey: "k" })).toBe(1);
      expect(seen.at(-1)).toBe("a-in");
    } finally { srv.close(); }
  }, 60_000);
});
