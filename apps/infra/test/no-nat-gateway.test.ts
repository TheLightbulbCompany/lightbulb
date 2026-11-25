import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

/**
 * No NAT of any kind. The backend's Fargate tasks sit in public subnets with a public IP;
 * the private subnets hold only the internal ALB and the VPC Link ENIs, which need no
 * outbound internet, so they are isolated. Dev and prod have the same shape.
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

describe("no NAT", () => {
  test.each(["dev", "prod"] as const)("%s: no NAT gateway or instance, the backend task gets a public IP", (env) => {
    const t = synthStage(env);
    t.network.resourceCountIs("AWS::EC2::NatGateway", 0);
    t.network.resourceCountIs("AWS::EC2::Instance", 0);
    t.network.resourceCountIs("AWS::EC2::EIP", 0);
    // Only the public route tables carry a default route (to the internet gateway).
    const defaults = Object.values(t.network.findResources("AWS::EC2::Route")).filter(
      (r: any) => r.Properties.DestinationCidrBlock === "0.0.0.0/0",
    );
    expect(defaults.length).toBe(env === "prod" ? 3 : 2);
    for (const r of defaults as any[]) expect(r.Properties.GatewayId).toBeDefined();
    t.service.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: Match.stringLikeRegexp("-backend$"),
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "ENABLED" }),
      },
    });
  });
});
