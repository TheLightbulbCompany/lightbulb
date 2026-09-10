import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ApiStack } from "../lib/stacks/api-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ROOT_DOMAIN } from "../lib/domains";
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
    [
      "dev",
      `https://dev.${ROOT_DOMAIN},https://admin.dev.${ROOT_DOMAIN},https://dev.company.${ROOT_DOMAIN},http://localhost:3000`,
    ],
    [
      "prod",
      `https://${ROOT_DOMAIN},https://www.${ROOT_DOMAIN},https://admin.${ROOT_DOMAIN},https://company.${ROOT_DOMAIN}`,
    ],
  ] as const)("%s authorizer gets azp allowlist in audit mode", (envName, parties) => {
    const template = envName === "dev" ? API_TEMPLATE : synthApiStack("prod");
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `lightbulb-${envName}-ws-authorizer`,
      Environment: {
        Variables: Match.objectLike({
          CLERK_AUTHORIZED_PARTIES: parties,
          CLERK_AZP_MODE: "audit",
        }),
      },
    });
  });
});


describe("ApiStack — production browser CORS origins", () => {
  test("prod API Gateway CORS accepts apex and www frontend origins", () => {
    const prod = synthApiStack("prod");
    prod.hasResourceProperties("AWS::ApiGatewayV2::Api", {
      CorsConfiguration: Match.objectLike({
        AllowOrigins: [`https://${ROOT_DOMAIN}`, `https://www.${ROOT_DOMAIN}`],
      }),
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

describe("ApiStack — browser-view CloudFront door", () => {
  // The distribution only synthesizes when cert + zone are provided (same
  // gate as the HTTP API custom domain), so this needs its own harness —
  // the shared one deliberately passes neither.
  function synthWithDomain(environment: "dev" | "prod"): Template {
    const app = new cdk.App();
    const env = { account: "877352799272", region: "us-east-1" };
    const network = new NetworkStack(app, `Network-${environment}`, { env, environment });
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
      certificate: acm.Certificate.fromCertificateArn(
        support,
        "Cert",
        "arn:aws:acm:us-east-1:877352799272:certificate/test-cert",
      ),
      hostedZone: route53.HostedZone.fromHostedZoneAttributes(support, "Zone", {
        hostedZoneId: "Z0TEST",
        zoneName: ROOT_DOMAIN,
      }),
      paperclipServiceTokenKeySecretName: `isol8/${environment}/paperclip_service_token_key`,
      paperclipKmsKeyArn: `arn:aws:kms:us-east-1:877352799272:key/test-key`,
      wsInternalSecretSecretName: `isol8/${environment}/ws_internal_secret`,
      containersTableName: containersTable.tableName,
    });
    return Template.fromStack(api);
  }

  test("distribution targets the ALB VPC origin on 8081, never :80", () => {
    // :80's default action is the whole backend target group; the path cap
    // lives only on the 8081 listener. A distribution pointed at 80 would
    // silently hand CloudFront the entire FastAPI surface.
    const template = synthWithDomain("dev");
    template.hasResourceProperties("AWS::CloudFront::VpcOrigin", {
      VpcOriginEndpointConfig: Match.objectLike({
        HTTPPort: 8081,
        OriginProtocolPolicy: "http-only",
      }),
    });
    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        Aliases: [`bv-dev.${ROOT_DOMAIN}`],
        DefaultCacheBehavior: Match.objectLike({
          // CachingDisabled + AllViewer managed policy ids — a cached or
          // header-stripped relay is a broken relay.
          CachePolicyId: "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
          OriginRequestPolicyId: "216adef6-5c7f-47e4-b989-5492eafa07d3",
          ViewerProtocolPolicy: "https-only",
        }),
      }),
    });
  });

  test("prod alias is bv.<root domain>", () => {
    synthWithDomain("prod").hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({ Aliases: [`bv.${ROOT_DOMAIN}`] }),
    });
  });

  test("no cert/zone -> no distribution (unknown-env synth stays clean)", () => {
    API_TEMPLATE.resourceCountIs("AWS::CloudFront::Distribution", 0);
  });
});

describe("ApiStack — Slack events route has its own throttle bucket", () => {
  const template: Template = API_TEMPLATE;

  test("explicit POST /api/v1/slack/events route exists", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
      RouteKey: "POST /api/v1/slack/events",
    });
  });

  test("stage carries route-level throttling for the Slack route", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      RouteSettings: Match.objectLike({
        "POST /api/v1/slack/events": Match.objectLike({
          ThrottlingBurstLimit: Match.anyValue(),
          ThrottlingRateLimit: Match.anyValue(),
        }),
      }),
    });
  });
});

describe("ApiStack — messaging AppSync Events (OIDC subscribe / IAM publish)", () => {
  // The Event API's per-namespace onSubscribe handler is the membership
  // check for every browser subscription; it is only expressible once the
  // CDK release exposes DynamoDB-backed handler configs (2.267.0). Before
  // the pin/resource work this fails because the resource is absent.
  test("rooms namespace has a DynamoDB-backed onSubscribe handler", () => {
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::DataSource", {
      Type: "AMAZON_DYNAMODB",
    });
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::ChannelNamespace", {
      Name: "rooms",
      HandlerConfigs: Match.objectLike({
        OnSubscribe: Match.objectLike({
          Behavior: "CODE",
          Integration: Match.objectLike({ DataSourceName: Match.anyValue() }),
        }),
      }),
    });
  });
});
