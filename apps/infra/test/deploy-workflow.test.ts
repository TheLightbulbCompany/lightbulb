import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Merging to main must never deploy prod: only a person running the workflow does.
const yaml = readFileSync(join(__dirname, "../../../.github/workflows/deploy.yml"), "utf8");
const job = (name: string) => {
  const start = yaml.indexOf(`\n  ${name}:\n`);
  expect(start, `job ${name}`).toBeGreaterThan(-1);
  const rest = yaml.slice(start + 1);
  const next = rest.slice(1).search(/\n {2}\S/);
  return next === -1 ? rest : rest.slice(0, next + 1);
};

describe("deploy.yml", () => {
  it("deploys prod only on workflow_dispatch, after dev", () => {
    const prod = job("prod");
    expect(prod).toMatch(/^ {4}if: github\.event_name == 'workflow_dispatch'$/m);
    expect(prod).toMatch(/^ {4}needs: dev$/m);
    expect(prod).toContain("cdk deploy 'prod/*'");
    expect(prod).not.toContain("'dev/*'");
  });

  it("dev never deploys a prod stack", () => {
    const dev = job("dev");
    expect(dev).toContain("cdk deploy 'dev/*'");
    expect(dev).not.toContain("prod/");
  });

  it("only these two jobs exist, and runs queue instead of evicting each other", () => {
    const jobs = yaml.slice(yaml.indexOf("\njobs:\n"));
    expect([...jobs.matchAll(/^ {2}([A-Za-z0-9_-]+):$/gm)].map((m) => m[1])).toEqual(["dev", "prod"]);
    expect(yaml).toMatch(/group: lightbulb-deploy\n {2}cancel-in-progress: false\n {2}queue: max/);
  });
});
