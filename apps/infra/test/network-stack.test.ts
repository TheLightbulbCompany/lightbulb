import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../lib/stacks/network-stack";

function buildNetworkStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const stack = new NetworkStack(app, `Network-${environment}`, {
    env,
    environment,
  });
  return Template.fromStack(stack);
}

describe("NetworkStack — backend target group drain", () => {
  const template = buildNetworkStack("dev");

  test("backend target group drains quickly (deregistration delay <= stopTimeout)", () => {
    // A draining task gets 25s to finish in-flight work before the ALB stops
    // counting it; this is <= the ECS container stopTimeout (30s) so the task
    // is fully drained before SIGKILL.
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::TargetGroup",
      {
        TargetGroupAttributes: Match.arrayWith([
          {
            Key: "deregistration_delay.timeout_seconds",
            Value: "25",
          },
        ]),
      },
    );
  });
});

describe.each(["dev", "prod"] as const)("NetworkStack — only what API Gateway uses (%s)", (environment) => {
  const template = buildNetworkStack(environment);

  test("one :80 listener to the one backend target group; no :443, :8082 or WAF", () => {
    template.resourceCountIs("AWS::ElasticLoadBalancingV2::Listener", 1);
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", { Port: 80, Protocol: "HTTP" });
    template.resourceCountIs("AWS::ElasticLoadBalancingV2::TargetGroup", 1);
    template.resourceCountIs("AWS::WAFv2::WebACL", 0);
    template.resourceCountIs("AWS::WAFv2::WebACLAssociation", 0);
  });

  test("the ALB security group admits only :80 (443 and 8082 are gone)", () => {
    const sg = Object.values(
      template.findResources("AWS::EC2::SecurityGroup", {
        Properties: { GroupDescription: Match.stringLikeRegexp("internal ALB security group") },
      }),
    )[0] as any;
    expect([...new Set(sg.Properties.SecurityGroupIngress.map((r: any) => r.FromPort))]).toEqual([80]);
  });
});
