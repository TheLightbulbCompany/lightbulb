import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guards the prod promotion gate in `lib/app.ts`.
 *
 * Merging to main deploys dev automatically and must NOT deploy prod. The gate
 * is `if: github.event_name == 'workflow_dispatch'` on every prod job — from
 * the stage's `jobSettings` for the stack Deploy jobs, and from an explicit
 * JsonPatch for `prod-DeployVercelProd` (a `GitHubActionStep`, which stage
 * `jobSettings` do not reach).
 *
 * cdk-pipelines-github's own drift guard only fires from inside deploy.yml
 * itself, i.e. post-merge, so these assertions are what catch a regression on
 * the PR. They pin the OUTCOME per job name rather than any line or index: the
 * prod stack set changes over time, and a newly added prod stack that arrives
 * ungated is exactly the failure worth failing on.
 */

const DEPLOY_YML = join(__dirname, "../../../.github/workflows/deploy.yml");
const PROMOTE_IF = "if: github.event_name == 'workflow_dispatch'";

/** Split the `jobs:` mapping into job-name -> raw block of that job's lines. */
function jobBlocks(yaml: string): Map<string, string> {
  const blocks = new Map<string, string>();
  let current: string | null = null;
  for (const line of yaml.slice(yaml.indexOf("\njobs:\n")).split("\n")) {
    const header = /^ {2}([A-Za-z0-9_-]+):\s*$/.exec(line);
    if (header) {
      current = header[1];
      blocks.set(current, "");
      continue;
    }
    // A column-0 key ends the jobs mapping (the workflow-level `permissions:`
    // block is emitted after it).
    if (/^\S/.test(line)) {
      current = null;
      continue;
    }
    if (current) blocks.set(current, `${blocks.get(current)}${line}\n`);
  }
  return blocks;
}

/** A job-level `if:` sits at 4 spaces; a step-level one at 8. */
const jobLevelIf = (block: string): string | undefined =>
  /^ {4}(if:.*)$/m.exec(block)?.[1];

describe("deploy.yml prod promotion gate", () => {
  const yaml = readFileSync(DEPLOY_YML, "utf8");
  const jobs = jobBlocks(yaml);
  const named = (prefix: string) => [...jobs.keys()].filter((n) => n.startsWith(prefix));

  it("still deploys on merge to main, and can be dispatched by hand", () => {
    const triggers = yaml.slice(yaml.indexOf("\non:\n"), yaml.indexOf("\nconcurrency:\n"));
    expect(triggers).toContain("push:");
    expect(triggers).toContain("- main");
    expect(triggers).toContain("workflow_dispatch:");
  });

  it("gates every prod job on workflow_dispatch", () => {
    const prodJobs = named("prod-");
    // Stack Deploys + the Vercel release. If this ever reads 0 the selector
    // broke and the rest of the suite would pass vacuously.
    expect(prodJobs.length).toBeGreaterThan(1);
    for (const job of prodJobs) {
      expect(jobLevelIf(jobs.get(job)!), `${job} is not gated`).toBe(PROMOTE_IF);
    }
  });

  it("gates the prod frontend release specifically", () => {
    // The user-visible half (isol8.co, www, admin). It is a GitHubActionStep,
    // so it is gated by patch rather than by stage jobSettings — a separate
    // mechanism, worth its own assertion.
    expect(jobs.has("prod-DeployVercelProd")).toBe(true);
    expect(jobLevelIf(jobs.get("prod-DeployVercelProd")!)).toBe(PROMOTE_IF);
  });

  it("leaves dev, synth and asset jobs ungated", () => {
    const auto = ["Build-Synth", ...named("dev-"), ...named("Assets-")];
    expect(auto.length).toBeGreaterThan(1);
    for (const job of auto) {
      expect(jobLevelIf(jobs.get(job)!), `${job} should run on every push`).toBeUndefined();
    }
  });

  it("keeps prod sequenced behind dev", () => {
    // The gate changes WHEN prod runs, never the DAG. Prod deploys still wait
    // on the dev stage inside the same run.
    const auth = jobs.get("prod-isol8-prod-auth-Deploy")!;
    expect(auth).toContain("- dev-isol8-dev-service-Deploy");
    expect(auth).toContain("- Build-Synth");
  });
});
