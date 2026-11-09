import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as fs from "fs";
import * as path from "path";
import { RuntimeRollBootstrapStack } from "../lib/runtime-roll-bootstrap";

const template = Template.fromStack(new RuntimeRollBootstrapStack(new cdk.App(), "RollBootstrap", {
  env: { account: "877352799272", region: "us-east-1" },
}));
const policies: any[] = Object.values(template.findResources("AWS::IAM::Policy"));
const roles: any[] = Object.values(template.findResources("AWS::IAM::Role"));
const statements = policies.flatMap((p) => p.Properties.PolicyDocument.Statement);
const text = (value: any): string => typeof value === "string" ? value : value["Fn::Join"][1]
  .map((part: any) => typeof part === "string" ? part : part.Ref === "AWS::Partition" ? "aws" : "UNEXPECTED").join("");

test("only two main-only environment roles are created; no existing deployer or OIDC provider changes", () => {
  expect(Object.keys(template.toJSON().Resources)).toHaveLength(4);
  expect(roles.map((r) => r.Properties.RoleName).sort()).toEqual(["lightbulb-dev-runtime-roll", "lightbulb-prod-runtime-roll"]);
  for (const r of roles) {
    expect(r.Properties.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals).toEqual({
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
    });
  }
  expect(JSON.stringify(policies.map((p) => p.Properties.Roles))).not.toContain("lightbulb-github-actions");
});

test("launches only backend definitions, creation tags bind task ownership to the exact IAM session", () => {
  for (const s of statements.filter((s) => s.Sid === "LaunchTaggedBackendTask")) {
    expect(text(s.Resource)).toMatch(/task-definition\/lightbulb-(dev|prod)-backend:\*$/);
    expect(s.Condition.StringEquals).toEqual({
      "aws:RequestTag/ManagedBy": "lightbulb-runtime-roll", "aws:RequestTag/RollSession": "${aws:userid}",
    });
    expect(text(s.Condition.ArnEquals["ecs:cluster"])).toMatch(/cluster\/lightbulb-(dev|prod)-container$/);
    expect(s.Condition.Bool["ecs:enable-execute-command"]).toBe("false");
  }
  for (const s of statements.filter((s) => s.Sid === "TagOnlyOnTaskCreation")) {
    expect(s.Condition.StringEquals["ecs:CreateAction"]).toBe("RunTask");
  }
});

test("stop and observation cannot target an untagged service task or another workflow session", () => {
  for (const s of statements.filter((s) => [s.Action].flat().includes("ecs:StopTask"))) {
    expect(text(s.Resource)).toMatch(/task\/lightbulb-(dev|prod)-container\/\*$/);
    expect(s.Condition.StringEquals).toEqual({
      "aws:ResourceTag/ManagedBy": "lightbulb-runtime-roll", "aws:ResourceTag/RollSession": "${aws:userid}",
    });
  }
});

test("no secret reads, task-definition/service writes, ECS exec, or broad role passing", () => {
  const actions: string[] = statements.flatMap((s) => [s.Action].flat());
  for (const action of actions) expect(action).not.toMatch(/secretsmanager|kms|RegisterTaskDefinition|UpdateService|ExecuteCommand|^\*$/);
  for (const s of statements.filter((s) => [s.Action].flat().includes("iam:PassRole"))) {
    expect(s.Resource).toHaveLength(2);
    for (const r of s.Resource) expect(text(r)).toMatch(/role\/lightbulb-(dev|prod)-backend-task(-execution)?$/);
    expect(s.Condition.StringEquals["iam:PassedToService"]).toBe("ecs-tasks.amazonaws.com");
  }
});

test("manual workflow uses checked-in fixed single-owner launcher, never a fleet/force command", () => {
  const workflow = fs.readFileSync(path.resolve(__dirname, "../../../.github/workflows/roll-hermes-runtime.yml"), "utf8");
  expect(workflow).toContain("workflow_dispatch:");
  expect(workflow).toContain("github.ref == 'refs/heads/main'");
  expect(workflow).toContain("role-session-name: runtime-roll-${{ github.run_id }}-${{ github.run_attempt }}");
  expect(workflow).toContain("--cleanup");
  expect(workflow).not.toMatch(/--all|--respec|--force|ecs execute-command|secretsmanager get-secret-value/);
});
