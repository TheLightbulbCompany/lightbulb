import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
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

describe("AuthStack secrets", () => {
  const template = buildStack("dev");

  test("keeps ses_ingest_secret, which the retired ses-inbound stack still imports", () => {
    template.hasResourceProperties("AWS::SecretsManager::Secret", { Name: "lightbulb/dev/ses_ingest_secret" });
  });

  test("drops the secrets Spark does not use", () => {
    const rendered = JSON.stringify(template.toJSON());
    for (const gone of ["gog-keyring-password", "ws_internal_secret", "network_proof_key", "metronome_api_key", "platform_ops_token_key"]) {
      expect(rendered).not.toContain(gone);
    }
  });
});
