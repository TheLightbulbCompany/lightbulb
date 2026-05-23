import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Template } from "aws-cdk-lib/assertions";
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
      wsApiId: "test-ws-api-id",
      cluster,
      efsFileSystem: efsFs,
      databaseTables: {
        usersTable: makeTable("Users"),
        containersTable: makeTable("Containers"),
        billingTable: makeTable("Billing"),
        apiKeysTable: makeTable("ApiKeys"),
        usageCountersTable: makeTable("Usage"),
        pendingUpdatesTable: makeTable("Pending"),
        channelLinksTable: makeTable("Channels"),
      },
      connectionsTableName: "isol8-dev-ws-connections",
      authorizerFunctionName: "isol8-dev-ws-authorizer",
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
    // 81 alarms: 11 page + 27 warn custom + 3 x 7 per-table DDB
    // + remaining AWS-native + 2 cost
    expect(count).toBeGreaterThanOrEqual(65);
  });

  test("creates 1 CloudWatch dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
  });

  // GuardDuty + Access Analyzer already exist at account level — not created by CDK.
  // We only create EventBridge rules to route their findings to SNS.
  test("creates EventBridge rules for security findings", () => {
    template.resourceCountIs("AWS::Events::Rule", 3); // GuardDuty + Access Analyzer + ECS TaskStopped
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

  test("EventBridge rule captures ECS task stopped events", () => {
    template.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: {
        source: ["aws.ecs"],
        "detail-type": ["ECS Task State Change"],
      },
    });
  });

  // ---------------------------------------------------------------------
  // Free-tier scale-to-zero reaper alarms
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

  test("creates the reaper-crash alarm on Sum(gateway.idle_checker.crash)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "gateway.idle_checker.crash",
      Namespace: "Isol8",
      Statistic: "Sum",
      Threshold: 1,
    });
  });

  // ---------------------------------------------------------------------
  // Scale-to-zero dashboard widgets (smoke tests — confirm rendered into
  // the CloudFormation Dashboard body, not a typo)
  // ---------------------------------------------------------------------

  test("dashboard widgets reference the new scale-to-zero metrics", () => {
    const dashboards = template.findResources("AWS::CloudWatch::Dashboard");
    const bodies = Object.values(dashboards).map((r) =>
      JSON.stringify(r.Properties.DashboardBody),
    );
    const allBodies = bodies.join("");

    // Each new metric should appear at least once in the dashboard body.
    expect(allBodies).toContain("gateway.running.count.by_tier");
    expect(allBodies).toContain("gateway.cold_start.count");
    expect(allBodies).toContain("gateway.cold_start.latency");
    expect(allBodies).toContain("gateway.record_activity.count");
  });
});
