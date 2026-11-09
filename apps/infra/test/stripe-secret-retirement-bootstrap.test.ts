import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { StripeSecretRetirementBootstrapStack } from "../lib/stripe-secret-retirement-bootstrap";

const template = Template.fromStack(new StripeSecretRetirementBootstrapStack(new cdk.App(), "Retirement", {
  env: { account: "877352799272", region: "us-east-1" },
}));
const resources: any = template.toJSON().Resources;
const role: any = Object.values(template.findResources("AWS::IAM::Role"))[0];
const policy: any = Object.values(template.findResources("AWS::IAM::Policy"))[0];
const statements = policy.Properties.PolicyDocument.Statement;
const arnText = (value: any): string => typeof value === "string" ? value : value["Fn::Join"][1]
  .map((part: any) => typeof part === "string" ? part : part.Ref === "AWS::Partition" ? "aws" : "UNEXPECTED_TOKEN").join("");

test("dedicated role trusts the actual main ref subject without an environment or unsupported ref claim", () => {
  expect(Object.keys(resources)).toHaveLength(2);
  template.resourceCountIs("AWS::IAM::Role", 1);
  template.resourceCountIs("AWS::IAM::OIDCProvider", 0);
  expect(role.Properties.RoleName).toBe("lightbulb-retire-stripe-secrets");
  expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  expect(role.Properties.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals).toEqual({
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
  });
  expect(JSON.stringify(policy.Properties.Roles)).not.toContain("lightbulb-github-actions");
});

test("only the four obsolete names can be described or recoverably deleted, never read or rotated", () => {
  const secretStatements = statements.filter((s: any) => s.Sid.includes("RetiredSecrets"));
  expect(secretStatements).toHaveLength(2);
  for (const statement of secretStatements) {
    expect(statement.Resource).toHaveLength(4);
    for (const arn of statement.Resource) expect(arnText(arn)).toMatch(
      /^arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb\/(dev|prod)\/stripe_(connect|marketplace)_webhook_secret-\?{6}$/,
    );
  }
  expect(statements.find((s: any) => s.Sid === "RecoverablyDeleteExactRetiredSecrets").Condition)
    .toEqual({ NumericEquals: { "secretsmanager:RecoveryWindowInDays": 30 } });
  const actions = statements.flatMap((s: any) => [s.Action].flat());
  expect(actions.filter((a: string) => a.startsWith("secretsmanager:"))).toEqual([
    "secretsmanager:DescribeSecret", "secretsmanager:DeleteSecret",
  ]);
  expect(actions.some((a: string) => a.startsWith("iam:") || a.startsWith("kms:"))).toBe(false);
});

test("plan cannot bootstrap IAM and execution never assumes the limited general deploy role", () => {
  const workflow = readFileSync(resolve(__dirname, "../../../.github/workflows/retire-unused-stripe-secrets.yml"), "utf8");
  const bootstrap = workflow.split("  bootstrap:\n")[1].split("  retire:\n")[0];
  const retire = workflow.split("  retire:\n")[1];
  expect(bootstrap).toContain("if: github.ref == 'refs/heads/main' && inputs.mode == 'bootstrap'");
  expect(retire).toContain("if: github.ref == 'refs/heads/main' && inputs.mode != 'bootstrap'");
  expect(bootstrap).toContain("stripe-secret-retirement-bootstrap-app.ts");
  expect(retire).not.toContain("cdk deploy");
  expect(retire).not.toContain("role/lightbulb-github-actions");
  expect(retire).toContain("role/lightbulb-retire-stripe-secrets");
  expect(workflow.slice(workflow.indexOf("jobs:"))).not.toContain("\n    environment:");
});
