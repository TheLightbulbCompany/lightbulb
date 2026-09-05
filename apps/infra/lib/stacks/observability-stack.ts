import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import { resourcePrefix } from "../table-names";
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
import * as route53 from "aws-cdk-lib/aws-route53";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../domains";

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
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  /**
   * Domain the platform sender + agent mail live on (see lib/domains.ts).
   * Defaults to PLATFORM_MAIL_DOMAIN. The alert mailbox lives here.
   */
  platformMailDomain?: string;
  // Service stack references
  backendService: ecs.IBaseService;
  backendLogGroupName: string;
  // Network / ALB
  alb: elbv2.ApplicationLoadBalancer;
  // Backend target group — REQUIRED for the ALB host-count alarms (W28/W48).
  // HealthyHostCount / UnHealthyHostCount are only published per (LoadBalancer,
  // TargetGroup); an alarm scoped to LoadBalancer alone never gets datapoints.
  // Concrete type (not the interface) so `targetGroupFullName` is available.
  targetGroup: elbv2.ApplicationTargetGroup;
  // API Gateway
  wsApiId: string;
  // Container stack references
  cluster: ecs.ICluster;
  efsFileSystem: efs.IFileSystem;
  // Database table NAMES (for DynamoDB alarms).
  //
  // Names, not ITable: taking the construct creates a cross-stack Export from
  // the database stack, and CloudFormation cannot change an exported value
  // while anything imports it — which is exactly what blocked the table rename
  // ("Cannot update export ...UsageCountersTable... as it is in use by
  // isol8-dev-observability"). These are only ever used as CloudWatch dimension
  // strings, so the construct was never needed.
  databaseTables: {
    usersTable: string;
    containersTable: string;
    billingTable: string;
    usageCountersTable: string;
    pendingUpdatesTable: string;
    channelLinksTable: string;
    companySlugsTable: string;
    agentEmailAddressesTable: string;
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
  /** Pre-built metric: a MathExpression, an AWS-native namespace with its own
   * dimension scheme, or a metric built from a construct reference.
   * Mutually exclusive with metricName. */
  metric?: cloudwatch.IMetric;
  /** Custom Isol8-namespace metric; env + service dimensions auto-injected. */
  metricName?: string;
  namespace?: string; // default "LightbulbAI"
  statistic?: string; // default "Sum"
  threshold: number;
  evaluationPeriods?: number; // default 1
  periodMinutes?: number; // required with metricName
  comparisonOperator?: cloudwatch.ComparisonOperator; // default GREATER_THAN_THRESHOLD
  treatMissingData?: cloudwatch.TreatMissingData; // default NOT_BREACHING
  /**
   * M-of-N flap resistance: require the metric to breach on this many of the
   * last `evaluationPeriods` datapoints before firing. Omit for 1-of-1 (fire on
   * a single breaching datapoint). Set this on spiky signals (latency p99, error
   * rates) so one anomalous datapoint does not page/warn and then immediately
   * clear, multiplying notifications.
   */
  datapointsToAlarm?: number;
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
  private readonly envName: string;
  private readonly rootDomain: string;
  /**
   * Alert mailbox. Baked into the SNS EmailSubscription CONSTRUCT ID, so a
   * value change replaces both subscriptions and needs a fresh confirmation
   * click — see the note at the assignment site below.
   */
  private readonly alertEmail: string;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    this.envName = props.envName;
    this.rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    this.alertEmail = `admin@${props.platformMailDomain ?? PLATFORM_MAIL_DOMAIN}`;

    // -----------------------------------------------------------------------
    // SNS Topics
    // -----------------------------------------------------------------------

    // Page tier: fires on customer-impacting events.
    this.pageTopic = new sns.Topic(this, "AlertsPage", {
      topicName: `${resourcePrefix(props.envName)}alerts-page`,
      displayName: "Isol8 Page",
    });

    // Warn tier.
    this.warnTopic = new sns.Topic(this, "AlertsWarn", {
      topicName: `${resourcePrefix(props.envName)}alerts-warn`,
      displayName: "Isol8 Warn",
    });

    // A CloudWatch alarm action publishes as the CloudWatch SERVICE principal,
    // not as this stack's deploy role, so it needs an explicit resource-policy
    // grant. That is not belt-and-braces: attaching an EventBridge rule to a
    // topic (the Aurora and GuardDuty rules further down both target the WARN
    // topic) makes CDK emit an `AWS::SNS::TopicPolicy`, and a TopicPolicy
    // REPLACES the topic's default account-owner statement rather than merging
    // with it. That is exactly how the warn topic ended up granting only
    // events.amazonaws.com while all 89 warn alarms failed their action with
    // `CloudWatch Alarms is not authorized to perform: SNS:Publish` — silently,
    // because a failed alarm action is not itself an alarm. The page tier
    // survived only because nothing targets it from EventBridge.
    //
    // Granting BOTH topics also narrows the page topic from the AWS default
    // policy to an explicit one. That is safe: only SERVICE principals need the
    // resource policy, and the two that publish here (cloudwatch, events) are
    // both granted. Same-account IAM principals are unaffected, because SNS
    // honours identity-based policies independently.
    this.allowCloudWatchAlarmPublish(this.pageTopic);
    this.allowCloudWatchAlarmPublish(this.warnTopic);

    // -----------------------------------------------------------------------
    // Alert destinations
    // -----------------------------------------------------------------------
    //
    // READ THIS BEFORE CHANGING AN ADDRESS. An SNS email subscription is only
    // live once a human clicks the confirmation link, and SNS DELETES the
    // subscription if nobody clicks within 3 days. CloudFormation does not
    // notice: it keeps reporting CREATE_COMPLETE against a subscription that
    // no longer exists, so no future `cdk deploy` recreates it.
    //
    // That is not hypothetical — it already happened here. Both topics ran
    // with ZERO subscribers while ~105 alarms fired into the void, and a
    // real alarm (bedrock-meter DLQ) sat unnoticed in ALARM for four weeks.
    //
    // So: changing an address here is NOT enough. CDK only replaces the
    // subscription when the CONSTRUCT ID changes (the address is part of the
    // generated id, so a new address does force a new resource) — but the
    // deploy is only half the job. Someone must then click the confirmation
    // link in the new inbox, and verify with:
    //
    //   aws sns list-subscriptions-by-topic --topic-arn <arn>
    //
    // A subscription is only real when that command shows a SubscriptionArn
    // that is not the literal string "PendingConfirmation".
    const alertEmail = this.alertEmail;

    this.pageTopic.addSubscription(new subs.EmailSubscription(alertEmail));
    this.warnTopic.addSubscription(new subs.EmailSubscription(alertEmail));

    // NOTE: there is deliberately no SMS subscription. SNS cannot send SMS
    // from this account — it has no registered US origination identity and
    // sits in the SMS sandbox, so `CreateSMSSandboxPhoneNumber` fails with
    // "No origination entities available to send". Adding an SMS subscription
    // here would deploy clean and silently never deliver. Wire it only once a
    // toll-free number is registered AND the account has left the sandbox.
    //
    // PagerDuty covers that gap instead: it turns a page into a phone push
    // with escalation, needs no carrier registration, and — unlike the email
    // subscriptions above — CANNOT silently rot, because PagerDuty answers
    // SNS's confirmation callback programmatically. There is no link to click
    // and no 3-day expiry window.
    if (props.envName === "prod") {
      // The URL embeds the integration key, so it lives in SSM rather than in
      // this file. Read with valueForStringParameter (a deploy-time CFN
      // dynamic reference), NOT valueFromLookup, which would resolve at synth
      // and bake the key into the committed template.
      const pagerDutyUrl = ssm.StringParameter.valueForStringParameter(
        this,
        "/lightbulb/prod/pagerduty-page-url",
      );

      // sns.Subscription directly, NOT pageTopic.addSubscription(new
      // UrlSubscription(...)). UrlSubscription hardcodes its subscriberId to
      // the URL string, and the URL here is an unresolved token — that would
      // put `${Token[TOKEN.nnn]}` in the construct id, which shifts between
      // synths and churns the resource. This spells the id out instead.
      new sns.Subscription(this, "PagerDutyPage", {
        topic: this.pageTopic,
        endpoint: pagerDutyUrl,
        protocol: sns.SubscriptionProtocol.HTTPS,
        // Must stay false. PagerDuty's CloudWatch integration parses the SNS
        // envelope and expects a nested JSON-encoded object in the Message
        // property; raw delivery strips that and nothing triggers.
        rawMessageDelivery: false,
      });
    }

    // Warn tier stays email-only on purpose. Routing ~90 warn alarms into
    // PagerDuty would create an incident per blip and train you to ignore the
    // thing whose entire job is to be impossible to ignore.

    // -----------------------------------------------------------------------
    // Alarms
    // -----------------------------------------------------------------------
    this.createPageAlarms(props);
    this.createWarnCustomMetricAlarms();
    this.createWarnAwsNativeAlarms(props);
    this.createPaperclipTierAlarms(props);
    this.createCostAlarms(props);
    this.createUptimeChecks();

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
      exportName: `${resourcePrefix(props.envName)}page-topic-arn`,
    });

    new cdk.CfnOutput(this, "WarnTopicArn", {
      value: this.warnTopic.topicArn,
      exportName: `${resourcePrefix(props.envName)}warn-topic-arn`,
    });
  }

  /**
   * Let same-account CloudWatch alarms publish to an alert topic.
   *
   * The ARN pattern is built from `resourcePrefix`, and so is every alarm name
   * that can publish here — this stack's (`createAlarm` and the one-off
   * constructors) AND the alarms in bedrock-gate, bedrock-meter, service and
   * agent-mail-outbound, which import these topics by ARN. That shared helper
   * is load-bearing, not cosmetic: a previous attempt at this fix hard-coded an
   * `isol8-${env}-*` pattern, the rebrand later renamed every alarm to
   * `lightbulb-*`, and the grant silently matched nothing while its test stayed
   * green. Two independently-maintained strings that happen to agree today are
   * exactly how that recurs, so both sides now read from one source.
   */
  private allowCloudWatchAlarmPublish(topic: sns.Topic): void {
    topic.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "AllowSameAccountCloudWatchAlarmsPublish",
        principals: [new iam.ServicePrincipal("cloudwatch.amazonaws.com")],
        actions: ["sns:Publish"],
        resources: [topic.topicArn],
        conditions: {
          StringEquals: {
            "AWS:SourceAccount": cdk.Aws.ACCOUNT_ID,
          },
          ArnLike: {
            "AWS:SourceArn": `arn:${cdk.Aws.PARTITION}:cloudwatch:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:alarm:${resourcePrefix(this.envName)}*`,
          },
        },
      }),
    );
  }

  // =========================================================================
  // Alarm helper
  // =========================================================================

  /**
   * Creates a single CloudWatch alarm from a definition object.
   *
   * Wires the alarm to its severity's SNS topic. EVERY alarm in this stack
   * goes through here.
   *
   * Pass `metricName` for a custom Isol8-namespace metric -- `env` and
   * `service` dimensions are auto-injected to match the EMF emitter output.
   * Pass `metric` instead for anything else: a MathExpression, an AWS-native
   * namespace (AWS/ApplicationELB, AWS/ApiGateway, ...) with its own
   * dimension scheme, or a metric built from a construct reference.
   */
  private createAlarm(def: AlarmDef): cloudwatch.Alarm {
    // Auto-inject env and service dimensions for custom Isol8 namespace
    const isCustomNamespace = !def.namespace || def.namespace === "LightbulbAI";
    const dimensions = isCustomNamespace
      ? {
          env: this.envName,
          service: "lightbulb-backend",
          ...(def.dimensions ?? {}),
        }
      : (def.dimensions ?? {});

    const metric =
      def.metric ??
      new cloudwatch.Metric({
        namespace: def.namespace ?? "LightbulbAI",
        metricName: def.metricName!,
        statistic: def.statistic ?? "Sum",
        period: cdk.Duration.minutes(def.periodMinutes!),
        dimensionsMap: dimensions,
      });

    const alarm = new cloudwatch.Alarm(this, def.id, {
      alarmName: `${resourcePrefix(this.envName)}${def.id}-${def.name}`,
      alarmDescription: def.description,
      metric,
      threshold: def.threshold,
      evaluationPeriods: def.evaluationPeriods ?? 1,
      datapointsToAlarm: def.datapointsToAlarm,
      comparisonOperator:
        def.comparisonOperator ??
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData:
        def.treatMissingData ?? cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarm.addAlarmAction(
      new cloudwatch_actions.SnsAction(
        def.severity === "page" ? this.pageTopic : this.warnTopic,
      ),
    );

    return alarm;
  }

  /**
   * A Sum over the [env, service, status] rollup put_metric publishes
   * (core/observability/metrics.py, _STATUS_ROLLUP_KEYS).
   *
   * Reach for this when an alarm cares about ONE dimension value
   * (status="error") but the emitter also tags a qualifier the alarm has no
   * business enumerating (op, provider). Binding the full set literally would
   * name a metric identity per qualifier value -- a permanently silent alarm
   * the moment a new provider appears. The { env, service } rollup is no help
   * either: it collapses the very dimension being filtered on, so an error
   * alarm would count successes too.
   *
   * This was a `SUM(SEARCH(...))` metric-math expression, which reads like the
   * natural answer and cannot work: CloudWatch rejects it with "SEARCH is not
   * supported on Metric Alarms" and rolled this stack back on EVERY deploy,
   * which also skipped every prod stage behind it. The limitation is at the
   * alarm layer with no workaround, so the aggregate has to be a real published
   * dimension set instead -- hence the emitter change rather than a cleverer
   * expression here. New qualifier values are still picked up automatically,
   * because they all land on the same rollup.
   */
  private statusSum(opts: {
    metricName: string;
    status: string;
    periodMinutes: number;
    label: string;
  }): cloudwatch.Metric {
    return new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: opts.metricName,
      statistic: "Sum",
      period: cdk.Duration.minutes(opts.periodMinutes),
      dimensionsMap: {
        env: this.envName,
        service: "lightbulb-backend",
        status: opts.status,
      },
      label: opts.label,
    });
  }

  // =========================================================================
  // Page-tier alarms
  // =========================================================================

  private createPageAlarms(props: ObservabilityStackProps): void {
    // P1: container-error-state
    const p1 = this.createAlarm({
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
      description: "Path traversal attempt blocked - security event",
    });

    // P11: marketplace-path-traversal
    // Backend emits marketplace.path_traversal_attempt (no extra dims) in
    // core/services/agent_export.py when a seller's agent_id resolves outside
    // their workspaces dir. Same breach-on-event security trip-wire shape as
    // P3 (workspace path traversal). Pages so a marketplace export attack is
    // never silent.
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
        "Marketplace agent export path traversal attempt blocked - security event",
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
      description: "Fleet-wide config patch invoked - audit trail",
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
        "Debug endpoint hit in production - should be 403d",
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
        "Update worker heartbeat absent for 5 min - loop may have died",
    });

    // P8: dynamodb-throttle-sustained (AWS-native metric math)
    // Repointed off the never-emitted custom `Isol8/dynamodb.throttle` to the
    // AWS-native `AWS/DynamoDB ThrottledRequests` metric so this is a real page
    // without any backend change. ThrottledRequests is per-table, so we sum
    // across the same table set W40/W41/W42 enumerate (account-wide math sum is
    // the acceptable fallback when a table set isn't available).
    const ddbThrottleTables: [string, string][] = [
      ["users", props.databaseTables.usersTable],
      ["containers", props.databaseTables.containersTable],
      ["billing", props.databaseTables.billingTable],
      ["usageCounters", props.databaseTables.usageCountersTable],
      ["pendingUpdates", props.databaseTables.pendingUpdatesTable],
      ["channelLinks", props.databaseTables.channelLinksTable],
      ["companySlugs", props.databaseTables.companySlugsTable],
      ["agentEmailAddresses", props.databaseTables.agentEmailAddressesTable],
    ];
    const ddbThrottlePerTable: Record<string, cloudwatch.IMetric> = {};
    for (const [key, table] of ddbThrottleTables) {
      ddbThrottlePerTable[key] = new cloudwatch.Metric({
        namespace: "AWS/DynamoDB",
        metricName: "ThrottledRequests",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { TableName: table },
      });
    }
    const ddbThrottleTotal = new cloudwatch.MathExpression({
      expression: Object.keys(ddbThrottlePerTable).join(" + "),
      usingMetrics: ddbThrottlePerTable,
      period: cdk.Duration.minutes(1),
    });
    this.createAlarm({
      id: "P8",
      name: "dynamodb-throttle-sustained",
      description: "DynamoDB throttling sustained for 2 consecutive minutes",
      metric: ddbThrottleTotal,
      threshold: 0,
      evaluationPeriods: 2,
      severity: "page",
    });

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
      alarmName: `${resourcePrefix(this.envName)}P9-alb-5xx-rate`,
      alarmDescription: "ALB 5xx error rate exceeds 5%",
      metric: albErrorRate,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    p9.addAlarmAction(new cloudwatch_actions.SnsAction(this.pageTopic));
    // P10: apigw-ws-server-error-rate (AWS-native metric math)
    //
    // BLIND-PAGE FIXED: this PAGE-tier alarm read `5XXError` and `Count` — WS
    // APIs publish NEITHER (verified live: a WebSocket ApiId publishes only
    // ClientError, ExecutionError, IntegrationError, IntegrationLatency,
    // ConnectCount, MessageCount). Numerator and denominator were both always
    // missing, so it could never fire: a page-tier alarm worse than none.
    // Repointed to the real server-side failure signal — ExecutionError (API GW
    // could not execute the route) + IntegrationError (the ws-* Lambda
    // integration returned an error) — as a rate over the two traffic metrics
    // that exist (MessageCount + ConnectCount). Distinct from P9 (ALB 5xx),
    // which is blind to the WS Lambda integration layer.
    //
    // datapointsToAlarm 2-of-3: a rate on a low-traffic launch fleet spikes when
    // one error lands in a near-empty window; requiring the elevated rate to
    // persist across 2 of 3 five-minute periods keeps a single blip from paging.
    //
    // FILL(m,0) on EVERY operand is load-bearing: CloudWatch arithmetic yields
    // NO datapoint for a period unless every operand has data in it, and these
    // error/traffic metrics are sparse (an error metric with no errors reports
    // nothing). Without FILL the rate goes null on almost every window, and
    // NOT_BREACHING then keeps the alarm from EVER firing — the same dead-page
    // failure mode we just fixed. FILL treats a missing operand as 0.
    const wsMetric = (metricName: string) =>
      new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName,
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ApiId: props.wsApiId },
      });
    const apiGwServerErrorRate = new cloudwatch.MathExpression({
      expression:
        "100 * (FILL(execErr,0) + FILL(intErr,0)) / IF((FILL(messages,0) + FILL(connects,0)) > 0, FILL(messages,0) + FILL(connects,0), 1)",
      usingMetrics: {
        execErr: wsMetric("ExecutionError"),
        intErr: wsMetric("IntegrationError"),
        messages: wsMetric("MessageCount"),
        connects: wsMetric("ConnectCount"),
      },
      period: cdk.Duration.minutes(5),
    });

    const p10 = new cloudwatch.Alarm(this, "P10", {
      alarmName: `${resourcePrefix(this.envName)}P10-apigw-ws-server-error-rate`,
      alarmDescription:
        "API Gateway WebSocket server-error rate (ExecutionError + IntegrationError) exceeds 5%",
      metric: apiGwServerErrorRate,
      threshold: 5,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    p10.addAlarmAction(new cloudwatch_actions.SnsAction(this.pageTopic));
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
    const reaperDims = { env: this.envName, service: "lightbulb-backend" };

    const heartbeatMetric = new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: "gateway.running.count",
      statistic: "SampleCount",
      period: cdk.Duration.minutes(10),
      dimensionsMap: reaperDims,
    });

    const heartbeatAlarm = new cloudwatch.Alarm(this, "ReaperHeartbeatAlarm", {
      alarmName: `${resourcePrefix(this.envName)}P12-backend-liveness`,
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

    // -------------------------------------------------------------------
    // P15: provision-inline-timeout
    // -------------------------------------------------------------------
    // Backend emits container.provision{status:"inline_timeout"} in
    // core/services/provision_dispatch.py when an inline provision attempt
    // hits its hard timeout (_INLINE_ATTEMPT_TIMEOUT_S, 1800s) and is
    // terminated. By the time this metric appears, one owner has already sat
    // WITHOUT a container for the entire window. The 2026-08-20 founder
    // outage had exactly this signature and paged nobody: the metric landed
    // on a dimension stream no alarm watched (W1 reads only status=ok/error),
    // and the SFN watchdog execution SUCCEEDED after taking the row over, so
    // the executions-failed/timed-out pages never fired either. This alarm
    // also covers the "unwatched attempt" degrade (StartExecution failed),
    // where the inline child is the ONLY thing running. Any occurrence pages.
    this.createAlarm({
      id: "P15",
      name: "provision-inline-timeout",
      metricName: "container.provision",
      dimensions: { status: "inline_timeout" },
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description:
        "An inline provision attempt hit its 1800s hard timeout - the owner " +
        "has been down for that whole window and recovery now rides the SFN " +
        "leg (or nothing, in the unwatched degrade).",
    });

    // -------------------------------------------------------------------
    // P16: company-stripe-account-created
    // -------------------------------------------------------------------
    // routers/company_stripe.py POST /company/stripe/create mints a real
    // Stripe Express account for an owner's company. That door is live in
    // prod and the owner has accepted the exposure, but there was no signal
    // at all when a real owner first touches it. Backend emits
    // company_stripe.account.created ONLY when a brand-new account is
    // actually minted and bound in that request (not the idempotent
    // early-return when one already exists, and not an account adopted
    // after losing a concurrent create race) — see routers/company_stripe.py.
    //
    // WARN, not page, and deliberately so: an owner minting their company
    // Stripe account is a DESIRABLE business event. It is worth knowing about
    // promptly while the door is new and unverified, but it fires again on
    // every future success, and an alarm that pages on success is how a team
    // learns to ignore pages. P4/P5/P11 page because the event they catch is
    // one nobody wants; this one is the opposite.
    this.createAlarm({
      id: "P16",
      name: "company-stripe-account-created",
      metricName: "company_stripe.account.created",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "A company Stripe Express account was minted via the live " +
        "create door - first real usage signal, not an outage.",
    });
  }

  // =========================================================================
  // Warn-tier custom metric alarms (W1-W27)
  // =========================================================================

  private createWarnCustomMetricAlarms(): void {
    const dims = { env: this.envName, service: "lightbulb-backend" };

    // -- Container & gateway (W1-W8) --

    // W1: container-provision-error-rate (metric math)
    // Backend emits container.provision with status="ok" or status="error"
    // dimension. These are the provision RUNNER outcomes (put_metric in
    // core/containers/ecs_manager.py — status="error" at ~3251/3255/3280/3319/
    // 3442/3473, status="ok" at ~3284/3323/3477), emitted on every leg the
    // runner executes on: the inline attempt AND the SFN-watchdog / admin /
    // recover legs. So error/(error+ok) is the real failure rate across all
    // provisions. (The dispatcher-layer statuses — inline_ok/inline_failed/
    // claim_lost/inline_queue_saturated — cover only the inline leg and are NOT
    // the provision-failure signal; do not repoint W1 at them. `list-metrics`
    // hides status="error" when nothing has failed recently, which is not the
    // same as it never being emitted. See also the P15 note below.)
    // CloudWatch EMF creates separate streams per dimension set, so we query
    // each status value explicitly and sum for the total.
    const provisionErrors = new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: "container.provision",
      statistic: "Sum",
      period: cdk.Duration.minutes(10),
      dimensionsMap: { ...dims, status: "error" },
    });
    const provisionOk = new cloudwatch.Metric({
      namespace: "LightbulbAI",
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
    this.createAlarm({
      id: "W1",
      name: "container-provision-error-rate",
      description: "Container provision error rate exceeds 5%",
      metric: provisionRate,
      threshold: 5,
      severity: "warn",
    });

    // W2: container-lifecycle-latency-p99
    // Flap resistance (2-of-3 over 10-min periods = a 30-min window): a single
    // slow ECS cold start pushing one p99 bucket over 60s is not an incident.
    // Missing data = no provisions = NOT_BREACHING (helper default).
    this.createAlarm({
      id: "W2",
      name: "container-lifecycle-latency-p99",
      metricName: "container.lifecycle.latency",
      statistic: "p99",
      threshold: 60000,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      periodMinutes: 10,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Container lifecycle p99 latency exceeds 60s",
    });

    // W3: container-efs-access-point-fail
    // The emitter tags { op, status } (op = create | delete), so a literal
    // { status: "error" } bound a set nothing publishes. Search across ops.
    this.createAlarm({
      id: "W3",
      name: "container-efs-access-point-fail",
      metric: this.statusSum({
        metricName: "container.efs.access_point",
        status: "error",
        periodMinutes: 1,
        label: "EFS access point errors (all ops)",
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
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

    // W5 (gateway-connection-drop) DELETED: absence-of-signal noise.
    // The `gateway.connection.open <= 0` design fired whenever no owner had a
    // chat tab open — which, on a launch-stage fleet, is most of the time. The
    // metric is an emitted gauge, so an idle backend publishes a real 0 (not
    // missing data), and NOT_BREACHING could not save it: it only suppresses
    // MISSING datapoints, not a genuine 0. So W5 sat in ALARM in dev AND prod
    // and emailed the warn topic on every quiet window — the owner's #1 source
    // of alarm noise.
    //
    // Not redefined: a real "connections dropped while traffic continues" signal
    // needs anomaly detection plus a co-metric for live traffic, and connection
    // health is ALREADY covered without it — W6 (health-check timeouts), W8
    // (RPC errors), W54 (reconnect-throttle storm) and P12 (backend-liveness via
    // gateway.running.count) all fire on a real gateway problem. A gauge falling
    // to zero because nobody is connected is not one. Deleting is the correct,
    // minimal fix.

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
        "Frontend connection prune storm - over 100 in 1 hour",
    });

    // W8: gateway-rpc-error-count
    // Was a metric-math RATE with chat.message.count as the denominator, but
    // RPC volume ≫ chat volume (every chat is many RPCs), so that ratio was
    // meaningless. No true RPC-total metric is emitted, so this is an ABSOLUTE
    // error-count threshold on gateway.rpc.error instead.
    // TODO(observability Phase 2): emit gateway.rpc.count for a true rate.
    const rpcErrors = new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: "gateway.rpc.error",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: dims,
    });
    this.createAlarm({
      id: "W8",
      name: "gateway-rpc-error-count",
      description: "Gateway RPC errors exceed 10 in 5 min",
      metric: rpcErrors,
      threshold: 10,
      severity: "warn",
    });

    // -- Chat (W9-W12) --

    // W9: chat-e2e-latency-p99
    // FLAPPING FIXED: was 1-of-1, so a single slow datapoint tripped it and it
    // cleared on the next period, multiplying notifications for a REAL but spiky
    // prod signal. 3-of-5 over 5-min periods (a 25-min window) requires the p99
    // to actually sit above the 20s SLO before warning. Missing data = no chats
    // = NOT_BREACHING (helper default): quiet windows never fire.
    this.createAlarm({
      id: "W9",
      name: "chat-e2e-latency-p99",
      metricName: "chat.e2e.latency",
      statistic: "p99",
      threshold: 20000,
      evaluationPeriods: 5,
      datapointsToAlarm: 3,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Chat p99 latency exceeds 20s SLO target",
    });

    // W10: chat-error-rate (metric math)
    const chatErrors = new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: "chat.error",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: dims,
    });
    const chatTotal = new cloudwatch.Metric({
      namespace: "LightbulbAI",
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
      alarmName: `${resourcePrefix(this.envName)}W10-chat-error-rate`,
      alarmDescription: "Chat error rate exceeds 1%",
      metric: chatErrorRate,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w10.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));
    // W10b: litestream-put-requests.
    // Litestream's documented failure modes are quiet: the idle-PUT pathology
    // (litestream#1171, ~480 PUT/hr against a DB with zero writes) and
    // checkpoint loops both burn S3 requests without erroring. Nominal idle is
    // ~0 PUTs/DB, so sustained six-figure hourly PUTs across the fleet is
    // never normal. Bucket name is the literal deterministic one (no
    // cross-stack ref — same anti-KMS-cycle posture as the rest of the repo).
    const litestreamPuts = new cloudwatch.Alarm(this, "W10B", {
      alarmName: `${resourcePrefix(this.envName)}W10B-litestream-put-requests`,
      alarmDescription:
        "Litestream S3 PUT volume far above the active-fleet ceiling - idle-PUT pathology or a runaway checkpoint loop",
      metric: new cloudwatch.Metric({
        namespace: "AWS/S3",
        metricName: "PutRequests",
        dimensionsMap: {
          BucketName: `lightbulb-${this.envName}-openclaw-replicas`,
          FilterId: "EntireBucket",
        },
        statistic: "Sum",
        period: cdk.Duration.hours(1),
      }),
      threshold: 100_000,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    litestreamPuts.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

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
    // Both channel metrics are tagged { provider, status }, so the old literal
    // { status: "error" } bound a set nothing publishes. Use the status rollup.
    this.createAlarm({
      id: "W13",
      name: "channel-rpc-error-rate",
      metric: this.statusSum({
        metricName: "channel.rpc",
        status: "error",
        periodMinutes: 60,
        label: "Channel RPC errors (all providers)",
      }),
      threshold: 10,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description: "Channel RPC errors exceed 10 per hour",
    });

    // W14: channel-configure-fail
    this.createAlarm({
      id: "W14",
      name: "channel-configure-fail",
      metric: this.statusSum({
        metricName: "channel.configure",
        status: "error",
        periodMinutes: 1,
        label: "Channel configure failures (all providers)",
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
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
        "JWT failures exceed 100 per hour - possible attack",
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
        "A per-user container is dropping Paperclip relay connections >10/15min - agent heartbeats can't stay connected; find the owner via the operator_proxy 'Operator upstream closed' logs",
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
        "Gateway reconnect throttles exceed 20 in 15 min - reconnect storm (misbehaving container burning the open-handshake budget)",
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
        "Metronome credit-gate balance refresh failing - gate serving stale cached balances; possible Metronome outage",
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
        "Metronome webhook signature verification failing - secret drift or forged-webhook probe; credit gate not getting trustworthy balance updates",
    });

    // W63: paperclip-down.
    // Backend emits paperclip.upstream.error{kind:"unreachable"} in
    // routers/paperclip_proxy.py + core/services/paperclip_admin_client.py
    // when the Paperclip BFF raises an httpx connect/transport error. Replaces
    // the dropped ALB-HealthyHostCount W63 (PaperclipStack has no ALB target
    // group, so there is no HealthyHostCount to alarm on). Sum > 0 over a short
    // window, NOT_BREACHING (absent metric = healthy, no error emissions).
    //
    // W63 measures IMPACT, not availability, and NOT_BREACHING is correct for
    // that: no error emissions genuinely means nothing is being hurt. The cost
    // is that a Paperclip outage in a quiet window produces no failed calls, so
    // W63 stays OK while Paperclip is down (seen live in the 2026-06-19 FIS S5
    // paperclip-kill run). AVAILABILITY is covered by its complement,
    // W65-paperclip-no-running-task (createPaperclipTierAlarms), which alarms on
    // the AWS/ECS LiveTaskCount of the Paperclip service — published on a timer
    // regardless of traffic. Keep both: W63 says "users are being hurt", W65
    // says "the container is gone". Neither subsumes the other.
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
        "Paperclip BFF is unreachable from the backend (httpx connect/transport errors) - Teams/agent flows are degraded. Replaces the dropped ALB-HealthyHostCount W63 (PaperclipStack has no ALB target group).",
    });

    // W64: provision-gate-block-spike.
    // Backend emits provision.gate.blocked{code} in core/services/provision_gate.py
    // whenever the gate blocks an owner. This alarm passes NO dimensions, so it
    // reads the {env,service} rollup put_metric publishes alongside the coded
    // set — one datapoint per block, summed across every code. The gate used to
    // ALSO emit a dimensionless copy for this alarm; that is now a double-count
    // and was removed with the rollup.
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
        "Provision gate blocking >10 owners in 15 min - likely a provider outage (Stripe/Metronome/OAuth) silently gating users, not normal unsubscribed traffic. Provisional threshold; sums across all codes.",
    });
  }

  // =========================================================================
  // Warn-tier AWS-native infrastructure alarms (W28-W48)
  // =========================================================================

  private createWarnAwsNativeAlarms(
    props: ObservabilityStackProps,
  ): void {
    const albFullName = props.alb.loadBalancerFullName;
    // HealthyHostCount / UnHealthyHostCount are per-target-group metrics — both
    // the LoadBalancer AND TargetGroup dimensions are required or CloudWatch
    // publishes no datapoints for the alarm.
    const tgFullName = props.targetGroup.targetGroupFullName;

    // -- Load balancer (W28-W29) --

    // W28: ALB UnHealthyHostCount
    this.createAlarm({
      id: "W28",
      name: "alb-unhealthy-hosts",
      description: "ALB unhealthy host count > 0 for 5 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "UnHealthyHostCount",
        statistic: "Maximum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { LoadBalancer: albFullName, TargetGroup: tgFullName },
      }),
      threshold: 0,
      evaluationPeriods: 5,
      severity: "warn",
    });

    // W29: ALB TargetResponseTime p99
    // Flap resistance: 3-of-5 over 5-min periods so one slow request bucket does
    // not warn-then-clear. Missing data = no traffic = NOT_BREACHING (default).
    this.createAlarm({
      id: "W29",
      name: "alb-response-time-p99",
      description: "ALB target response time p99 exceeds 5s",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "TargetResponseTime",
        statistic: "p99",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { LoadBalancer: albFullName },
      }),
      threshold: 5,
      evaluationPeriods: 5,
      datapointsToAlarm: 3,
      severity: "warn",
    });

    // -- API Gateway WebSocket (W30-W32) --

    // W30: API GW WS client-error rate
    //
    // BLIND-ALARM FIXED: read `4XXError` and `Count`, which WS APIs do not
    // publish (only ClientError, ExecutionError, IntegrationError,
    // IntegrationLatency, ConnectCount, MessageCount exist), so it could never
    // fire. Repointed to ClientError (the WS 4xx-equivalent: rejected/malformed
    // client frames) as a rate over the traffic metrics that exist
    // (MessageCount + ConnectCount). 3-of-5 over 5-min periods so a single
    // client-error blip on a quiet window does not warn-then-clear.
    //
    // FILL(m,0) on every operand: CloudWatch arithmetic drops a period unless
    // every operand has data, and ClientError/traffic are sparse — without FILL
    // the rate (and the whole denominator) goes null the moment one operand is
    // missing, so a real ClientError spike could produce no datapoint at all.
    const wsMetric30 = (metricName: string) =>
      new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName,
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ApiId: props.wsApiId },
      });
    const apiGwClientErrorRate = new cloudwatch.MathExpression({
      expression:
        "100 * FILL(errors,0) / IF((FILL(messages,0) + FILL(connects,0)) > 0, FILL(messages,0) + FILL(connects,0), 1)",
      usingMetrics: {
        errors: wsMetric30("ClientError"),
        messages: wsMetric30("MessageCount"),
        connects: wsMetric30("ConnectCount"),
      },
      period: cdk.Duration.minutes(5),
    });
    this.createAlarm({
      id: "W30",
      name: "apigw-ws-client-error-rate",
      description: "API Gateway WebSocket client-error rate exceeds 5%",
      metric: apiGwClientErrorRate,
      threshold: 5,
      evaluationPeriods: 5,
      datapointsToAlarm: 3,
      severity: "warn",
    });

    // W31: API GW WS IntegrationLatency p99
    // FLAPPING FIXED: was 1-of-1, so a single slow integration datapoint tripped
    // it then cleared — a real prod signal that multiplied notifications. 3-of-5
    // over 5-min periods (25-min window). Missing data = no messages =
    // NOT_BREACHING (helper default).
    this.createAlarm({
      id: "W31",
      name: "apigw-ws-integration-latency",
      description: "API Gateway WebSocket integration latency p99 exceeds 2s",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName: "IntegrationLatency",
        statistic: "p99",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ApiId: props.wsApiId },
      }),
      threshold: 2000,
      evaluationPeriods: 5,
      datapointsToAlarm: 3,
      severity: "warn",
    });

    // W32: API GW WS ConnectCount drop (static threshold — zero connections)
    // NOT_BREACHING: only catches a real non-zero → zero transition, not the
    // simple absence of connections on idle dev (the old BREACHING config
    // false-fired constantly when nobody was connected).
    this.createAlarm({
      id: "W32",
      name: "apigw-ws-connect-drop",
      description: "API Gateway WebSocket connect count dropped to zero",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName: "ConnectCount",
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ApiId: props.wsApiId },
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
    });

    // -- Lambda authorizer (W33-W35) --
    const lambdaDims = {
      FunctionName: props.authorizerFunctionName,
    };

    // W33: Lambda Errors
    this.createAlarm({
      id: "W33",
      name: "lambda-auth-errors",
      description: "Lambda authorizer errors > 0",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Errors",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: lambdaDims,
      }),
      threshold: 0,
      severity: "warn",
    });

    // W34: Lambda Throttles
    this.createAlarm({
      id: "W34",
      name: "lambda-auth-throttles",
      description: "Lambda authorizer throttles > 0",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Throttles",
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: lambdaDims,
      }),
      threshold: 0,
      severity: "warn",
    });

    // W35: Lambda Duration p99
    // Flap resistance: 3-of-5 over 5-min periods so one cold-start-skewed p99
    // bucket does not warn-then-clear. Missing data = no invocations =
    // NOT_BREACHING (helper default).
    this.createAlarm({
      id: "W35",
      name: "lambda-auth-duration-p99",
      description: "Lambda authorizer p99 duration exceeds 1s",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Duration",
        statistic: "p99",
        period: cdk.Duration.minutes(5),
        dimensionsMap: lambdaDims,
      }),
      threshold: 1000,
      evaluationPeriods: 5,
      datapointsToAlarm: 3,
      severity: "warn",
    });

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
    this.createAlarm({
      id: "W36",
      name: "ecs-backend-cpu",
      description: "Backend service CPU utilization exceeds 80% for 15 min",
      metric: cpuPct,
      threshold: 80,
      evaluationPeriods: 3,
      severity: "warn",
    });

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
    this.createAlarm({
      id: "W37",
      name: "ecs-backend-memory",
      description: "Backend service memory utilization exceeds 80% for 15 min",
      metric: memPct,
      threshold: 80,
      evaluationPeriods: 3,
      severity: "warn",
    });

    // W39: Fargate TaskStopped non-essential reason
    // EventBridge rule captures ECS task state changes with STOPPED status,
    // publishes a custom metric, and we alarm on that metric.
    const taskStoppedMetric = new cloudwatch.Metric({
      namespace: "LightbulbAI/ECS",
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
      ruleName: `lightbulb-${this.envName}-ecs-task-stopped`,
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

    // Lambda to publish a custom metric ONLY on unexpected task stops.
    //
    // Discriminates on ECS's structured `stopCode`, NOT on exit codes. The
    // previous version checked exit codes FIRST and returned "unexpected" on
    // any non-zero exit, before it ever looked at the reason. A rolling deploy
    // SIGTERMs the container, which exits 143 — so W39 fired on EVERY deploy
    // (confirmed: the backend task stopped 3 min after the 2026-08-14 deploy
    // with stopCode=ServiceSchedulerInitiated, reason="Scaling activity
    // initiated by (deployment ...)", exitCode=143). The reason filter that
    // would have caught it was unreachable.
    //
    // A crashing container is reported as EssentialContainerExited, so the
    // exit code carries no signal the stopCode does not already carry.
    // SpotInterruption is routine — the whole fleet runs Fargate Spot, and a
    // reclaim self-heals; a genuine storm still trips W58's crash-loop count.
    // ServiceSchedulerInitiated is the ambiguous one: the scheduler both rolls
    // deploys AND replaces tasks that fail health checks, so it is split on
    // the reason text. An unknown stopCode alarms — fail loud.
    const taskStoppedFn = new lambda.Function(this, "TaskStoppedFn", {
      functionName: `${resourcePrefix(this.envName)}task-stopped-metric`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(10),
      code: lambda.Code.fromInline(`
import boto3
cw = boto3.client('cloudwatch')

# stopCodes that are routine lifecycle, never a failure:
#   UserInitiated     - operator or per-user container teardown
#   SpotInterruption  - Fargate Spot reclaim; the fleet runs Spot, it self-heals
EXPECTED_STOP_CODES = ('UserInitiated', 'SpotInterruption')

# Substrings (lowercased) of stoppedReason that mark a ServiceSchedulerInitiated
# stop as a ROUTINE rollout rather than the scheduler replacing a sick task.
ROLLOUT_REASON_SUBSTRINGS = ('deployment', 'scaling activity')

def _is_unexpected(detail):
    # Exit codes are deliberately NOT consulted. A rolling deploy SIGTERMs the
    # container (exit 143); treating that as a crash made W39 fire on every
    # deploy. A real crash arrives as EssentialContainerExited instead.
    stop_code = detail.get('stopCode') or ''

    if stop_code in EXPECTED_STOP_CODES:
        return False

    if stop_code == 'ServiceSchedulerInitiated':
        # The scheduler both rolls deploys and replaces tasks that fail health
        # checks. Only the rollout is routine.
        reason = (detail.get('stoppedReason') or '').lower()
        return not any(s in reason for s in ROLLOUT_REASON_SUBSTRINGS)

    # EssentialContainerExited, TaskFailedToStart, or an unrecognised code.
    return True

def handler(event, context):
    detail = event.get('detail', {}) or {}
    if not _is_unexpected(detail):
        return {'statusCode': 200, 'emitted': False}
    cluster = detail.get('clusterArn', '').split('/')[-1]
    cw.put_metric_data(
        Namespace='LightbulbAI/ECS',
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

    this.createAlarm({
      id: "W39",
      name: "fargate-task-stopped",
      description: "Fargate task stopped unexpectedly",
      metric: taskStoppedMetric,
      threshold: 0,
      severity: "warn",
    });

    // W58: per-user container crash-loop. W39 fires on ANY single unexpected
    // stop; a crash-LOOP (CPU-starve → healthcheck SIGKILL → restart → repeat)
    // produces a burst of unexpected stops. Reuses the same TaskStoppedUnexpected
    // metric the free TaskStoppedFn Lambda already emits — no new emission.
    // Sum >= 3 in 5min distinguishes a loop from a one-off. Provisional
    // threshold, baseline after observation.
    const crashLoopMetric = new cloudwatch.Metric({
      namespace: "LightbulbAI/ECS",
      metricName: "TaskStoppedUnexpected",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ClusterName: clusterName },
    });
    this.createAlarm({
      id: "W58",
      name: "per-user-crash-loop",
      description: ">=3 unexpected container stops in 5 min - a per-user container is in a crash/restart loop (likely CPU-starve healthcheck SIGKILL). Provisional threshold.",
      metric: crashLoopMetric,
      threshold: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
    });

    // W59: EFS >1-task-per-access-point detector. Two RUNNING tasks on one
    // per-user service write the same EFS access point → OpenClaw WAL-SQLite
    // corruption (root cause mitigated by maxPercent=100 single-writer deploys;
    // this detects a REGRESSION). Basic AWS/ECS has no RunningTaskCount (that's
    // Container Insights, which we keep off for cost), so a tiny EventBridge→
    // Lambda checks running-task count on each RUNNING transition. Reuses the
    // free-Lambda pattern; events are low-volume.
    const multiTaskRule = new events.Rule(this, "TaskRunningRule", {
      ruleName: `lightbulb-${this.envName}-ecs-task-running`,
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
      functionName: `${resourcePrefix(this.envName)}multi-task-metric`,
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
        cw.put_metric_data(Namespace='LightbulbAI/ECS', MetricData=[{
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
        conditions: { StringEquals: { "cloudwatch:namespace": "LightbulbAI/ECS" } },
      }),
    );
    multiTaskRule.addTarget(new events_targets.LambdaFunction(multiTaskFn));

    const multiTaskMetric = new cloudwatch.Metric({
      namespace: "LightbulbAI/ECS",
      metricName: "MultiTaskPerService",
      statistic: "Sum",
      period: cdk.Duration.minutes(1),
      dimensionsMap: { ClusterName: clusterName },
    });
    this.createAlarm({
      id: "W59",
      name: "efs-multi-task-per-ap",
      description: "A per-user service has >1 RUNNING task - two writers on one EFS access point, the OpenClaw WAL-SQLite corruption precondition. Single-writer deploy (maxPercent=100) should prevent this; firing = regression.",
      metric: multiTaskMetric,
      threshold: 0,
      severity: "warn",
    });

    // -- DynamoDB (W40-W42) --
    // Create alarms for each table
    const allTables: [string, string][] = [
      ["users", props.databaseTables.usersTable],
      ["containers", props.databaseTables.containersTable],
      ["billing", props.databaseTables.billingTable],
      ["usage-counters", props.databaseTables.usageCountersTable],
      ["pending-updates", props.databaseTables.pendingUpdatesTable],
      ["channel-links", props.databaseTables.channelLinksTable],
      ["company-slugs", props.databaseTables.companySlugsTable],
      ["agent-email-addresses", props.databaseTables.agentEmailAddressesTable],
    ];

    // W40: ConsumedReadCapacityUnits anomaly (on-demand tables)
    // For on-demand tables we alarm when read consumption spikes
    for (const [shortName, table] of allTables) {
      const w40 = new cloudwatch.Alarm(
        this,
        `W40-${shortName}`,
        {
          alarmName: `${resourcePrefix(this.envName)}W40-ddb-read-${shortName}`,
          alarmDescription: `DynamoDB ${shortName} read capacity anomaly`,
          metric: new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ConsumedReadCapacityUnits",
            statistic: "Sum",
            period: cdk.Duration.minutes(5),
            dimensionsMap: { TableName: table },
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
          alarmName: `${resourcePrefix(this.envName)}W41-ddb-write-${shortName}`,
          alarmDescription: `DynamoDB ${shortName} write capacity anomaly`,
          metric: new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ConsumedWriteCapacityUnits",
            statistic: "Sum",
            period: cdk.Duration.minutes(5),
            dimensionsMap: { TableName: table },
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
          alarmName: `${resourcePrefix(this.envName)}W42-ddb-errors-${shortName}`,
          alarmDescription: `DynamoDB ${shortName} system errors > 0`,
          metric: new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "SystemErrors",
            statistic: "Sum",
            period: cdk.Duration.minutes(1),
            dimensionsMap: { TableName: table },
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
    this.createAlarm({
      id: "W43",
      name: "efs-io-limit",
      description: "EFS PercentIOLimit exceeds 80%",
      metric: new cloudwatch.Metric({
        namespace: "AWS/EFS",
        metricName: "PercentIOLimit",
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { FileSystemId: efsId },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      severity: "warn",
    });

    // W44: EFS BurstCreditBalance low
    const w44 = new cloudwatch.Alarm(this, "W44", {
      alarmName: `${resourcePrefix(this.envName)}W44-efs-burst-credits`,
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
    this.createAlarm({
      id: "W45",
      name: "efs-client-connections",
      description: "EFS client connections dropped to zero",
      metric: new cloudwatch.Metric({
        namespace: "AWS/EFS",
        metricName: "ClientConnections",
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { FileSystemId: efsId },
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
    });

    // -- Bedrock (W46-W47) --
    //
    // ENV-BLIND, so PROD-GATED. These read AWS/Bedrock dimensionless — the
    // metric is account-global and AWS/Bedrock exposes no env-bearing dimension
    // (dev and prod share one account/region), so the alarm cannot be scoped to
    // a single environment. It CAN, however, be created only once: dev and prod
    // both deploy this stack, so an ungated alarm renders twice (lightbulb-dev-*
    // and lightbulb-prod-*), both watching the SAME account-global number and
    // both emailing admin@ — a duplicate for every Bedrock blip. The prod copy
    // already covers account-global throttles/errors (dev-origin included), so
    // the dev copy is pure duplicate noise. Same reasoning applies to the
    // account-global Bedrock cost alarms W50/W52 (createCostAlarms).
    if (this.envName === "prod") {
      // W46: Bedrock ModelInvocationThrottles
      this.createAlarm({
        id: "W46",
        name: "bedrock-throttles",
        description: "Bedrock model invocation throttles > 0",
        metric: new cloudwatch.Metric({
          namespace: "AWS/Bedrock",
          metricName: "InvocationThrottles",
          statistic: "Sum",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 0,
        severity: "warn",
      });

      // W47: Bedrock InvocationClientErrors
      this.createAlarm({
        id: "W47",
        name: "bedrock-client-errors",
        description: "Bedrock invocation client errors exceed 5 per min",
        metric: new cloudwatch.Metric({
          namespace: "AWS/Bedrock",
          metricName: "InvocationClientErrors",
          statistic: "Sum",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 5,
        severity: "warn",
      });
    }

    // -- Network (W48) --

    // W48: NLB / Cloud Map healthy host count drop
    // Monitor ALB healthy host count as proxy for backend availability
    this.createAlarm({
      id: "W48",
      name: "alb-healthy-hosts-drop",
      description: "ALB healthy host count dropped to zero",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "HealthyHostCount",
        statistic: "Minimum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { LoadBalancer: albFullName, TargetGroup: tgFullName },
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      severity: "warn",
    });
  }

  // =========================================================================
  // Paperclip tier alarms (W60, W61, AuroraFailoverRule, W62, W65)
  // =========================================================================

  private createPaperclipTierAlarms(props: ObservabilityStackProps): void {
    const dbId = props.paperclipDbCluster.clusterIdentifier;
    // W60: Aurora CPU
    this.createAlarm({
      id: "W60",
      name: "aurora-cpu",
      description: "Paperclip Aurora CPU >80% for 15 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS",
        metricName: "CPUUtilization",
        statistic: "Average",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { DBClusterIdentifier: dbId },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      severity: "warn",
    });

    // W61: Aurora connections — SATURATION only (high watermark). Provisional.
    this.createAlarm({
      id: "W61",
      name: "aurora-connections",
      description: "Paperclip Aurora DatabaseConnections >100 for 15 min - connection saturation. Provisional threshold.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS",
        metricName: "DatabaseConnections",
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { DBClusterIdentifier: dbId },
      }),
      threshold: 100,
      evaluationPeriods: 3,
      severity: "warn",
    });

    // W61b: Aurora OUTAGE / FAILOVER — distinct from saturation. W60/W61 (CPU,
    // high-connections, NOT_BREACHING) do NOT fire when a failover/outage drops
    // connections toward zero or stops reporting. RDS publishes failover +
    // failure events to EventBridge; route them straight to the warn topic so
    // the 3C Aurora-failover scenario has a recovery signal. (Event categories:
    // "failover" + "failure"; covers RDS-EVENT-0031/0071/0036 etc.)
    const auroraEventRule = new events.Rule(this, "AuroraFailoverRule", {
      ruleName: `lightbulb-${this.envName}-aurora-failover`,
      description: "Paperclip Aurora failover/failure events -> warn",
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
    this.createAlarm({
      id: "W62",
      name: "paperclip-service-cpu",
      description: "Paperclip Fargate service CPU >80% for 15 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ECS",
        metricName: "CPUUtilization",
        statistic: "Average",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ClusterName: clusterName, ServiceName: pcServiceName },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      severity: "warn",
    });

    // W65: Paperclip service has NO running task — the AVAILABILITY half of
    // "is Paperclip up?". W63 (paperclip.upstream.error) is the IMPACT half: it
    // only fires when the backend actually fails a call, so a Paperclip outage
    // during a window with no Teams traffic emits nothing and W63 stays OK.
    // Observed live: the 2026-06-19 FIS S5 (paperclip-kill) run killed the task
    // for ~2 min and W63 never fired — no concurrent traffic to fail. W65 closes
    // exactly that gap; the two are complementary, not redundant.
    //
    // Metric choice — READ BEFORE CHANGING. `LiveTaskCount` is a real AWS/ECS
    // metric, dimensioned {ClusterName, ServiceName} exactly like W62's
    // CPUUtilization, published once a minute whether or not anyone calls
    // Paperclip. It is NOT `RunningTaskCount`, which lives in
    // ECS/ContainerInsights — deliberately OFF on this cluster (see
    // container-stack.ts) because Container Insights bills ~15 metrics per
    // service and this cluster carries the whole per-user fleet. Verified
    // against the live account: AWS/ECS publishes exactly CPUUtilization,
    // MemoryUtilization, LiveTaskCount, and both paperclip services report it.
    //
    // treatMissingData BREACHING is LOAD-BEARING, not defensive boilerplate.
    // ECS does not publish LiveTaskCount=0 when a service drops to zero tasks —
    // it STOPS PUBLISHING (verified on a scaled-to-zero per-user service: the
    // datapoints simply end). Absence IS the outage, so NOT_BREACHING would
    // rebuild the exact quiet-window blind spot this alarm exists to close.
    // The `< 1` comparison therefore almost never trips on a live datapoint;
    // it is the honest statement of "below the desiredCount floor of 1"
    // (paperclip-stack.ts) and covers the case where ECS does report a 0.
    //
    // 3 × 1 min: PaperclipStack sets minHealthyPercent=100, so a healthy rolling
    // deploy never drops to zero tasks and cannot trip this. Three consecutive
    // minutes rides out a single CloudWatch delivery gap while still catching a
    // silently-dead container long before a user hits Teams.
    this.createAlarm({
      id: "W65",
      name: "paperclip-no-running-task",
      description:
        "Paperclip Fargate service has no running task for 3 min - the BFF is gone or crash-looping. Native AWS/ECS availability signal, published regardless of traffic; complements W63, which only fires when a real backend call fails. Missing data = breaching: ECS stops publishing LiveTaskCount at zero tasks rather than reporting 0.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ECS",
        metricName: "LiveTaskCount",
        statistic: "Minimum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { ClusterName: clusterName, ServiceName: pcServiceName },
      }),
      threshold: 1,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      severity: "warn",
    });

    // W66: onboarding-bootstrap-failed
    //
    // The activation funnel had NO alarm at all until this one. Four prod
    // owners signed up, submitted a goal, and never got an agent response;
    // nothing anywhere emitted a metric for it, so the only detection was an
    // owner complaining. `bootstrap.state{status:"failed"}` is written by
    // PaperclipRepo.mark_bootstrap_failed on the conditional write that lands
    // the terminal transition — one datapoint per owner permanently stuck.
    //
    // Uses the status rollup (see statusSum) rather than a literal dimension
    // map so the alarm binds the [env, service, status] set the emitter
    // actually publishes.
    //
    // Threshold 0 / GREATER_THAN over 15 min: ONE wedged owner is worth
    // looking at — this is not a rate to be tuned, it is a state nobody
    // should be in. Warn, not page: the owner is already stuck by the time it
    // fires, so it wants a human within the hour, not out of bed. The bounded
    // in-dispatch retries and the /readyz gate mean a routine boot race no
    // longer reaches `failed`, so this should be quiet in steady state.
    this.createAlarm({
      id: "W66",
      name: "onboarding-bootstrap-failed",
      description:
        "Friday's first-run kickoff hit a terminal failure for at least one owner in 15 min - they submitted a goal and will never get an agent response without a bootstrap reset. Check bootstrap_last_error on the paperclip-companies row.",
      metric: this.statusSum({
        metricName: "bootstrap.state",
        status: "failed",
        periodMinutes: 15,
        label: "Wedged onboarding kickoffs",
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
    });
  }

  // =========================================================================
  // Cost alarms (W49a, W49b, W50, W51)
  // =========================================================================

  private createCostAlarms(props: ObservabilityStackProps): void {
    // W49a + W49b: AWS Budget — 80% warn + 100% page
    //
    // $500 was far below the actual run rate, so the budget sat permanently
    // breached and both notifications were pure noise. Observed account spend:
    // Apr $808, May $2,432, Jun $2,643, Jul $1,654, Aug tracking ~$900.
    //
    // $1,500 is a deliberately TIGHT leash for launch — roughly the current
    // run rate, below the June peak — so it is expected to trip if spend
    // climbs back toward historical highs. That is the intent: an early
    // tripwire while launch traffic is unknown, not a ceiling. Raise it once
    // post-launch spend settles, rather than letting it sit breached again.
    // DO NOT set an explicit budgetName here. `notificationsWithSubscribers`
    // is an update-requires-REPLACEMENT property, so any change to the alert
    // addresses makes CloudFormation create a new budget BEFORE deleting the
    // old one. With a fixed name the two collide and the deploy dies with:
    //
    //   A budget or resource with the same name but a different internalId
    //   already exists (Service: Budgets, Status Code: 400)
    //
    // which is exactly what happened repointing these from oncall@/alerts@ to
    // admin@. Letting CloudFormation generate the physical name costs a less
    // readable string in the Budgets console and buys a resource that can
    // actually be updated. The budget is identified by the alarm/notification
    // it produces, not by its console name.
    new budgets.CfnBudget(this, "MonthlyBudget", {
      budget: {
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: { amount: 1500, unit: "USD" },
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
          // Budgets email the address directly — no SNS, so no confirmation
          // click and none of the silent-expiry problem the topics had. It
          // still has to be a mailbox somebody reads.
          subscribers: [
            {
              subscriptionType: "EMAIL",
              address: this.alertEmail,
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
              address: this.alertEmail,
            },
          ],
        },
      ],
    });

    // W50: Bedrock spend anomaly — ENV-BLIND, so PROD-GATED (see W46/W47).
    // AWS/Bedrock Invocations is account-global with no env dimension; dev and
    // prod both deploy this stack, so an ungated alarm double-fires on the same
    // number. The prod copy covers account-global invocations (dev included).
    // Use CloudWatch alarm on AWS/Bedrock InvocationCount as a proxy for cost.
    if (this.envName === "prod") {
      const w50 = new cloudwatch.Alarm(this, "W50", {
        alarmName: `${resourcePrefix(this.envName)}W50-bedrock-spend-anomaly`,
        alarmDescription:
          "Bedrock invocation count spike - potential cost anomaly",
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
    }

    // W51: NAT Gateway data transfer anomaly
    const w51 = new cloudwatch.Alarm(this, "W51", {
      alarmName: `${resourcePrefix(this.envName)}W51-nat-gateway-data-transfer`,
      alarmDescription:
        "NAT Gateway data transfer spike - potential cost anomaly",
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

    // W52: Bedrock cache-write token volume — runaway-inference guard.
    // ENV-BLIND, so PROD-GATED (see W46/W47). AWS/Bedrock CacheWriteInputTokenCount
    // is account-global with no env dimension. Sums across the whole account over
    // a 1-day window. This is the metric a runaway agent loop blows through (the
    // 2026 incident hit ~17-21M cache-write tokens/day, ~$130+/day, silent for
    // weeks). Pages so it can't go unnoticed again. Threshold lives in
    // BEDROCK_DAILY_CACHE_WRITE_TOKEN_THRESHOLD — tune as the fleet grows.
    // Only the prod copy is created (with PagerDuty on the prod page topic); it
    // still catches an account-global runaway regardless of which env caused it,
    // so gating loses no coverage while removing the dev duplicate.
    if (this.envName === "prod") {
      const w52 = new cloudwatch.Alarm(this, "W52", {
        alarmName: `${resourcePrefix(this.envName)}W52-bedrock-cache-write-token-volume`,
        alarmDescription:
          "Bedrock cache-write input tokens exceeded the daily ceiling - " +
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
    }

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
  // Uptime checks (P13, P14)
  // =========================================================================

  /**
   * Outside-in uptime checks on the two public front doors.
   *
   * Every other alarm in this stack is an INTERNAL signal — a metric the
   * backend emits about itself, or an AWS metric about our own resources.
   * All of them can look perfectly healthy while a customer cannot reach the
   * product at all: an expired certificate, a broken Vercel deploy, a DNS
   * mistake, a dead Clerk dependency, or a WAF rule that blackholes the world
   * are invisible from the inside. These two checks are the only thing here
   * that asks the question a user asks.
   *
   * Route53 health checks rather than CloudWatch Synthetics on purpose. A
   * Synthetics canary needs an artifacts bucket, an execution role and a
   * bundled script to answer "does this URL return 200"; a Route53 health
   * check is a config block that probes from multiple AWS regions and
   * publishes HealthCheckStatus natively. Reach for Synthetics when we want
   * the real signup->ready journey in a browser, which is deliberately NOT
   * this — that belongs after launch, and it is a different alarm.
   *
   * Note the metric only exists in us-east-1 regardless of where the check
   * runs, which is where this stack already lives.
   */
  private createUptimeChecks(): void {
    const isProd = this.envName === "prod";

    // Mirrors api-stack (`api.{root}` / `api-{env}.{root}`) and lightbulb-stage
    // (`{root}` / `dev.{root}`).
    const root = this.rootDomain;
    const appHost = isProd ? root : `${this.envName}.${root}`;
    const apiHost = isProd ? `api.${root}` : `api-${this.envName}.${root}`;

    const targets = [
      {
        id: "P13",
        name: "app-unreachable",
        host: appHost,
        path: "/",
        description:
          "The marketing/app front door is failing its health check from " +
          "multiple AWS regions. Users cannot load the product.",
      },
      {
        id: "P14",
        name: "api-unreachable",
        host: apiHost,
        // Unauthenticated 200 {"status":"healthy"} — the only public path
        // that answers without a Clerk token.
        path: "/health",
        description:
          "The backend API is failing its health check from multiple AWS " +
          "regions. Chat, billing and provisioning are all down.",
      },
    ];

    for (const target of targets) {
      const check = new route53.CfnHealthCheck(this, `${target.id}HealthCheck`, {
        healthCheckConfig: {
          type: "HTTPS",
          fullyQualifiedDomainName: target.host,
          resourcePath: target.path,
          port: 443,
          // Route53 probes from several regions on this interval and flips
          // the status only after failureThreshold consecutive failures, so
          // a single blip or one bad region cannot page.
          requestInterval: 30,
          failureThreshold: 3,
          enableSni: true,
          measureLatency: false,
        },
        healthCheckTags: [
          { key: "Name", value: `lightbulb-${this.envName}-${target.name}` },
        ],
      });

      const metric = new cloudwatch.Metric({
        namespace: "AWS/Route53",
        metricName: "HealthCheckStatus",
        // Minimum, not Average: if ANY probing region says down, treat it as
        // down rather than letting healthy regions average the outage away.
        statistic: "Minimum",
        period: cdk.Duration.minutes(1),
        dimensionsMap: { HealthCheckId: check.attrHealthCheckId },
      });

      const alarm = new cloudwatch.Alarm(this, target.id, {
        alarmName: `${resourcePrefix(this.envName)}${target.id}-${target.name}`,
        alarmDescription: target.description,
        metric,
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        // ~90s of probe failures before the status flips, then 2 minutes of
        // that status before we page.
        evaluationPeriods: 2,
        // Route53 publishes this every minute unconditionally. Missing data
        // means the check itself is gone or not reporting, which we cannot
        // distinguish from an outage and must not silently treat as healthy.
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      });

      // Dev going dark should not wake anyone; prod should.
      //
      // No pageAlarmArns registration: that collection existed only to feed
      // FIS experiment stop-conditions, and FIS was removed. An outside-in
      // uptime check would be a poor stop-condition anyway — it measures the
      // public front door rather than the fault being injected.
      alarm.addAlarmAction(
        new cloudwatch_actions.SnsAction(isProd ? this.pageTopic : this.warnTopic),
      );
    }
  }

  // =========================================================================
  // Dashboard (~30 widgets)
  // =========================================================================

  private createDashboard(props: ObservabilityStackProps): void {
    const dims = { env: this.envName, service: "lightbulb-backend" };
    const albFullName = props.alb.loadBalancerFullName;

    const dashboard = new cloudwatch.Dashboard(this, "OrrDashboard", {
      dashboardName: `lightbulb-${this.envName}-orr`,
    });

    // ----- Row 1: SLOs (2 widgets) -----
    const chatSuccessRate = new cloudwatch.MathExpression({
      expression: "100 * (1 - errors / IF(total > 0, total, 1))",
      usingMetrics: {
        errors: new cloudwatch.Metric({
          namespace: "LightbulbAI",
          metricName: "chat.error",
          statistic: "Sum",
          dimensionsMap: dims,
        }),
        total: new cloudwatch.Metric({
          namespace: "LightbulbAI",
          metricName: "chat.message.count",
          statistic: "Sum",
          dimensionsMap: dims,
        }),
      },
      period: cdk.Duration.hours(1),
    });

    const chatP99Latency = new cloudwatch.Metric({
      namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
            metricName: "container.provision",
            statistic: "Sum",
            dimensionsMap: { ...dims, status: "ok" },
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
            metricName: "chat.message.count",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
            metricName: "chat.e2e.latency",
            statistic: "p50",
            dimensionsMap: dims,
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
            metricName: "gateway.cold_start.count",
            statistic: "Sum",
            dimensionsMap: { ...dims, outcome: "ok" },
            label: "cold starts (ok)",
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
            metricName: "gateway.cold_start.count",
            statistic: "Sum",
            dimensionsMap: { ...dims, outcome: "error" },
            label: "cold starts (error)",
          }),
        ],
        right: [
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
            metricName: "gateway.cold_start.latency",
            statistic: "p50",
            dimensionsMap: dims,
            label: "p50 latency",
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
            metricName: "channel.rpc",
            statistic: "Sum",
            dimensionsMap: { ...dims, status: "ok" },
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
            metricName: "stripe.webhook.received",
            statistic: "Sum",
            dimensionsMap: dims,
          }),
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
            namespace: "LightbulbAI",
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
        // WS APIs publish ClientError/ExecutionError/IntegrationError — NOT the
        // REST-API 5XXError/4XXError this widget used to plot (a permanently
        // empty graph, same nonexistent metrics removed from P10/W30).
        title: "API GW WebSocket errors",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/ApiGateway",
            metricName: "ClientError",
            statistic: "Sum",
            dimensionsMap: { ApiId: props.wsApiId },
          }),
          new cloudwatch.Metric({
            namespace: "AWS/ApiGateway",
            metricName: "ExecutionError",
            statistic: "Sum",
            dimensionsMap: { ApiId: props.wsApiId },
          }),
          new cloudwatch.Metric({
            namespace: "AWS/ApiGateway",
            metricName: "IntegrationError",
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
              TableName: props.databaseTables.usersTable,
            },
            label: "users",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.containersTable,
            },
            label: "containers",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.billingTable,
            },
            label: "billing",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/DynamoDB",
            metricName: "ThrottledRequests",
            statistic: "Sum",
            dimensionsMap: {
              TableName: props.databaseTables.usageCountersTable,
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
            namespace: "LightbulbAI",
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
      ruleName: `lightbulb-${this.envName}-guardduty-findings`,
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
        ruleName: `lightbulb-${this.envName}-access-analyzer-findings`,
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
