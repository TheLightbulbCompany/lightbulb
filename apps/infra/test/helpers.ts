import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { ApiStack } from "../lib/stacks/api-stack";
import { NetworkStack } from "../lib/stacks/network-stack";

/**
 * Builds a real ApiStack wired to a real upstream NetworkStack. Mirrors the
 * wiring in isol8-stage.ts so the synthesized template matches what gets
 * deployed. Used by api-stack.test.ts (and any future ApiStack assertion
 * tests) so the ws-connections table + its GSIs are exercised as-deployed.
 */
export function synthApiStack(
  environment: "dev" | "prod" = "dev",
): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };

  const network = new NetworkStack(app, `Network-${environment}`, {
    env,
    environment,
  });

  // Throwaway containers table (owner_id PK, matching DatabaseStack) so the
  // authorizer's device-key registry read is granted + env-wired as-deployed.
  const support = new cdk.Stack(app, `Support-${environment}`, { env });
  const containersTable = new dynamodb.Table(support, "Containers", {
    partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
  });

  const api = new ApiStack(app, `Api-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    alb: network.alb,
    albHttpListenerArn: network.albHttpListenerArn,
    albSecurityGroup: network.albSecurityGroup,
    // Pass secret name + KMS key ARN as plain strings — same cross-stack
    // pattern as isol8-stage.ts (avoids KMS auto-grant cycles).
    paperclipServiceTokenKeySecretName: `isol8/${environment}/paperclip_service_token_key`,
    paperclipKmsKeyArn: `arn:aws:kms:us-east-1:877352799272:key/test-key`,
    wsInternalSecretSecretName: `isol8/${environment}/ws_internal_secret`,
    containersTable,
  });

  return Template.fromStack(api);
}
