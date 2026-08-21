import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guards the S3 cdk.out transport patches in `lib/app.ts`.
 *
 * This file replaces an earlier retention-days guard. That guard existed
 * because cdk.out was shipped between jobs as a GitHub Actions artifact and
 * the ~33 MB upload exhausted the org's shared Actions+Packages storage,
 * which blocks EVERY deploy — including both Vercel jobs, since Vercel
 * deploys through this pipeline rather than its own Git integration. Capping
 * retention slowed the accumulation; moving the transport to S3 removes the
 * pipeline from that quota entirely, so retention no longer applies.
 *
 * cdk-pipelines-github hardcodes the artifact actions and exposes no option
 * to change them, so the swap is an RFC 6902 patch over the generated
 * workflow. These assertions pin the OUTCOME rather than any step index: a
 * construct upgrade that reorders steps must not silently reintroduce an
 * artifact upload.
 */

const DEPLOY_YML = join(__dirname, "../../../.github/workflows/deploy.yml");
const APP_TS = join(__dirname, "../lib/app.ts");

describe("deploy.yml cdk.out transport", () => {
  const yaml = readFileSync(DEPLOY_YML, "utf8");

  it("ships cdk.out over S3, never as an Actions artifact", () => {
    expect(yaml).not.toMatch(/uses: actions\/(upload|download)-artifact/);
  });

  it("uploads cdk.out exactly once", () => {
    const uploads = yaml.match(/aws s3 cp apps\/infra\/cdk\.out/g) ?? [];
    expect(uploads).toHaveLength(1);
  });

  it("gives every consumer a download", () => {
    // One per Assets-* job; the set grows and shrinks with the asset list, so
    // this asserts the relationship rather than a fixed count.
    const assetJobs = yaml.match(/^ {2}Assets-[A-Za-z0-9]+:$/gm) ?? [];
    // Match the step NAME: the generated YAML wraps long run: lines, so the
    // command itself is not reliably on one line.
    const downloads = yaml.match(/- name: Download cdk\.out from S3/g) ?? [];
    expect(assetJobs.length).toBeGreaterThan(0);
    expect(downloads).toHaveLength(assetJobs.length);
  });

  it("authenticates before touching S3 in Build-Synth", () => {
    const synthJob = yaml.match(/^ {2}Build-Synth:$[\s\S]*?(?=^ {2}[A-Za-z])/m)?.[0] ?? "";
    const authAt = synthJob.indexOf("Authenticate Via OIDC Role");
    const uploadAt = synthJob.indexOf("Upload cdk.out to S3");
    expect(authAt).toBeGreaterThan(-1);
    expect(uploadAt).toBeGreaterThan(authAt);
  });

  it("builds the temporary browser transport as WebRTC in dev and VNC in prod", () => {
    const source = readFileSync(APP_TS, "utf8");
    const dev = yaml.match(/^  dev-DeployVercelDev:$[\s\S]*?(?=^  [A-Za-z])/m)?.[0] ?? "";
    const prod = yaml.match(/^  prod-DeployVercelProd:$[\s\S]*?(?=^  [A-Za-z])/m)?.[0] ?? "";
    const devSource = source.match(/new GitHubActionStep\("DeployVercelDev", \{[\s\S]*?\{\s*name: "Build Frontend",[\s\S]*?\n\s*\},\n\s*\{/)?.[0] ?? "";
    const prodSource = source.match(/new GitHubActionStep\("DeployVercelProd", \{[\s\S]*?\{\s*name: "Build Frontend \(Production\)",[\s\S]*?\n\s*\},\n\s*\{/)?.[0] ?? "";
    expect(devSource).toMatch(/NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT:\s*"webrtc"/);
    expect(prodSource).toMatch(/NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT:\s*"vnc"/);
    expect(dev).toContain("NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT: webrtc");
    expect(prod).toContain("NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT: vnc");
  });
});
