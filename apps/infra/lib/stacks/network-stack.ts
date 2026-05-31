import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import { Construct } from "constructs";

export interface NetworkStackProps extends cdk.StackProps {
  environment: string;
  certificate?: acm.ICertificate;
}

const ENV_CONFIG: Record<string, { maxAzs: number; cidr: string }> = {
  dev: { maxAzs: 2, cidr: "10.0.0.0/16" },
  prod: { maxAzs: 3, cidr: "10.2.0.0/16" },
};

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  public readonly albHttpListenerArn: string;
  public readonly albHttpsListenerArn?: string;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const config = ENV_CONFIG[env] ?? ENV_CONFIG.dev;

    this.vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr(config.cidr),
      maxAzs: config.maxAzs,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
      restrictDefaultSecurityGroup: true,
    });

    // -------------------------------------------------------------------------
    // ALB Security Group
    // -------------------------------------------------------------------------
    this.albSecurityGroup = new ec2.SecurityGroup(this, "AlbSecurityGroup", {
      vpc: this.vpc,
      description: `Isol8 ${env} internal ALB security group`,
      allowAllOutbound: true,
    });

    // ALB accepts traffic from within VPC (API Gateway VPC Link)
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      "HTTPS from VPC (API Gateway)",
    );
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(80),
      "HTTP from VPC (API Gateway)",
    );

    // -------------------------------------------------------------------------
    // ALB (internal)
    // -------------------------------------------------------------------------
    this.alb = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc: this.vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroup: this.albSecurityGroup,
      idleTimeout: cdk.Duration.seconds(300),
      deletionProtection: env === "prod",
    });

    // -------------------------------------------------------------------------
    // Target Group (IP target type for Fargate awsvpc networking)
    // -------------------------------------------------------------------------
    this.targetGroup = new elbv2.ApplicationTargetGroup(this, "TargetGroup", {
      vpc: this.vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      // Drain a deregistering task quickly so a rolling deploy / task stop
      // finishes in-flight work and then exits. Kept <= the ECS container
      // stopTimeout (30s) so the task is fully drained before SIGKILL.
      deregistrationDelay: cdk.Duration.seconds(25),
      healthCheck: {
        path: "/health",
        interval: cdk.Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        timeout: cdk.Duration.seconds(10),
        healthyHttpCodes: "200",
      },
      stickinessCookieDuration: cdk.Duration.hours(1),
    });

    // -------------------------------------------------------------------------
    // HTTPS Listener (only if certificate is provided)
    // -------------------------------------------------------------------------
    if (props.certificate) {
      const httpsListener = this.alb.addListener("HttpsListener", {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [props.certificate],
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
        defaultTargetGroups: [this.targetGroup],
      });
      this.albHttpsListenerArn = httpsListener.listenerArn;
    }

    // -------------------------------------------------------------------------
    // HTTP Listener — forwards to target group (NOT redirect)
    // API Gateway VPC Link sends plain HTTP internally (API GW handles public TLS)
    // -------------------------------------------------------------------------
    const httpListener = this.alb.addListener("HttpListener", {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [this.targetGroup],
    });
    this.albHttpListenerArn = httpListener.listenerArn;
  }
}
