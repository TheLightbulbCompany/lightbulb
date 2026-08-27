import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { ApiStack } from "../lib/stacks/api-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../lib/domains";

/**
 * Static CDK context so `HostedZone.fromLookup` resolves without an AWS API
 * call. Mirrors the cached entry in `cdk.context.json` — a synth test with no
 * cache falls through to CDK's dummy zone and every Route53 assertion
 * silently compares against `example.com` instead of failing.
 *
 * Single copy on purpose: the only other place this value lives is
 * `cdk.context.json`, so changing ROOT_DOMAIN needs exactly one hand-added
 * cache entry there, not a sweep of the test suite.
 */
export const HOSTED_ZONE_CONTEXT: Record<string, unknown> = {
  "availability-zones:account=877352799272:region=us-east-1": [
    "us-east-1a",
    "us-east-1b",
    "us-east-1c",
    "us-east-1d",
    "us-east-1e",
    "us-east-1f",
  ],
  [`hosted-zone:account=877352799272:domainName=${ROOT_DOMAIN}:region=us-east-1`]:
    {
      Id: "/hostedzone/Z09248243AOUC775CDUI4",
      Name: `${ROOT_DOMAIN}.`,
    },
  // Mail lives on its own domain and SesInboundStack looks its zone up by
  // name, so a synth without this entry falls through to a dummy zone.
  [`hosted-zone:account=877352799272:domainName=${PLATFORM_MAIL_DOMAIN}:region=us-east-1`]:
    {
      Id: "/hostedzone/Z09913772LJJ6H4X9JJX0",
      Name: `${PLATFORM_MAIL_DOMAIN}.`,
    },
};

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
