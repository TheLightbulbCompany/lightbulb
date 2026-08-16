import { Match, Template } from "aws-cdk-lib/assertions";
import { synthApiStack } from "./helpers";

// Hoist a single ApiStack synthesis and reuse across tests.
const API_TEMPLATE = synthApiStack();

describe("ApiStack — ws-connections owner-keyed fan-out", () => {
  const template: Template = API_TEMPLATE;

  test("ws-connections has a by-owner-id GSI projecting userId + connectionType", () => {
    // Owner-keyed fan-out reads recipients from this GSI: given an ownerId
    // (org_id for orgs, user_id for personal), return every active connection
    // for that owner. INCLUDE projection carries the member userId (per-member
    // demux) + connectionType (chat filter excludes node/operator pipes).
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner-id",
          KeySchema: [{ AttributeName: "ownerId", KeyType: "HASH" }],
          Projection: {
            ProjectionType: "INCLUDE",
            NonKeyAttributes: Match.arrayWith(["userId", "connectionType"]),
          },
        }),
      ]),
    });
  });
});

describe("ApiStack — WS authorizer Clerk azp (SEC-8) wiring", () => {
  // The WS authorizer is the second door for Clerk tokens; without this env
  // the azp check silently no-ops (empty allowlist = off). Mode pins the
  // audit rollout default.
  test.each([
    ["dev", "https://dev.isol8.co,https://admin.dev.isol8.co,https://dev.company.isol8.co,http://localhost:3000"],
    ["prod", "https://isol8.co,https://admin.isol8.co,https://company.isol8.co"],
  ] as const)("%s authorizer gets azp allowlist in audit mode", (envName, parties) => {
    const template = envName === "dev" ? API_TEMPLATE : synthApiStack("prod");
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `isol8-${envName}-ws-authorizer`,
      Environment: {
        Variables: Match.objectLike({
          CLERK_AUTHORIZED_PARTIES: parties,
          CLERK_AZP_MODE: "audit",
        }),
      },
    });
  });
});

describe("ApiStack — API Gateway account CloudWatch role (access/execution logging)", () => {
  // The WS stage's accessLogSettings silently deliver NOTHING when the
  // account-level CloudWatch role is missing or points at a deleted IAM
  // role (observed live: cloudwatchRoleArn referenced a NoSuchEntity role
  // and both env's access-log groups stayed empty despite real traffic).
  // The role + AWS::ApiGateway::Account setting are ACCOUNT-level
  // singletons: only the prod stack may own them, so tearing down a dev
  // stack can never delete the role out from under every API in the
  // account.

  test("prod stack owns the account-level CloudWatch role", () => {
    const prod = synthApiStack("prod");
    prod.hasResourceProperties("AWS::IAM::Role", {
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: "apigateway.amazonaws.com" },
          }),
        ]),
      }),
      ManagedPolicyArns: Match.arrayWith([
        Match.objectLike({
          "Fn::Join": Match.arrayWith([
            Match.arrayWith([
              Match.stringLikeRegexp("AmazonAPIGatewayPushToCloudWatchLogs"),
            ]),
          ]),
        }),
      ]),
    });
    prod.resourceCountIs("AWS::ApiGateway::Account", 1);
  });

  test("dev stack does NOT own the account-level singleton", () => {
    API_TEMPLATE.resourceCountIs("AWS::ApiGateway::Account", 0);
  });
});
