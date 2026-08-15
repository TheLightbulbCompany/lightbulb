import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as rds from "aws-cdk-lib/aws-rds";
import { Match, Template } from "aws-cdk-lib/assertions";
import {
  ObservabilityStack,
  ObservabilityStackProps,
} from "../lib/stacks/observability-stack";

/**
 * Synthesize the stack for one environment.
 *
 * Parameterized on envName because several code paths are prod-only, and a
 * suite that synthesizes ONLY dev cannot see them. That gap has already cost
 * a red main: a prod-only branch referenced a property deleted by the FIS
 * cleanup, every dev test passed, and `cdk synth` broke on deploy. vitest
 * compiles through esbuild, which strips types without checking them, so
 * green tests are not evidence the prod path even compiles.
 */
function buildTemplate(envName: "dev" | "prod"): Template {
  {
    const app = new cdk.App();
    const env = {
      account: "123456789012",
      region: "us-east-1",
    };

    // Create mock dependent resources in a support stack
    const supportStack = new cdk.Stack(app, "SupportStack", { env });

    const vpc = new ec2.Vpc(supportStack, "Vpc");
    const alb = new elbv2.ApplicationLoadBalancer(supportStack, "Alb", {
      vpc,
      internetFacing: false,
    });

    const cluster = new ecs.Cluster(supportStack, "Cluster", { vpc });

    const efsFs = new efs.FileSystem(supportStack, "Efs", { vpc });

    const taskDef = new ecs.FargateTaskDefinition(
      supportStack,
      "TaskDef",
    );
    taskDef.addContainer("backend", {
      image: ecs.ContainerImage.fromRegistry("alpine"),
      portMappings: [{ containerPort: 8000 }],
    });

    const tg = new elbv2.ApplicationTargetGroup(supportStack, "TG", {
      vpc,
      port: 8000,
      targetType: elbv2.TargetType.IP,
    });

    const service = new ecs.FargateService(supportStack, "Service", {
      cluster,
      taskDefinition: taskDef,
    });

    const paperclipService = new ecs.FargateService(
      supportStack,
      "PaperclipService",
      {
        cluster,
        taskDefinition: taskDef,
      },
    );

    const paperclipDbCluster = new rds.DatabaseCluster(
      supportStack,
      "PaperclipDb",
      {
        engine: rds.DatabaseClusterEngine.auroraPostgres({
          version: rds.AuroraPostgresEngineVersion.VER_16_4,
        }),
        writer: rds.ClusterInstance.serverlessV2("writer"),
        serverlessV2MinCapacity: 0,
        serverlessV2MaxCapacity: 4,
        vpc,
        clusterIdentifier: "isol8-dev-paperclip-db",
      },
    );

    const makeTable = (id: string) =>
      new dynamodb.Table(supportStack, id, {
        partitionKey: {
          name: "pk",
          type: dynamodb.AttributeType.STRING,
        },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

    const props: ObservabilityStackProps = {
      env,
      envName,
      backendService: service,
      backendLogGroupName: `/ecs/isol8-${envName}`,
      alb,
      targetGroup: tg,
      wsApiId: "test-ws-api-id",
      cluster,
      efsFileSystem: efsFs,
      databaseTables: {
        usersTable: makeTable("Users"),
        containersTable: makeTable("Containers"),
        billingTable: makeTable("Billing"),
        usageCountersTable: makeTable("Usage"),
        pendingUpdatesTable: makeTable("Pending"),
        channelLinksTable: makeTable("Channels"),
        companySlugsTable: makeTable("CompanySlugs"),
        agentEmailAddressesTable: makeTable("AgentEmailAddresses"),
      },
      connectionsTableName: `isol8-${envName}-ws-connections`,
      authorizerFunctionName: `isol8-${envName}-ws-authorizer`,
      paperclipDbCluster,
      paperclipService,
    };

    const obsStack = new ObservabilityStack(
      app,
      "TestObservabilityStack",
      props,
    );
    return Template.fromStack(obsStack);
  }
}

// Prod synthesis is asserted on separately below; building it here also means
// a prod-only compile/synth break fails the suite instead of the deploy.
const PROD_TEMPLATE = buildTemplate("prod");

describe("ObservabilityStack", () => {
  let template: Template;

  beforeAll(() => {
    template = buildTemplate("dev");
  });

  test("creates 2 SNS topics", () => {
    template.resourceCountIs("AWS::SNS::Topic", 2);
  });

  test("creates at least 65 CloudWatch alarms", () => {
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    const count = Object.keys(alarms).length;
    // 82 alarms: 11 page + 28 warn custom + 3 x 7 per-table DDB
    // + remaining AWS-native + 2 cost
    expect(count).toBeGreaterThanOrEqual(65);
  });

  test("creates 1 CloudWatch dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
  });

  // GuardDuty + Access Analyzer already exist at account level — not created by CDK.
  // We only create EventBridge rules to route their findings to SNS.
  test("creates EventBridge rules for security findings", () => {
    // GuardDuty + Access Analyzer + ECS TaskStopped (W39) + ECS TaskRunning
    // (W59 multi-task detector) + Aurora failover/failure (W61b).
    template.resourceCountIs("AWS::Events::Rule", 5);
  });

  test("creates AWS Budget", () => {
    template.resourceCountIs("AWS::Budgets::Budget", 1);
  });

  // Both tiers must have a live destination. Alarms are worthless without one:
  // these topics previously ran with ZERO subscribers while ~105 alarms fired
  // into the void. Assert on BOTH topics, and assert the count, so a
  // subscription that gets dropped rather than repointed also fails here.
  test("both alert topics subscribe the alert email", () => {
    const subs = template.findResources("AWS::SNS::Subscription");
    const byTopic = Object.values(subs).map((s: any) => ({
      topic: JSON.stringify(s.Properties.TopicArn),
      endpoint: s.Properties.Endpoint,
      protocol: s.Properties.Protocol,
    }));

    expect(byTopic).toHaveLength(2);
    expect(new Set(byTopic.map((s) => s.topic)).size).toBe(2); // page + warn, not two on one
    for (const s of byTopic) {
      expect(s.protocol).toBe("email");
      expect(s.endpoint).toBe("admin@isol8.co");
    }
  });

  // The only outside-in signal in the stack. Every other alarm is internal
  // and can look healthy while users cannot reach the product at all.
  test("creates Route53 health checks for the app and API front doors", () => {
    const checks = template.findResources("AWS::Route53::HealthCheck");
    const hosts = Object.values(checks).map(
      (c: any) => c.Properties.HealthCheckConfig.FullyQualifiedDomainName,
    );
    expect(hosts.sort()).toEqual(["api-dev.isol8.co", "dev.isol8.co"]);

    for (const c of Object.values(checks) as any[]) {
      expect(c.Properties.HealthCheckConfig.Type).toBe("HTTPS");
    }
  });

  test("uptime alarms treat missing data as breaching", () => {
    // Route53 publishes HealthCheckStatus every minute unconditionally, so
    // absent data means the check is gone — indistinguishable from an
    // outage, and must never be read as healthy.
    for (const alarmName of [
      "isol8-dev-P13-app-unreachable",
      "isol8-dev-P14-api-unreachable",
    ]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: alarmName,
        ComparisonOperator: "LessThanThreshold",
        TreatMissingData: "breaching",
      });
    }
  });

  test("page-tier alarm P1 exists with correct name", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "isol8-dev-P1-container-error-state",
    });
  });

  test("dashboard has correct name", () => {
    template.hasResourceProperties("AWS::CloudWatch::Dashboard", {
      DashboardName: "isol8-dev-orr",
    });
  });

  test("ALB host-count alarms (W28/W48) carry BOTH LoadBalancer and TargetGroup dimensions", () => {
    // HealthyHostCount / UnHealthyHostCount are per-target-group metrics: an
    // alarm scoped to LoadBalancer alone receives no datapoints, so W48
    // (TreatMissingData=breaching) sits in ALARM forever. Guard both alarms so
    // the TargetGroup dimension cannot be dropped again.
    for (const alarmName of [
      "isol8-dev-W48-alb-healthy-hosts-drop",
      "isol8-dev-W28-alb-unhealthy-hosts",
    ]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: alarmName,
        Dimensions: Match.arrayWith([
          Match.objectLike({ Name: "LoadBalancer" }),
          Match.objectLike({ Name: "TargetGroup" }),
        ]),
      });
    }
  });

  test("EventBridge rule captures ECS task stopped events", () => {
    template.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: {
        source: ["aws.ecs"],
        "detail-type": ["ECS Task State Change"],
      },
    });
  });

  // ---------------------------------------------------------------------
  // Backend-liveness heartbeat (formerly the reaper heartbeat; the metric
  // gateway.running.count survived the flat-fee cutover, the alarm was
  // renamed backend-liveness in Phase 1).
  // ---------------------------------------------------------------------

  test("creates the reaper heartbeat alarm on SampleCount(gateway.running.count)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "gateway.running.count",
      Namespace: "Isol8",
      Statistic: "SampleCount",
      ComparisonOperator: "LessThanThreshold",
      Threshold: 5,
      TreatMissingData: "breaching",
    });
  });

  // NOTE: the former "reaper-crash alarm" test (Sum gateway.idle_checker.crash)
  // and the "scale-to-zero dashboard widgets" test (gateway.running.count.by_tier
  // / record_activity.count / idle.scale_to_zero) were removed here: Phase 1
  // (alarm hygiene) deleted both the reaper-crash phantom alarm and the
  // scale-to-zero dashboard widgets (the reaper/scale-to-zero feature was
  // removed in the 2026-04 flat-fee cutover). They asserted deleted resources
  // and had been failing on main since Phase 1. The heartbeat test above still
  // holds — Phase 1 kept gateway.running.count, renaming its alarm to
  // backend-liveness.
});

describe("ObservabilityStack — prod-only paths", () => {
  const template = PROD_TEMPLATE;

  // PagerDuty is what turns a page into a phone push. It is prod-only, so
  // nothing in the dev suite covers it.
  test("prod page topic also delivers to PagerDuty over HTTPS", () => {
    const subs = Object.values(template.findResources("AWS::SNS::Subscription"));
    const https = subs.filter((s: any) => s.Properties.Protocol === "https");

    expect(https).toHaveLength(1);
    // Raw delivery MUST stay off: PagerDuty's CloudWatch integration parses
    // the SNS envelope and expects a nested JSON object in Message. Raw
    // delivery strips it and no incident is ever created.
    expect(https[0].Properties.RawMessageDelivery).toBe(false);
  });

  // The URL embeds the integration key. It must arrive as a deploy-time SSM
  // reference, never as a literal baked into the synthesized template.
  test("the PagerDuty integration key is not baked into the template", () => {
    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).not.toMatch(/events\.pagerduty\.com/);

    const subs = Object.values(template.findResources("AWS::SNS::Subscription"));
    const https: any = subs.find((s: any) => s.Properties.Protocol === "https");
    expect(https.Properties.Endpoint).toHaveProperty("Ref");
  });

  test("dev does NOT get a PagerDuty subscription", () => {
    const devSubs = Object.values(
      buildTemplate("dev").findResources("AWS::SNS::Subscription"),
    );
    expect(devSubs.every((s: any) => s.Properties.Protocol === "email")).toBe(true);
  });

  test("prod uptime alarms page rather than warn", () => {
    for (const name of [
      "isol8-prod-P13-app-unreachable",
      "isol8-prod-P14-api-unreachable",
    ]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: name,
        AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
      });
    }
  });

  test("prod health checks target the real public hostnames", () => {
    const hosts = Object.values(template.findResources("AWS::Route53::HealthCheck"))
      .map((c: any) => c.Properties.HealthCheckConfig.FullyQualifiedDomainName);
    expect(hosts.sort()).toEqual(["api.isol8.co", "isol8.co"]);
  });
});
