import { describe, expect, it } from "vitest";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runControls, writeControls } from "./controls";
import { writeReport } from "./report";

describe("writeControls", () => {
  it("a serve that throws still writes a red controls.json, and the report withholds scores", async () => {
    const out = mkdtempSync(join(tmpdir(), "ctl-"));
    const c = await writeControls(out, { serve: async () => { throw new Error("docker is down"); } });
    expect(c.referencePassed).toBe(false);
    expect(JSON.parse(readFileSync(join(out, "controls.json"), "utf8"))).toEqual({ referencePassed: false, brokenFailedAllProbes: false, judgeOnReference: 0 });
    expect(readFileSync(join(out, "controls.log"), "utf8")).toContain("docker is down");
    expect(writeReport(out)).toContain("Scores withheld");
    rmSync(out, { recursive: true, force: true });
  });
});

// Serves both control apps (Docker + pnpm + Chrome); run with EVAL_IT=1. The judge is faked.
describe.skipIf(!process.env.EVAL_IT)("controls (b01-todo)", () => {
  let result: Awaited<ReturnType<typeof runControls>>;
  const run = async () => (result ??= await runControls({ judge: async () => ({ score: 1, reason: "fake" }) }));

  it("reference app scores 100 on its journeys and probes", async () => {
    const r = await run();
    expect(r.detail).toMatch(/journeys .*PASS/);
    expect(r.detail).not.toMatch(/reference:[^\n]*FAIL/);
    expect(r.referencePassed, r.detail).toBe(true);
    expect(r.judgeOnReference).toBe(1);
  }, 25 * 60_000);

  it("broken twin fails every probe", async () => {
    const r = await run();
    expect(r.brokenFailedAllProbes, r.detail).toBe(true);
  }, 25 * 60_000);

  it("unauthenticated_route_probe_fails_on_the_broken_twin", async () => {
    const r = await run();
    expect(r.detail).toMatch(/broken.*unauth-tasks: FAIL/s);
  }, 25 * 60_000);
});
