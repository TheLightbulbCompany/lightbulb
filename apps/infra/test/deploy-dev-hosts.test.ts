import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guards the dev hostnames' Vercel binding in `lib/app.ts`.
 *
 * A Vercel project domain with no git branch is a PRODUCTION domain: every
 * `vercel deploy --prod` re-points it at the new prod build. The dev hosts
 * were registered that way in the 2026-10 domain move, so after each prod
 * promotion dev.<root> served the prod bundle (prod Clerk, prod API) until the
 * next dev deploy. The dev job must bind every dev host it registers to the
 * preview branch, and the prod job must never do that to a prod host.
 */

const DEPLOY_YML = join(__dirname, "../../../.github/workflows/deploy.yml");

/** The raw lines of one job, from its header to the next job's header. */
function jobBlock(yaml: string, job: string): string {
  const start = yaml.indexOf(`\n  ${job}:\n`);
  expect(start, `job ${job} not found`).toBeGreaterThan(-1);
  const rest = yaml.slice(start + 1);
  const next = rest.slice(1).search(/\n {2}[A-Za-z0-9_-]+:\n/);
  return next === -1 ? rest : rest.slice(0, next + 1);
}

const stepNames = (block: string) =>
  [...block.matchAll(/^ {6}- name: (.+)$/gm)].map((m) => m[1]);

describe("deploy.yml — dev hostnames stay off production deploys", () => {
  const yaml = readFileSync(DEPLOY_YML, "utf8");
  const dev = jobBlock(yaml, "dev-DeployVercelDev");
  const names = stepNames(dev);

  it("binds every dev host it registers to the preview branch, before aliasing it", () => {
    const registered = names
      .map((n) => /^Register (\S+) project domain$/.exec(n)?.[1])
      .filter((h): h is string => Boolean(h));
    expect(registered.length).toBeGreaterThan(0);
    const firstAlias = names.findIndex((n) => n.startsWith("Alias to "));
    for (const host of registered) {
      const bind = names.indexOf(`Keep ${host} off production deploys`);
      expect(bind, `no branch binding for ${host}`).toBeGreaterThan(names.indexOf(`Register ${host} project domain`));
      expect(bind).toBeLessThan(firstAlias);
    }
    // The binding itself: a PATCH that sets the git branch.
    expect(dev).toMatch(/-X PATCH/);
    expect(dev).toMatch(/gitBranch/);
  });

  it("never binds a prod hostname to a branch", () => {
    const prod = jobBlock(yaml, "prod-DeployVercelProd");
    expect(stepNames(prod).some((n) => n.includes("off production deploys"))).toBe(false);
    expect(prod).not.toMatch(/gitBranch/);
  });
});
