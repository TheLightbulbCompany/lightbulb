import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import type { Task } from "./adapters/types";
import { signUpOn } from "./probes";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
export const JUDGE_MODEL = "claude-opus-5-5";
const MAX_IMAGES = 6;
const MAX_TEXT_CHARS = 80_000; // ~20k tokens at ~4 chars/token
const DIMS = ["fit", "design", "clarity", "completeness"];

/** Score 0..1 from judge/rubric-screens.md's JSON: `score` if given, else the mean of the dimensions. Unparseable → NaN (no score). */
export function parseJudgeScore(text: string): number {
  const clamp = (x: number) => Math.min(1, Math.max(0, x));
  try {
    const o = JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1));
    if (typeof o.score === "number") return clamp(o.score);
    const xs = DIMS.map((d) => o[d]).filter((x): x is number => typeof x === "number");
    if (xs.length) return clamp(xs.reduce((a, b) => a + b, 0) / xs.length);
  } catch { /* fall through */ }
  console.error(`[judge] unparseable reply, no score: ${text.slice(0, 200)}`);
  return NaN;
}

export function judgeRequest(task: Task, rubric: string, shots: Buffer[]) {
  const text = [
    rubric,
    "## Task prompt",
    task.prompt,
  ].join("\n\n").slice(0, MAX_TEXT_CHARS);
  return {
    model: JUDGE_MODEL,
    max_tokens: 1024,
    messages: [{
      role: "user" as const,
      content: [
        ...shots.slice(0, MAX_IMAGES).map((b) => ({ type: "image", source: { type: "base64", media_type: "image/png", data: b.toString("base64") } })),
        { type: "text", text },
      ] as any[],
    }],
  };
}

/** Screenshot the contract's static pages, signed in as a fresh user when the app has sign-up. */
async function screenshots(url: string, dir: string, task: Task): Promise<Buffer[]> {
  const f = existsSync(join(dir, "contract.json")) ? join(dir, "contract.json") : join(root, "tasks", task.id, "contract.json");
  const routes: string[] = (JSON.parse(readFileSync(f, "utf8")).routes ?? []).filter((r: string) => !r.includes("[") && !r.startsWith("/api/"));
  const pages = [...routes.filter((r) => !r.startsWith("/auth/")), ...routes.filter((r) => r.startsWith("/auth/"))];
  if (!pages.includes("/")) pages.unshift("/");
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({ baseURL: url, viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    const shots: Buffer[] = [];
    await page.goto("/auth/sign-up").catch(() => {});
    if (await page.getByTestId("signup-name").count()) { // the task has accounts: judge the signed-in app
      shots.push(await page.screenshot());
      page.setDefaultTimeout(10_000);
      // Broken auth is the app's problem, not a judge failure: judge the signed-out screens.
      await signUpOn(page, "Judge").catch((e) => console.error(`[judge] ${task.id}: sign-up failed, judging signed-out screens: ${String(e).split("\n")[0]}`));
    }
    for (const r of pages.filter((p) => !p.startsWith("/auth/"))) {
      if (shots.length >= MAX_IMAGES) break;
      await page.goto(r).catch(() => {});
      shots.push(await page.screenshot());
    }
    return shots;
  } finally { await browser.close(); }
}

/**
 * LLM judge: screenshots + screens rubric + prompt → Claude → 0..1.
 * Never throws: any failure (no key, screenshots, API, unparseable reply) returns NaN and is logged; the runner records it.
 */
export async function judge(url: string, dir: string, task: Task, o: { fetch?: typeof fetch; shots?: Buffer[]; apiKey?: string; retryMs?: number } = {}): Promise<number> {
  const key = o.apiKey ?? process.env.ANTHROPIC_API_KEY ?? "";
  try {
    if (!key) throw new Error("ANTHROPIC_API_KEY is not set");
    const shots = o.shots ?? (await screenshots(url, dir, task));
    const body = JSON.stringify(judgeRequest(task, readFileSync(join(root, "judge/rubric-screens.md"), "utf8"), shots));
    const call = () => (o.fetch ?? fetch)("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body,
    });
    let r = await call();
    if (r.status === 429 || r.status >= 500) { // one retry on rate limit or overload
      await new Promise((ok) => setTimeout(ok, o.retryMs ?? 10_000));
      r = await call();
    }
    if (!r.ok) throw new Error(`Messages API ${r.status}: ${(await r.text()).slice(0, 200)}`);
    const reply = (await r.json()) as { content?: { type: string; text?: string }[] };
    return parseJudgeScore((reply.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join(""));
  } catch (e) {
    console.error(`[judge] ${task.id}: ${e instanceof Error ? e.message : e}; no score`);
    return NaN;
  }
}
