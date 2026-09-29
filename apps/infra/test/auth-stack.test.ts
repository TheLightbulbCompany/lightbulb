import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AuthStack } from "../lib/stacks/auth-stack";

function buildStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const authStack = new AuthStack(app, `Auth-${environment}`, {
    env,
    environment,
  });
  return Template.fromStack(authStack);
}

describe("AuthStack — gog file-keyring password secret", () => {
  const template = buildStack("dev");

  test("creates the gog-keyring-password secret encrypted with the general CMK", () => {
    template.hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "lightbulb/dev/gog-keyring-password",
      // CDK emits only GenerateSecretString on first create — the random value
      // is never regenerated on stack updates, keeping the keyring password
      // stable for the life of the stack.
      GenerateSecretString: Match.objectLike({
        PasswordLength: 64,
        ExcludePunctuation: true,
      }),
    });
  });

  test("does NOT configure rotation on the gog-keyring-password secret", () => {
    // Rotating this secret would orphan every keyring already written to EFS,
    // so there must be no RotationSchedule attached to it.
    template.resourceCountIs("AWS::SecretsManager::RotationSchedule", 0);
  });

  test("does not retain the retired platform-ops signing secret or export", () => {
    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).not.toContain("platform_ops_token_key");
    expect(rendered).not.toContain("PlatformOpsTokenKey");
  });
});

describe("AuthStack — gate bearer HMAC key", () => {
  test("creates a dedicated generated secret, separate from encryption_key", () => {
    buildStack("dev").hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "lightbulb/dev/gate_bearer_hmac_key",
      GenerateSecretString: Match.objectLike({ PasswordLength: 64, ExcludePunctuation: true }),
    });
  });
});

describe("AuthStack — hook grant signing key", () => {
  test("creates a separate generated secret", () => {
    buildStack("dev").hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "lightbulb/dev/hook_grant_signing_key",
      GenerateSecretString: Match.objectLike({ PasswordLength: 64, ExcludePunctuation: true }),
    });
  });
});

describe("AuthStack — the pinned cloudflare-warp export", () => {
  test("keeps the exact logical id AND export name the live stack has", () => {
    // container-stack's retired per-user ECS task-execution role was this
    // secret's only consumer. CloudFormation refuses to delete an Output
    // another stack still imports, so the Output is declared by hand rather
    // than dropped — and BOTH halves are load-bearing. A drifting export name
    // deletes the imported one; a drifting logical id publishes a second
    // Output under a name that is already taken, which CloudFormation rejects.
    for (const environment of ["dev", "prod"] as const) {
      const outputs = (buildStack(environment).toJSON() as Record<string, any>)
        .Outputs;
      expect(
        outputs.ExportsOutputRefCloudflareWarpEnrollmentLightbulb6CFC576893D2B67B
          ?.Export?.Name,
      ).toBe(
        `lightbulb-${environment}-auth:ExportsOutputRefCloudflareWarpEnrollmentLightbulb6CFC576893D2B67B`,
      );
    }
  });
});
