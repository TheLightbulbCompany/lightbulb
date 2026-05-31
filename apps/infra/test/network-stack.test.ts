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
