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

describe("ObservabilityStack", () => {
  let template: Template;

  beforeAll(() => {
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
      envName: "dev",
      backendService: service,
      backendLogGroupName: "/ecs/isol8-dev",
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
      },
      connectionsTableName: "isol8-dev-ws-connections",
      authorizerFunctionName: "isol8-dev-ws-authorizer",
      paperclipDbCluster,
      paperclipService,
    };

    const obsStack = new ObservabilityStack(
      app,
      "TestObservabilityStack",
      props,
    );
    template = Template.fromStack(obsStack);
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

  test("page topic has email subscription", () => {
    template.hasResourceProperties("AWS::SNS::Subscription", {
      Protocol: "email",
      Endpoint: "oncall@isol8.co",
    });
  });

  test("warn topic has email subscription", () => {
    template.hasResourceProperties("AWS::SNS::Subscription", {
      Protocol: "email",
      Endpoint: "alerts@isol8.co",
    });
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
