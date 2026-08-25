import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guards the GitHub deployment environments declared in `lib/app.ts`.
 *
 * Merging to main deploys dev and then prod in one run. The prod jobs `needs:`
 * the dev jobs, which ORDERS them — it does not GATE them. `environment: prod`
 * is what lets GitHub hold those jobs for a required reviewer, so it is the
 * only thing standing between a merge and a production deploy.
 *
 * The drift check in `.github/workflows/infra-ci.yml` cannot catch its removal:
 * deleting `gitHubEnvironment` from app.ts and re-synthing leaves app.ts and
 * deploy.yml perfectly in sync with each other, and the gate silently gone.
 * These assertions are what fail instead.
 *
 * What this file CANNOT assert: whether the gate is armed. Protection rules
 * (required reviewers) are repo settings, not code — see Settings >
 * Environments > prod. With no reviewers configured, every job below still
 * deploys automatically; the wiring is necessary but not sufficient.
 */

const DEPLOY_YML = join(__dirname, "../../../.github/workflows/deploy.yml");

/** Job name -> its YAML block, for every job in the generated workflow. */
function parseJobs(yaml: string): Map<string, string> {
  // Anchor on `jobs:` so 2-space keys elsewhere (`on: push:`) are not jobs.
  const body = yaml.slice(yaml.indexOf("\njobs:\n"));
  const headers = [...body.matchAll(/^ {2}([A-Za-z0-9_.-]+):$/gm)];
  return new Map(
    headers.map((m, i) => [
      m[1],
      body.slice(m.index!, i + 1 < headers.length ? headers[i + 1].index! : body.length),
    ]),
  );
}

const environmentOf = (block: string) => /^ {4}environment: (.+)$/m.exec(block)?.[1];

describe("deploy.yml deployment environments", () => {
  const jobs = parseJobs(readFileSync(DEPLOY_YML, "utf8"));
  const named = (prefix: string) => [...jobs].filter(([name]) => name.startsWith(prefix));

  it("puts every prod job in the `prod` environment", () => {
    const prod = named("prod-");
    // 13 stacks + the Vercel production deploy. Asserted as a floor so adding
    // a stack does not fail the suite, but losing the whole stage does.
    expect(prod.length).toBeGreaterThanOrEqual(14);
    for (const [name, block] of prod) {
      expect(environmentOf(block), `${name} is missing environment: prod`).toBe("prod");
    }
  });

  it("puts every dev job in the `dev` environment", () => {
    // 10 stacks + the Vercel preview deploy. Dev is three stacks short of prod
    // because bedrock-gate, bedrock-meter and ses-inbound are account-scoped
    // singletons that exist only in the prod stage.
    const dev = named("dev-");
    expect(dev.length).toBeGreaterThanOrEqual(11);
    for (const [name, block] of dev) {
      expect(environmentOf(block), `${name} is missing environment: dev`).toBe("dev");
    }
  });

  it("leaves the shared synth and asset jobs ungated", () => {
    // Build-Synth and the Assets-* jobs are shared by BOTH stages: an
    // `environment:` here would make a prod approval a precondition for the
    // dev deploy, which is exactly the coupling the gate exists to remove.
    const shared = [...jobs].filter(([n]) => n === "Build-Synth" || n.startsWith("Assets-"));
    expect(shared.length).toBeGreaterThan(0);
    for (const [name, block] of shared) {
      expect(environmentOf(block), `${name} must not be gated`).toBeUndefined();
    }
  });

  it("orders prod after dev without relying on it as the gate", () => {
    // If this edge ever goes away the environment is the only thing left
    // holding prod, which is the intended design — but dev-first ordering is
    // load-bearing today, so a silent loss should be visible.
    const auth = jobs.get("prod-isol8-prod-auth-Deploy") ?? "";
    expect(auth).toContain("- dev-isol8-dev-auth-Deploy");
  });
});
