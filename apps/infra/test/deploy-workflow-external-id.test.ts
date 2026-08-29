import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guards `NoExternalIdCredentials` in `lib/app.ts`.
 *
 * CDK bootstrap v32 rewrote the trust policy on all four `cdk-hnb659fds-*`
 * roles to add `"Condition": {"Null": {"sts:ExternalId": "true"}}` — assume is
 * permitted ONLY when NO ExternalId is supplied. cdk-pipelines-github hardcodes
 * `roleExternalId: 'Pipeline'` on its "Assume CDK Deploy Role" step, so a
 * re-bootstrapped account AccessDenies every deploy and asset job in BOTH
 * environments at that step.
 *
 * The fix is a delegating `AwsCredentialsProvider` that strips the key. These
 * assertions pin the OUTCOME: nothing may reintroduce an external id, and the
 * strip must not have taken the role assumption with it.
 */

const DEPLOY_YML = join(__dirname, "../../../.github/workflows/deploy.yml");

describe("deploy.yml CDK role assumption", () => {
  const yaml = readFileSync(DEPLOY_YML, "utf8");

  it("never sends an sts ExternalId", () => {
    expect(yaml).not.toMatch(/role-external-id/);
  });

  it("still assumes the bootstrap deploy role in every deploy job", () => {
    const assumeSteps = yaml.match(/- name: Assume CDK Deploy Role/g) ?? [];
    const deployRoles = yaml.match(/role-to-assume: \S*cdk-hnb659fds-deploy-role\S*/g) ?? [];
    expect(assumeSteps.length).toBeGreaterThan(0);
    expect(deployRoles).toHaveLength(assumeSteps.length);
  });
});
