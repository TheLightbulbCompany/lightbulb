import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatch_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subs from "aws-cdk-lib/aws-sns-subscriptions";
// GuardDuty + Access Analyzer already enabled at account level — not managed by CDK
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as events from "aws-cdk-lib/aws-events";
import * as events_targets from "aws-cdk-lib/aws-events-targets";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as rds from "aws-cdk-lib/aws-rds";
import { Construct } from "constructs";

// ---------------------------------------------------------------------------
// Tunable thresholds
// ---------------------------------------------------------------------------

// Daily Bedrock cache-write token ceiling before we page. A runaway agent loop
// once drove a single container to ~17-21M cache-write tokens/day (~$130+/day)
// and went unnoticed for weeks. Legitimate single-tenant usage sits well below
// 10M/day, so 10M catches the runaway with margin while staying quiet for normal
// traffic. Bump this as the fleet grows (it's a fleet-wide Sum across all
// containers, not per-tenant).
const BEDROCK_DAILY_CACHE_WRITE_TOKEN_THRESHOLD = 10_000_000;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ObservabilityStackProps extends cdk.StackProps {
  envName: string;
  // Service stack references
  backendService: ecs.IBaseService;
  backendLogGroupName: string;
  // Network / ALB
  alb: elbv2.ApplicationLoadBalancer;
  // API Gateway
  wsApiId: string;
  // Container stack references
  cluster: ecs.ICluster;
  efsFileSystem: efs.IFileSystem;
  // Database tables (for DynamoDB alarms)
  databaseTables: {
    usersTable: dynamodb.ITable;
    containersTable: dynamodb.ITable;
    billingTable: dynamodb.ITable;
    usageCountersTable: dynamodb.ITable;
    pendingUpdatesTable: dynamodb.ITable;
    channelLinksTable: dynamodb.ITable;
  };
  connectionsTableName: string;
  // Lambda authorizer function name (for Lambda alarms)
  authorizerFunctionName: string;
  // Paperclip tier references (for Aurora + Paperclip-service alarms)
  paperclipDbCluster: rds.IDatabaseCluster;
  paperclipService: ecs.IBaseService;
}

// ---------------------------------------------------------------------------
// Alarm definition interface
// ---------------------------------------------------------------------------

interface AlarmDef {
  id: string;
  name: string;
  metricName: string;
  namespace?: string; // default "Isol8"
  statistic?: string; // default "Sum"
  threshold: number;
  evaluationPeriods: number;
  periodMinutes: number;
  comparisonOperator: cloudwatch.ComparisonOperator;
  treatMissingData?: cloudwatch.TreatMissingData;
  dimensions?: Record<string, string>;
  severity: "page" | "warn";
  description: string;
}

// ---------------------------------------------------------------------------
// Stack
// ---------------------------------------------------------------------------

export class ObservabilityStack extends cdk.Stack {
  public readonly pageTopic: sns.Topic;
  public readonly warnTopic: sns.Topic;
  /**
   * ARNs of the page-tier CloudWatch alarms. Consumed by FisStack (dev only)
   * as FIS experiment stop-conditions: a real customer-impacting page alarm
   * aborts a running chaos experiment. Populated only with page-severity
   * alarms — warn/cost alarms are deliberately excluded.
   */
  public readonly pageAlarmArns: string[] = [];
  private readonly envName: string;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    this.envName = props.envName;

    // -----------------------------------------------------------------------
    // SNS Topics
    // -----------------------------------------------------------------------

    // Page tier: SMS + email — fires on customer-impacting events
    this.pageTopic = new sns.Topic(this, "AlertsPage", {
      topicName: `isol8-${props.envName}-alerts-page`,
      displayName: "Isol8 Page",
    });

    // Email subscription (always present)
    this.pageTopic.addSubscription(
      new subs.EmailSubscription("oncall@isol8.co"),
    );

    // SMS subscription will be added manually after the oncall phone secret
    // is created in Secrets Manager. See docs/ops/setup-oncall.md.

    // Warn tier: email only
    this.warnTopic = new sns.Topic(this, "AlertsWarn", {
      topicName: `isol8-${props.envName}-alerts-warn`,
      displayName: "Isol8 Warn",
    });
    this.warnTopic.addSubscription(
      new subs.EmailSubscription("alerts@isol8.co"),
    );

    // -----------------------------------------------------------------------
    // Alarms
    // -----------------------------------------------------------------------
    this.createPageAlarms(props);
    this.createWarnCustomMetricAlarms();
    this.createWarnAwsNativeAlarms(props);
    this.createPaperclipTierAlarms(props);
    this.createCostAlarms(props);

    // -----------------------------------------------------------------------
    // Dashboard
    // -----------------------------------------------------------------------
    this.createDashboard(props);

    // -----------------------------------------------------------------------
    // Account hardening
    // -----------------------------------------------------------------------
    this.createAccountHardening(props);

    // -----------------------------------------------------------------------
    // Outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, "PageTopicArn", {
      value: this.pageTopic.topicArn,
      exportName: `isol8-${props.envName}-page-topic-arn`,
    });

    new cdk.CfnOutput(this, "WarnTopicArn", {
      value: this.warnTopic.topicArn,
      exportName: `isol8-${props.envName}-warn-topic-arn`,
    });
  }

  // =========================================================================
  // Alarm helper
  // =========================================================================

  /**
   * Creates a single CloudWatch alarm from a definition object.
   *
   * For custom Isol8 namespace metrics, auto-injects `env` and `service`
   * dimensions to match the EMF emitter output. AWS-native namespace metrics
   * (AWS/ApplicationELB, AWS/ApiGateway, etc.) should NOT use this helper --
   * they have their own dimension schemes.
   */
  private createAlarm(def: AlarmDef): cloudwatch.Alarm {
    // Auto-inject env and service dimensions for custom Isol8 namespace
    const isCustomNamespace = !def.namespace || def.namespace === "Isol8";
    const dimensions = isCustomNamespace
      ? {
          env: this.envName,
          service: "isol8-backend",
          ...(def.dimensions ?? {}),
        }
      : (def.dimensions ?? {});

    const metric = new cloudwatch.Metric({
      namespace: def.namespace ?? "Isol8",
      metricName: def.metricName,
      statistic: def.statistic ?? "Sum",
      period: cdk.Duration.minutes(def.periodMinutes),
      dimensionsMap: dimensions,
    });

    const alarm = new cloudwatch.Alarm(this, def.id, {
      alarmName: `isol8-${this.envName}-${def.id}-${def.name}`,
      alarmDescription: def.description,
      metric,
      threshold: def.threshold,
      evaluationPeriods: def.evaluationPeriods,
      comparisonOperator: def.comparisonOperator,
      treatMissingData:
        def.treatMissingData ?? cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarm.addAlarmAction(
      new cloudwatch_actions.SnsAction(
        def.severity === "page" ? this.pageTopic : this.warnTopic,
      ),
    );

    // Collect page-tier alarms as FIS stop-condition sources (dev only).
    if (def.severity === "page") {
      this.pageAlarmArns.push(alarm.alarmArn);
    }

    return alarm;
  }

  // =========================================================================
  // Page-tier alarms
  // =========================================================================

  private createPageAlarms(props: ObservabilityStackProps): void {
    // P1: container-error-state
    this.createAlarm({
      id: "P1",
      name: "container-error-state",
      metricName: "container.error_state",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description: "Per-user OpenClaw container in stuck/error state",
    });

    // P2: stripe-webhook-sig-fail
    this.createAlarm({
      id: "P2",
      name: "stripe-webhook-sig-fail",
      metricName: "stripe.webhook.sig_fail",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description: "Stripe webhook signature verification failed",
    });

    // P3: workspace-path-traversal
    this.createAlarm({
      id: "P3",
      name: "workspace-path-traversal",
      metricName: "workspace.path_traversal.attempt",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description: "Path traversal attempt blocked — security event",
    });

    // P11: marketplace-path-traversal
    // Backend emits marketplace.path_traversal_attempt (no extra dims) in
    // core/services/agent_export.py when a seller's agent_id resolves outside
    // their workspaces dir. Same breach-on-event security trip-wire shape as
    // P3 (workspace path traversal). Pages so a marketplace export attack is
    // never silent. Goes on pageAlarmArns via createAlarm, but a breach-on-event
    // trip-wire must NOT be a FIS stop-condition — it is not in any FIS set.
    this.createAlarm({
      id: "P11",
      name: "marketplace-path-traversal",
      metricName: "marketplace.path_traversal_attempt",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description:
        "Marketplace agent export path traversal attempt blocked — security event",
    });

    // P4: update-fleet-patch-invoked
    this.createAlarm({
      id: "P4",
      name: "update-fleet-patch-invoked",
      metricName: "update.fleet_patch.invoked",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description: "Fleet-wide config patch invoked — audit trail",
    });

    // P5: debug-endpoint-prod-hit
    this.createAlarm({
      id: "P5",
      name: "debug-endpoint-prod-hit",
      metricName: "debug.endpoint.prod_hit",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description:
        "Debug endpoint hit in production — should be 403d",
    });

    // P6: billing-pricing-missing-model
    this.createAlarm({
      id: "P6",
      name: "billing-pricing-missing-model",
      metricName: "billing.pricing.missing_model",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description: "Chat used a model with no pricing row configured",
    });

    // P7: update-worker-stalled (heartbeat absence)
    this.createAlarm({
      id: "P7",
      name: "update-worker-stalled",
      metricName: "update.scheduled_worker.heartbeat",
      threshold: 0,
      evaluationPeriods: 5,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      severity: "page",
      description:
        "Update worker heartbeat absent for 5 min — loop may have died",
    });

    // P8: dynamodb-throttle-sustained (AWS-native metric math)
    // Repointed off the never-emitted custom `Isol8/dynamodb.throttle` to the
    // AWS-native `AWS/DynamoDB ThrottledRequests` metric so this is a real page
    // without any backend change. ThrottledRequests is per-table, so we sum
    // across the same table set W40/W41/W42 enumerate (account-wide math sum is
    // the acceptable fallback when a table set isn't available).
    const ddbThrottleTables: [string, dynamodb.ITable][] = [
      ["users", props.databaseTables.usersTable],
      ["containers", props.databaseTables.containersTable],
      ["billing", props.databaseTables.billingTable],
      ["usageCounters", props.databaseTables.usageCountersTable],
      ["pendingUpdates", props.databaseTables.pendingUpdatesTable],
      ["channelLinks", props.databaseTables.channelLinksTable],
    ];
    const ddbThrottlePerTable: Record<string, cloudwatch.IMetric> = {};
    for (const [key, table] of ddbThrottleTables) {
      ddbThrottlePerTable[key] = new cloudwatch.Metric({
        namespace: "AWS/DynamoDB",
        metricName: "ThrottledRequests",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { TableName: table.tableName },
      });
    }
    const ddbThrottleTotal = new cloudwatch.MathExpression({
      expression: Object.keys(ddbThrottlePerTable).join(" + "),
      usingMetrics: ddbThrottlePerTable,
      period: cdk.Duration.minutes(1),
    });
    const p8 = new cloudwatch.Alarm(this, "P8", {
      alarmName: `isol8-${this.envName}-P8-dynamodb-throttle-sustained`,
      alarmDescription:
        "DynamoDB throttling sustained for 2 consecutive minutes",
      metric: ddbThrottleTotal,
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    p8.addAlarmAction(new cloudwatch_actions.SnsAction(this.pageTopic));
    this.pageAlarmArns.push(p8.alarmArn);

    // P9: alb-5xx-rate (AWS-native metric math)
    const albFullName = props.alb.loadBalancerFullName;
    const albErrors = new cloudwatch.Metric({
      namespace: "AWS/ApplicationELB",
      metricName: "HTTPCode_Target_5XX_Count",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { LoadBalancer: albFullName },
    });
    const albRequests = new cloudwatch.Metric({
      namespace: "AWS/ApplicationELB",
      metricName: "RequestCount",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { LoadBalancer: albFullName },
    });
    const albErrorRate = new cloudwatch.MathExpression({
      expression: "100 * errors / IF(total > 0, total, 1)",
      usingMetrics: { errors: albErrors, total: albRequests },
      period: cdk.Duration.minutes(5),
    });

    const p9 = new cloudwatch.Alarm(this, "P9", {
      alarmName: `isol8-${this.envName}-P9-alb-5xx-rate`,
      alarmDescription: "ALB 5xx error rate exceeds 5%",
      metric: albErrorRate,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    p9.addAlarmAction(new cloudwatch_actions.SnsAction(this.pageTopic));
    this.pageAlarmArns.push(p9.alarmArn);

    // P10: apigw-ws-5xx-rate (AWS-native metric math)
    const apiGwErrors = new cloudwatch.Metric({
      namespace: "AWS/ApiGateway",
      metricName: "5XXError",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ApiId: props.wsApiId },
    });
    const apiGwCount = new cloudwatch.Metric({
      namespace: "AWS/ApiGateway",
      metricName: "Count",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ApiId: props.wsApiId },
    });
    const apiGwErrorRate = new cloudwatch.MathExpression({
      expression: "100 * errors / IF(total > 0, total, 1)",
      usingMetrics: { errors: apiGwErrors, total: apiGwCount },
      period: cdk.Duration.minutes(5),
    });

    const p10 = new cloudwatch.Alarm(this, "P10", {
      alarmName: `isol8-${this.envName}-P10-apigw-ws-5xx-rate`,
      alarmDescription: "API Gateway WebSocket 5xx error rate exceeds 5%",
      metric: apiGwErrorRate,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    p10.addAlarmAction(new cloudwatch_actions.SnsAction(this.pageTopic));
    this.pageAlarmArns.push(p10.alarmArn);

    // -------------------------------------------------------------------
    // P12: backend metrics-loop liveness heartbeat
    // -------------------------------------------------------------------
    // The free-tier idle-reaper (and its scale-to-zero path) was REMOVED in the
    // flat-fee cutover, so this is no longer a "reaper-dead" page. It survives
    // as a generic BACKEND-LIVENESS check: the backend metrics loop still emits
    // gateway.running.count every ~60s regardless of workload, so absence of
    // samples means the backend's metrics loop is dead. Expect ~10 samples per
    // 10-min window when healthy; threshold 5 tolerates one skipped cycle.
    // Companion P13 (reaper-crash) was deleted with the reaper.
    //
    // Do NOT use as a FIS stop-condition — it self-trips any backend-touching
    // experiment (stopping/stressing the backend stops the heartbeat gauge).
    const reaperDims = { env: this.envName, service: "isol8-backend" };

    const heartbeatMetric = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "gateway.running.count",
      statistic: "SampleCount",
      period: cdk.Duration.minutes(10),
      dimensionsMap: reaperDims,
    });

    const heartbeatAlarm = new cloudwatch.Alarm(this, "ReaperHeartbeatAlarm", {
      alarmName: `isol8-${this.envName}-P12-backend-liveness`,
      alarmDescription:
        "PAGE: backend metrics loop has emitted no heartbeat in 10 minutes. " +
        "The backend emits gateway.running.count every ~60s regardless of " +
        "workload, so absence of samples = the backend metrics loop is dead.",
      metric: heartbeatMetric,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      // Legit liveness check: missing data IS the failure signal.
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    heartbeatAlarm.addAlarmAction(
      new cloudwatch_actions.SnsAction(this.pageTopic),
    );
    this.pageAlarmArns.push(heartbeatAlarm.alarmArn);
  }

  // =========================================================================
  // Warn-tier custom metric alarms (W1-W27)
  // =========================================================================

  private createWarnCustomMetricAlarms(): void {
    const dims = { env: this.envName, service: "isol8-backend" };

    // -- Container & gateway (W1-W8) --

    // W1: container-provision-error-rate (metric math)
    // Backend emits container.provision with status="ok" or status="error" dimension.
    // CloudWatch EMF creates separate streams per dimension set, so we must
    // query each status value explicitly and sum for the total.
    const provisionErrors = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "container.provision",
      statistic: "Sum",
      period: cdk.Duration.minutes(10),
      dimensionsMap: { ...dims, status: "error" },
    });
    const provisionOk = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "container.provision",
      statistic: "Sum",
      period: cdk.Duration.minutes(10),
      dimensionsMap: { ...dims, status: "ok" },
    });
    const provisionRate = new cloudwatch.MathExpression({
      expression: "100 * errors / IF((errors + ok) > 0, errors + ok, 1)",
      usingMetrics: { errors: provisionErrors, ok: provisionOk },
      period: cdk.Duration.minutes(10),
    });
    const w1 = new cloudwatch.Alarm(this, "W1", {
      alarmName: `isol8-${this.envName}-W1-container-provision-error-rate`,
      alarmDescription: "Container provision error rate exceeds 5%",
      metric: provisionRate,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w1.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W2: container-lifecycle-latency-p99
    this.createAlarm({
      id: "W2",
      name: "container-lifecycle-latency-p99",
      metricName: "container.lifecycle.latency",
      statistic: "p99",
      threshold: 60000,
      evaluationPeriods: 1,
      periodMinutes: 10,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Container lifecycle p99 latency exceeds 60s",
    });

    // W3: container-efs-access-point-fail
    this.createAlarm({
      id: "W3",
      name: "container-efs-access-point-fail",
      metricName: "container.efs.access_point",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { status: "error" },
      severity: "warn",
      description: "EFS access point operation failed",
    });

    // W4: container-task-def-register-fail
    this.createAlarm({
      id: "W4",
      name: "container-task-def-register-fail",
      metricName: "container.task_def.register",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { status: "error" },
      severity: "warn",
      description: "ECS task definition registration failed",
    });

    // W5: gateway-connection-drop (gauge → zero)
    // NOT anomaly detection: with NOT_BREACHING this only catches a non-zero →
    // zero transition (open connections present, then gone). It deliberately
    // does NOT fire when the metric is simply absent — that was the old
    // BREACHING config, which paged constantly on idle dev (no connections at
    // all = no samples).
    const gwOpenMetric = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "gateway.connection.open",
      statistic: "Average",
      period: cdk.Duration.minutes(5),
      dimensionsMap: dims,
    });
    const w5 = new cloudwatch.Alarm(this, "W5", {
      alarmName: `isol8-${this.envName}-W5-gateway-connection-drop`,
      alarmDescription: "Gateway open connections dropped sharply",
      metric: gwOpenMetric,
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w5.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W6: gateway-health-check-timeout
    this.createAlarm({
      id: "W6",
      name: "gateway-health-check-timeout",
      metricName: "gateway.health_check.timeout",
      threshold: 5,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Gateway health check timeouts exceed 5 in 5 min",
    });

    // W7: gateway-frontend-prune-storm
    this.createAlarm({
      id: "W7",
      name: "gateway-frontend-prune-storm",
      metricName: "gateway.frontend.prune",
      threshold: 100,
      evaluationPeriods: 1,
      periodMinutes: 60,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "Frontend connection prune storm — over 100 in 1 hour",
    });

    // W8: gateway-rpc-error-count
    // Was a metric-math RATE with chat.message.count as the denominator, but
    // RPC volume ≫ chat volume (every chat is many RPCs), so that ratio was
    // meaningless. No true RPC-total metric is emitted, so this is an ABSOLUTE
    // error-count threshold on gateway.rpc.error instead.
    // TODO(observability Phase 2): emit gateway.rpc.count for a true rate.
    const rpcErrors = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "gateway.rpc.error",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: dims,
    });
    const w8 = new cloudwatch.Alarm(this, "W8", {
      alarmName: `isol8-${this.envName}-W8-gateway-rpc-error-count`,
      alarmDescription: "Gateway RPC errors exceed 10 in 5 min",
      metric: rpcErrors,
      threshold: 10,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w8.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- Chat (W9-W12) --

    // W9: chat-e2e-latency-p99
    this.createAlarm({
      id: "W9",
      name: "chat-e2e-latency-p99",
      metricName: "chat.e2e.latency",
      statistic: "p99",
      threshold: 20000,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Chat p99 latency exceeds 20s SLO target",
    });

    // W10: chat-error-rate (metric math)
    const chatErrors = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "chat.error",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: dims,
    });
    const chatTotal = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "chat.message.count",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: dims,
    });
    const chatErrorRate = new cloudwatch.MathExpression({
      expression: "100 * errors / IF(total > 0, total, 1)",
      usingMetrics: { errors: chatErrors, total: chatTotal },
      period: cdk.Duration.minutes(5),
    });
    const w10 = new cloudwatch.Alarm(this, "W10", {
      alarmName: `isol8-${this.envName}-W10-chat-error-rate`,
      alarmDescription: "Chat error rate exceeds 1%",
      metric: chatErrorRate,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w10.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W11: chat-session-usage-fetch-error
    this.createAlarm({
      id: "W11",
      name: "chat-session-usage-fetch-error",
      metricName: "chat.session_usage.fetch.error",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Failed to fetch session usage from container",
    });

    // W12 (chat-bedrock-throttle) DELETED: the custom chat.bedrock.throttle
    // metric was never emitted. Bedrock throttles are covered by the AWS-native
    // W46-bedrock-throttles alarm (AWS/Bedrock InvocationThrottles).

    // -- Channels (W13-W15) --

    // W13: channel-rpc-error-rate
    this.createAlarm({
      id: "W13",
      name: "channel-rpc-error-rate",
      metricName: "channel.rpc",
      threshold: 10,
      evaluationPeriods: 1,
      periodMinutes: 60,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { status: "error" },
      severity: "warn",
      description: "Channel RPC errors exceed 10 per hour",
    });

    // W14: channel-configure-fail
    this.createAlarm({
      id: "W14",
      name: "channel-configure-fail",
      metricName: "channel.configure",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { status: "error" },
      severity: "warn",
      description: "Channel configure step failed",
    });

    // W15 (channel-webhook-inbound-absent) DELETED: the channel.webhook.inbound
    // metric is never emitted, so a BREACHING absence alarm fired constantly.
    // Re-add prod-gated + NOT_BREACHING in Phase 2 if we emit the metric.

    // -- Stripe & billing (W16-W20) --

    // W16 (stripe-meter-event-fail), W17 (stripe-subscription-latency),
    // W18 (stripe-api-error-rate), W19 (billing-budget-check-error) DELETED:
    // none of their metrics (stripe.meter_event.fail, stripe.subscription.latency,
    // stripe.api.error, billing.budget_check.error) are ever emitted, so the
    // alarms never fired. W18 (stripe-api-error-rate) re-adds in Phase 2 once
    // stripe.api.error is actually emitted.

    // W20: webhook-clerk-sig-fail
    this.createAlarm({
      id: "W20",
      name: "webhook-clerk-sig-fail",
      metricName: "webhook.clerk.sig_fail",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Clerk webhook signature verification failed",
    });

    // -- Auth (W21-W23) --

    // W21: auth-jwt-fail-spike
    this.createAlarm({
      id: "W21",
      name: "auth-jwt-fail-spike",
      metricName: "auth.jwt.fail",
      threshold: 100,
      evaluationPeriods: 1,
      periodMinutes: 60,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "JWT failures exceed 100 per hour — possible attack",
    });

    // W22: auth-jwks-refresh-fail
    this.createAlarm({
      id: "W22",
      name: "auth-jwks-refresh-fail",
      metricName: "auth.jwks.refresh",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { status: "error" },
      severity: "warn",
      description: "JWKS cache refresh failed",
    });

    // W23: auth-org-admin-denied-spike
    this.createAlarm({
      id: "W23",
      name: "auth-org-admin-denied-spike",
      metricName: "auth.org_admin.denied",
      threshold: 50,
      evaluationPeriods: 1,
      periodMinutes: 60,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "Org admin denied requests exceed 50 per hour",
    });

    // -- Workspace, proxy, update (W24-W27) --

    // W24: workspace-file-write-error
    this.createAlarm({
      id: "W24",
      name: "workspace-file-write-error",
      metricName: "workspace.file.write.error",
      threshold: 10,
      evaluationPeriods: 1,
      periodMinutes: 60,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "EFS file write errors exceed 10 per hour",
    });

    // W25 (proxy-upstream-5xx) and W26 (proxy-budget-check-fail) DELETED:
    // proxy.upstream is never emitted, and proxy.budget_check.fail was a
    // free-tier construct removed in the flat-fee cutover. Neither alarm fired.

    // W27: update-worker-error
    this.createAlarm({
      id: "W27",
      name: "update-worker-error",
      metricName: "update.scheduled_worker.error",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Update worker loop iteration caught an exception",
    });

    // W53: operator-relay-churn (next free id — W2-W27 are custom-warn,
    // W28-W48 are the AWS-native block, W49-W52 are taken). A per-user
    // container is dropping its Paperclip relay connection (operator upstream
    // closed) abnormally often. A healthy relay establishes once and holds;
    // sustained churn means the container is too unhealthy to keep an agent
    // connected, so heartbeat runs fail at the connect handshake — a
    // degradation the ECS healthcheck reports as HEALTHY.
    this.createAlarm({
      id: "W53",
      name: "operator-relay-churn",
      metricName: "operator.upstream.closed",
      threshold: 10,
      evaluationPeriods: 1,
      periodMinutes: 15,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "A per-user container is dropping Paperclip relay connections >10/15min — agent heartbeats can't stay connected; find the owner via the operator_proxy 'Operator upstream closed' logs",
    });

    // W54: gateway-reconnect-throttle-storm (next free id after W53).
    // Backend emits gateway.connection{event:reconnect_throttled} in
    // core/gateway/connection_pool.py:_respect_reconnect_floor when a per-user
    // reconnect fires inside the sub-second reconnect floor. A healthy fleet
    // throttles occasionally; a spike means a reconnect storm (a misbehaving
    // container + frontend exponential reconnect colliding with in-flight
    // server retries). Same "storm counter" shape as W7 — Sum over a window.
    this.createAlarm({
      id: "W54",
      name: "gateway-reconnect-throttle-storm",
      metricName: "gateway.connection",
      threshold: 20,
      evaluationPeriods: 1,
      periodMinutes: 15,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { event: "reconnect_throttled" },
      severity: "warn",
      description:
        "Gateway reconnect throttles exceed 20 in 15 min — reconnect storm (misbehaving container burning the open-handshake budget)",
    });

    // W55: metronome-gate-refresh-error.
    // Backend emits metronome.gate.refresh_error (no extra dims) in
    // core/services/metronome_gate.py when the credit-gate balance refresh
    // (getNetBalance) raises — i.e. Metronome is unreachable/erroring. The gate
    // then falls back to the cached fast-gate row, so a Metronome outage is
    // otherwise invisible until credits silently drift. Sum > 0, NOT_BREACHING
    // (absent metric = healthy, no refresh failures).
    this.createAlarm({
      id: "W55",
      name: "metronome-gate-refresh-error",
      metricName: "metronome.gate.refresh_error",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "Metronome credit-gate balance refresh failing — gate serving stale cached balances; possible Metronome outage",
    });

    // W56: metronome-webhook-bad-signature.
    // Backend emits metronome.webhook{result:bad_signature} in
    // routers/metronome_webhook.py on a failed HMAC verification. That explicit
    // single-dim shape ({env,service,result}) is cleanly alarmable. A spike of
    // bad-signature webhooks means a secret rotation drift or a forged-webhook
    // probe — either way the credit gate stops getting trustworthy updates.
    this.createAlarm({
      id: "W56",
      name: "metronome-webhook-bad-signature",
      metricName: "metronome.webhook",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      dimensions: { result: "bad_signature" },
      severity: "warn",
      description:
        "Metronome webhook signature verification failing — secret drift or forged-webhook probe; credit gate not getting trustworthy balance updates",
    });

    // W63: paperclip-down.
    // Backend emits paperclip.upstream.error{kind:"unreachable"} in
    // routers/paperclip_proxy.py + core/services/paperclip_admin_client.py
    // when the Paperclip BFF raises an httpx connect/transport error. Replaces
    // the dropped ALB-HealthyHostCount W63 (PaperclipStack has no ALB target
    // group, so there is no HealthyHostCount to alarm on). Sum > 0 over a short
    // window, NOT_BREACHING (absent metric = healthy, no error emissions).
    this.createAlarm({
      id: "W63",
      name: "paperclip-down",
      metricName: "paperclip.upstream.error",
      dimensions: { kind: "unreachable" },
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      description:
        "Paperclip BFF is unreachable from the backend (httpx connect/transport errors) — Teams/agent flows are degraded. Replaces the dropped ALB-HealthyHostCount W63 (PaperclipStack has no ALB target group).",
    });

    // W64: provision-gate-block-spike.
    // Backend emits provision.gate.blocked (dimensionless, plus a {code}
    // breakdown stream) in core/services/provision_gate.py whenever the gate
    // blocks an owner. This alarm passes NO dimensions, so it queries the
    // {env,service} stream — matching the dimensionless emission (NOT the
    // {code} streams, each of which only sees one code's blocks). A provider
    // outage that silently gates all new chats shows as a spike in blocks, so
    // the threshold is a spike (not >0 — some blocks are normal, e.g. a
    // genuinely-unsubscribed user).
    this.createAlarm({
      id: "W64",
      name: "provision-gate-block-spike",
      metricName: "provision.gate.blocked",
      threshold: 10,
      evaluationPeriods: 1,
      periodMinutes: 15,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      description:
        "Provision gate blocking >10 owners in 15 min — likely a provider outage (Stripe/Metronome/OAuth) silently gating users, not normal unsubscribed traffic. Provisional threshold; sums across all codes.",
    });
  }

  // =========================================================================
  // Warn-tier AWS-native infrastructure alarms (W28-W48)
  // =========================================================================

  private createWarnAwsNativeAlarms(
    props: ObservabilityStackProps,
  ): void {
    const albFullName = props.alb.loadBalancerFullName;

    // -- Load balancer (W28-W29) --

    // W28: ALB UnHealthyHostCount
    const w28 = new cloudwatch.Alarm(this, "W28", {
      alarmName: `isol8-${this.envName}-W28-alb-unhealthy-hosts`,
      alarmDescription: "ALB unhealthy host count > 0 for 5 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "UnHealthyHostCount",
        statistic: "Maximum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { LoadBalancer: albFullName },
      }),
      threshold: 0,
      evaluationPeriods: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w28.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W29: ALB TargetResponseTime p99
    const w29 = new cloudwatch.Alarm(this, "W29", {
      alarmName: `isol8-${this.envName}-W29-alb-response-time-p99`,
      alarmDescription: "ALB target response time p99 exceeds 5s",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "TargetResponseTime",
        statistic: "p99",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { LoadBalancer: albFullName },
      }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w29.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- API Gateway WebSocket (W30-W32) --

    // W30: API GW WS 4XXError rate
    const apiGw4xx = new cloudwatch.Metric({
      namespace: "AWS/ApiGateway",
      metricName: "4XXError",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ApiId: props.wsApiId },
    });
    const apiGwTotal = new cloudwatch.Metric({
      namespace: "AWS/ApiGateway",
      metricName: "Count",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ApiId: props.wsApiId },
    });
    const apiGw4xxRate = new cloudwatch.MathExpression({
      expression: "100 * errors / IF(total > 0, total, 1)",
      usingMetrics: { errors: apiGw4xx, total: apiGwTotal },
      period: cdk.Duration.minutes(5),
    });
    const w30 = new cloudwatch.Alarm(this, "W30", {
      alarmName: `isol8-${this.envName}-W30-apigw-ws-4xx-rate`,
      alarmDescription:
        "API Gateway WebSocket 4xx error rate exceeds 5%",
      metric: apiGw4xxRate,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w30.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W31: API GW WS IntegrationLatency p99
    const w31 = new cloudwatch.Alarm(this, "W31", {
      alarmName: `isol8-${this.envName}-W31-apigw-ws-integration-latency`,
      alarmDescription:
        "API Gateway WebSocket integration latency p99 exceeds 2s",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName: "IntegrationLatency",
        statistic: "p99",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ApiId: props.wsApiId },
      }),
      threshold: 2000,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w31.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W32: API GW WS ConnectCount drop (static threshold — zero connections)
    // NOT_BREACHING: only catches a real non-zero → zero transition, not the
    // simple absence of connections on idle dev (the old BREACHING config
    // false-fired constantly when nobody was connected).
    const w32 = new cloudwatch.Alarm(this, "W32", {
      alarmName: `isol8-${this.envName}-W32-apigw-ws-connect-drop`,
      alarmDescription:
        "API Gateway WebSocket connect count dropped to zero",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName: "ConnectCount",
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ApiId: props.wsApiId },
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w32.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- Lambda authorizer (W33-W35) --
    const lambdaDims = {
      FunctionName: props.authorizerFunctionName,
    };

    // W33: Lambda Errors
    const w33 = new cloudwatch.Alarm(this, "W33", {
      alarmName: `isol8-${this.envName}-W33-lambda-auth-errors`,
      alarmDescription: "Lambda authorizer errors > 0",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Errors",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: lambdaDims,
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w33.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W34: Lambda Throttles
    const w34 = new cloudwatch.Alarm(this, "W34", {
      alarmName: `isol8-${this.envName}-W34-lambda-auth-throttles`,
      alarmDescription: "Lambda authorizer throttles > 0",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Throttles",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: lambdaDims,
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w34.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W35: Lambda Duration p99
    const w35 = new cloudwatch.Alarm(this, "W35", {
      alarmName: `isol8-${this.envName}-W35-lambda-auth-duration-p99`,
      alarmDescription: "Lambda authorizer p99 duration exceeds 1s",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Duration",
        statistic: "p99",
        period: cdk.Duration.minutes(5),
        dimensionsMap: lambdaDims,
      }),
      threshold: 1000,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w35.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- ECS (W36-W39) --

    const clusterName = props.cluster.clusterName;

    // W36: Backend service CPU utilization (AWS/ECS standard metric — no
    // Container Insights needed).  AWS/ECS publishes CPUUtilization as a
    // percentage per-service automatically.
    const backendServiceName = cdk.Fn.select(
      2,
      cdk.Fn.split("/", props.backendService.serviceArn),
    );

    const cpuPct = new cloudwatch.Metric({
      namespace: "AWS/ECS",
      metricName: "CPUUtilization",
      statistic: "Average",
      period: cdk.Duration.minutes(5),
      dimensionsMap: {
        ClusterName: clusterName,
        ServiceName: backendServiceName,
      },
    });
    const w36 = new cloudwatch.Alarm(this, "W36", {
      alarmName: `isol8-${this.envName}-W36-ecs-backend-cpu`,
      alarmDescription:
        "Backend service CPU utilization exceeds 80% for 15 min",
      metric: cpuPct,
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w36.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W37: Backend service Memory utilization (AWS/ECS standard metric)
    const memPct = new cloudwatch.Metric({
      namespace: "AWS/ECS",
      metricName: "MemoryUtilization",
      statistic: "Average",
      period: cdk.Duration.minutes(5),
      dimensionsMap: {
        ClusterName: clusterName,
        ServiceName: backendServiceName,
      },
    });
    const w37 = new cloudwatch.Alarm(this, "W37", {
      alarmName: `isol8-${this.envName}-W37-ecs-backend-memory`,
      alarmDescription:
        "Backend service memory utilization exceeds 80% for 15 min",
      metric: memPct,
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w37.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W39: Fargate TaskStopped non-essential reason
    // EventBridge rule captures ECS task state changes with STOPPED status,
    // publishes a custom metric, and we alarm on that metric.
    const taskStoppedMetric = new cloudwatch.Metric({
      namespace: "Isol8/ECS",
      metricName: "TaskStoppedUnexpected",
      statistic: "Sum",
      period: cdk.Duration.minutes(1),
      dimensionsMap: { ClusterName: clusterName },
    });

    // EventBridge rule for ECS task stopped events.
    // The rule itself stays broad (every STOPPED task in the cluster) because
    // the authoritative "is this stop unexpected?" decision needs the full
    // event (per-container exitCode + the free-text stoppedReason), which is
    // easier to evaluate in the Lambda than in an EventBridge content filter.
    // The Lambda below only emits the alarm metric for genuinely-unexpected
    // stops, so normal per-user teardown + deploys no longer fire W39.
    const taskStoppedRule = new events.Rule(this, "TaskStoppedRule", {
      ruleName: `isol8-${this.envName}-ecs-task-stopped`,
      description: "Captures ECS task stopped events for alarming",
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          clusterArn: [props.cluster.clusterArn],
          lastStatus: ["STOPPED"],
        },
      },
    });

    // Lambda to publish custom metric ONLY on unexpected task stops.
    // A stop is treated as EXPECTED (and ignored) when either:
    //   - every container exited cleanly (exitCode == 0 / absent), OR
    //   - the stoppedReason matches a normal-lifecycle phrase (scaling,
    //     deployment, service-scheduler, user-initiated).
    // Anything else (non-zero container exit, OOM, host failure, unknown
    // reason) is emitted as TaskStoppedUnexpected and pages W39.
    const taskStoppedFn = new lambda.Function(this, "TaskStoppedFn", {
      functionName: `isol8-${this.envName}-task-stopped-metric`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(10),
      code: lambda.Code.fromInline(`
import boto3
cw = boto3.client('cloudwatch')

# Substrings (lowercased) of stoppedReason values that represent NORMAL
# lifecycle stops we must NOT alarm on: autoscaling, rolling deploys, the
# service scheduler draining/replacing tasks, and operator/user-initiated
# stops (incl. per-user container teardown).
EXPECTED_REASON_SUBSTRINGS = (
    'scaling',
    'deployment',
    'service-scheduler',
    'service scheduler',
    'user initiated',
    'user-initiated',
    'stopped by user',
)

def _is_unexpected(detail):
    # Clean container exits (exitCode 0 / absent) are never unexpected.
    containers = detail.get('containers') or []
    if all((c.get('exitCode') or 0) == 0 for c in containers):
        # Still flag if a non-zero exit hid behind an empty container list
        # AND the reason looks abnormal — handled by the reason check below.
        pass
    else:
        # At least one container exited non-zero → genuinely unexpected.
        return True
    reason = (detail.get('stoppedReason') or '').lower()
    if not reason:
        # No reason + clean exits → benign (e.g. normal shutdown).
        return False
    return not any(s in reason for s in EXPECTED_REASON_SUBSTRINGS)

def handler(event, context):
    detail = event.get('detail', {}) or {}
    if not _is_unexpected(detail):
        return {'statusCode': 200, 'emitted': False}
    cluster = detail.get('clusterArn', '').split('/')[-1]
    cw.put_metric_data(
        Namespace='Isol8/ECS',
        MetricData=[{
            'MetricName': 'TaskStoppedUnexpected',
            'Dimensions': [{'Name': 'ClusterName', 'Value': cluster}],
            'Value': 1,
            'Unit': 'Count',
        }],
    )
    return {'statusCode': 200, 'emitted': True}
`),
    });

    taskStoppedFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
      }),
    );

    taskStoppedRule.addTarget(
      new events_targets.LambdaFunction(taskStoppedFn),
    );

    const w39 = new cloudwatch.Alarm(this, "W39", {
      alarmName: `isol8-${this.envName}-W39-fargate-task-stopped`,
      alarmDescription: "Fargate task stopped unexpectedly",
      metric: taskStoppedMetric,
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w39.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W58: per-user container crash-loop. W39 fires on ANY single unexpected
    // stop; a crash-LOOP (CPU-starve → healthcheck SIGKILL → restart → repeat)
    // produces a burst of unexpected stops. Reuses the same TaskStoppedUnexpected
    // metric the free TaskStoppedFn Lambda already emits — no new emission.
    // Sum >= 3 in 5min distinguishes a loop from a one-off. Provisional
    // threshold, baseline after observation.
    const crashLoopMetric = new cloudwatch.Metric({
      namespace: "Isol8/ECS",
      metricName: "TaskStoppedUnexpected",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ClusterName: clusterName },
    });
    const w58 = new cloudwatch.Alarm(this, "W58", {
      alarmName: `isol8-${this.envName}-W58-per-user-crash-loop`,
      alarmDescription:
        "≥3 unexpected container stops in 5 min — a per-user container is in a crash/restart loop (likely CPU-starve healthcheck SIGKILL). Provisional threshold.",
      metric: crashLoopMetric,
      threshold: 3,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w58.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W59: EFS >1-task-per-access-point detector. Two RUNNING tasks on one
    // per-user service write the same EFS access point → OpenClaw WAL-SQLite
    // corruption (root cause mitigated by maxPercent=100 single-writer deploys;
    // this detects a REGRESSION). Basic AWS/ECS has no RunningTaskCount (that's
    // Container Insights, which we keep off for cost), so a tiny EventBridge→
    // Lambda checks running-task count on each RUNNING transition. Reuses the
    // free-Lambda pattern; events are low-volume.
    const multiTaskRule = new events.Rule(this, "TaskRunningRule", {
      ruleName: `isol8-${this.envName}-ecs-task-running`,
      description:
        "Captures ECS task RUNNING transitions for >1-task-per-service detection",
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          clusterArn: [props.cluster.clusterArn],
          lastStatus: ["RUNNING"],
        },
      },
    });
    const multiTaskFn = new lambda.Function(this, "MultiTaskFn", {
      functionName: `isol8-${this.envName}-multi-task-metric`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(15),
      code: lambda.Code.fromInline(`
import boto3
ecs = boto3.client('ecs')
cw = boto3.client('cloudwatch')

def handler(event, _ctx):
    detail = event.get('detail') or {}
    group = detail.get('group') or ''            # e.g. "service:openclaw-<uid>-<hash>"
    cluster_arn = detail.get('clusterArn') or ''
    if not group.startswith('service:'):
        return                                   # only per-user SERVICE tasks
    service = group.split(':', 1)[1]
    if 'openclaw' not in service:
        return                                   # ignore the backend/paperclip services
    # Count currently-RUNNING tasks for this service. >1 = the double-writer
    # precondition for EFS WAL-SQLite corruption.
    running = ecs.list_tasks(cluster=cluster_arn, serviceName=service, desiredStatus='RUNNING')
    count = len(running.get('taskArns') or [])
    if count > 1:
        cw.put_metric_data(Namespace='Isol8/ECS', MetricData=[{
            'MetricName': 'MultiTaskPerService',
            'Dimensions': [{'Name': 'ClusterName', 'Value': cluster_arn.split('/')[-1]}],
            'Value': 1.0, 'Unit': 'Count',
        }])
`),
    });
    // TWO separate statements: cloudwatch:PutMetricData does NOT support the
    // ecs:cluster condition key, so combining them would make IAM evaluate the
    // condition as non-matching and DENY the metric write (the alarm would get
    // no datapoints). ecs:ListTasks keeps the cluster condition; PutMetricData
    // is scoped instead by a cloudwatch:namespace condition.
    multiTaskFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:ListTasks"],
        resources: ["*"],
        conditions: { StringEquals: { "ecs:cluster": props.cluster.clusterArn } },
      }),
    );
    multiTaskFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringEquals: { "cloudwatch:namespace": "Isol8/ECS" } },
      }),
    );
    multiTaskRule.addTarget(new events_targets.LambdaFunction(multiTaskFn));

    const multiTaskMetric = new cloudwatch.Metric({
      namespace: "Isol8/ECS",
      metricName: "MultiTaskPerService",
      statistic: "Sum",
      period: cdk.Duration.minutes(1),
      dimensionsMap: { ClusterName: clusterName },
    });
    const w59 = new cloudwatch.Alarm(this, "W59", {
      alarmName: `isol8-${this.envName}-W59-efs-multi-task-per-ap`,
      alarmDescription:
        "A per-user service has >1 RUNNING task — two writers on one EFS access point, the OpenClaw WAL-SQLite corruption precondition. Single-writer deploy (maxPercent=100) should prevent this; firing = regression.",
      metric: multiTaskMetric,
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w59.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- DynamoDB (W40-W42) --
    // Create alarms for each table
    const allTables: [string, dynamodb.ITable][] = [
      ["users", props.databaseTables.usersTable],
      ["containers", props.databaseTables.containersTable],
      ["billing", props.databaseTables.billingTable],
      ["usage-counters", props.databaseTables.usageCountersTable],
      ["pending-updates", props.databaseTables.pendingUpdatesTable],
      ["channel-links", props.databaseTables.channelLinksTable],
    ];

    // W40: ConsumedReadCapacityUnits anomaly (on-demand tables)
    // For on-demand tables we alarm when read consumption spikes
    for (const [shortName, table] of allTables) {
      const w40 = new cloudwatch.Alarm(
        this,
        `W40-${shortName}`,
        {
          alarmName: `isol8-${this.envName}-W40-ddb-read-${shortName}`,
          alarmDescription: `DynamoDB ${shortName} read capacity anomaly`,
          metric: new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ConsumedReadCapacityUnits",
            statistic: "Sum",
            period: cdk.Duration.minutes(5),
            dimensionsMap: { TableName: table.tableName },
          }),
          // High threshold for on-demand — alert on sustained high usage
          threshold: 1000,
          evaluationPeriods: 3,
          comparisonOperator:
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        },
      );
      w40.addAlarmAction(
        new cloudwatch_actions.SnsAction(this.warnTopic),
      );
    }

    // W41: ConsumedWriteCapacityUnits anomaly
    for (const [shortName, table] of allTables) {
      const w41 = new cloudwatch.Alarm(
        this,
        `W41-${shortName}`,
        {
          alarmName: `isol8-${this.envName}-W41-ddb-write-${shortName}`,
          alarmDescription: `DynamoDB ${shortName} write capacity anomaly`,
          metric: new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ConsumedWriteCapacityUnits",
            statistic: "Sum",
            period: cdk.Duration.minutes(5),
            dimensionsMap: { TableName: table.tableName },
          }),
          threshold: 1000,
          evaluationPeriods: 3,
          comparisonOperator:
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        },
      );
      w41.addAlarmAction(
        new cloudwatch_actions.SnsAction(this.warnTopic),
      );
    }

    // W42: SystemErrors per table
    for (const [shortName, table] of allTables) {
      const w42 = new cloudwatch.Alarm(
        this,
        `W42-${shortName}`,
        {
          alarmName: `isol8-${this.envName}-W42-ddb-errors-${shortName}`,
          alarmDescription: `DynamoDB ${shortName} system errors > 0`,
          metric: new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "SystemErrors",
            statistic: "Sum",
            period: cdk.Duration.minutes(1),
            dimensionsMap: { TableName: table.tableName },
          }),
          threshold: 0,
          evaluationPeriods: 1,
          comparisonOperator:
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        },
      );
      w42.addAlarmAction(
        new cloudwatch_actions.SnsAction(this.warnTopic),
      );
    }

    // -- EFS (W43-W45) --
    const efsId = props.efsFileSystem.fileSystemId;

    // W43: EFS PercentIOLimit
    const w43 = new cloudwatch.Alarm(this, "W43", {
      alarmName: `isol8-${this.envName}-W43-efs-io-limit`,
      alarmDescription: "EFS PercentIOLimit exceeds 80%",
      metric: new cloudwatch.Metric({
        namespace: "AWS/EFS",
        metricName: "PercentIOLimit",
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { FileSystemId: efsId },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w43.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W44: EFS BurstCreditBalance low
    const w44 = new cloudwatch.Alarm(this, "W44", {
      alarmName: `isol8-${this.envName}-W44-efs-burst-credits`,
      alarmDescription: "EFS burst credit balance is low",
      metric: new cloudwatch.Metric({
        namespace: "AWS/EFS",
        metricName: "BurstCreditBalance",
        statistic: "Minimum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { FileSystemId: efsId },
      }),
      // Alert when credits drop below 1 TB (in bytes)
      threshold: 1099511627776,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w44.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W45: EFS ClientConnections drop
    // NOT_BREACHING: only catches a real non-zero → zero transition, not the
    // simple absence of the metric on idle dev (the old BREACHING config
    // false-fired when no clients were mounted).
    const w45 = new cloudwatch.Alarm(this, "W45", {
      alarmName: `isol8-${this.envName}-W45-efs-client-connections`,
      alarmDescription: "EFS client connections dropped to zero",
      metric: new cloudwatch.Metric({
        namespace: "AWS/EFS",
        metricName: "ClientConnections",
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { FileSystemId: efsId },
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w45.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- Bedrock (W46-W47) --

    // W46: Bedrock ModelInvocationThrottles
    const w46 = new cloudwatch.Alarm(this, "W46", {
      alarmName: `isol8-${this.envName}-W46-bedrock-throttles`,
      alarmDescription: "Bedrock model invocation throttles > 0",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Bedrock",
        metricName: "InvocationThrottles",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w46.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W47: Bedrock InvocationClientErrors
    const w47 = new cloudwatch.Alarm(this, "W47", {
      alarmName: `isol8-${this.envName}-W47-bedrock-client-errors`,
      alarmDescription:
        "Bedrock invocation client errors exceed 5 per min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Bedrock",
        metricName: "InvocationClientErrors",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
      }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w47.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // -- Network (W48) --

    // W48: NLB / Cloud Map healthy host count drop
    // Monitor ALB healthy host count as proxy for backend availability
    const w48 = new cloudwatch.Alarm(this, "W48", {
      alarmName: `isol8-${this.envName}-W48-alb-healthy-hosts-drop`,
      alarmDescription: "ALB healthy host count dropped to zero",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "HealthyHostCount",
        statistic: "Minimum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { LoadBalancer: albFullName },
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    w48.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));
  }

  // =========================================================================
  // Paperclip tier alarms (W60, W61, AuroraFailoverRule, W62)
  // =========================================================================

  private createPaperclipTierAlarms(props: ObservabilityStackProps): void {
    const dbId = props.paperclipDbCluster.clusterIdentifier;
    // W60: Aurora CPU
    const w60 = new cloudwatch.Alarm(this, "W60", {
      alarmName: `isol8-${this.envName}-W60-aurora-cpu`,
      alarmDescription: "Paperclip Aurora CPU >80% for 15 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS",
        metricName: "CPUUtilization",
        statistic: "Average",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { DBClusterIdentifier: dbId },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w60.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W61: Aurora connections — SATURATION only (high watermark). Provisional.
    const w61 = new cloudwatch.Alarm(this, "W61", {
      alarmName: `isol8-${this.envName}-W61-aurora-connections`,
      alarmDescription:
        "Paperclip Aurora DatabaseConnections >100 for 15 min — connection saturation. Provisional threshold.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS",
        metricName: "DatabaseConnections",
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { DBClusterIdentifier: dbId },
      }),
      threshold: 100,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w61.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W61b: Aurora OUTAGE / FAILOVER — distinct from saturation. W60/W61 (CPU,
    // high-connections, NOT_BREACHING) do NOT fire when a failover/outage drops
    // connections toward zero or stops reporting. RDS publishes failover +
    // failure events to EventBridge; route them straight to the warn topic so
    // the 3C Aurora-failover scenario has a recovery signal. (Event categories:
    // "failover" + "failure"; covers RDS-EVENT-0031/0071/0036 etc.)
    const auroraEventRule = new events.Rule(this, "AuroraFailoverRule", {
      ruleName: `isol8-${this.envName}-aurora-failover`,
      description: "Paperclip Aurora failover/failure events → warn",
      eventPattern: {
        source: ["aws.rds"],
        detailType: ["RDS DB Cluster Event", "RDS DB Instance Event"],
        detail: {
          SourceIdentifier: [dbId],
          EventCategories: ["failover", "failure"],
        },
      },
    });
    auroraEventRule.addTarget(new events_targets.SnsTopic(this.warnTopic));

    // W62: Paperclip Fargate service CPU. The Paperclip service is singular +
    // CDK-known, so a static per-service AWS/ECS alarm works (unlike the
    // per-user fleet).
    const clusterName = props.cluster.clusterName;
    const pcServiceName = cdk.Fn.select(
      2,
      cdk.Fn.split("/", props.paperclipService.serviceArn),
    );
    const w62 = new cloudwatch.Alarm(this, "W62", {
      alarmName: `isol8-${this.envName}-W62-paperclip-service-cpu`,
      alarmDescription: "Paperclip Fargate service CPU >80% for 15 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ECS",
        metricName: "CPUUtilization",
        statistic: "Average",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ClusterName: clusterName, ServiceName: pcServiceName },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w62.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // NO W63 here. The "Paperclip down" signal does NOT come from an ALB
    // HealthyHostCount alarm — VERIFIED: PaperclipStack has no ALB target group
    // (it registers via cloudMapOptions and is reached through the backend), so
    // there is no HealthyHostCount metric to alarm on. The down signal is the
    // Part B `paperclip.upstream.error{kind:"unreachable"}` metric + its alarm
    // (which keeps the W63 id), emitted from the backend's Paperclip-call paths.
  }

  // =========================================================================
  // Cost alarms (W49a, W49b, W50, W51)
  // =========================================================================

  private createCostAlarms(props: ObservabilityStackProps): void {
    // W49a + W49b: AWS Budget — 80% warn + 100% page
    new budgets.CfnBudget(this, "MonthlyBudget", {
      budget: {
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: { amount: 500, unit: "USD" },
        budgetName: `isol8-${this.envName}-monthly`,
      },
      notificationsWithSubscribers: [
        {
          // W49a: 80% threshold → warn topic
          notification: {
            notificationType: "ACTUAL",
            comparisonOperator: "GREATER_THAN",
            threshold: 80,
            thresholdType: "PERCENTAGE",
          },
          subscribers: [
            {
              subscriptionType: "EMAIL",
              address: "alerts@isol8.co",
            },
          ],
        },
        {
          // W49b: 100% threshold → page topic
          notification: {
            notificationType: "ACTUAL",
            comparisonOperator: "GREATER_THAN",
            threshold: 100,
            thresholdType: "PERCENTAGE",
          },
          subscribers: [
            {
              subscriptionType: "EMAIL",
              address: "oncall@isol8.co",
            },
          ],
        },
      ],
    });

    // W50: Bedrock spend anomaly
    // Use CloudWatch alarm on AWS/Bedrock InvocationCount as a proxy for cost
    const w50 = new cloudwatch.Alarm(this, "W50", {
      alarmName: `isol8-${this.envName}-W50-bedrock-spend-anomaly`,
      alarmDescription:
        "Bedrock invocation count spike — potential cost anomaly",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Bedrock",
        metricName: "Invocations",
        statistic: "Sum",
        period: cdk.Duration.hours(1),
      }),
      // Alert on >500 invocations/hour as a baseline — adjust after observation
      threshold: 500,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w50.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W51: NAT Gateway data transfer anomaly
    const w51 = new cloudwatch.Alarm(this, "W51", {
      alarmName: `isol8-${this.envName}-W51-nat-gateway-data-transfer`,
      alarmDescription:
        "NAT Gateway data transfer spike — potential cost anomaly",
      metric: new cloudwatch.Metric({
        namespace: "AWS/NATGateway",
        metricName: "BytesOutToDestination",
        statistic: "Sum",
        period: cdk.Duration.hours(1),
      }),
      // 10 GB/hour threshold — adjust after baselining
      threshold: 10_737_418_240,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w51.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W52: Bedrock cache-write token volume — runaway-inference guard
    // Sums AWS/Bedrock CacheWriteInputTokenCount across the whole account over a
    // 1-day window. This is the metric a runaway agent loop blows through (the
    // 2026 incident hit ~17-21M cache-write tokens/day, ~$130+/day, silent for
    // weeks). Pages so it can't go unnoticed again. Threshold lives in
    // BEDROCK_DAILY_CACHE_WRITE_TOKEN_THRESHOLD — tune as the fleet grows.
    const w52 = new cloudwatch.Alarm(this, "W52", {
      alarmName: `isol8-${this.envName}-W52-bedrock-cache-write-token-volume`,
      alarmDescription:
        "Bedrock cache-write input tokens exceeded the daily ceiling — " +
        "possible runaway inference loop driving cost",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Bedrock",
        metricName: "CacheWriteInputTokenCount",
        statistic: "Sum",
        period: cdk.Duration.days(1),
      }),
      threshold: BEDROCK_DAILY_CACHE_WRITE_TOKEN_THRESHOLD,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w52.addAlarmAction(new cloudwatch_actions.SnsAction(this.pageTopic));
    w52.addOkAction(new cloudwatch_actions.SnsAction(this.pageTopic));

    // NOTE: AWS Cost Anomaly Detection is intentionally NOT managed here.
    // A Cost Anomaly Detection monitor is an ACCOUNT-GLOBAL resource; a
    // per-environment stack (dev + prod both deploy this stack) cannot own
    // one without colliding with the other env's monitor and with any
    // pre-existing account monitor. The runaway signal is covered by the
    // W52 token-volume alarm (above) plus the per-owner invocation-logging
    // meter; if managed $-anomaly detection is wanted, configure it ONCE at
    // account scope, not in this per-env stack.
  }

  // =========================================================================
  // Dashboard (~30 widgets)
  // =========================================================================

  private createDashboard(props: ObservabilityStackProps): void {
    const dims = { env: this.envName, service: "isol8-backend" };
    const albFullName = props.alb.loadBalancerFullName;

    const dashboard = new cloudwatch.Dashboard(this, "OrrDashboard", {
      dashboardName: `isol8-${this.envName}-orr`,
    });

    // ----- Row 1: SLOs (2 widgets) -----
    const chatSuccessRate = new cloudwatch.MathExpression({
      expression: "100 * (1 - errors / IF(total > 0, total, 1))",
      usingMetrics: {
        errors: new cloudwatch.Metric({
          namespace: "Isol8",
          metricName: "chat.error",
          statistic: "Sum",
          dimensionsMap: dims,
        }),
        total: new cloudwatch.Metric({
          namespace: "Isol8",
          metricName: "chat.message.count",
          statistic: "Sum",
          dimensionsMap: dims,
        }),
      },
      period: cdk.Duration.hours(1),
    });

    const chatP99Latency = new cloudwatch.Metric({
      namespace: "Isol8",
      metricName: "chat.e2e.latency",
      statistic: "p99",
      period: cdk.Duration.hours(1),
      dimensionsMap: dims,
    });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Chat success rate (SLO: 99.5%)",
        left: [chatSuccessRate],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Chat p99 latency (SLO: <20s)",
        left: [chatP99Latency],
        width: 12,
        height: 6,
      }),
    );

    // ----- Row 2: Containers & Gateway (4 widgets) -----
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Container provisions",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "container.provision",
            statistic: "Sum",
            dimensionsMap: { ...dims, status: "ok" },
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "container.provision",
            statistic: "Sum",
            dimensionsMap: { ...dims, status: "error" },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Container lifecycle latency",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "container.lifecycle.latency",
            statistic: "p99",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Gateway connections (open)",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.connection.open",
            statistic: "Average",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Gateway RPC errors",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.rpc.error",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
    );

    // ----- Row 3: Chat pipeline (3 widgets) -----
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Chat messages & errors",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "chat.message.count",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "chat.error",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Chat E2E latency",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "chat.e2e.latency",
            statistic: "p50",
            dimensionsMap: dims,
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "chat.e2e.latency",
            statistic: "p99",
            dimensionsMap: dims,
          }),
        ],
        width: 8,
        height: 6,
      }),
      // "Bedrock throttles" widget removed — chat.bedrock.throttle is never
      // emitted; AWS-native Bedrock throttles are on the W46 alarm.
    );

    // ----- Row 3.5: Runtime gauges (2 widgets) -----
    // Backend-liveness heartbeat is the P12 alarm; these widgets are for
    // at-a-glance visibility, not gating. The old free-tier scale-to-zero
    // widgets ("Running by tier" → gateway.running.count.by_tier,
    // "record_activity outcomes & scale-to-zero events" →
    // gateway.record_activity.count / gateway.idle.scale_to_zero) were removed:
    // those metrics are never emitted (reaper deleted in the flat-fee cutover).
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Running containers (gauge)",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.running.count",
            statistic: "Maximum",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Cold starts & latency",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.cold_start.count",
            statistic: "Sum",
            dimensionsMap: { ...dims, outcome: "ok" },
            label: "cold starts (ok)",
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.cold_start.count",
            statistic: "Sum",
            dimensionsMap: { ...dims, outcome: "error" },
            label: "cold starts (error)",
          }),
        ],
        right: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.cold_start.latency",
            statistic: "p50",
            dimensionsMap: dims,
            label: "p50 latency",
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "gateway.cold_start.latency",
            statistic: "p99",
            dimensionsMap: dims,
            label: "p99 latency",
          }),
        ],
        width: 6,
        height: 6,
      }),
    );

    // ----- Row 4: Channels & Billing (4 widgets) -----
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Channel RPC by status",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "channel.rpc",
            statistic: "Sum",
            dimensionsMap: { ...dims, status: "ok" },
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "channel.rpc",
            statistic: "Sum",
            dimensionsMap: { ...dims, status: "error" },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Stripe webhooks",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "stripe.webhook.received",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "stripe.webhook.sig_fail",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Stripe API latency",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "stripe.api.latency",
            statistic: "p99",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
      // "Billing budget check errors" widget removed —
      // billing.budget_check.error is never emitted.
    );

    // ----- Row 5: Auth & Security (3 widgets) -----
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Auth JWT failures",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "auth.jwt.fail",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Workspace path traversal attempts",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "workspace.path_traversal.attempt",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Debug endpoint prod hits",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "debug.endpoint.prod_hit",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 8,
        height: 6,
      }),
    );

    // ----- Row 6: Infrastructure ALB + API GW (4 widgets) -----
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "ALB 5xx / request count",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ApplicationELB",
            metricName: "HTTPCode_Target_5XX_Count",
            statistic: "Sum",
            dimensionsMap: { LoadBalancer: albFullName },
          }),
          new cloudwatch.Metric({
            namespace: "AWS/ApplicationELB",
            metricName: "RequestCount",
            statistic: "Sum",
            dimensionsMap: { LoadBalancer: albFullName },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "ALB response time p99",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ApplicationELB",
            metricName: "TargetResponseTime",
            statistic: "p99",
            dimensionsMap: { LoadBalancer: albFullName },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "API GW WebSocket errors",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ApiGateway",
            metricName: "5XXError",
            statistic: "Sum",
            dimensionsMap: { ApiId: props.wsApiId },
          }),
          new cloudwatch.Metric({
            namespace: "AWS/ApiGateway",
            metricName: "4XXError",
            statistic: "Sum",
            dimensionsMap: { ApiId: props.wsApiId },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "API GW WebSocket latency",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ApiGateway",
            metricName: "IntegrationLatency",
            statistic: "p99",
            dimensionsMap: { ApiId: props.wsApiId },
          }),
        ],
        width: 6,
        height: 6,
      }),
    );

    // ----- Row 7: ECS + EFS (4 widgets) -----
    const clusterName = props.cluster.clusterName;
    const dashServiceName = cdk.Fn.select(
      2,
      cdk.Fn.split("/", props.backendService.serviceArn),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Backend CPU %",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ECS",
            metricName: "CPUUtilization",
            statistic: "Average",
            dimensionsMap: {
              ClusterName: clusterName,
              ServiceName: dashServiceName,
            },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Backend Memory %",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ECS",
            metricName: "MemoryUtilization",
            statistic: "Average",
            dimensionsMap: {
              ClusterName: clusterName,
              ServiceName: dashServiceName,
            },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "EFS I/O limit %",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/EFS",
            metricName: "PercentIOLimit",
            statistic: "Maximum",
            dimensionsMap: {
              FileSystemId: props.efsFileSystem.fileSystemId,
            },
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "EFS burst credits",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/EFS",
            metricName: "BurstCreditBalance",
            statistic: "Minimum",
            dimensionsMap: {
              FileSystemId: props.efsFileSystem.fileSystemId,
            },
          }),
        ],
        width: 6,
        height: 6,
      }),
    );

    // ----- Row 8: DynamoDB + Update worker + Bedrock (3 widgets) -----
    // "Proxy upstream errors" widget removed — proxy.upstream is never emitted.
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        // Repointed off the never-emitted custom Isol8/dynamodb.throttle to the
        // AWS-native AWS/DynamoDB ThrottledRequests (summed across our tables),
        // matching the P8 alarm.
        title: "DynamoDB throttled requests (AWS-native)",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.usersTable.tableName,
            },
            label: "users",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.containersTable.tableName,
            },
            label: "containers",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.billingTable.tableName,
            },
            label: "billing",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.usageCountersTable.tableName,
            },
            label: "usage-counters",
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Update worker heartbeat",
        left: [
          new cloudwatch.Metric({
            namespace: "Isol8",
            metricName: "update.scheduled_worker.heartbeat",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Bedrock invocations",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/Bedrock",
            metricName: "Invocations",
            statistic: "Sum",
          }),
        ],
        width: 6,
        height: 6,
      }),
    );
  }

  // =========================================================================
  // Account hardening
  // =========================================================================

  private createAccountHardening(
    props: ObservabilityStackProps,
  ): void {
    // GuardDuty and Access Analyzer are already enabled at the account level
    // (prod has them pre-configured). We only create EventBridge rules to
    // route their findings to our SNS warn topic.

    // GuardDuty findings → warn SNS via EventBridge
    const guardDutyRule = new events.Rule(this, "GuardDutyFindings", {
      ruleName: `isol8-${this.envName}-guardduty-findings`,
      description: "Route GuardDuty findings to warn SNS topic",
      eventPattern: {
        source: ["aws.guardduty"],
        detailType: ["GuardDuty Finding"],
      },
    });
    guardDutyRule.addTarget(new events_targets.SnsTopic(this.warnTopic));

    // Access Analyzer findings → warn SNS via EventBridge
    const accessAnalyzerRule = new events.Rule(
      this,
      "AccessAnalyzerFindings",
      {
        ruleName: `isol8-${this.envName}-access-analyzer-findings`,
        description:
          "Route IAM Access Analyzer findings to warn SNS topic",
        eventPattern: {
          source: ["aws.access-analyzer"],
          detailType: ["Access Analyzer Finding"],
        },
      },
    );
    accessAnalyzerRule.addTarget(
      new events_targets.SnsTopic(this.warnTopic),
    );
  }
}
