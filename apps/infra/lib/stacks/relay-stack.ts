import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { ROOT_DOMAIN } from "../domains";
import { logGroupPath, resourcePrefix } from "../table-names";

export interface RelayStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  vpc: ec2.IVpc;
  /** The platform cluster (container-stack.ts). */
  cluster: ecs.ICluster;
  /** The existing internal ALB, its security group and its HTTPS listener. */
  alb: elbv2.IApplicationLoadBalancer;
  albSecurityGroup: ec2.ISecurityGroup;
  albHttpsListenerArn: string;
  certificate: acm.ICertificate;
  hostedZone: route53.IHostedZone;
}

/**
 * The Lightbulb Connect relay: one iroh-relay task behind the existing internal
 * ALB, reached from the internet through CloudFront → VPC origin (the bv- door
 * pattern in api-stack.ts). TLS ends at the ALB; the task serves plain HTTP on
 * :8080 as a nonroot user. No NLB and no UDP: QUIC address discovery is off.
 * A relay forwards end-to-end QUIC ciphertext only.
 */
export class RelayStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: RelayStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const host = env === "prod" ? `relay.${rootDomain}` : `relay-${env}.${rootDomain}`;

    const taskDefinition = new ecs.FargateTaskDefinition(this, "RelayTask", {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    taskDefinition.addContainer("relay", {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, "..", "..", "relay"), {
        platform: Platform.LINUX_ARM64,
      }),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "relay",
        logGroup: new logs.LogGroup(this, "RelayLogs", {
          logGroupName: logGroupPath(env, "relay"),
          retention: logs.RetentionDays.ONE_MONTH,
        }),
      }),
      // 9090 (metrics) is deliberately not mapped or routed: VPC only.
      portMappings: [{ containerPort: 8080, protocol: ecs.Protocol.TCP }],
    });

    const taskSg = new ec2.SecurityGroup(this, "RelayTaskSg", {
      vpc: props.vpc,
      description: `Lightbulb ${env} Connect relay task`,
      allowAllOutbound: true,
    });
    taskSg.addIngressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(9090),
      "iroh-relay metrics, VPC only",
    );

    const service = new ecs.FargateService(this, "RelayService", {
      cluster: props.cluster,
      taskDefinition,
      serviceName: `${resourcePrefix(env)}relay`,
      desiredCount: 1,
      securityGroups: [taskSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      circuitBreaker: { rollback: true },
      // Stop the old task before the new one starts (no overlap; members
      // reconnect). ECS refuses maximumPercent <= 100 with AZ rebalancing on.
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      availabilityZoneRebalancing: ecs.AvailabilityZoneRebalancing.DISABLED,
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, "RelayTargetGroup", {
      vpc: props.vpc,
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targets: [service],
      deregistrationDelay: cdk.Duration.seconds(10),
      healthCheck: { path: "/generate_204", healthyHttpCodes: "204" },
    });

    // One host-header rule on the EXISTING HTTPS listener (network-stack.ts).
    // Registering the target group against it also opens :8080 on the task SG
    // to the ALB's SG, and nothing else.
    new elbv2.ApplicationListenerRule(this, "RelayHostRule", {
      listener: elbv2.ApplicationListener.fromApplicationListenerAttributes(this, "HttpsListener", {
        listenerArn: props.albHttpsListenerArn,
        // Immutable here: the ALB's SG is network-stack's; only the task SG
        // (this stack) gains a rule, so no cross-stack cycle.
        securityGroup: ec2.SecurityGroup.fromSecurityGroupId(
          this,
          "AlbSg",
          props.albSecurityGroup.securityGroupId,
          { mutable: false },
        ),
      }),
      priority: 10,
      conditions: [elbv2.ListenerCondition.hostHeaders([host])],
      targetGroups: [targetGroup],
    });

    // CloudFront → VPC origin → ALB :443. Step 1 of a two-step first deploy:
    // a freshly created VpcOrigin can still be "Deploying" when a distribution
    // points at it (api-stack.ts; it broke a prod first create), so the
    // distribution, its aliases and alarms land in the next deploy.
    new cloudfront.VpcOrigin(this, "RelayVpcOrigin", {
      endpoint: cloudfront.VpcOriginEndpoint.applicationLoadBalancer(props.alb),
      vpcOriginName: `${resourcePrefix(env)}relay`,
      httpsPort: 443,
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
    });

    new cdk.CfnOutput(this, "RelayUrlOutput", { value: `https://${host}` });
  }
}
