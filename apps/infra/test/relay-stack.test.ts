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
  // No load balancer or listener of its own (NLB or otherwise): TLS ends at the
  // existing ALB, and nothing (9090 included) gets a listener here.
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 0);
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::Listener", 0);
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
  // The one target group serves the relay port; 9090 is never routed.
  dev.resourceCountIs("AWS::ElasticLoadBalancingV2::TargetGroup", 1);
  dev.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
    Port: 8080,
    HealthCheckPath: "/generate_204",
    Matcher: { HttpCode: "204" },
  });
  // The task SG's only inline rule: 9090 from the VPC CIDR.
  dev.hasResourceProperties("AWS::EC2::SecurityGroup", {
    SecurityGroupIngress: [Match.objectLike({ FromPort: 9090, CidrIp: Match.anyValue() })],
  });
  // The only other ingress: 8080 from the ALB's security group.
  dev.resourceCountIs("AWS::EC2::SecurityGroupIngress", 1);
  dev.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
    FromPort: 8080,
    SourceSecurityGroupId: Match.anyValue(),
  });
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

test("relay_image_loads_its_config_as_nonroot", () => {
  const read = (f: string) => fs.readFileSync(path.join(__dirname, "..", "relay", f), "utf8");
  // TLS ends at the ALB: a [tls] table would make the relay serve HTTPS.
  expect(read("config.toml")).not.toMatch(/^\[tls\]/m);
  // A missing config file is silently Config::default(), so the image must put it
  // exactly where the entrypoint reads it.
  const dockerfile = read("Dockerfile");
  const configPath = /"--config-path", "([^"]+)"/.exec(dockerfile)?.[1];
  expect(configPath).toBeDefined();
  expect(dockerfile).toContain(`COPY config.toml ${configPath}`);
  expect(dockerfile).toMatch(/^FROM gcr\.io\/distroless\/static-debian12:nonroot@sha256:/m);
  expect(dockerfile).toMatch(/sha256sum -c/);
});
