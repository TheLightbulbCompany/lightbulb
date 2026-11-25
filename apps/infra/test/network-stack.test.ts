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

describe("NetworkStack: engine listener", () => {
  const template = buildNetworkStack("dev");

  test("8082 forwards to the engine target group, which health-checks /health on 8000", () => {
    const [tgId] = Object.keys(
      template.findResources("AWS::ElasticLoadBalancingV2::TargetGroup", {
        Properties: { Name: "lightbulb-dev-engine" },
      }),
    );
    expect(tgId).toBeDefined();
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
      Name: "lightbulb-dev-engine",
      Port: 8000,
      TargetType: "ip",
      HealthCheckPath: "/health",
    });
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
      Port: 8082,
      Protocol: "HTTP",
      DefaultActions: [Match.objectLike({ Type: "forward", TargetGroupArn: { Ref: tgId } })],
    });
  });

  test("the network stack opens 8082 to nobody (the service stack admits only its SG)", () => {
    const ingress = [
      ...Object.values(template.findResources("AWS::EC2::SecurityGroupIngress")).map((r: any) => r.Properties),
      ...Object.values(template.findResources("AWS::EC2::SecurityGroup")).flatMap(
        (r: any) => r.Properties.SecurityGroupIngress ?? [],
      ),
    ];
    expect(ingress.filter((r: any) => r.FromPort <= 8082 && r.ToPort >= 8082)).toEqual([]);
  });
});

