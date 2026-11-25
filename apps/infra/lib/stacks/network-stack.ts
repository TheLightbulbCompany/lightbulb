import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import { Construct } from "constructs";

export interface NetworkStackProps extends cdk.StackProps {
  environment: string;
}

const ENV_CONFIG: Record<string, { maxAzs: number; cidr: string }> = {
  dev: { maxAzs: 2, cidr: "10.0.0.0/16" },
  prod: { maxAzs: 3, cidr: "10.2.0.0/16" },
};

/**
 * No NAT at all. The backend's Fargate tasks sit in the public subnets with a
 * public IP. The private subnets hold only the internal ALB and the API Gateway
 * VPC Link ENIs, which need no outbound internet, so they are isolated. Same
 * shape in dev and prod.
 */
export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  public readonly albHttpListenerArn: string;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const config = ENV_CONFIG[env] ?? ENV_CONFIG.dev;

    this.vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr(config.cidr),
      maxAzs: config.maxAzs,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          // The group name keeps the subnets' logical ids, CIDRs and export names.
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
      restrictDefaultSecurityGroup: true,
    });

    // S3 Gateway Endpoint — free, routes S3 traffic (incl. ECR image layers) through AWS.
    this.vpc.addGatewayEndpoint("S3Endpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    // -------------------------------------------------------------------------
    // ALB Security Group
    // -------------------------------------------------------------------------
    this.albSecurityGroup = new ec2.SecurityGroup(this, "AlbSecurityGroup", {
      vpc: this.vpc,
      // Do not "fix" the description: changing it replaces the security group.
      description: `Isol8 ${env} internal ALB security group`,
      allowAllOutbound: true,
    });

    // ALB accepts plain HTTP from within the VPC (the API Gateway VPC Link).
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(80),
      "HTTP from VPC (API Gateway)",
    );

    // -------------------------------------------------------------------------
    // ALB (internal) + target group + listener. The `V2` construct ids are the
    // deployed logical ids and stay.
    // -------------------------------------------------------------------------
    this.alb = new elbv2.ApplicationLoadBalancer(this, "AlbV2", {
      vpc: this.vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroup: this.albSecurityGroup,
      idleTimeout: cdk.Duration.seconds(300),
      deletionProtection: env === "prod",
      loadBalancerName: `lightbulb-${env}`,
    });

    // Target Group (IP target type for Fargate awsvpc networking)
    this.targetGroup = new elbv2.ApplicationTargetGroup(this, "TargetGroupV2", {
      vpc: this.vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targetGroupName: `lightbulb-${env}-backend`,
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

    // HTTP Listener — forwards to the target group (NOT redirect). The API
    // Gateway VPC Link sends plain HTTP internally; API Gateway terminates TLS.
    const httpListener = this.alb.addListener("HttpListener", {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [this.targetGroup],
    });
    this.albHttpListenerArn = httpListener.listenerArn;
  }
}
