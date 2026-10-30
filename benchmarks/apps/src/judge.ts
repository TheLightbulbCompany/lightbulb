import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import type { Task } from "./adapters/types";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
export const JUDGE_MODEL = "claude-opus-5-5";
const MAX_IMAGES = 6;
const MAX_TEXT_CHARS = 80_000; // ~20k tokens at ~4 chars/token
const DIMS = ["bugs", "security", "code_quality", "schema_quality"];

/** Score 0..1 from the rubric's JSON: `score` if given, else the mean of the 0-10 dimensions / 10. Unparseable → 0. */
export function parseJudgeScore(text: string): number {
  const clamp = (x: number) => Math.min(1, Math.max(0, x));
  try {
    const o = JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1));
    if (typeof o.score === "number") return clamp(o.score);
    const xs = DIMS.map((d) => o[d]).filter((x): x is number => typeof x === "number");
    if (xs.length) return clamp(xs.reduce((a, b) => a + b, 0) / xs.length / 10);
  } catch { /* fall through */ }
  console.error(`[judge] unparseable reply, scoring 0: ${text.slice(0, 200)}`);
  return 0;
}

export function judgeRequest(task: Task, rubric: string, shots: Buffer[]) {
  const text = [
    rubric,
    "## This run",
    "You see the task prompt and up to six screenshots of the running app (no diff, source or test results). Score every dimension from that evidence; where a dimension cannot be seen, score what the screens imply.",
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
    if (await page.getByTestId("signup-name").count()) {
      shots.push(await page.screenshot());
      await page.getByTestId("signup-name").fill("Judge");
      await page.getByTestId("signup-email").fill(`judge-${Date.now().toString(36)}@example.test`);
      await page.getByTestId("signup-password").fill("Passw0rd!eval1");
      await page.getByTestId("signup-submit").click();
      await page.waitForURL((u) => !u.pathname.startsWith("/auth/"), { timeout: 20_000 }).catch(() => {});
    }
    for (const r of pages.filter((p) => !p.startsWith("/auth/"))) {
      if (shots.length >= MAX_IMAGES) break;
      await page.goto(r).catch(() => {});
      shots.push(await page.screenshot());
    }
    return shots;
  } finally { await browser.close(); }
}

/** LLM judge: screenshots + rubric + prompt → Claude → 0..1. Never throws; any failure scores 0 and is logged. */
export async function judge(url: string, dir: string, task: Task, o: { fetch?: typeof fetch; shots?: Buffer[]; apiKey?: string } = {}): Promise<number> {
  const key = o.apiKey ?? process.env.ANTHROPIC_API_KEY ?? "";
  try {
    if (!key) throw new Error("ANTHROPIC_API_KEY is not set");
    const shots = o.shots ?? (await screenshots(url, dir, task));
    const r = await (o.fetch ?? fetch)("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: JSON.stringify(judgeRequest(task, readFileSync(join(root, "judge/rubric.md"), "utf8"), shots)),
    });
    if (!r.ok) throw new Error(`Messages API ${r.status}: ${(await r.text()).slice(0, 200)}`);
    const body = (await r.json()) as { content?: { type: string; text?: string }[] };
    return parseJudgeScore((body.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join(""));
  } catch (e) {
    console.error(`[judge] ${task.id}: ${e instanceof Error ? e.message : e}; scoring 0`);
    return 0;
  }
}
