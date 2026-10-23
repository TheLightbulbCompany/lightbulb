import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as sns from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";
import { ROOT_DOMAIN } from "../domains";
import { alertTopicArn, logGroupPath, resourcePrefix } from "../table-names";

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

    // CloudFront → VPC origin → ALB :443. The viewer Host header is forwarded
    // (AllViewer), so the ALB rule matches and CloudFront checks the ALB's
    // wildcard certificate against relay[-dev].<root>.
    const vpcOrigin = new cloudfront.VpcOrigin(this, "RelayVpcOrigin", {
      endpoint: cloudfront.VpcOriginEndpoint.applicationLoadBalancer(props.alb),
      vpcOriginName: `${resourcePrefix(env)}relay`,
      httpsPort: 443,
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
    });
    const distribution = new cloudfront.Distribution(this, "RelayDistribution", {
      comment: `lightbulb ${env} Connect relay`,
      domainNames: [host],
      certificate: props.certificate,
      defaultBehavior: {
        // iroh-relay pings every client at least every ~20 s, well inside
        // CloudFront's 60 s read timeout and the ALB's 300 s idle timeout.
        origin: origins.VpcOrigin.withVpcOrigin(vpcOrigin, {
          readTimeout: cdk.Duration.seconds(60),
        }),
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
      },
    });
    // A and AAAA: CloudFront serves IPv6 by default.
    const alias = route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution));
    new route53.ARecord(this, "RelayDnsRecord", { zone: props.hostedZone, recordName: host, target: alias });
    new route53.AaaaRecord(this, "RelayDnsRecordV6", { zone: props.hostedZone, recordName: host, target: alias });

    const warn = new cloudwatchActions.SnsAction(
      sns.Topic.fromTopicArn(this, "WarnTopic", alertTopicArn(this.region, this.account, env, "warn")),
    );
    const hour = { period: cdk.Duration.hours(1) };
    // ponytail: 20 GiB/h (~$1.70/h of CloudFront egress) is a starting guess;
    // tune it once real traffic is known.
    new cloudwatch.Alarm(this, "RelayBytesAlarm", {
      alarmName: `${resourcePrefix(env)}relay-bytes-transferred`,
      alarmDescription: "Connect relay moved more than 20 GiB in an hour (abuse or a stuck pair).",
      metric: new cloudwatch.MathExpression({
        expression: "down + up",
        label: "BytesTransferred",
        usingMetrics: {
          down: distribution.metricBytesDownloaded(hour),
          up: distribution.metricBytesUploaded(hour),
        },
        period: hour.period,
      }),
      threshold: 20 * 1024 ** 3,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(warn);
    new cloudwatch.Alarm(this, "Relay5xxAlarm", {
      alarmName: `${resourcePrefix(env)}relay-5xx`,
      alarmDescription: "Connect relay: CloudFront 5xx rate above 5% for 15 minutes.",
      metric: distribution.metric5xxErrorRate({ period: cdk.Duration.minutes(5) }),
      threshold: 5,
      evaluationPeriods: 3,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(warn);

    new cdk.CfnOutput(this, "RelayUrlOutput", { value: `https://${host}` });
  }
}
