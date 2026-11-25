import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import { logGroupPath, resourcePrefix } from "../table-names";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudwatch_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
// GuardDuty + Access Analyzer already enabled at account level — not managed by CDK
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as events from "aws-cdk-lib/aws-events";
import * as events_targets from "aws-cdk-lib/aws-events-targets";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../domains";

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
  // Container stack references
  cluster: ecs.ICluster;
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
    sparkTranscriptsTable: string;
  };
}

/**
 * The one table set P8, W40, W41 and W42 cover.
 *
 * One list, one place to add a table. Short names are kebab because they land in alarm names; P8 underscores them
 * for metric-math ids.
 */
function alarmedTables(
  tables: ObservabilityStackProps["databaseTables"],
): [string, string][] {
  return [
    ["users", tables.usersTable],
    ["containers", tables.containersTable],
    ["spark-transcripts", tables.sparkTranscriptsTable],
  ];
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
      displayName: "Lightbulb Page",
    });

    // Warn tier.
    this.warnTopic = new sns.Topic(this, "AlertsWarn", {
      topicName: `${resourcePrefix(props.envName)}alerts-warn`,
      displayName: "Lightbulb Warn",
    });

    // A CloudWatch alarm action publishes as the CloudWatch SERVICE principal,
    // not as this stack's deploy role, so it needs an explicit resource-policy
    // grant. That is not belt-and-braces: attaching an EventBridge rule to a
    // topic (the GuardDuty rule further down targets the WARN
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
    //
    // ⚠️ RECREATED AGAIN 2026-10-27 (the `…V3` ids). CloudTrail showed no
    // Unsubscribe after 09-12: SNS silently deleted all three V2 subscriptions
    // (both emails AND PagerDuty) because none was ever confirmed, and every
    // page from then on reached only a hand-added admin@isol8.co mailbox nobody
    // reads. That masked a dry OpenRouter account for 3 days. After a deploy,
    // run `aws sns list-subscriptions-by-topic` within minutes: a PagerDuty row
    // still PendingConfirmation means the integration URL in SSM is broken.
    //
    // ⚠️ RECREATED 2026-09-12 (the `…V2` ids). The prod subscriptions CFN
    // believed it owned were gone: SNS deleted all three after the three-day
    // unconfirmed window, and CloudFormation still reported them
    // CREATE_COMPLETE — so no ordinary deploy would ever put them back. New
    // construct ids are the ONLY way to make CDK create fresh ones.
    //
    // ⚠️ THE DEPLOY IS HALF THE JOB. A human must open the confirmation mail
    // at the address below and click the link WITHIN 3 DAYS of the deploy, or
    // SNS deletes these subscriptions again and both tiers go silent again.
    // The `SubscriptionsConfirmed` monitor further down is what tells you it
    // happened (or didn't) instead of leaving it to be noticed weeks later.
    //
    // sns.Subscription directly, not `addSubscription(new EmailSubscription)`:
    // that helper derives the construct id from the address, so the id cannot
    // be bumped without also changing where the mail goes.
    const alertEmail = this.alertEmail;

    new sns.Subscription(this, "AlertsPageEmailV3", {
      topic: this.pageTopic,
      endpoint: alertEmail,
      protocol: sns.SubscriptionProtocol.EMAIL,
    });
    new sns.Subscription(this, "AlertsWarnEmailV3", {
      topic: this.warnTopic,
      endpoint: alertEmail,
      protocol: sns.SubscriptionProtocol.EMAIL,
    });

    // NOTE: there is deliberately no SMS subscription. SNS cannot send SMS
    // from this account — it has no registered US origination identity and
    // sits in the SMS sandbox, so `CreateSMSSandboxPhoneNumber` fails with
    // "No origination entities available to send". Adding an SMS subscription
    // here would deploy clean and silently never deliver. Wire it only once a
    // toll-free number is registered AND the account has left the sandbox.
    //
    // PagerDuty covers that gap instead: it turns a page into a phone push
    // with escalation, needs no carrier registration, and — unlike the email
    // subscriptions above — needs no click: PagerDuty answers SNS's confirmation
    // callback itself. It still rots if that callback never succeeds — the V2
    // subscription sat unconfirmed and SNS deleted it. Check it after a deploy.
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
      //
      // `…V2` for the same reason as the email subscriptions above: the
      // original was deleted out from under a CloudFormation record that
      // still says CREATE_COMPLETE, and only a new construct id recreates it.
      new sns.Subscription(this, "PagerDutyPageV3", {
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

    this.createSubscriptionMonitor();

    // -----------------------------------------------------------------------
    // Alarms
    // -----------------------------------------------------------------------
    this.createPageAlarms(props);
    this.createWarnCustomMetricAlarms();
    this.createWarnAwsNativeAlarms(props);
    this.createCostAlarms(props);
    this.createUptimeChecks();

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
   * constructors) AND the alarms in service and agent-mail-outbound, which import these topics by ARN. That shared helper
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

  /**
   * Watch the alert topics' own subscriptions, because nothing else does.
   *
   * SNS deletes an email subscription nobody confirms within three days, and
   * CloudFormation keeps reporting CREATE_COMPLETE against the corpse — that
   * is how both tiers ran with ZERO destinations while ~105 alarms fired into
   * the void and a real prod alarm sat unnoticed for four weeks. There is no
   * AWS-native metric for "this topic has a live subscriber", so poll for it.
   *
   * An hourly Lambda counts subscriptions whose `SubscriptionArn` is not the
   * literal `PendingConfirmation` and emits the count per tier via EMF. The
   * alarm is BREACHING on missing data on purpose: the monitor not running is
   * indistinguishable from the destinations being gone, and both mean nobody
   * is being paged. Both alarms page — a warn-tier alarm about a dead warn
   * topic would deliver to the topic it is reporting on.
   */
  private createSubscriptionMonitor(): void {
    const topics: { tier: "page" | "warn"; topic: sns.Topic }[] = [
      { tier: "page", topic: this.pageTopic },
      { tier: "warn", topic: this.warnTopic },
    ];

    const monitor = new lambda.Function(this, "AlertSubscriptionMonitor", {
      functionName: `${resourcePrefix(this.envName)}alert-subscription-monitor`,
      runtime: lambda.Runtime.PYTHON_3_13,
      logGroup: this.functionLogGroup("AlertSubscriptionMonitorLogs", "alert-subscription-monitor"),
      handler: "index.handler",
      timeout: cdk.Duration.seconds(30),
      environment: {
        // The metric is per env: with only a topic dimension, dev's one-subscriber
        // page topic and prod's two shared a series, so prod's P17 read
        // Minimum = 1 < 2 and sat in ALARM for months.
        ENV_NAME: this.envName,
        PAGE_TOPIC_ARN: this.pageTopic.topicArn,
        WARN_TOPIC_ARN: this.warnTopic.topicArn,
      },
      code: lambda.Code.fromInline(`
import boto3
import json
import os
import time

sns = boto3.client("sns")


def handler(event, context):
    del event, context
    paginator = sns.get_paginator("list_subscriptions_by_topic")
    for tier in ("page", "warn"):
        arn = os.environ[tier.upper() + "_TOPIC_ARN"]
        confirmed = 0
        for page in paginator.paginate(TopicArn=arn):
            for sub in page.get("Subscriptions", []):
                # SNS reports an unconfirmed subscription with this literal in
                # place of an ARN. It is not a destination until it is gone.
                if sub.get("SubscriptionArn") != "PendingConfirmation":
                    confirmed += 1
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "LightbulbAI/Alerting",
                    "Dimensions": [["env", "topic"]],
                    "Metrics": [{"Name": "SubscriptionsConfirmed", "Unit": "Count"}],
                }],
            },
            "env": os.environ["ENV_NAME"],
            "topic": tier,
            "SubscriptionsConfirmed": confirmed,
        }))
`),
    });
    monitor.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["sns:ListSubscriptionsByTopic"],
        resources: topics.map((t) => t.topic.topicArn),
      }),
    );
    new events.Rule(this, "AlertSubscriptionMonitorSchedule", {
      schedule: events.Schedule.rate(cdk.Duration.hours(1)),
      targets: [new events_targets.LambdaFunction(monitor)],
    });

    for (const { tier } of topics) {
      // Prod's page tier alone has two destinations (the alert mailbox AND
      // PagerDuty); every other tier/env combination has just the mailbox.
      const expected = this.envName === "prod" && tier === "page" ? 2 : 1;
      this.createAlarm({
        id: tier === "page" ? "P17" : "P18",
        name: `alert-subscriptions-${tier}-topic`,
        description:
          `PAGE: the ${tier} topic has fewer than ${expected} CONFIRMED ` +
          "subscriptions. Nobody is receiving its alarms. Usually means the " +
          "confirmation mail was never clicked and SNS deleted the " +
          "subscription after three days.",
        namespace: "LightbulbAI/Alerting",
        metricName: "SubscriptionsConfirmed",
        statistic: "Minimum",
        dimensions: { env: this.envName, topic: tier },
        threshold: expected,
        periodMinutes: 60,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        severity: "page",
      });
    }
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
   * An owned, expiring log group for one of this stack's Lambdas. Without one,
   * Lambda auto-creates `/aws/lambda/<fn>` with no retention. A new path, not
   * that one: the auto-created group already exists and CloudFormation cannot
   * adopt it. The old group stops receiving logs and can be deleted by hand.
   */
  private functionLogGroup(id: string, suffix: string): logs.LogGroup {
    return new logs.LogGroup(this, id, {
      logGroupName: logGroupPath(this.envName, suffix),
      retention:
        this.envName === "prod" ? logs.RetentionDays.THREE_MONTHS : logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
  }

  // =========================================================================
  // Page-tier alarms
  // =========================================================================

  private createPageAlarms(props: ObservabilityStackProps): void {
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

    // P11 (marketplace-path-traversal) DELETED: its emitter went with the
    // Marketplace (#4975), so it could never fire.

    // P8: dynamodb-throttle-sustained (AWS-native metric math)
    // Repointed off the never-emitted custom `Isol8/dynamodb.throttle` to the
    // AWS-native `AWS/DynamoDB ThrottledRequests` metric so this is a real page
    // without any backend change. ThrottledRequests is per-table, so we sum
    // across the same table set W40/W41/W42 enumerate (account-wide math sum is
    // the acceptable fallback when a table set isn't available).
    const ddbThrottlePerTable: Record<string, cloudwatch.IMetric> = {};
    for (const [shortName, table] of alarmedTables(props.databaseTables)) {
      // Metric-math ids are [a-z][a-zA-Z0-9_]*, so the kebab short name has to
      // be underscored before it can be an operand.
      ddbThrottlePerTable[shortName.replace(/-/g, "_")] = new cloudwatch.Metric({
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
  }

  // =========================================================================
  // Warn-tier custom metric alarms (W1-W27)
  // =========================================================================

  private createWarnCustomMetricAlarms(): void {
    const dims = { env: this.envName, service: "lightbulb-backend" };

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

    // W78: backend-healthy-hosts (ruling D8, sub-project D machine relay).
    // The backend went from a fixed desiredCount:1 to autoscaled 2-4
    // (service-stack.ts) — P12 (gateway.running.count heartbeat) catches
    // "every task is dead" but is instance-blind, so it cannot see one of N
    // healthy tasks disappear. This alarm is the instance-aware companion:
    // Minimum HealthyHostCount on the backend target group, 2-of-2 five-minute
    // periods so a single deploy's transient dip (old task draining before the
    // new one is healthy) does not warn-then-clear.
    this.createAlarm({
      id: "W78",
      name: "backend-healthy-hosts",
      description:
        "Fewer than 2 healthy backend targets for 10 min (2 consecutive " +
        "5-min periods) - a deploy is stuck, a task is crash-looping, or " +
        "autoscaling can't keep up.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB",
        metricName: "HealthyHostCount",
        statistic: "Minimum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { LoadBalancer: albFullName, TargetGroup: tgFullName },
      }),
      threshold: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      evaluationPeriods: 2,
      datapointsToAlarm: 2,
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
      logGroup: this.functionLogGroup("TaskStoppedFnLogs", "task-stopped-metric"),
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
      description: ">=3 unexpected task stops in 5 min on the platform cluster (backend) - a crash/restart loop. Provisional threshold.",
      metric: crashLoopMetric,
      threshold: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
    });

    // -- DynamoDB (W40-W42) --
    // Create alarms for each table
    const allTables = alarmedTables(props.databaseTables);

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

    // -- Network (W48) --

    // W48: ALB healthy host count drop
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
    // ENV-BLIND, so PROD-GATED (see W46/W47 above). AWS Budgets are
    // account-wide with no per-environment scope; dev and prod both deploy
    // this stack, so an ungated budget renders twice and both copies email
    // admin@ for the same account-wide spend number.
    if (this.envName === "prod") {
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
    }

    // W90: a Clerk user.deleted whose tenant wipe failed a step. The webhook answers 503 so Svix
    // redelivers, but once Svix gives up a failed Stripe step leaves a held-back billing row and a
    // live subscription that nothing else reports (routers/webhooks.py).
    this.createAlarm({
      id: "W90",
      name: "user-deleted-wipe-failed",
      description: "A deleted person's tenant wipe failed a step (Svix redelivers; a live subscription may remain)",
      metricName: "clerk.webhook.user_deleted_wipe_failed",
      periodMinutes: 5,
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
    });

    // W91-W97: the Spark store's delete and retention jobs (core/services spark_store; put_metric, no dims).
    // Any one occurrence in 15 minutes warns.
    [
      ["forget-start-failed", "A Spark store delete could not be started"],
      ["forget-stuck", "A Spark store delete is stuck"],
      ["forget-remaining", "A Spark store delete finished with rows still remaining"],
      ["jobs-failed", "A Spark store maintenance job failed"],
      ["retention-late", "The Spark store 90-day retention run is late"],
      ["forget-disabled", "Spark store writes are on but deletes are off (misconfiguration)"],
      ["forget-error-skipped", "A Spark store error object was too large to sweep for a delete"],
    ].forEach(([name, description], i) => {
      this.createAlarm({
        id: `W${91 + i}`,
        name: `spark-store-${name}`,
        description,
        metricName: `spark_store.${name.replace(/-/g, "_")}`,
        periodMinutes: 15,
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        severity: "warn",
      });
    });

    // NOTE: AWS Cost Anomaly Detection is intentionally NOT managed here.
    // A Cost Anomaly Detection monitor is an ACCOUNT-GLOBAL resource; a
    // per-environment stack (dev + prod both deploy this stack) cannot own
    // one without colliding with the other env's monitor and with any
    // pre-existing account monitor. The runaway signal is covered by the
    // W52 token-volume alarm (above); if managed $-anomaly detection is wanted, configure it ONCE at
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
  // Account hardening
  // =========================================================================

  private createAccountHardening(
    props: ObservabilityStackProps,
  ): void {
    // GuardDuty and Access Analyzer are already enabled at the account level
    // (prod has them pre-configured). We only create EventBridge rules to
    // route their findings to our SNS warn topic.
    //
    // ENV-BLIND, so PROD-GATED (see W46/W47/W49a-b above). GuardDuty and
    // Access Analyzer findings are account-wide with no per-environment
    // scope; dev and prod both deploy this stack, so an ungated rule renders
    // twice and both copies email admin@ for the same account-wide finding.
    if (this.envName !== "prod") {
      return;
    }

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
