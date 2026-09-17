import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// No YAML parser dependency for this package (see deploy-prod-promotion.test.ts /
// deploy-workflow-*.test.ts for the same raw-text convention) — plain string/regex
// assertions on the workflow's `on:` block are enough to pin the contract.
const ROOT = join(__dirname, "../../..");
const workflow = readFileSync(join(ROOT, ".github/workflows/build-hermes-image.yml"), "utf8");
const dockerfile = readFileSync(join(ROOT, "apps/infra/hermes/Dockerfile"), "utf8");
const HERMES_VERSION: { base?: { image?: string } } = JSON.parse(
  readFileSync(join(ROOT, "hermes-version.json"), "utf8"),
);

/** The `on:` trigger block only, so a path string mentioned elsewhere (e.g. a comment
 * about the build job) can't accidentally satisfy a trigger assertion. */
function onBlock(yaml: string): string {
  const start = yaml.indexOf("\non:");
  const end = yaml.indexOf("\njobs:");
  return yaml.slice(start, end === -1 ? undefined : end);
}

describe("build-hermes-image workflow", () => {
  const triggers = onBlock(workflow);

  it("is path-filtered on the hermes directory only", () => {
    expect(triggers).toContain("apps/infra/hermes/**");
    expect(triggers).not.toContain("apps/backend/**");
  });

  it("triggers on push to main and workflow_dispatch, never the spike branch", () => {
    expect(triggers).toMatch(/branches:\s*\n\s*-\s*main/);
    expect(triggers).toContain("workflow_dispatch");
    expect(workflow).not.toContain("spike/hermes-agentcore");
  });

  it("builds arm64 — AgentCore runtimes are arm64 only", () => {
    expect(workflow).toContain("linux/arm64");
  });

  it("pushes to the Hermes ECR repo and never writes the version pin", () => {
    expect(workflow).toContain("lightbulb/hermes-agent-arm64");
    expect(workflow).not.toContain("AGENTCORE_HERMES_IMAGE");
  });

  it("pushes both the short-sha tag and the hermes-version.json promotion tag (M4-R30)", () => {
    expect(workflow).toContain("hermes-version.json");
    expect(workflow).toMatch(/sha-\$\{?SHORT_SHA\}?/);
  });

  it("reuses the same OIDC builder role as the OpenClaw/Paperclip image builds", () => {
    expect(workflow).toContain("arn:aws:iam::877352799272:role/lightbulb-openclaw-image-builder");
  });

  it("the production image bakes the plugins, skills and managed overlay", () => {
    expect(dockerfile).toContain("COPY plugins/ /opt/hermes/plugins/");
    expect(dockerfile).toContain("COPY skills/ /opt/hermes-skills/");
    expect(dockerfile).toContain("COPY managed/config.yaml /etc/hermes/config.yaml");
  });

  it("pins its base by DIGEST, never a tag (M4-R31 — nousresearch/hermes-agent ships no numbered tags)", () => {
    // A tag pin (`:0.21.3`, `:latest`, anything without `@sha256:...`) must fail this.
    expect(dockerfile).toMatch(/^FROM nousresearch\/hermes-agent@sha256:[0-9a-f]{64}$/m);
    expect(dockerfile).not.toMatch(/^FROM nousresearch\/hermes-agent:\S/m);

    const fromLine = dockerfile.match(/^FROM (nousresearch\/hermes-agent@sha256:[0-9a-f]{64})$/m);
    expect(fromLine?.[1]).toBe(HERMES_VERSION.base?.image);
  });

  it("switches USER exactly once, with nothing after CMD", () => {
    const lines = dockerfile
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0 && !line.startsWith("#"));
    const userLines = lines.filter((line) => line.startsWith("USER "));
    expect(userLines).toEqual(["USER 1000"]);
    const cmdIndex = lines.findIndex((line) => line.startsWith("CMD "));
    expect(cmdIndex).toBeGreaterThan(-1);
    expect(cmdIndex).toBe(lines.length - 1);
  });

  it("the production image carries no diagnostic hatch and no ws echo", () => {
    const server = readFileSync(join(ROOT, "apps/infra/hermes/bridge/server.py"), "utf8");
    expect(server).not.toContain("diagnostic");
    expect(server).not.toContain("_ws_echo");
  });
});
