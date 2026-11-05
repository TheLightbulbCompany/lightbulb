import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { DeployRunnerStack } from "../lib/stacks/deploy-runner-stack";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

const AWS_ENV = { account: "877352799272", region: "us-east-1" };
const CDK_BOOTSTRAP_ROLES = "arn:aws:iam::877352799272:role/cdk-hnb659fds-*-877352799272-us-east-1";

test("dev deploy runner: one privileged CodeBuild project, main pushes only, runs the repo's buildspec", () => {
  const app = new cdk.App();
  const stack = new DeployRunnerStack(app, "DeployRunner", { env: AWS_ENV, envName: "dev" });
  const template = Template.fromStack(stack);

  template.resourceCountIs("AWS::CodeBuild::Project", 1);
  template.hasResourceProperties("AWS::CodeBuild::Project", {
    Name: "lightbulb-dev-deploy",
    Environment: Match.objectLike({ PrivilegedMode: true, ComputeType: "BUILD_GENERAL1_MEDIUM" }),
    Source: Match.objectLike({
      Type: "GITHUB",
      Location: "https://github.com/TheLightbulbCompany/lightbulb.git",
      BuildSpec: "apps/infra/codebuild/buildspec-deploy-dev.yml",
      ReportBuildStatus: true,
    }),
    Triggers: {
      Webhook: true,
      FilterGroups: [
        [
          { Type: "EVENT", Pattern: "PUSH" },
          { Type: "HEAD_REF", Pattern: "^refs/heads/main$" },
        ],
      ],
    },
    TimeoutInMinutes: 90,
  });
  // Serialisation lives in deploy-dev.sh; a project limit would drop builds.
  template.hasResourceProperties("AWS::CodeBuild::Project", { ConcurrentBuildLimit: Match.absent() });
});

test("the runner's role may assume only the cdk bootstrap roles, and role-policy.json is what it gets", () => {
  const app = new cdk.App();
  const stack = new DeployRunnerStack(app, "DeployRunner", { env: AWS_ENV, envName: "dev" });
  const template = Template.fromStack(stack);

  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyName: "lightbulb-dev-deploy-runner",
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({ Sid: "AssumeCdkBootstrapRoles", Action: "sts:AssumeRole", Resource: CDK_BOOTSTRAP_ROLES }),
        Match.objectLike({ Sid: "ReadDeploySecret", Action: "secretsmanager:GetSecretValue" }),
        Match.objectLike({ Sid: "SerialiseBuilds" }),
      ]),
    },
  });

  // Every statement of every policy on the role: no sts:AssumeRole elsewhere, no Action "*".
  const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
    (policy) => policy.Properties.PolicyDocument.Statement as Array<{ Action: unknown; Resource: unknown }>,
  );
  for (const statement of statements) {
    const actions = ([] as unknown[]).concat(statement.Action);
    expect(actions).not.toContain("*");
    if (actions.includes("sts:AssumeRole")) {
      expect(statement.Resource).toBe(CDK_BOOTSTRAP_ROLES);
    }
  }
});

test("the runner exists in the dev stage and not in prod", () => {
  const stackNames = (environment: "dev" | "prod") => {
    const app = new cdk.App({ context: HOSTED_ZONE_CONTEXT });
    const stage = new LightbulbStage(app, environment, { environment, env: AWS_ENV });
    return stage.node.children.filter((c): c is cdk.Stack => c instanceof cdk.Stack).map((s) => s.stackName);
  };
  expect(stackNames("dev")).toContain("lightbulb-dev-deploy-runner");
  expect(stackNames("prod")).not.toContain("lightbulb-prod-deploy-runner");
});
