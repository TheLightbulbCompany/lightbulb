import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { StripeSecretRetirementBootstrapStack } from "../lib/stripe-secret-retirement-bootstrap";

const template = Template.fromStack(new StripeSecretRetirementBootstrapStack(new cdk.App(), "Retirement", {
  env: { account: "877352799272", region: "us-east-1" },
  // The CLI enables analytics in real deployment. It must not add a third
  // resource that the exact authority preflight would correctly reject.
  analyticsReporting: true,
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

test("actual synthesized bootstrap template passes plan without self-reference and rejects scope changes", () => {
  const result = spawnSync("python3", ["-c", `
import copy, json, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, "apps/backend/scripts")
import retire_unused_stripe_secrets as r
template = json.load(sys.stdin)
cf, ecs, lm, sm, sts, session = [Mock() for _ in range(6)]
cf.get_paginator.return_value.paginate.return_value = [{"StackSummaries": [{"StackName": r.AUTHORITY_STACK, "StackId": "owned", "StackStatus": "CREATE_COMPLETE"}]}]
cf.get_template.return_value = {"TemplateBody": template}
ecs.get_paginator.return_value.paginate.return_value = [{"clusterArns": []}]
lm.get_paginator.return_value.paginate.return_value = [{"Functions": []}]
sts.get_caller_identity.return_value = {"Account": r.ACCOUNT}
session.client.side_effect = lambda name, **kwargs: {"cloudformation":cf,"ecs":ecs,"lambda":lm,"secretsmanager":sm,"sts":sts}[name]
sm.describe_secret.side_effect = lambda SecretId: {"Name":SecretId,"ARN":f"arn:aws:secretsmanager:{r.REGION}:{r.ACCOUNT}:secret:{SecretId}-abcdef","LastChangedDate":datetime(2026,8,1,tzinfo=timezone.utc)}
with tempfile.TemporaryDirectory() as directory, patch.object(r, "source_references", return_value=[]):
    receipt = r.run(session,"dev","plan","",Path(directory))
    assert len(receipt["secrets"]) == 2
    sm.delete_secret.assert_not_called()
    sm.get_secret_value.assert_not_called()
for mutation in ("lambda", "permissions", "other-stack"):
    changed = copy.deepcopy(template)
    stack_name = r.AUTHORITY_STACK
    if mutation == "lambda": changed["Resources"]["Unexpected"] = {"Type":"AWS::Lambda::Function"}
    if mutation == "permissions":
        policy = next(v for v in changed["Resources"].values() if v["Type"] == "AWS::IAM::Policy")
        policy["Properties"]["PolicyDocument"]["Statement"].append({"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"*"})
    if mutation == "other-stack": stack_name = "lightbulb-unrelated-bootstrap"
    cf.get_template.return_value = {"TemplateBody":changed}
    cf.get_paginator.return_value.paginate.return_value = [{"StackSummaries":[{"StackName":stack_name,"StackId":"owned","StackStatus":"CREATE_COMPLETE"}]}]
    try: r.consumer_checks(session,["lightbulb/dev/stripe_connect_webhook_secret"])
    except ValueError: pass
    else: raise AssertionError("Unsafe bootstrap was excluded: " + mutation)
`], { input: JSON.stringify(template.toJSON()), cwd: resolve(__dirname, "../../.."), encoding: "utf8" });
  expect(result.stderr).toBe("");
  expect(result.status).toBe(0);
});
