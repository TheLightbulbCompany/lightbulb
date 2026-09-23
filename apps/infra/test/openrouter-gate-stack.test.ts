import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { OpenRouterGateStack } from "../lib/stacks/openrouter-gate-stack";

test("dev OpenRouter gate is a token-gated streaming Lambda with owner-key access", () => {
  const app = new cdk.App();
  const stack = new OpenRouterGateStack(app, "OpenRouterGate", {
    env: { account: "877352799272", region: "us-east-1" },
    environment: "dev",
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "lightbulb-dev-openrouter-gate",
    Runtime: "nodejs24.x",
    Environment: { Variables: Match.objectLike({
      TOKENS_TABLE: "lightbulb-dev-bedrock-gate-tokens",
      OPENROUTER_KEYS_TABLE: "lightbulb-dev-openrouter-keys",
      REQUESTS_TABLE: "lightbulb-dev-openrouter-requests",
      BILLING_TABLE: "lightbulb-dev-billing-accounts",
      FAST_GATE_TABLE: "lightbulb-dev-metronome-fast-gate",
      MODEL_POLICY_JSON: "{}",
      OPENROUTER_MANAGED_ENABLED: "false",
      OPENROUTER_MARKUP: "1.2",
    }) },
  });
  template.hasResourceProperties("AWS::Lambda::Url", {
    AuthType: "NONE",
    InvokeMode: "RESPONSE_STREAM",
  });
  template.hasResourceProperties("AWS::SSM::Parameter", {
    Name: "/lightbulb/dev/openrouter-gate-url",
  });
  const gatePolicies = JSON.stringify(Object.entries(template.findResources("AWS::IAM::Policy"))
    .filter(([id]) => id.startsWith("GateRole")));
  expect(gatePolicies).toContain("lightbulb/dev/openrouter/owners/*");
  expect(gatePolicies).not.toContain("lightbulb/dev/openrouter/management");
});

test("dev gate consumes an explicitly supplied model policy and rollout flag", () => {
  const oldPolicy = process.env.OPENROUTER_MODEL_POLICY_JSON_DEV;
  const oldEnabled = process.env.OPENROUTER_MANAGED_ENABLED_DEV;
  process.env.OPENROUTER_MODEL_POLICY_JSON_DEV = '{"revision":"ultra-free-v1","models":["nvidia/nemotron-3-ultra-550b-a55b:free"],"max_prompt_tokens":1000,"max_price":{"prompt":0,"completion":0}}';
  process.env.OPENROUTER_MANAGED_ENABLED_DEV = "true";
  try {
    const app = new cdk.App();
    const stack = new OpenRouterGateStack(app, "OpenRouterGatePolicy", {
      env: { account: "877352799272", region: "us-east-1" },
      environment: "dev",
    });
    Template.fromStack(stack).hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "lightbulb-dev-openrouter-gate",
      Environment: { Variables: Match.objectLike({
        MODEL_POLICY_JSON: '{"revision":"ultra-free-v1","models":["nvidia/nemotron-3-ultra-550b-a55b:free"],"max_prompt_tokens":1000,"max_price":{"prompt":0,"completion":0}}',
        OPENROUTER_MANAGED_ENABLED: "true",
      }) },
    });
  } finally {
    if (oldPolicy === undefined) delete process.env.OPENROUTER_MODEL_POLICY_JSON_DEV;
    else process.env.OPENROUTER_MODEL_POLICY_JSON_DEV = oldPolicy;
    if (oldEnabled === undefined) delete process.env.OPENROUTER_MANAGED_ENABLED_DEV;
    else process.env.OPENROUTER_MANAGED_ENABLED_DEV = oldEnabled;
  }
});

test("empty rollout flag stays disabled", () => {
  const oldEnabled = process.env.OPENROUTER_MANAGED_ENABLED_DEV;
  process.env.OPENROUTER_MANAGED_ENABLED_DEV = "";
  try {
    const app = new cdk.App();
    const stack = new OpenRouterGateStack(app, "OpenRouterGateEmptyFlag", {
      env: { account: "877352799272", region: "us-east-1" },
      environment: "dev",
    });
    Template.fromStack(stack).hasResourceProperties("AWS::Lambda::Function", {
      Environment: { Variables: Match.objectLike({ OPENROUTER_MANAGED_ENABLED: "false" }) },
    });
  } finally {
    if (oldEnabled === undefined) delete process.env.OPENROUTER_MANAGED_ENABLED_DEV;
    else process.env.OPENROUTER_MANAGED_ENABLED_DEV = oldEnabled;
  }
});

test("dev settlement carries the rate-card proof and runs the daily audit", () => {
  const app = new cdk.App();
  const stack = new OpenRouterGateStack(app, "OpenRouterGate", {
    env: { account: "877352799272", region: "us-east-1" },
    environment: "dev",
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "lightbulb-dev-openrouter-settlement",
    Environment: { Variables: Match.objectLike({
      REQUESTS_TABLE: "lightbulb-dev-openrouter-requests",
      OPENROUTER_KEYS_TABLE: "lightbulb-dev-openrouter-keys",
      FAST_GATE_TABLE: "lightbulb-dev-metronome-fast-gate",
      OPENROUTER_MARKUP: "1.2",
      USAGE_COUNTERS_TABLE: "lightbulb-dev-usage-counters",
      OPENROUTER_MONEY_CONTRACT_VERIFIED: "true",
      OPENROUTER_RATE_CARD_PROOF_ID: "4c39b72e-a9bc-4272-a3b3-eddd2b5e5001",
      OPENROUTER_WORKSPACE_ID: "",
    }) },
  });
  template.hasResourceProperties("AWS::Events::Rule", { ScheduleExpression: "rate(5 minutes)", State: "ENABLED" });
  template.hasResourceProperties("AWS::Lambda::EventInvokeConfig", { MaximumRetryAttempts: 0 });
  template.hasResourceProperties("AWS::Events::Rule", {
    ScheduleExpression: "cron(15 1 * * ? *)",
    State: "ENABLED",
    Targets: Match.arrayWith([Match.objectLike({ Input: '{"audit_previous_day":true}' })]),
  });
  template.resourceCountIs("AWS::SQS::Queue", 1);
  // Ingest reads metronome_api_key, which sits on a CMK: KMS must be reachable via Secrets Manager.
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: ["kms:Decrypt", "kms:GenerateDataKey"],
      Condition: { StringEquals: { "kms:ViaService": ["secretsmanager.us-east-1.amazonaws.com", "dynamodb.us-east-1.amazonaws.com"] } },
    })]) },
  });
});

test("prod settlement is scheduled but cannot ingest money: no proof, audit off", () => {
  const app = new cdk.App();
  const template = Template.fromStack(new OpenRouterGateStack(app, "OpenRouterGateProd", {
    env: { account: "877352799272", region: "us-east-1" },
    environment: "prod",
  }));
  template.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "lightbulb-prod-openrouter-settlement",
    Environment: { Variables: Match.objectLike({
      OPENROUTER_MONEY_CONTRACT_VERIFIED: "false",
      OPENROUTER_RATE_CARD_PROOF_ID: "",
    }) },
  });
  template.hasResourceProperties("AWS::Events::Rule", { ScheduleExpression: "rate(5 minutes)", State: "ENABLED" });
  template.hasResourceProperties("AWS::Events::Rule", { ScheduleExpression: "cron(15 1 * * ? *)", State: "DISABLED" });
});

test("the gate deploys in prod too: a runtime resolved to openrouter_managed needs its URL", () => {
  // 2026-09-22: prod shipped OPENROUTER_MANAGED_ENABLED_PROD with the stack still dev-only, so
  // every prod provision/roll onto the managed route failed with GateUrlUnavailable.
  const yaml = readFileSync(join(__dirname, "../../../.github/workflows/deploy.yml"), "utf8");
  expect(yaml).toMatch(/^ {2}prod-lightbulb-prod-openrouter-gate-Deploy:$/m);
  expect(yaml).toMatch(/^ {2}dev-lightbulb-dev-openrouter-gate-Deploy:$/m);
});
