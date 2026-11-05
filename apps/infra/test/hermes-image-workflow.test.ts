import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// No YAML parser dependency for this package (see deploy-prod-promotion.test.ts /
// deploy-workflow-*.test.ts for the same raw-text convention) — plain string/regex
// assertions on the workflow's `on:` block are enough to pin the contract.
const ROOT = join(__dirname, "../../..");
const workflow = readFileSync(join(ROOT, ".github/workflows/build-hermes-image.yml"), "utf8");
const dockerfile = readFileSync(join(ROOT, "apps/infra/hermes/Dockerfile"), "utf8");
const HERMES_VERSION: { base?: { image?: string; revision?: string }; patches?: string } = JSON.parse(
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

  it("is path-filtered on the hermes directory, which holds the skills source too", () => {
    // One path covers the image AND the bundled skills: their source is
    // apps/infra/hermes/skills-src, which build-skills.sh derives the baked tree from.
    expect(triggers).toContain("apps/infra/hermes/**");
    expect(triggers).not.toContain("apps/backend/**");
  });

  it("materializes the derived skills tree into the build context before docker build", () => {
    // The Dockerfile COPYs the DERIVED tree, and apps/infra/hermes/skills is gitignored —
    // so without this step COPY skills/ fails.
    const buildStep = workflow.indexOf("apps/infra/hermes/build-skills.sh");
    const dockerStep = workflow.indexOf("docker/build-push-action");
    expect(buildStep).toBeGreaterThan(-1);
    expect(buildStep).toBeLessThan(dockerStep);
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
    expect(workflow).not.toMatch(/>\s*\.?\/?hermes-version\.json/);
  });

  it("pushes exactly one sha-derived tag per build, and the push target never references the JSON pin (M4-R34)", () => {
    // Sha-derived: a static tag re-pushed every build either 409s under ECR tag
    // immutability or silently re-points the dev image with no human gate.
    expect(workflow).toContain("git rev-parse --short=7 HEAD");
    expect(workflow).toMatch(/tag=h-\$\{DATE\}-\$\{SHORT_SHA\}/);

    // The `tags:` line under the build-push-action step is the actual push target.
    // It must be built entirely from this job's own computed output, never from
    // hermes-version.json (that file is the promotion record a human edits after
    // the fact, not something this workflow reads or writes).
    const tagsLine = workflow.match(/^\s*tags:.*$/m)?.[0] ?? "";
    expect(tagsLine).not.toBe("");
    expect(tagsLine).toContain("steps.meta.outputs.tag");
    expect(tagsLine).not.toContain("hermes-version.json");
    expect(tagsLine).not.toMatch(/\.dev\.tag|\.prod\.tag/);
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

  describe("Lightbulb patch series (wave 1 lane H, spec 02 §4.2)", () => {
    const PATCH_DIR = join(ROOT, "apps/infra/hermes/hermes-patches");
    const series = readFileSync(join(PATCH_DIR, "SERIES"), "utf8").split("\n").filter(Boolean);
    const files = series.filter((line) => /^\d{4}-.*\.patch$/.test(line));

    it("hermes-version.json points at the SERIES and records the pin the base image was built from", () => {
      expect(HERMES_VERSION.patches).toBe("apps/infra/hermes/hermes-patches/SERIES");
      expect(series[0]).toBe(`pin=${HERMES_VERSION.base?.revision}`);
      expect(HERMES_VERSION.base?.revision).toBe("d5b3cad7b98b3bcd157ea8b585759b0bb4fb8a6d");
      expect(series[1]).toMatch(/^fork=TheLightbulbCompany\/hermes-agent-lightbulb@[0-9a-f]{40}$/);
    });

    it("SERIES lists every vendored patch exactly once, in number order, and every listed file exists", () => {
      const onDisk = readdirSync(PATCH_DIR).filter((f) => f.endsWith(".patch")).sort();
      expect(files).toEqual(onDisk);
      expect(files.map((f) => f.slice(0, 4))).toEqual(files.map((_, i) => String(i + 1).padStart(4, "0")));
      for (const f of files) expect(existsSync(join(PATCH_DIR, f))).toBe(true);
      expect(series.length).toBe(files.length + 2); // pin, fork, then the files: nothing else
    });

    it("the Dockerfile applies the series one patch at a time, --check before apply, after git is installed and before the bridge", () => {
      const aptGit = dockerfile.search(/apt-get install[^\n]*\\\n\s*git\b/);
      const check = dockerfile.indexOf('git apply --check "/tmp/hermes-patches/$p"');
      const apply = dockerfile.indexOf('; \\\n      git apply "/tmp/hermes-patches/$p"');
      const bridge = dockerfile.indexOf("COPY bridge /opt/bridge/bridge");
      expect(aptGit).toBeGreaterThan(-1);
      expect(check).toBeGreaterThan(aptGit);
      expect(apply).toBeGreaterThan(check);
      expect(bridge).toBeGreaterThan(apply);
      // in SERIES order, never a glob: a later patch edits a file an earlier one creates
      expect(dockerfile).toContain("/tmp/hermes-patches/SERIES");
      const code = dockerfile.split("\n").filter((line) => !line.trim().startsWith("#")).join("\n");
      expect(code).not.toMatch(/git apply[^\n]*\*\.patch/);
      // under `set -e`, `false && x` does not stop the loop: --check must be its own command
      expect(code).not.toMatch(/git apply --check[^\n]*&&/);
    });

    it("the build runs the patched-image smoke and leaves no patch files in the layer", () => {
      expect(dockerfile).toContain("python3 /tmp/patched_hermes_smoke.py");
      expect(dockerfile).toContain("rm -rf /tmp/hermes-patches /tmp/patched_hermes_smoke.py");
      const smoke = readFileSync(join(ROOT, "apps/infra/hermes/tests/image_smoke/patched_hermes_smoke.py"), "utf8");
      expect(smoke).toContain('print("patched hermes ok")');
    });
  });
});
