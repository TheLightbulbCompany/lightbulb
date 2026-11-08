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

describe("NetworkStack — WAF web ACL (count-only)", () => {
  const template = buildNetworkStack("dev");

  test("ACL exists, allows by default, and is associated to the ALB", () => {
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Scope: "REGIONAL",
      DefaultAction: { Allow: {} },
    });
    template.hasResourceProperties("AWS::WAFv2::WebACLAssociation", {
      ResourceArn: { Ref: Match.stringLikeRegexp("^AlbV2") },
    });
  });

  test("every rule is count-only: switching to block is a deliberate, separate change", () => {
    const acls = Object.values(template.findResources("AWS::WAFv2::WebACL"));
    expect(acls).toHaveLength(1);
    const rules: any[] = (acls[0] as any).Properties.Rules;
    expect(rules.length).toBe(3);
    for (const r of rules) {
      if (r.Statement.ManagedRuleGroupStatement) {
        expect(r.OverrideAction).toEqual({ Count: {} });
        expect(r.Action).toBeUndefined();
      } else {
        expect(r.Action).toEqual({ Count: {} });
      }
      expect(r.VisibilityConfig.CloudWatchMetricsEnabled).toBe(true);
      // Samples would store raw URLs/headers (bearer tokens).
      expect(r.VisibilityConfig.SampledRequestsEnabled).toBe(false);
      // Every inner managed rule counts too, else the first inner Block ends evaluation.
      const inner = r.Statement.ManagedRuleGroupStatement?.RuleActionOverrides;
      if (r.Statement.ManagedRuleGroupStatement) {
        expect(inner.length).toBeGreaterThan(10);
        for (const o of inner) expect(o.ActionToUse).toEqual({ Count: {} });
      }
    }
    expect((acls[0] as any).Properties.VisibilityConfig.SampledRequestsEnabled).toBe(false);
    // Rate rule keys on the header API GW overwrites, never client-suppliable XFF.
    const rate = rules.find((r) => r.Statement.RateBasedStatement);
    expect(rate.Statement.RateBasedStatement.ForwardedIPConfig.HeaderName).toBe("X-Lightbulb-Client-Ip");
    const scope = JSON.stringify(rate.Statement.RateBasedStatement.ScopeDownStatement);
    expect(scope).toContain("NotStatement");
    expect(scope).toContain('"UriPath":{}');
    expect(scope).not.toContain("/__paperclip_proxy__");
    expect(scope).toContain("/api/v1/gateway/"); // bv- door: client can set the header
  });
});
