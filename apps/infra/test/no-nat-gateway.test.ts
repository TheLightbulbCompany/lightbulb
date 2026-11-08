import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

/**
 * Phase 2 task 6: no managed NAT gateway. Fargate tasks sit in public subnets with a public IP;
 * the private subnets keep egress through one t4g.micro NAT instance with a fixed EIP, because
 * AgentCore runtimes, the workspace browser and the VPC Lambdas get no public IP of their own.
 * Dev and prod have the same shape.
 */
function synthStage(env: "dev" | "prod") {
  const app = new cdk.App({ context: HOSTED_ZONE_CONTEXT });
  const stage = new LightbulbStage(app, `${env}-no-nat`, {
    environment: env,
    env: { account: "877352799272", region: "us-east-1" },
  });
  const stacks = stage.node.children.filter((c): c is cdk.Stack => c instanceof cdk.Stack);
  const get = (suffix: string) => Template.fromStack(stacks.find((s) => s.stackName.endsWith(suffix))!);
  return { network: get("-network"), service: get("-service") };
}

const BACKEND_SERVICES = ["backend", "temporal-worker", "temporal-watchdog"];

function assertNoNat(t: { network: Template; service: Template }) {
  t.network.resourceCountIs("AWS::EC2::NatGateway", 0);
  t.network.hasResourceProperties("AWS::EC2::Instance", {
    InstanceType: "t4g.micro",
    SourceDestCheck: false,
  });
  // A fresh logical id forces a new instance, so the first-boot user data (iptables MASQUERADE) runs.
  expect(Object.keys(t.network.findResources("AWS::EC2::Instance"))).toEqual(["NatInstanceV2"]);
  t.network.resourceCountIs("AWS::EC2::EIP", 1);
  // A failed host check recovers the same instance (keeps its id, ENI and EIP, so routes survive)
  // and pages; a failed guest check pages.
  const nat = { Dimensions: [{ Name: "InstanceId", Value: { Ref: "NatInstanceV2" } }] };
  t.network.hasResourceProperties("AWS::CloudWatch::Alarm", {
    ...nat,
    MetricName: "StatusCheckFailed_System",
    AlarmName: Match.stringLikeRegexp("^lightbulb-(dev|prod)-nat-instance-system-check$"),
    AlarmActions: Match.arrayWith([
      Match.stringLikeRegexp("^arn:aws:sns:us-east-1:877352799272:lightbulb-(dev|prod)-alerts-page$"),
      { "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, ":automate:us-east-1:ec2:recover"]] },
    ]),
  });
  t.network.hasResourceProperties("AWS::CloudWatch::Alarm", {
    ...nat,
    MetricName: "StatusCheckFailed_Instance",
    AlarmName: Match.stringLikeRegexp("^lightbulb-(dev|prod)-nat-instance-status-check$"),
    AlarmActions: [Match.stringLikeRegexp("^arn:aws:sns:us-east-1:877352799272:lightbulb-(dev|prod)-alerts-page$")],
  });
  // The NAT instance admits the VPC only; CDK's default would open it to 0.0.0.0/0.
  const natSgs = Object.values(
    t.network.findResources("AWS::EC2::SecurityGroup", {
      Properties: { GroupDescription: "Security Group for NAT instances" },
    }),
  );
  expect(natSgs).toHaveLength(1);
  expect(natSgs[0].Properties.SecurityGroupIngress.map((r: { CidrIp: string }) => r.CidrIp)).toEqual([
    expect.stringMatching(/^10\.\d+\.0\.0\/16$/),
  ]);
  for (const name of BACKEND_SERVICES) {
    t.service.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: Match.stringLikeRegexp(`-${name}$`),
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "ENABLED" }),
      },
    });
  }
}

describe("no NAT gateway (phase 2 task 6)", () => {
  test.each(["dev", "prod"] as const)("%s: 0 NAT gateways, backend + Temporal tasks get a public IP", (env) => {
    assertNoNat(synthStage(env));
  });
});
