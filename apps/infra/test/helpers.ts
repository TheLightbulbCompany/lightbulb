import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { ApiStack } from "../lib/stacks/api-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import {
  LEGACY_MAIL_DOMAIN,
  LEGACY_ROOT_DOMAIN,
  PLATFORM_MAIL_DOMAIN,
  ROOT_DOMAIN,
} from "../lib/domains";

/**
 * Static CDK context so `HostedZone.fromLookup` resolves without an AWS API
 * call. Mirrors the cached entry in `cdk.context.json` — a synth test with no
 * cache falls through to CDK's dummy zone and every Route53 assertion
 * silently compares against `example.com` instead of failing.
 *
 * Carries an entry per distinct zone the stacks look up. The product domain,
 * the mail domain and the legacy domain were the SAME string until the rebrand
 * split them, so a single entry used to be enough; once they diverge, a stack
 * looking up a zone with no entry falls through to CDK's dummy zone and every
 * Route53 assertion silently compares against `example.com` rather than failing.
 * Deduped, so while the values coincide this is still one entry.
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
  // Every zone any stack may look up, by its REAL id. A two-way ternary here
  // silently handed the mail domain the product domain's zone; an explicit map
  // cannot. Domains are deduped because these constants overlap during the move.
  ...Object.fromEntries(
    [
      // Both LEGACY_* constants are "" now that those domains are retired;
      // an empty entry would synthesize a `domainName=` context key that
      // matches no lookup.
      ...new Set(
        [
          ROOT_DOMAIN,
          PLATFORM_MAIL_DOMAIN,
          LEGACY_ROOT_DOMAIN,
          LEGACY_MAIL_DOMAIN,
        ].filter(Boolean),
      ),
    ].map((domain) => [
      `hosted-zone:account=877352799272:domainName=${domain}:region=us-east-1`,
      {
        Id:
          {
            "flickmeon.ai": "/hostedzone/Z03529981ZCUW1R3FM806",
            "isol8.co": "/hostedzone/Z09248243AOUC775CDUI4",
            "thelightbulb.company": "/hostedzone/Z09913772LJJ6H4X9JJX0",
          }[domain] ?? "/hostedzone/Z09248243AOUC775CDUI4",
        Name: `${domain}.`,
      },
    ]),
  ),
};

/**
 * Builds a real ApiStack wired to a real upstream NetworkStack. Mirrors the
 * wiring in lightbulb-stage.ts so the synthesized template matches what gets
 * deployed. Used by api-stack.test.ts (and any future ApiStack assertion
 * tests) so the ws-connections table + its GSIs are exercised as-deployed.
 */
export function synthApiStack(
  environment: "dev" | "prod" = "dev",
): Template {
  return Template.fromStack(buildApiStack(environment));
}

/**
 * The same wiring, returning the STACK rather than its template — for the few
 * assertions that need to compare a `public readonly` field against the
 * resource it names (the messaging names the observability dashboard imports
 * are literals, so nothing else would catch them drifting).
 */
export function buildApiStack(
  environment: "dev" | "prod" = "dev",
): ApiStack {
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
    albV2: network.albV2,
    albHttpListenerArn: network.albHttpListenerArn,
    albSecurityGroup: network.albSecurityGroup,
    clerkIssuerSecretName: `lightbulb/${environment}/clerk_issuer`,
    // Pass secret name + KMS key ARN as plain strings — same cross-stack
    // pattern as lightbulb-stage.ts (avoids KMS auto-grant cycles).
    paperclipKmsKeyArn: `arn:aws:kms:us-east-1:877352799272:key/test-key`,
    wsInternalSecretSecretName: `isol8/${environment}/ws_internal_secret`,
    containersTableName: containersTable.tableName,
  });

  return api;
}
