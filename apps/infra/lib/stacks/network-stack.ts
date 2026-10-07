import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
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
  /** Same objects as `alb`/`targetGroup`; kept so the stage wiring reads either name. */
  public readonly albV2: elbv2.ApplicationLoadBalancer;
  public readonly targetGroupV2: elbv2.ApplicationTargetGroup;
  public readonly albHttpListenerArnV2: string;
  public readonly albHttpsListenerArnV2?: string;

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

    // S3 Gateway Endpoint — free, routes S3 traffic (incl. ECR image layers)
    // through AWS instead of the NAT gateway ($0.045/GB data processing)
    this.vpc.addGatewayEndpoint("S3Endpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
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
    // ALB (internal) + target group + listeners. The legacy auto-named set was
    // deleted in the last step of the isol8 -> lightbulb rename; the `V2`
    // construct ids are the deployed logical ids and stay.
    // -------------------------------------------------------------------------
    const named = this.buildAlb("V2", props, {
      deletionProtection: env === "prod",
      loadBalancerName: `lightbulb-${env}`,
      targetGroupName: `lightbulb-${env}-backend`,
    });
    this.alb = named.alb;
    this.targetGroup = named.targetGroup;
    this.albHttpListenerArn = named.httpListenerArn;
    this.albHttpsListenerArn = named.httpsListenerArn;
    this.albV2 = named.alb;
    this.targetGroupV2 = named.targetGroup;
    this.albHttpListenerArnV2 = named.httpListenerArn;
    this.albHttpsListenerArnV2 = named.httpsListenerArn;

    // -------------------------------------------------------------------------
    // WAF web ACL on the ALB — COUNT ONLY. WAFv2 cannot attach to an API Gateway
    // HTTP API (REST APIs only), and the AppSync Event API is not a supported
    // resource, so the ALB behind the VPC Link is the one choke point that
    // covers the backend REST, the Paperclip host route and the bv- listener.
    // Every rule counts and the default action is allow: this ships to read the
    // metrics. Switching to block is a separate, owner-approved change, and
    // test/network-stack.test.ts fails if a rule stops being count-only.
    // -------------------------------------------------------------------------
    const countOnly = { count: {} };
    const managed = (name: string, priority: number): wafv2.CfnWebACL.RuleProperty => ({
      name,
      priority,
      overrideAction: countOnly,
      statement: { managedRuleGroupStatement: { vendorName: "AWS", name } },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        sampledRequestsEnabled: true,
        metricName: `${env}-${name}`,
      },
    });
    const webAcl = new wafv2.CfnWebACL(this, "AlbWebAcl", {
      name: `lightbulb-${env}-alb`,
      scope: "REGIONAL",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        sampledRequestsEnabled: true,
        metricName: `lightbulb-${env}-alb`,
      },
      rules: [
        managed("AWSManagedRulesCommonRuleSet", 10),
        managed("AWSManagedRulesKnownBadInputsRuleSet", 20),
        {
          // 10,000 requests / 5 min / client IP (~33 rps sustained). API Gateway's
          // stage throttle (prod 200 rps, 500 burst) is aggregate, so this only
          // flags a single source flooding. The ALB sits behind API Gateway, so
          // the source address is API GW's: key on the forwarded client IP.
          // ponytail: X-Forwarded-For's first hop is client-suppliable; fine for
          // counting, revisit before switching this rule to block.
          name: "PerIpRateLimit",
          priority: 30,
          action: countOnly,
          statement: {
            rateBasedStatement: {
              limit: 10000,
              evaluationWindowSec: 300,
              aggregateKeyType: "FORWARDED_IP",
              forwardedIpConfig: { headerName: "X-Forwarded-For", fallbackBehavior: "MATCH" },
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            sampledRequestsEnabled: true,
            metricName: `${env}-PerIpRateLimit`,
          },
        },
      ],
    });
    new wafv2.CfnWebACLAssociation(this, "AlbWebAclAssociation", {
      resourceArn: named.alb.loadBalancerArn,
      webAclArn: webAcl.attrArn,
    });

    this.albSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(8081),
      "CloudFront VPC origin to browser-view relay (path-capped by listener rule)",
    );
  }

  private buildAlb(
    idSuffix: string,
    props: NetworkStackProps,
    names: { deletionProtection: boolean; loadBalancerName?: string; targetGroupName?: string },
  ): {
    alb: elbv2.ApplicationLoadBalancer;
    targetGroup: elbv2.ApplicationTargetGroup;
    httpListenerArn: string;
    httpsListenerArn?: string;
  } {
    const alb = new elbv2.ApplicationLoadBalancer(this, `Alb${idSuffix}`, {
      vpc: this.vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroup: this.albSecurityGroup,
      idleTimeout: cdk.Duration.seconds(300),
      deletionProtection: names.deletionProtection,
      loadBalancerName: names.loadBalancerName,
    });

    // Target Group (IP target type for Fargate awsvpc networking)
    const targetGroup = new elbv2.ApplicationTargetGroup(this, `TargetGroup${idSuffix}`, {
      vpc: this.vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targetGroupName: names.targetGroupName,
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

    // HTTPS Listener (only if certificate is provided)
    let httpsListenerArn: string | undefined;
    if (props.certificate) {
      const httpsListener = alb.addListener("HttpsListener", {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [props.certificate],
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
        defaultTargetGroups: [targetGroup],
      });
      httpsListenerArn = httpsListener.listenerArn;
    }

    // HTTP Listener — forwards to target group (NOT redirect)
    // API Gateway VPC Link sends plain HTTP internally (API GW handles public TLS)
    const httpListener = alb.addListener("HttpListener", {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [targetGroup],
    });

    // Browser-view listener (:8081) — the ONLY WebSocket-capable door to the
    // backend. api[-env] is an API Gateway HTTP API, and HTTP APIs do not pass
    // WebSocket upgrades (verified live: the upgrade dies with an ALB 400 and
    // never reaches uvicorn), so the machine gateway relay
    // (routers/gateway_relay.py) rides CloudFront → VPC origin → this listener
    // instead (api-stack.ts, BrowserViewDistribution). The construct ids keep
    // their BrowserView names: renaming one replaces the listener and the
    // distribution, and the container-browser live view they were named for is
    // gone, not the door.
    //
    // A dedicated port because path scoping must live HERE: CloudFront VPC
    // origins put their ENIs behind an AWS-managed security group that only
    // exists after the first deploy, so the ALB ingress rule admits the VPC
    // CIDR — meaning anything in-VPC (per-user containers included) can reach
    // this port. The fixed-404 default + single path rule cap that at exactly
    // the one Clerk-authenticated relay route, which is the same exposure the
    // public CloudFront door grants anyway. Widening this listener's routing
    // widens what a hostile container can reach.
    const browserViewListener = alb.addListener("BrowserViewListener", {
      port: 8081,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultAction: elbv2.ListenerAction.fixedResponse(404, {
        contentType: "application/json",
        messageBody: '{"detail":"Not found"}',
      }),
    });
    browserViewListener.addAction("BrowserViewRelay", {
      priority: 10,
      // The raw machine-relay WS door (routers/gateway_relay.py — desktop node
      // hosts + Paperclip operators) is the only route left on this listener:
      // /api/v1/browser-view/* went with routers/browser_view.py, so the cap is
      // narrowed to match. A device or service token authenticates at the
      // application layer; widening this pattern widens what a hostile in-VPC
      // caller can reach.
      conditions: [elbv2.ListenerCondition.pathPatterns(["/api/v1/gateway/*"])],
      action: elbv2.ListenerAction.forward([targetGroup]),
    });

    return { alb, targetGroup, httpListenerArn: httpListener.listenerArn, httpsListenerArn };
  }
}
