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

describe("NetworkStack — browser-view listener", () => {
  const template = buildNetworkStack("dev");

  // The :8081 listener is the only WebSocket-capable door to the backend, and
  // its SG rule admits the WHOLE VPC CIDR (CloudFront VPC-origin ENIs sit
  // behind an AWS-managed SG that doesn't exist until first deploy, so the
  // rule can't be narrower). What keeps that safe is the routing cap: 404 by
  // default, exactly two paths forwarded (browser-view live view + the
  // machine gateway relay). These assertions ARE that cap — if one fails,
  // in-VPC callers (per-user containers included) just gained reach into
  // more of FastAPI than those two Clerk/device/service-authenticated routes.
  test("8081 listener defaults to 404 and forwards ONLY the machine relay path", () => {
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
      Port: 8081,
      Protocol: "HTTP",
      DefaultActions: [
        Match.objectLike({
          Type: "fixed-response",
          FixedResponseConfig: Match.objectLike({ StatusCode: "404" }),
        }),
      ],
    });
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::ListenerRule", {
      Conditions: [
        Match.objectLike({
          Field: "path-pattern",
          PathPatternConfig: { Values: ["/api/v1/gateway/*"] },
        }),
      ],
      Actions: [Match.objectLike({ Type: "forward" })],
    });
  });

  test("ALB security group admits ONLY the VPC CIDR on 8081", () => {
    // The source matters as much as the port: an ingress of 0.0.0.0/0 here
    // would satisfy a port-only assertion while exposing the listener to the
    // internet. The CIDR is a same-stack reference to the VPC resource, so it
    // renders as Fn::GetAtt [Vpc, CidrBlock] — pin exactly that shape.
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          FromPort: 8081,
          ToPort: 8081,
          IpProtocol: "tcp",
          CidrIp: { "Fn::GetAtt": [Match.stringLikeRegexp("^Vpc"), "CidrBlock"] },
        }),
      ]),
    });
  });
});
