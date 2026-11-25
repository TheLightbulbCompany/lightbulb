import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ApiStack } from "../lib/stacks/api-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ROOT_DOMAIN } from "../lib/domains";
import { synthApiStack } from "./helpers";

// Hoist a single ApiStack synthesis and reuse across tests.
const API_TEMPLATE = synthApiStack();

// Hoist a single ApiStack synthesis and reuse across tests.

describe("ApiStack — dead WebSocket API removed", () => {
  test("no WebSocket API, Lambdas, stage, domain or export", () => {
    const names = Object.values(API_TEMPLATE.findResources("AWS::Lambda::Function")).map(
      (r: any) => JSON.stringify(r.Properties?.FunctionName ?? ""),
    );
    expect(names.filter((n) => /ws-(authorizer|connect|disconnect|message)/.test(n))).toEqual([]);
    const wsApis = Object.values(API_TEMPLATE.findResources("AWS::ApiGatewayV2::Api")).filter(
      (r: any) => r.Properties?.ProtocolType === "WEBSOCKET",
    );
    expect(wsApis).toEqual([]);
    expect(Object.keys(API_TEMPLATE.toJSON().Outputs ?? {}).filter((k) => /WebSocketApi/.test(k))).toEqual([]);
  });
});

describe("ApiStack — production browser CORS origins", () => {
  test("prod API Gateway CORS accepts apex and www frontend origins", () => {
    const prod = synthApiStack("prod");
    prod.hasResourceProperties("AWS::ApiGatewayV2::Api", {
      CorsConfiguration: Match.objectLike({
        AllowOrigins: [
          `https://${ROOT_DOMAIN}`,
          `https://www.${ROOT_DOMAIN}`,
        ],
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


describe("ApiStack — only the HTTP API is left", () => {
  test.each(["dev", "prod"] as const)("%s has no messaging plane, bv- door or engine 404 Lambda", (env) => {
    const t = synthApiStack(env);
    for (const type of [
      "AWS::DynamoDB::Table",
      "AWS::AppSync::Api",
      "AWS::SQS::Queue",
      "AWS::Pipes::Pipe",
      "AWS::Lambda::Function",
      "AWS::CloudFront::Distribution",
    ]) {
      expect(Object.keys(t.findResources(type))).toEqual([]);
    }
    t.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
  });
});
