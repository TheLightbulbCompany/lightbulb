import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { LegacyRuntimeRetirementBootstrapStack } from "../lib/legacy-runtime-retirement-bootstrap";

const template = Template.fromStack(new LegacyRuntimeRetirementBootstrapStack(new cdk.App(), "Retirement", {
  env: { account: "877352799272", region: "us-east-1" },
}));
const policy: any = Object.values(template.findResources("AWS::IAM::Policy"))[0];
const statements = policy.Properties.PolicyDocument.Statement;

test("one main-only role, no managed policies", () => {
  template.resourceCountIs("AWS::IAM::Role", 1);
  const role: any = Object.values(template.findResources("AWS::IAM::Role"))[0];
  expect(role.Properties.RoleName).toBe("lightbulb-retire-legacy-runtimes");
  expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  expect(role.Properties.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"])
    .toBe("repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main");
});

test("delete reaches per-owner runtimes only, never a pool, role, secret or bucket", () => {
  const del = statements.find((s: any) => [s.Action].flat().includes("bedrock-agentcore:DeleteAgentRuntime"));
  expect(del.Resource).toEqual([
    "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/lb_dev_org_*",
    "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/lb_prod_org_*",
  ]);
  for (const arn of del.Resource) expect(arn).not.toMatch(/lightbulb_.*_hermes_pool/);
  const actions: string[] = statements.flatMap((s: any) => [s.Action].flat());
  expect(actions.filter(a => !/:(Get|Scan|GetMetricData)/.test(a))).toEqual(["bedrock-agentcore:DeleteAgentRuntime"]);
  expect(actions.some(a => /^(iam|secretsmanager|s3|kms):/.test(a))).toBe(false);
});

test("workflow defaults to a dry run and only bootstrap deploys", () => {
  const workflow = readFileSync(resolve(__dirname, "../../../.github/workflows/retire-legacy-runtimes.yml"), "utf8");
  const retire = workflow.split("  retire:\n")[1];
  expect(workflow).toMatch(/options: \[plan, apply, bootstrap\]\n\s+default: plan/);
  expect(retire).not.toContain("cdk deploy");
  expect(retire).toContain("role/lightbulb-retire-legacy-runtimes");
  expect(retire).toContain("if [ \"$RETIRE_MODE\" = apply ]; then set -- --apply; fi");
});
