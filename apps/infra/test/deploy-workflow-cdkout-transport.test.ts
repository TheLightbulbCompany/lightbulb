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
 *
 * The transport also ships cdk.out as a single tar.gz rather than a recursive
 * copy of its ~6,376 files -- see the tarball guard below for why that is a
 * cost invariant and not a style choice.
 */

const DEPLOY_YML = join(__dirname, "../../../.github/workflows/deploy.yml");

describe("deploy.yml cdk.out transport", () => {
  const yaml = readFileSync(DEPLOY_YML, "utf8");

  it("ships cdk.out over S3, never as an Actions artifact", () => {
    expect(yaml).not.toMatch(/uses: actions\/(upload|download)-artifact/);
  });

  it("uploads cdk.out exactly once", () => {
    const uploads = yaml.match(/tar -czf cdk\.out\.tgz -C apps\/infra cdk\.out/g) ?? [];
    expect(uploads).toHaveLength(1);
  });

  it("GUARD: moves cdk.out as one tarball, never as loose objects", () => {
    // The cost guard, not a style preference. cdk.out is ~6,376 files and the
    // bootstrap assets bucket is the account's only `aws:kms` bucket with
    // `BucketKeyEnabled=false`, so a recursive copy bills one KMS Decrypt per
    // file per download, times 39 asset jobs, times every run: measured at
    // $223.81/mo of KMS plus $52 of S3 requests over 2026-08-08..09-07. A
    // single object makes that one Decrypt per job. Anyone reverting to
    // `--recursive` reintroduces ~$300/mo silently, so it fails here instead.
    // Synth uploads ~42 slice objects once per run (a recursive PUT is fine);
    // what must never be recursive is the per-job DOWNLOAD.
    expect(yaml).not.toMatch(/aws s3 cp s3:\/\/\S*gha-cdk-out\S*[\s\S]{0,200}?--recursive/);
    expect(yaml).toContain("tar -xzf cdk.out.tgz");
  });

  it("GUARD: an S3 download fetches the base and its own asset slice, never the whole tarball", () => {
    // DEPLOY_RUNNER moved to GitHub-hosted runners on 2026-11-05, so the host
    // cache never hits and every S3 GET is internet egress: 41 jobs x the full
    // ~40 MB tarball was 75-98 GB/day, ~$260/mo (docs/ops/2026-11-10-cost-cuts.md).
    const assetJobs = yaml.match(/^ {2}Assets-[A-Za-z0-9]+:$/gm) ?? [];
    expect(yaml.match(/\/slices\/base\.tgz cdk\.out\.tgz/g) ?? []).toHaveLength(assetJobs.length);
    expect(yaml.match(/\/slices\/\$id\.tgz" asset\.tgz/g) ?? []).toHaveLength(assetJobs.length);
    expect(yaml).not.toMatch(/aws s3 cp s3:\/\/\S*\/cdk\.out\.tgz/);
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

  it("GUARD: every download reads the host cache before S3", () => {
    // The deploy runners sit outside AWS, so each S3 GET of the ~34 MB
    // tarball is internet egress: 43 jobs per run came to ~817 GB/mo (~$70).
    // Synth seeds a per-run host cache; asset jobs must check it first.
    const assetJobs = yaml.match(/^ {2}Assets-[A-Za-z0-9]+:$/gm) ?? [];
    const cacheHits = yaml.match(/echo "cdk\.out: host cache hit"/g) ?? [];
    expect(cacheHits).toHaveLength(assetJobs.length);
    expect(yaml).toMatch(/lightbulb-cdkout\/\$\{\{ github\.run_id \}\}-\$\{\{\s+github\.run_attempt \}\}/);
  });

  it("authenticates before touching S3 in Build-Synth", () => {
    const synthJob = yaml.match(/^ {2}Build-Synth:$[\s\S]*?(?=^ {2}[A-Za-z])/m)?.[0] ?? "";
    const authAt = synthJob.indexOf("Authenticate Via OIDC Role");
    const uploadAt = synthJob.indexOf("Upload cdk.out to S3");
    expect(authAt).toBeGreaterThan(-1);
    expect(uploadAt).toBeGreaterThan(authAt);
  });
});
