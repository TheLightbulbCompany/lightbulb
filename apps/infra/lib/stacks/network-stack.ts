import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatch_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as sns from "aws-cdk-lib/aws-sns";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import { alertTopicArn, resourcePrefix } from "../table-names";

export interface NetworkStackProps extends cdk.StackProps {
  environment: string;
  certificate?: acm.ICertificate;
}

const ENV_CONFIG: Record<string, { maxAzs: number; cidr: string }> = {
  dev: { maxAzs: 2, cidr: "10.0.0.0/16" },
  prod: { maxAzs: 3, cidr: "10.2.0.0/16" },
};

/**
 * No managed NAT gateway ($32.85/mo + $0.045/GB each). Fargate tasks get a public IP in the
 * public subnets (ECS: "When using a public subnet, you can assign a public IP address to the
 * task ENI"); the private subnets keep egress through one t4g.micro NAT instance, because the
 * AgentCore runtimes, the workspace browser and the VPC Lambdas cannot hold a public IP.
 * Same shape in dev and prod.
 */

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

    // ponytail: one NAT instance in one AZ. A host failure is auto-recovered and pages (alarms below);
    // an AZ outage still cuts runtime/browser/Lambda egress (the API and Temporal tasks keep their own
    // public IPs). Add fck-nat's HA mode or a NAT per AZ if that outage starts to matter.
    const natInstance = ec2.NatProvider.instanceV2({
      // Not t4g.nano: its 512 MB OOM-kills the default user data's `yum install iptables-services`
      // on AL2023, so the instance boots with no MASQUERADE rule and forwards nothing (dev, 8 Nov).
      instanceType: new ec2.InstanceType("t4g.micro"),
      // CDK's default admits 0.0.0.0/0 on every port; only the VPC routes through it.
      defaultAllowedTraffic: ec2.NatTrafficDirection.OUTBOUND_ONLY,
    });

    this.vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr(config.cidr),
      maxAzs: config.maxAzs,
      natGateways: 1,
      natGatewayProvider: natInstance,
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

    // User data runs only on an instance's first boot, and CDK's NAT Instance does not replace on a
    // user-data or type change, so bump this id to force a fresh instance when either changes.
    (natInstance.gatewayInstances[0].node.defaultChild as ec2.CfnInstance).overrideLogicalId("NatInstanceV2");
    natInstance.connections.allowFrom(ec2.Peer.ipv4(config.cidr), ec2.Port.allTraffic(), "VPC egress via NAT instance");
    // A fixed address: the workspace browser leaves from it (sites that bind a sign-in to an IP),
    // and it survives the instance being replaced on an AMI update.
    new ec2.CfnEIP(this, "NatInstanceEip", {
      domain: "vpc",
      instanceId: natInstance.gatewayInstances[0].instanceId,
    });

    // Every agent's model and tool calls ride this instance while the backend health checks stay
    // green, so its failures page. The topic is a constructed ARN (as service-stack does): a real
    // reference to ObservabilityStack would close a stack cycle.
    const pageAction = new cloudwatch_actions.SnsAction(
      sns.Topic.fromTopicArn(this, "AlertsPageImported", alertTopicArn(this.region, this.account, env, "page")),
    );
    const natCheck = (name: string, metricName: string, description: string) => {
      const alarm = new cloudwatch.Alarm(this, name, {
        alarmName: `${resourcePrefix(env)}${name}`,
        alarmDescription: description,
        metric: new cloudwatch.Metric({
          namespace: "AWS/EC2",
          metricName,
          dimensionsMap: { InstanceId: natInstance.gatewayInstances[0].instanceId },
          statistic: "Maximum",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.MISSING,
      });
      alarm.addAlarmAction(pageAction);
      return alarm;
    };
    // EC2 recover moves the instance to new hardware with the same id, ENI and EIP, so routes survive.
    natCheck(
      "nat-instance-system-check",
      "StatusCheckFailed_System",
      "PAGE: the NAT instance failed its AWS host check; EC2 is recovering it. Runtimes, the workspace browser and VPC Lambdas have no egress until it is back.",
    ).addAlarmAction(new cloudwatch_actions.Ec2Action(cloudwatch_actions.Ec2InstanceAction.RECOVER));
    natCheck(
      "nat-instance-status-check",
      "StatusCheckFailed_Instance",
      "PAGE: the NAT instance failed its guest status check (OS, network or iptables). Reboot it; runtimes, the workspace browser and VPC Lambdas have no egress.",
    );

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
    // covers the backend REST and the bv- listener.
    // Every rule counts and the default action is allow: this ships to read the
    // metrics. Switching to block is a separate, owner-approved change, and
    // test/network-stack.test.ts fails if a rule stops being count-only.
    // Request sampling is OFF: samples store raw URLs and headers, and this ALB
    // carries bearer tokens (device token in the bv- query string, Authorization).
    // -------------------------------------------------------------------------
    const countOnly = { count: {} };
    // A group-level count still lets an inner Block end evaluation at the first
    // match, hiding later rules' metrics, so each inner rule is overridden to
    // count too (AWS's testing shape). Names are from `aws wafv2
    // describe-managed-rule-group` (2026-10-07); a rule AWS adds later is still
    // covered by the group-level override, just not broken out in metrics.
    const managed = (
      name: string,
      priority: number,
      innerRules: string[],
    ): wafv2.CfnWebACL.RuleProperty => ({
      name,
      priority,
      overrideAction: countOnly,
      statement: {
        managedRuleGroupStatement: {
          vendorName: "AWS",
          name,
          ruleActionOverrides: innerRules.map((r) => ({ name: r, actionToUse: countOnly })),
        },
      },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        sampledRequestsEnabled: false,
        metricName: `${env}-${name}`,
      },
    });
    const webAcl = new wafv2.CfnWebACL(this, "AlbWebAcl", {
      name: `lightbulb-${env}-alb`,
      scope: "REGIONAL",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        sampledRequestsEnabled: false,
        metricName: `lightbulb-${env}-alb`,
      },
      rules: [
        managed("AWSManagedRulesCommonRuleSet", 10, [
          "NoUserAgent_HEADER",
          "UserAgent_BadBots_HEADER",
          "SizeRestrictions_QUERYSTRING",
          "SizeRestrictions_Cookie_HEADER",
          "SizeRestrictions_BODY",
          "SizeRestrictions_URIPATH",
          "EC2MetaDataSSRF_BODY",
          "EC2MetaDataSSRF_COOKIE",
          "EC2MetaDataSSRF_URIPATH",
          "EC2MetaDataSSRF_QUERYARGUMENTS",
          "GenericLFI_QUERYARGUMENTS",
          "GenericLFI_URIPATH",
          "GenericLFI_BODY",
          "RestrictedExtensions_URIPATH",
          "RestrictedExtensions_QUERYARGUMENTS",
          "GenericRFI_QUERYARGUMENTS",
          "GenericRFI_BODY",
          "GenericRFI_URIPATH",
          "CrossSiteScripting_COOKIE",
          "CrossSiteScripting_QUERYARGUMENTS",
          "CrossSiteScripting_BODY",
          "CrossSiteScripting_URIPATH",
        ]),
        managed("AWSManagedRulesKnownBadInputsRuleSet", 20, [
          "JavaDeserializationRCE_BODY",
          "JavaDeserializationRCE_URIPATH",
          "JavaDeserializationRCE_QUERYSTRING",
          "JavaDeserializationRCE_HEADER",
          "Host_localhost_HEADER",
          "PROPFIND_METHOD",
          "ExploitablePaths_URIPATH",
          "Log4JRCE_QUERYSTRING",
          "Log4JRCE_BODY",
          "Log4JRCE_URIPATH",
          "Log4JRCE_HEADER",
          "ReactJSRCE_BODY",
        ]),
        {
          // 10,000 requests / 5 min / client IP (~33 rps sustained). API Gateway's
          // stage throttle (prod 200 rps, 500 burst) is aggregate, so this only
          // flags a single source flooding. The ALB sits behind API Gateway, so
          // the source address is API GW's: key on X-Lightbulb-Client-Ip, which
          // the API GW integration overwrites with $context.identity.sourceIp
          // (api-stack.ts), so a client cannot spoof it. Traffic that skips API
          // GW (the bv- door) can carry a client-set one, so it is scoped out below.
          name: "PerIpRateLimit",
          priority: 30,
          action: countOnly,
          statement: {
            rateBasedStatement: {
              limit: 10000,
              evaluationWindowSec: 300,
              aggregateKeyType: "FORWARDED_IP",
              forwardedIpConfig: { headerName: "X-Lightbulb-Client-Ip", fallbackBehavior: "NO_MATCH" },
              // The bv- door (/api/v1/gateway/*) is scoped out: CloudFront forwards every
              // viewer header there, so a client could set X-Lightbulb-Client-Ip itself.
              scopeDownStatement: {
                notStatement: {
                  statement: {
                    byteMatchStatement: {
                      searchString: "/api/v1/gateway/",
                      fieldToMatch: { uriPath: {} },
                      positionalConstraint: "STARTS_WITH",
                      textTransformations: [{ priority: 0, type: "NONE" }],
                    },
                  },
                },
              },
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            sampledRequestsEnabled: false,
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
      // hosts) is the only route left on this listener:
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
