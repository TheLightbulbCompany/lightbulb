import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as fs from "node:fs";
import * as path from "node:path";
import { expect, test } from "vitest";
import { RelayStack } from "../lib/stacks/relay-stack";

const LISTENER_ARN =
  "arn:aws:elasticloadbalancing:us-east-1:877352799272:listener/app/lightbulb-dev/abc/def";

function build(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const support = new cdk.Stack(app, `Support-${environment}`, { env });
  const vpc = new ec2.Vpc(support, "Vpc", { maxAzs: 2 });
  const albSecurityGroup = new ec2.SecurityGroup(support, "AlbSg", { vpc });
  const alb = new elbv2.ApplicationLoadBalancer(support, "Alb", {
    vpc,
    internetFacing: false,
    securityGroup: albSecurityGroup,
  });
  const stack = new RelayStack(app, `Relay-${environment}`, {
    env,
    environment,
    vpc,
    cluster: new ecs.Cluster(support, "Cluster", { vpc }),
    alb,
    albSecurityGroup,
    albHttpsListenerArn: LISTENER_ARN,
    certificate: acm.Certificate.fromCertificateArn(
      support,
      "Cert",
      "arn:aws:acm:us-east-1:877352799272:certificate/test-cert",
    ),
    hostedZone: route53.HostedZone.fromHostedZoneAttributes(support, "Zone", {
      hostedZoneId: "Z0TEST",
      zoneName: "thelightbulb.company",
    }),
  });
  return Template.fromStack(stack);
}

const dev = build("dev");

test("relay_has_no_nlb_and_no_udp", () => {
  // No load balancer of its own (NLB or otherwise): TLS ends at the existing ALB.
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 0);
  const json = JSON.stringify(dev.toJSON());
  expect(json).not.toMatch(/"udp"|"UDP"|7842/);
  dev.hasResourceProperties("AWS::ECS::TaskDefinition", {
    RuntimePlatform: { CpuArchitecture: "ARM64", OperatingSystemFamily: "LINUX" },
    ContainerDefinitions: [
      Match.objectLike({ PortMappings: [{ ContainerPort: 8080, Protocol: "tcp" }] }),
    ],
  });
});

test("relay_rule_is_host_header_on_the_existing_alb", () => {
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::ListenerRule", 1);
  dev.hasResourceProperties("AWS::ElasticLoadBalancingV2::ListenerRule", {
    ListenerArn: LISTENER_ARN,
    Conditions: [
      { Field: "host-header", HostHeaderConfig: { Values: ["relay-dev.thelightbulb.company"] } },
    ],
  });
  // CloudFront reaches the internal ALB through a VPC origin on HTTPS 443.
  dev.hasResourceProperties("AWS::CloudFront::VpcOrigin", {
    VpcOriginEndpointConfig: Match.objectLike({
      HTTPSPort: 443,
      OriginProtocolPolicy: "https-only",
    }),
  });
  dev.hasResourceProperties("AWS::CloudFront::Distribution", {
    DistributionConfig: Match.objectLike({
      DefaultCacheBehavior: Match.objectLike({
        // CachingDisabled + AllViewer: the Host header (the ALB rule, the origin
        // cert check) and the WebSocket upgrade reach the ALB verbatim.
        CachePolicyId: "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        OriginRequestPolicyId: "216adef6-5c7f-47e4-b989-5492eafa07d3",
        ViewerProtocolPolicy: "https-only",
      }),
    }),
  });
});

test("relay_deploy_stops_before_it_starts", () => {
  dev.hasResourceProperties("AWS::ECS::Service", {
    DesiredCount: 1,
    DeploymentConfiguration: Match.objectLike({
      MinimumHealthyPercent: 0,
      MaximumPercent: 100,
    }),
  });
});

test("relay_metrics_are_not_routed", () => {
  // The only routed port is 8080; 9090 has no target group, rule or listener.
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::TargetGroup", 1);
  dev.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
    Port: 8080,
    HealthCheckPath: "/generate_204",
    Matcher: { HttpCode: "204" },
  });
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::Listener", 0);
  // 9090 is VPC-only: the one ingress rule on it is from the VPC CIDR.
  const ingress = Object.values(dev.findResources("AWS::EC2::SecurityGroupIngress")).concat(
    Object.values(dev.findResources("AWS::EC2::SecurityGroup")).flatMap((sg) =>
      ((sg as { Properties?: { SecurityGroupIngress?: unknown[] } }).Properties
        ?.SecurityGroupIngress ?? []).map((p) => ({ Properties: p })),
    ),
  ) as { Properties: Record<string, unknown> }[];
  const metrics = ingress.filter((r) => r.Properties.FromPort === 9090);
  expect(metrics).toHaveLength(1);
  expect(metrics[0].Properties.CidrIp).toBeDefined();
  expect(metrics[0].Properties.SourceSecurityGroupId).toBeUndefined();
  // Port 8080 admits the ALB's security group only.
  const relay = ingress.filter((r) => r.Properties.FromPort === 8080);
  expect(relay).toHaveLength(1);
  expect(relay[0].Properties.SourceSecurityGroupId).toBeDefined();
});

test("relay_host_is_relay_dev_in_dev", () => {
  for (const [template, host] of [
    [dev, "relay-dev.thelightbulb.company"],
    [build("prod"), "relay.thelightbulb.company"],
  ] as const) {
    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({ Aliases: [host] }),
    });
    for (const Type of ["A", "AAAA"]) {
      template.hasResourceProperties("AWS::Route53::RecordSet", { Name: `${host}.`, Type });
    }
  }
});

test("relay_alarms_watch_bytes_and_5xx", () => {
  dev.resourceCountIs("AWS::CloudWatch::Alarm", 2);
  dev.hasResourceProperties("AWS::CloudWatch::Alarm", {
    Metrics: Match.arrayWith([
      Match.objectLike({ Label: "BytesTransferred", ReturnData: true }),
    ]),
  });
  dev.hasResourceProperties("AWS::CloudWatch::Alarm", {
    MetricName: "5xxErrorRate",
    Namespace: "AWS/CloudFront",
  });
});

test("relay_config_matches_the_brief", () => {
  const config = fs.readFileSync(path.join(__dirname, "..", "relay", "config.toml"), "utf8");
  for (const line of [
    "enable_relay = true",
    'http_bind_addr = "[::]:8080"',
    "enable_quic_addr_discovery = false",
    "enable_metrics = true",
    'access = "everyone"',
    "accept_conn_limit = 10.0",
    "accept_conn_burst = 50",
    "bytes_per_second = 1048576",
    "max_burst_bytes = 4194304",
  ]) {
    expect(config).toContain(line);
  }
  expect(config).not.toMatch(/^\[tls\]/m);
  // A missing config file is silently Config::default(), so the image must put it
  // exactly where the entrypoint reads it, and run as nonroot.
  const dockerfile = fs.readFileSync(path.join(__dirname, "..", "relay", "Dockerfile"), "utf8");
  expect(dockerfile).toContain("COPY config.toml /etc/iroh-relay/config.toml");
  expect(dockerfile).toContain('"--config-path", "/etc/iroh-relay/config.toml"');
  expect(dockerfile).toMatch(/^FROM gcr\.io\/distroless\/static-debian12:nonroot@sha256:/m);
  expect(dockerfile).toMatch(/sha256sum -c/);
});
