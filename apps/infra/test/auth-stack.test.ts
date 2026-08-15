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

  test("retains the platform-ops secret and live export during removal phase one", () => {
    template.hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "isol8/dev/platform_ops_token_key",
    });
    template.hasOutput("ExportsOutputRefPlatformOpsTokenKeyAF16BB4E545E5253", {
      Export: {
        Name: "Auth-dev:ExportsOutputRefPlatformOpsTokenKeyAF16BB4E545E5253",
      },
    });
  });
});
