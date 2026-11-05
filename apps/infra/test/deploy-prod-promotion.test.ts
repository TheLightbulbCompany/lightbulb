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

  it("queues pending runs instead of evicting them", () => {
    // `cancel-in-progress: false` only protects a run that is already
    // in_progress. A PENDING run is evicted by the next arrival unless the
    // group opts into a deeper queue, and prod ships only by dispatch — so a
    // merge landing while a promotion waits for a runner silently cancels the
    // promotion (three times on 2026-09-07, each with zero jobs run).
    const block = yaml.slice(yaml.indexOf("\nconcurrency:\n"), yaml.indexOf("\njobs:\n"));
    expect(block).toContain("group: lightbulb-deploy");
    // One group for every event, so two runs can never deploy the same stacks
    // at once. queue:max only changes what happens to the ones waiting.
    expect(block).toContain("queue: max");
    // GitHub rejects the workflow outright if these two are combined.
    expect(block).toContain("cancel-in-progress: false");
    expect(block).not.toContain("cancel-in-progress: true");
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
    // The holding-company site is a second GitHubActionStep in the prod stage,
    // gated by its own patch line — a forgotten patch ships it on merge.
    expect(jobs.has("prod-DeployCompanyProd")).toBe(true);
    expect(jobLevelIf(jobs.get("prod-DeployCompanyProd")!)).toBe(PROMOTE_IF);
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
    const auth = jobs.get("prod-lightbulb-prod-auth-Deploy")!;
    expect(auth).toContain("- dev-lightbulb-dev-service-Deploy");
    expect(auth).toContain("- Build-Synth");
  });
});

describe("desktop-build.yml prod promotion gate", () => {
  const yaml = readFileSync(join(__dirname, "../../../.github/workflows/desktop-build.yml"), "utf8");
  const jobs = jobBlocks(yaml);
  // #4597 (hosted Mac fallback): a dispatch with hosted_runner runs test-desktop
  // first, so prod `needs` it. `always()` is only there so a SKIPPED test job (a
  // normal dispatch on the signing Mac) doesn't skip prod; prod still never runs
  // on anything but a manual dispatch, and never after a failed or cancelled test.
  const DESKTOP_PROMOTE_IF =
    "if: always() && github.event_name == 'workflow_dispatch' && " +
    "(needs.test-desktop.result == 'success' || needs.test-desktop.result == 'skipped')";

  it("gates the entire production desktop job on manual dispatch", () => {
    expect(jobs.has("build-desktop-prod")).toBe(true);
    expect(jobLevelIf(jobs.get("build-desktop-prod")!)).toBe(DESKTOP_PROMOTE_IF);
  });

  it("builds nothing on a push: there is no development app", () => {
    // Owner decision 2026-10-04 (#3692): no dev desktop builds. A pull request
    // runs the tests, a manual dispatch builds prod, a push to main does neither.
    const triggers = yaml.slice(yaml.indexOf("\n'on':\n"), yaml.indexOf("\npermissions:\n"));
    expect(triggers).not.toContain("push:");
    expect(triggers).toContain("pull_request:");
    expect(triggers).toContain("workflow_dispatch:");
    expect(jobs.has("build-desktop-dev")).toBe(false);
  });

  it("runs the Rust tests on pull requests without building", () => {
    expect(jobs.has("test-desktop")).toBe(true);
    // Also on a hosted_runner dispatch (#4597), where it runs on a fresh GitHub-hosted
    // Mac, not the persistent signing one.
    expect(jobLevelIf(jobs.get("test-desktop")!)).toBe(
      "if: github.event_name == 'pull_request' || inputs.hosted_runner == true",
    );
    expect(jobs.get("test-desktop")).toMatch(/cargo test --manifest-path\n\s+apps\/desktop\/src-tauri\/Cargo.toml --lib(?:\r?\n|$)/);
    expect(jobs.get("test-desktop")).toMatch(/--example local_session_probe/);
    // The workflow-level token is `contents: write` for the signing/publishing
    // build jobs; a PR-triggered job running fork-authored code on the
    // persistent self-hosted Mac must not inherit it.
    expect(jobs.get("test-desktop")).toContain("permissions:\n      contents: read");
  });

  it("builds the production app on dispatch with no dependency on the development job", () => {
    // Prod used to need the dev job only to inherit a warm Rust cache, which
    // now lives on the Mac. The dev job is gone (#3692). Its only dependency is
    // the test job (#4597).
    const prod = jobs.get("build-desktop-prod")!;
    expect(jobLevelIf(prod)).toBe(DESKTOP_PROMOTE_IF);
    expect(prod).toMatch(/^ {4}needs: test-desktop$/m);
    expect(prod).not.toContain("build-desktop-dev");
  });

  it("runs Infra CI for desktop-workflow-only pull requests", () => {
    const ci = readFileSync(join(__dirname, "../../../.github/workflows/infra-ci.yml"), "utf8");
    const triggers = ci.slice(ci.indexOf("\non:\n"), ci.indexOf("\nconcurrency:\n"));
    expect(triggers).toContain("pull_request:");
    expect(triggers).toContain("- '.github/workflows/desktop-build.yml'");
  });
});
