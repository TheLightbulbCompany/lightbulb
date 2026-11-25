import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Template } from "aws-cdk-lib/assertions";
import { ContainerStack } from "../lib/stacks/container-stack";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

/** What this stack holds: the platform ECS cluster and its task-state-change trail. */
function buildStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const vpc = new ec2.Vpc(new cdk.Stack(app, `Support-${environment}`, { env }), "Vpc");
  return Template.fromStack(new ContainerStack(app, `Container-${environment}`, { env, environment, vpc }));
}

describe.each(["dev", "prod"] as const)("ContainerStack — %s", (environment) => {
  let template: Template;
  beforeAll(() => {
    template = buildStack(environment);
  });

  test("owns the platform ECS cluster the backend runs on", () => {
    template.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterName: `lightbulb-${environment}-container`,
    });
  });

  test("keeps the ECS task-state-change trail for the platform cluster", () => {
    // ECS drops stoppedReason after ~1h. This log group is the durable record.
    template.hasResourceProperties("AWS::Events::Rule", {
      Name: `lightbulb-${environment}-ecs-task-state-change`,
    });
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: `/lightbulb/${environment}/ecs-task-events`,
    });
  });

  test("holds nothing from the retired agent runtime", () => {
    // The cluster must take the network stack's VPC: without one, ecs.Cluster builds its own.
    template.resourceCountIs("AWS::EC2::VPC", 0);
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
    template.resourceCountIs("AWS::IAM::ManagedPolicy", 0);
    template.resourceCountIs("AWS::BedrockAgentCore::Memory", 0);
    template.resourceCountIs("AWS::BedrockAgentCore::BrowserCustom", 0);
    template.resourceCountIs("AWS::ECS::TaskDefinition", 0);
  });
});

/**
 * Export names have to be asserted against a FULL stage synth: each is generated
 * by a cross-stack reference from -service or -observability.
 */
describe("ContainerStack — CFN exports", () => {
  test("publishes only the cluster exports its live consumers import", () => {
    const app = new cdk.App({ context: HOSTED_ZONE_CONTEXT });
    const stage = new LightbulbStage(app, "dev", {
      environment: "dev",
      env: { account: "877352799272", region: "us-east-1" },
    });
    const containerStack = stage.node.children.find(
      (c): c is cdk.Stack => c instanceof cdk.Stack && c.stackName === "lightbulb-dev-container",
    );
    expect(containerStack).toBeDefined();
    const exports = Object.values((Template.fromStack(containerStack!).toJSON() as any).Outputs ?? {})
      .flatMap((o: any) => (o.Export ? [o.Export.Name as string] : []))
      .sort();
    expect(exports).toEqual([
      "lightbulb-dev-container:ExportsOutputFnGetAttClusterEB0386A7Arn2F2E3C3F",
      "lightbulb-dev-container:ExportsOutputRefClusterEB0386A796A0E3FE",
    ]);
  });
});
