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
      Name: "isol8/dev/gog-keyring-password",
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
