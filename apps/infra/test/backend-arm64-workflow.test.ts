import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(__dirname, "../../..");
const smoke = readFileSync(join(root, ".github/workflows/backend-arm64.yml"), "utf8");
const deploy = readFileSync(join(root, ".github/workflows/deploy.yml"), "utf8");

describe("backend ARM64 build and deployment coherence", () => {
  it("tests a loaded ARM64 image and native entrypoints with networking disabled", () => {
    expect(smoke).toContain("platforms: linux/arm64");
    expect(smoke).toContain("load: true");
    expect(smoke).toContain("push: false");
    expect(smoke).toContain("docker image inspect");
    expect(smoke).toContain("--network none --platform linux/arm64");
    expect(smoke).toContain("--entrypoint /app/.venv/bin/python");
    expect(smoke).toContain("backend-image-smoke.py");
  });

  it("the candidate check has no deployment or secret-reading authority", () => {
    expect(smoke).toContain("contents: read");
    expect(smoke).not.toMatch(/id-token|configure-aws|role-to-assume|secrets\./);
    expect(smoke).not.toContain("cdk deploy");
    expect(smoke).toContain("pull_request:");
    expect(smoke).toContain("workflow_dispatch:");
  });

  it("every generated Docker asset job enables ARM64 before publishing", () => {
    const jobs = [...deploy.matchAll(/^  (Assets-DockerAsset\d+):\n([\s\S]*?)(?=^  \S|$(?![\s\S]))/gm)];
    expect(jobs.length).toBeGreaterThan(0);
    for (const [, , job] of jobs) {
      const qemu = job.indexOf("docker/setup-qemu-action@v3");
      const publish = job.indexOf("- id: Publish");
      expect(qemu).toBeGreaterThan(-1);
      expect(qemu).toBeLessThan(publish);
      expect(job).toContain("platforms: arm64");
      expect(job.indexOf("Authenticate Via OIDC Role")).toBeLessThan(job.indexOf("Download cdk.out from S3"));
    }
  });
});
