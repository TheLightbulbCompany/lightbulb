import { describe, expect, it } from "vitest";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Task } from "./adapters/types";
import { judge, judgeRequest, parseJudgeScore } from "./judge";

const task = (prompt = "Build a to-do app."): Task => ({ id: "b01-todo", kind: "internal-tool", type: "build", prompt, fixture: "blank", journeys: "j", probes: [], timeoutMin: 45 });
const png = Buffer.from("fake-png");

describe("judge", () => {
  it("parses a score from the rubric's JSON, clamps it, and gives 0 for junk", () => {
    expect(parseJudgeScore('{"bugs": 8, "security": 6, "code_quality": 7, "schema_quality": 9, "rationale": "x"}')).toBeCloseTo(0.75, 6);
    expect(parseJudgeScore('Sure.\n```json\n{"bugs": 10, "security": 10, "code_quality": 10, "schema_quality": 10}\n```')).toBe(1);
    expect(parseJudgeScore('{"score": 0.4}')).toBeCloseTo(0.4, 6);
    expect(parseJudgeScore('{"bugs": 14, "security": 10, "code_quality": 10, "schema_quality": 10}')).toBe(1);
    expect(parseJudgeScore("I cannot judge this")).toBe(0);
    expect(parseJudgeScore('{"rationale": "no numbers"}')).toBe(0);
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
      return new Response(JSON.stringify({ content: [{ type: "text", text: '{"bugs": 5, "security": 5, "code_quality": 5, "schema_quality": 5}' }] }), { status: 200 });
    }) as unknown as typeof fetch;
    const score = await judge("http://x", "/nowhere", task(), { fetch: fake, shots: [png, png], apiKey: "k-test" });
    expect(score).toBeCloseTo(0.5, 6);
    expect(calls[0].url).toBe("https://api.anthropic.com/v1/messages");
    const h = calls[0].init.headers as Record<string, string>;
    expect(h["x-api-key"]).toBe("k-test");
    expect(h["anthropic-version"]).toBe("2023-06-01");
    expect(JSON.parse(String(calls[0].init.body)).messages[0].content.filter((c: any) => c.type === "image")).toHaveLength(2);
  });

  it("an API error or a missing key scores 0 instead of crashing the run", async () => {
    const fail = (async () => new Response("overloaded", { status: 529 })) as unknown as typeof fetch;
    expect(await judge("http://x", "/nowhere", task(), { fetch: fail, shots: [png], apiKey: "k" })).toBe(0);
    expect(await judge("http://x", "/nowhere", task(), { fetch: fail, shots: [png], apiKey: "" })).toBe(0);
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
});
