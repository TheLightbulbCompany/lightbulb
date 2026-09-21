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

test("dev settlement worker is scheduled but cannot ingest money before contract proof", () => {
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
      OPENROUTER_MONEY_CONTRACT_VERIFIED: "false",
      OPENROUTER_RATE_CARD_PROOF_ID: "",
      OPENROUTER_WORKSPACE_ID: "",
    }) },
  });
  template.hasResourceProperties("AWS::Events::Rule", { ScheduleExpression: "rate(5 minutes)", State: "DISABLED" });
  template.hasResourceProperties("AWS::Events::Rule", {
    ScheduleExpression: "cron(15 1 * * ? *)",
    State: "DISABLED",
    Targets: Match.arrayWith([Match.objectLike({ Input: '{"audit_previous_day":true}' })]),
  });
  template.resourceCountIs("AWS::SQS::Queue", 1);
});
