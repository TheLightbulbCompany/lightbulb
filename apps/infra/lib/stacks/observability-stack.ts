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
import { superAgentEnabled } from "../super-agent";

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
    companySlugsTable: string;
    agentEmailAddressesTable: string;
    /**
     * The canonical messaging store. It lives in `api-stack.ts`, not
     * `database-stack.ts` — still just a dimension string here, so the same
     * names-not-constructs rule above applies. The messaging table is now the product's hottest table
     * (every room message, member and work row) and had no throttle, capacity
     * or SystemErrors alarm at all.
     */
    messagingTable: string;
  };
  /**
   * Messaging-plane names for the dashboard's Messaging block. All owned by
   * ApiStack.
   *
   * The three names are LITERALS on the ApiStack side, not `queue.queueName` /
   * `fn.functionName` — a token consumed across stacks becomes a
   * CloudFormation Export, and an export cannot be changed while anything
   * imports it (the deadlock documented on `databaseTables` above).
   *
   * `appsyncEventApiId` is the single exception and the only import this stack
   * takes from ApiStack: AppSync assigns the id at create time, so there is no
   * literal to write. See its declaration in `api-stack.ts` for the
   * consequence (replacing the Event API needs these widgets removed first).
   */
  messaging: {
    workQueueName: string;
    workDlqName: string;
    projectorFunctionName: string;
    appsyncEventApiId: string;
  };
}

/**
 * The one table set P8, W40, W41, W42 and the Row 8 dashboard widget cover.
 *
 * It was four hand-maintained lists (two of eight tables, one of four in the
 * widget), which is why the messaging table — now the busiest table we own —
 * had no DynamoDB alarm on it at all. One list, one place to add a table.
 * Short names are kebab because they land in alarm names; P8 underscores them
 * for metric-math ids.
 */
function alarmedTables(
  tables: ObservabilityStackProps["databaseTables"],
): [string, string][] {
  return [
    ["users", tables.usersTable],
    ["containers", tables.containersTable],
    ["billing", tables.billingTable],
    ["company-slugs", tables.companySlugsTable],
    ["agent-email-addresses", tables.agentEmailAddressesTable],
    ["messaging", tables.messagingTable],
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
    this.createMessagingAlarms();
    this.createWarnCustomMetricAlarms();
    this.createWarnAwsNativeAlarms(props);
    this.createLateAlarms(props);
    this.createCostAlarms(props);
    this.createUptimeChecks();
    if (superAgentEnabled(this.envName)) {
      this.createWatchdogAlarms();
    }

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

    // P28: provision-reconcile-failed
    // hermes_provisioning.reconcile_runtime (provider switch, plan change,
    // admin door, end of every provision) never flips a RUNNING workspace to
    // error: the old runtime keeps serving and this metric is the only trace
    // that billing and the runtime now disagree. Every step is idempotent, so
    // a failure is deterministic until someone looks. The SFN side of the same
    // pair is service-stack's provision-executions-failed alarm.
    this.createAlarm({
      id: "P28",
      name: "provision-reconcile-failed",
      metricName: "provision.reconcile.failed",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "page",
      description:
        "reconcile_runtime failed - a workspace runtime no longer matches its billing (provider/plan); the old runtime is still serving",
    });

    // P26: jev-decision-error-rate
    // Jev routes ambient room traffic. An error/timeout/429 decision fails the
    // routing call (the message still lands; only the wake decision is lost), so
    // a sustained error share means agents silently stop answering room_events.
    // Hourly, with a floor of 3 decisions: the old floor (10 per 15 min) sat above prod's
    // whole traffic (12-29 decisions per HOUR), so 28 of 28 decisions failed on 2026-09-27
    // (the gateway 403'd the model) and this never left OK. More than half, not 20%: a paste
    // burst that draws a few 429s retries by itself and is not an outage.
    const jevOutcome = (outcome?: string) =>
      this.messagingMetric("messaging.jev.decision", outcome ? { outcome } : {});
    this.createAlarm({
      id: "P26",
      name: "jev-decision-error-rate",
      metric: new cloudwatch.MathExpression({
        expression:
          "IF(all >= 3, 100 * (FILL(e, 0) + FILL(f, 0) + FILL(t, 0) + FILL(r, 0)) / all, 0)",
        usingMetrics: {
          all: jevOutcome(),
          e: jevOutcome("error"),
          // A 401/403 is its own outcome since #3318: the outage this alarm slept through.
          f: jevOutcome("forbidden"),
          t: jevOutcome("timeout"),
          r: jevOutcome("rate_limited"),
        },
        period: cdk.Duration.hours(1),
      }),
      threshold: 50,
      evaluationPeriods: 1,
      severity: "page",
      description:
        "Jev routing decisions failing (error/forbidden/timeout/429) > 50% over 1 h with >= 3 decisions - room messages are going to the blind fallback responder (see backend log 'Jev returned')",
    });

    // P29: free-tier-first-turn-failures
    // A free-tier workspace's run failing before it has EVER had a reply is a new user's
    // first impression dying (the 09-22 gate-413 bug failed every one, silently). Two in
    // 2 h, not one, so a single flaky turn on a quiet night does not wake anybody.
    this.createAlarm({
      id: "P29",
      name: "free-tier-first-turn-failures",
      metricName: "messaging.run.first_turn_failed",
      threshold: 2,
      evaluationPeriods: 1,
      periodMinutes: 120,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "page",
      description:
        "2+ free-tier workspaces had their first-ever agent turn fail in 2 h - new users see a broken product (log: 'free-tier first turn failed')",
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
    // P10 (apigw-ws-server-error-rate) DELETED with W30-W35: the WebSocket API
    // only carries the retiring desktop relay, and old clients kept W30 in ALARM.
    // -------------------------------------------------------------------
    // P12: backend metrics-loop liveness heartbeat
    // -------------------------------------------------------------------
    // The free-tier idle-reaper (and its scale-to-zero path) was REMOVED in the
    // flat-fee cutover, so this is no longer a "reaper-dead" page. It survives
    // as a generic BACKEND-LIVENESS check: the backend metrics loop still emits
    // gateway.running.count every ~60s regardless of workload, so absence of
    // samples means the backend's metrics loop is dead. Expect ~10 samples per
    // 10-min window when healthy per instance.
    // Companion P13 (reaper-crash) was deleted with the reaper.
    //
    // At N >= 2 a per-instance ALARM is not expressible: a CloudWatch alarm
    // binds a fixed dimensionsMap at synth time, ECS task ids are unknown
    // then and change on every deploy, and SEARCH is rejected outright on
    // metric alarms (core/observability/metrics.py:60-66 carries this same
    // scar). So the fleet SampleCount is divided by the live instance count
    // (AWS/ApplicationELB HealthyHostCount, which W28 below also binds) --
    // instance-count-independent, so one dead loop of two is visible instead
    // of arithmetically indistinguishable from a healthy single instance.
    // The `instance` dimension main.py now tags the gauge with is for
    // diagnosis on a dashboard / Logs Insights, not for this alarm's binding.
    //
    // Deliberately instance-blind: it only proves SOME task is alive out of
    // N. W78 (backend-healthy-hosts) is the instance-aware companion — it
    // catches "one of two" where this only catches "all dead".
    const reaperDims = { env: this.envName, service: "lightbulb-backend" };

    const heartbeatMetric = new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: "gateway.running.count",
      statistic: "SampleCount",
      period: cdk.Duration.minutes(10),
      dimensionsMap: reaperDims,
    });

    // Minimum, not Average: a rolling deploy runs at maxHealthyPercent: 200,
    // so Average would count the transient extra tasks in the denominator
    // while their sample windows are only partly filled, dipping the ratio
    // into a false page.
    const healthyHosts = new cloudwatch.Metric({
      namespace: "AWS/ApplicationELB",
      metricName: "HealthyHostCount",
      statistic: "Minimum",
      period: cdk.Duration.minutes(10),
      dimensionsMap: {
        LoadBalancer: props.alb.loadBalancerFullName,
        TargetGroup: props.targetGroup.targetGroupFullName,
      },
    });

    const heartbeatAlarm = new cloudwatch.Alarm(this, "BackendLivenessAlarm", {
      alarmName: `${resourcePrefix(this.envName)}P12-backend-liveness`,
      alarmDescription:
        "PAGE: at least one backend instance has stopped emitting its 60 s " +
        "heartbeat. Samples per healthy ALB target have fallen below 8 " +
        "(healthy is ~10).",
      metric: new cloudwatch.MathExpression({
        expression: "beats / hosts",
        usingMetrics: { beats: heartbeatMetric, hosts: healthyHosts },
        period: cdk.Duration.minutes(10),
      }),
      // ponytail: 8, not 7 -- prod autoscales 2-4 (C6), and one dead loop of
      // FOUR reads 7.5 (3 alive / 4 hosts * ~10), which 7 would not catch.
      // 8 catches one dead loop at every N in range while healthy (~10) never
      // trips it. Retune here, not by changing the expression, if deploy
      // churn or a period change moves the healthy baseline.
      threshold: 8,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      // Legit liveness check: missing data IS the failure signal.
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    // MathExpression has no way to name its own top-level entry (CDK always
    // auto-generates "expr_N" for it -- only usingMetrics keys become their
    // own ids), so the ratio's rendered Metrics[0].Id is renamed here rather
    // than via a second wrapper MathExpression, which would add a redundant
    // expression layer computing the same value twice.
    (heartbeatAlarm.node.defaultChild as cloudwatch.CfnAlarm).addPropertyOverride(
      "Metrics.0.Id",
      "beatsPerInstance",
    );
    heartbeatAlarm.addAlarmAction(
      new cloudwatch_actions.SnsAction(this.pageTopic),
    );
    // The construct id says BackendLiveness (the reaper is long gone), but the
    // LOGICAL id must stay exactly as deployed. A CloudWatch alarm's physical
    // id is its AlarmName, and AlarmName is unchanged here — so a new logical
    // id would make CloudFormation create the "new" alarm (an upsert onto the
    // same name) and then DELETE it again in the cleanup phase when it removes
    // the old logical resource. The result is a page-tier alarm silently gone.
    // Pinning the logical id renames the code with no template diff at all.
    //
    // The hash suffix is LOAD-BEARING and is NOT decoration: CDK derives the
    // logical id as <construct id><hash of the construct path>, so the
    // deployed id is `ReaperHeartbeatAlarm31FA055D`, not `ReaperHeartbeatAlarm`
    // (verified by synthesizing with the override removed). Pinning the
    // unhashed string would have caused the exact deletion this override
    // exists to prevent. The test below asserts this literal key.
    (heartbeatAlarm.node.defaultChild as cloudwatch.CfnAlarm).overrideLogicalId(
      "ReaperHeartbeatAlarm31FA055D",
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
  }

  // =========================================================================
  // Messaging platform alarms (P19-P22, W68-W76)
  // =========================================================================

  /**
   * Rooms, DMs and agent dispatch — the product's main path, and until now
   * watched only from the infrastructure side (`api-stack.ts` alarms the work
   * queue, the Pipe and the projector Lambda). Nothing alarmed on the parts a
   * user actually feels: a dead worker, a message that reaches no agent, a
   * reply that never comes.
   *
   * ⚠️ EVERY metric below is emitted by a backend change shipping in parallel
   * (see the 2026-09-12 observability tasks). Until that image is deployed the
   * alarms below have no data — which is why all of them are `notBreaching`.
   *
   * ⚠️ EVERY single-dimension filter below names `outcome`, `status_class` or
   * `count_bucket` — and that is a hard constraint, not a coincidence.
   * CloudWatch has no filtered view of a metric, so an alarm can only bind an
   * identity the emitter actually publishes: the full dimension set, the
   * [env, service] rollup, or an [env, service, d] rollup for exactly `status`,
   * `outcome`, `status_class`, `count_bucket` and `source` (`metrics.py`).
   * Filtering on `event_kind`, `kind`, `frame_type`, `error_kind` or
   * `room_kind` here would bind nothing and sit green forever. The
   * dimension-contract test enforces it.
   */
  private createMessagingAlarms(): void {
    // -- P19: the work-queue consumer is failing --
    // The consumer is a Lambda on an SQS event source (service-stack
    // `WorkConsumer`, scripts/work_lambda.py); an idle queue invokes nothing, so
    // liveness is not a heartbeat any more. Per-message failures are reported
    // batch items, not errors, so `Errors` counts only crashes and timeouts —
    // a consumer that cannot run. A consumer that never gets invoked shows up in
    // api-stack's `openclaw-work-oldest-age` page instead.
    this.createAlarm({
      id: "P19",
      name: "messaging-worker-errors",
      namespace: "AWS/Lambda",
      metricName: "Errors",
      dimensions: { FunctionName: `${resourcePrefix(this.envName)}openclaw-work-consumer` },
      statistic: "Sum",
      threshold: 1,
      periodMinutes: 1,
      evaluationPeriods: 5,
      datapointsToAlarm: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "page",
      description:
        "PAGE: the openclaw-work consumer Lambda is crashing or timing out in " +
        "3 of the last 5 minutes. Room work is not draining openclaw-work.fifo, " +
        "so agents are not being woken for room messages.",
    });

    // -- P20: work sits in the queue --
    // Distinct from `openclaw-work-oldest-age` (api-stack), which includes
    // retries and visibility waits. This measures time before each delivery.
    this.createAlarm({
      id: "P20",
      name: "messaging-queue-wait-p99",
      metricName: "messaging.work.queue_wait",
      statistic: "p99",
      threshold: 30_000,
      periodMinutes: 5,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      severity: "page",
      description:
        "PAGE: p99 wait before the worker picks work up exceeds 30s - the " +
        "worker is falling behind the queue.",
    });

    // -- P21: agents stop replying --
    // Time from the human message to the agent message that answers it. The
    // one number a user would recognise as "is this thing working".
    this.createAlarm({
      id: "P21",
      name: "messaging-reply-latency-p95",
      metricName: "messaging.reply.latency",
      statistic: "p95",
      threshold: 300_000,
      periodMinutes: 5,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      severity: "page",
      description:
        "PAGE: p95 agent reply latency exceeds 5 minutes (the stated SLO).",
    });

    // -- P22: the container refuses our ingress --
    // A 4xx from the container's channel plugin is OUR bug (bad agent id,
    // wrong auth, malformed body), not a transient — it will not self-heal and
    // every affected message is silently dropped. Any occurrence pages.
    this.createAlarm({
      id: "P22",
      name: "messaging-ingress-4xx",
      metricName: "messaging.ingress.post",
      dimensions: { status_class: "4xx" },
      threshold: 0,
      periodMinutes: 5,
      severity: "page",
      description:
        "PAGE: the container's lightbulb-channel ingress rejected a dispatch " +
        "with a 4xx. Non-transient: those messages never reach the agent.",
    });

    // -- W68: messages that mention an agent and reach nobody --
    // `count_bucket=0` means select_dispatch_targets returned no targets: the
    // user addressed an agent and nothing was woken. Sustained across a full
    // 15 minutes (3 of 3) so a single quiet-room blip stays quiet.
    this.createAlarm({
      id: "W68",
      name: "messaging-dispatch-zero-targets",
      metricName: "messaging.dispatch.targets",
      dimensions: { count_bucket: "0" },
      threshold: 5,
      periodMinutes: 5,
      evaluationPeriods: 3,
      datapointsToAlarm: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
      description:
        "Messages are selecting zero dispatch targets - agents are being " +
        "addressed and nothing is running.",
    });

    // -- W69: @handles that resolve to nothing --
    this.createAlarm({
      id: "W69",
      name: "messaging-mention-unresolved",
      metricName: "messaging.mention.unresolved",
      threshold: 5,
      periodMinutes: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
      description:
        "@mentions are failing to resolve to an agent (ambiguous or no " +
        "match) - the user believes they addressed someone.",
    });

    // -- W70: the container is unreachable --
    // Timeouts and transport failures together: both mean the POST never got
    // an answer, as opposed to getting a bad one (that is P22 / the 5xx path).
    // FILL(...,0) on both operands because an error metric publishes nothing
    // when there are no errors, and CloudWatch arithmetic yields NO datapoint
    // for a period unless every operand has data in it.
    const ingressUnreachable = new cloudwatch.MathExpression({
      expression: "FILL(timeouts, 0) + FILL(transport, 0)",
      usingMetrics: {
        timeouts: this.messagingMetric("messaging.ingress.post", {
          status_class: "timeout",
        }),
        transport: this.messagingMetric("messaging.ingress.post", {
          status_class: "transport",
        }),
      },
      period: cdk.Duration.minutes(5),
    });
    this.createAlarm({
      id: "W70",
      name: "messaging-ingress-unreachable",
      metric: ingressUnreachable,
      threshold: 10,
      severity: "warn",
      description:
        "Dispatch POSTs to owner containers are timing out or failing at the " +
        "transport layer - containers unreachable, not rejecting.",
    });

    // -- W71: work retrying in a loop --
    this.createAlarm({
      id: "W71",
      name: "messaging-worker-retry-storm",
      metricName: "messaging.worker.message",
      dimensions: { outcome: "retry" },
      threshold: 20,
      periodMinutes: 5,
      severity: "warn",
      description:
        "Work items are retrying in bulk - a poison pointer or a fleet-wide " +
        "wake failure, ahead of anything reaching the DLQ.",
    });

    // -- W73: live events not reaching AppSync --
    this.createAlarm({
      id: "W73",
      name: "messaging-live-publish-error",
      metricName: "messaging.live.publish",
      dimensions: { outcome: "error" },
      threshold: 5,
      periodMinutes: 5,
      severity: "warn",
      description:
        "Live event publishes to AppSync are failing - clients fall back to " +
        "polling and see stale rooms.",
    });

    // -- W74: wakes refused --
    // `blocked` (gated by billing/trial) and `operator_stopped` (deliberately
    // stopped) are both legitimate individually; in bulk they mean a cohort of
    // owners is sending messages nothing will ever answer.
    const wakeSuppressed = new cloudwatch.MathExpression({
      expression: "FILL(blocked, 0) + FILL(stopped, 0)",
      usingMetrics: {
        blocked: this.messagingMetric("messaging.wake.outcome", {
          outcome: "blocked",
        }),
        stopped: this.messagingMetric("messaging.wake.outcome", {
          outcome: "operator_stopped",
        }),
      },
      period: cdk.Duration.minutes(5),
    });
    this.createAlarm({
      id: "W74",
      name: "messaging-wake-suppressed",
      metric: wakeSuppressed,
      threshold: 5,
      severity: "warn",
      description:
        "Container wakes are being refused (blocked or operator-stopped) - " +
        "those owners' messages will never be answered.",
    });

    // -- W75: mobile push failing at the projector --
    // NOTE the `service` override: the projector Lambda emits its own EMF with
    // service="messaging-projector", not the backend's "lightbulb-backend".
    this.createAlarm({
      id: "W75",
      name: "messaging-projector-push-send-error",
      metricName: "messaging.projector.push",
      dimensions: { service: "messaging-projector", outcome: "send_error" },
      threshold: 10,
      periodMinutes: 5,
      severity: "warn",
      description:
        "The projector is failing to hand notifications to Expo - mobile " +
        "users get no banner for new room messages.",
    });

    // -- W76: backend push failing --
    this.createAlarm({
      id: "W76",
      name: "push-send-error",
      metricName: "push.send",
      dimensions: { outcome: "error" },
      threshold: 10,
      periodMinutes: 5,
      severity: "warn",
      description: "Backend push sends to Expo are failing.",
    });

    // -- W77: messaging-rpc-errors (next free id — W73 is already
    // messaging-live-publish-error). `core/services/container_rpc.py` is the
    // sole way the backend reaches an owner's container plugin; a sustained
    // error rate here means real RPCs (agents.*, channels.*, health, ...) are
    // failing at the plugin, distinct from W70 (unreachable/timeout, not a
    // failure response).
    this.createAlarm({
      id: "W77",
      name: "messaging-rpc-errors",
      metricName: "messaging.rpc",
      dimensions: { outcome: "error" },
      threshold: 10,
      periodMinutes: 5,
      evaluationPeriods: 2,
      datapointsToAlarm: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
      description:
        "container_rpc calls to owner containers are failing (gateway " +
        "method errors, not timeouts/unreachable - that's W70) - 10+ in " +
        "5 min, sustained across 2 periods.",
    });
  }

  /** A 5-minute Sum on a backend-emitted messaging metric, for metric math. */
  private messagingMetric(
    metricName: string,
    dimensions: Record<string, string>,
  ): cloudwatch.Metric {
    return new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName,
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: {
        env: this.envName,
        service: "lightbulb-backend",
        ...dimensions,
      },
    });
  }

  // =========================================================================
  // Warn-tier custom metric alarms (W1-W27)
  // =========================================================================

  private createWarnCustomMetricAlarms(): void {
    const dims = { env: this.envName, service: "lightbulb-backend" };

    // -- Container (W1-W8; the gateway ids W5-W8 are retired, see below) --

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
    // health is ALREADY covered without it — P12 (backend-liveness via
    // gateway.running.count) only proves the backend process is alive, and W77
    // (messaging-rpc-errors) is the actual container-reachability alarm. A
    // gauge falling to zero because nobody is connected is neither. Deleting
    // is the correct, minimal fix.

    // W6 (gateway-health-check-timeout), W7 (gateway-frontend-prune-storm),
    // W8 (gateway-rpc-error-count), W9 (chat-e2e-latency-p99), W10
    // (chat-error-rate) and W11 (chat-session-usage-fetch-error) DELETED with
    // the gateway connection pool (Task 10, stateless-backend): all six were
    // reading metrics the pool used to emit (gateway.health_check.timeout,
    // gateway.frontend.prune, gateway.rpc.error, chat.e2e.latency/chat.error
    // scoped to source=webchat, chat.session_usage.fetch.error), and the pool
    // is gone — the backend now calls the container plugin over HTTP
    // (container_rpc) per RPC, with no persistent connection to prune,
    // reconnect, or health-check on its own timer. Room traffic keeps its own
    // SLOs (P21 on messaging.reply.latency).

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

    // W12 (chat-bedrock-throttle) DELETED: the custom chat.bedrock.throttle
    // metric was never emitted. Bedrock throttles are covered by the AWS-native
    // W46-bedrock-throttles alarm (AWS/Bedrock InvocationThrottles).

    // -- Channels (W13-W15) --

    // W13 (channel-rpc-error-rate) and W14 (channel-configure-fail) DELETED:
    // nothing on main emits channel.rpc or channel.configure any more.

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

    // -- Proxy, update (W25-W27) --

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

    // W53 (operator-relay-churn) DELETED: operator.upstream.closed was emitted by
    // the per-user Paperclip relay, which is gone.

    // W54 (gateway-reconnect-throttle-storm) DELETED with the gateway
    // connection pool (Task 10, stateless-backend): it read
    // gateway.connection{event:reconnect_throttled}, emitted by the pool's
    // own reconnect-floor guard (_respect_reconnect_floor). container_rpc
    // makes one HTTP call per RPC with no persistent per-user connection to
    // reconnect or throttle.

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

    // W30-W32 (API Gateway WebSocket) and W33-W35 (its ws-authorizer Lambda)
    // DELETED: see P10. The dashboard still charts the WebSocket API until it goes.

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
  // Funnel, voice and gate alarms (W66, W84, W85, P34, ...)
  // =========================================================================

  private createLateAlarms(props: ObservabilityStackProps): void {
    // W66 (onboarding-bootstrap-failed) DELETED: bootstrap.state was emitted by
    // the Paperclip repo, which is gone.

    // W84: open voice windows near the GPT-Live concurrent-session tier
    // (voice_calls.VOICE_SESSION_TIER = 25). The sweeper emits the gauge once
    // a minute; Maximum so a second backend task's sample cannot halve it.
    this.createAlarm({
      id: "W84",
      name: "voice-sessions-near-tier",
      metricName: "voice.calls.open",
      statistic: "Maximum",
      threshold: 17.5,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      periodMinutes: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      severity: "warn",
      description:
        "Open voice windows reached 70% of the 25-session GPT-Live tier for 2 of 3 minutes - raise the tier before new Live calls start failing.",
    });

    // W85: ERROR lines from the voice routers (routers.voice, routers.voice_phone).
    // No existing 5xx metric carries the route, so this is a filter on the backend
    // log group. A JSON filter cannot set dimensions, so the env rides in the
    // namespace (dev and prod share one account) and the alarm reads the filter's own metric.
    const voiceErrors = new logs.MetricFilter(this, "VoiceErrorsFilter", {
      logGroup: logs.LogGroup.fromLogGroupName(
        this,
        "BackendLogGroupRef",
        props.backendLogGroupName,
      ),
      filterPattern: logs.FilterPattern.literal(
        '{ $.level = "ERROR" && $.logger = "routers.voice*" }',
      ),
      metricNamespace: `LightbulbAI/Voice/${this.envName}`,
      metricName: "voice.errors",
      metricValue: "1",
    });
    this.createAlarm({
      id: "W85",
      name: "voice-errors",
      metric: voiceErrors.metric({
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 3,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      severity: "warn",
      description:
        "3+ ERROR log lines from the voice routers in 5 min - check the backend log group for routers.voice / routers.voice_phone.",
    });

    // W86: a Twilio number was bought but its PHONE#/LINE row failed to write
    // (voice_onboarding._provision_number). It bills monthly with nothing routing
    // to it; a Go-live retry completes the row from the receipt.
    this.createAlarm({
      id: "W86",
      name: "voice-number-orphaned",
      metricName: "voice.number.orphaned",
      statistic: "Sum",
      threshold: 1,
      evaluationPeriods: 1,
      periodMinutes: 60,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      severity: "warn",
      description:
        "A voice number was bought but its line row failed to write - the workspace's Go-live retry completes it; check the backend log for the workspace id.",
    });

    // P34: OpenRouter refusing keys that still have daily allowance. The gate
    // (lambda/openrouter-gate/core.mjs) emits this when "Key limit exceeded" arrives
    // for a key with money left: the shared OpenRouter ACCOUNT is out of credits, and
    // every managed workspace is silent. 2026-10-13 it ran a day unnoticed.
    this.createAlarm({
      id: "P34",
      name: "openrouter-account-refused",
      metricName: "openrouter.gate.account_refused",
      dimensions: { service: "openrouter-gate" },
      periodMinutes: 5,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      severity: "page",
      description:
        "PAGE: OpenRouter refused managed requests as 'Key limit exceeded' for a key that still has allowance - the OpenRouter account is likely out of credits. Top it up at openrouter.ai/settings/credits; every managed workspace is silent until then.",
    });

    // P35: a second engine holds a workspace's member key. The runtime's sidecar and
    // an impostor supersede each other on every redial; the bridge
    // (hermes/bridge/network.py) emits this after 4 `superseded` events in 10 min.
    this.createAlarm({
      id: "P35",
      name: "network-member-key-contested",
      metricName: "network.member_key_contested",
      dimensions: { service: "hermes-bridge" },
      periodMinutes: 10,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      severity: "page",
      description:
        "PAGE: a workspace's network member key is in use by a second engine (more than 3 superseded links in 10 min). First rule out a split session (two microVMs for one owner; see docs/runbooks/runtime-split-brain-detection.md). Otherwise treat the key as stolen: the owner reconnects the workspace (a new key and team; every computer pastes a new line).",
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

    // W51 (nat-gateway-data-transfer) DELETED: there are no NAT gateways any more
    // (NAT instances, see network-stack), so AWS/NATGateway has no data.

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

    // W79 + P25: OpenRouter account balance — ENV-BLIND, so PROD-GATED (see
    // W46/W47 above). Dev and prod share ONE OpenRouter account (one
    // management key, one credits balance), so an ungated pair would double
    // page on the same number every time the shared balance dips. The backend
    // emits openrouter.account.balance_usd from the running-gauges loop
    // (main.py) roughly every 15 min; treatMissingData is NOT_BREACHING
    // because a deploy gap with no emission is normal and should not page.
    if (this.envName === "prod") {
      // W79: balance < $5 — warn.
      this.createAlarm({
        id: "W79",
        name: "openrouter-balance-low",
        metricName: "openrouter.account.balance_usd",
        statistic: "Minimum",
        threshold: 5,
        evaluationPeriods: 1,
        periodMinutes: 30,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        severity: "warn",
        description: "OpenRouter account balance below $5 - top up soon",
      });

      // P25: balance < $1 — page.
      this.createAlarm({
        id: "P25",
        name: "openrouter-balance-critical",
        metricName: "openrouter.account.balance_usd",
        statistic: "Minimum",
        threshold: 1,
        evaluationPeriods: 1,
        periodMinutes: 30,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        severity: "page",
        description: "OpenRouter account balance below $1 - inference will start failing",
      });
    }

    // W80: OpenRouter settlement worker failing. Per-env (each env runs its own
    // worker). The function has no async retries and no failure destination, and a
    // run raises on any row it may not settle, so this is the only signal that
    // capped cost / key retirement has stopped. 3 errors in 30 min = ~half the ticks.
    this.createAlarm({
      id: "W80",
      name: "openrouter-settlement-errors",
      description: "OpenRouter settlement Lambda failing (>= 3 errors in 30 min)",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Errors",
        statistic: "Sum",
        period: cdk.Duration.minutes(30),
        dimensionsMap: { FunctionName: `${resourcePrefix(this.envName)}openrouter-settlement` },
      }),
      threshold: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      severity: "warn",
    });

    // W81: OpenRouter settlement debited more than the gate reserved. The real debit is
    // kept (Metronome bills it), so the fast-gate pool can go negative; admission reads
    // that as exhausted. EMF line from the settlement Lambda, service=openrouter-settlement.
    this.createAlarm({
      id: "W81",
      name: "openrouter-settlement-overshoot",
      description: "OpenRouter settlement debit exceeded its gate reservation",
      metricName: "openrouter.settlement.overshoot_microcents",
      dimensions: { service: "openrouter-settlement" },
      periodMinutes: 30,
      threshold: 0,
      severity: "warn",
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
  /**
   * Watchdog alarms on the watchdog itself (spec 11 §3.7, §4.4; wave-2 plan W7). Dev only, behind the
   * super-agent gate: prod has no watchdog service. P48 is BREACHING on missing data on purpose (the P7
   * pattern): the watchdog not ticking is exactly what it must catch.
   */
  private createWatchdogAlarms(): void {
    this.createAlarm({
      id: "P48",
      name: "watchdog-silent",
      metricName: "watchdog.tick",
      statistic: "SampleCount",
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 15,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      severity: "warn",
      description: "Watchdog has not ticked for 15 min (schedule paused, service down or stuck)",
    });
    this.createAlarm({
      id: "P49",
      name: "watchdog-write-rate",
      metricName: "watchdog.write",
      threshold: 300,
      periodMinutes: 60,
      severity: "warn",
      description: "Watchdog wrote > 300 rows in an hour: a finding storm or a loop in the watchdog itself",
    });
    this.createAlarm({
      id: "P50",
      name: "watchdog-cap-hit",
      metricName: "watchdog.cap_hit",
      threshold: 0,
      periodMinutes: 60,
      severity: "warn",
      description: "Watchdog hit its fleet action cap or judge budget in the last hour",
    });
    this.createAlarm({
      id: "P51",
      name: "watchdog-judge-spend",
      metricName: "watchdog.judge.cost",
      threshold: 5,
      periodMinutes: 1440,
      severity: "warn",
      description: "Watchdog judge (Jev) spent > $5 in a day",
    });
  }

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

  /**
   * The Messaging block — rooms, dispatch, the work queue, live delivery.
   *
   * The messaging platform is the product, and none of it was on this
   * dashboard: `api-stack.ts` alarms its queue and projector, but there was
   * nowhere to LOOK when something felt slow. Sits right after the runtime
   * gauges, before the older channel/billing rows.
   *
   * Splits by `outcome` / `status_class` / `count_bucket` bind the emitter's
   * per-dimension rollups (see `metrics.py`). The one split on a dimension
   * with no rollup — `author_kind` — uses a SEARCH expression, which is
   * allowed on a widget even though CloudWatch rejects it on an alarm.
   */
  private addMessagingDashboardRows(
    dashboard: cloudwatch.Dashboard,
    props: ObservabilityStackProps,
    dims: Record<string, string>,
  ): void {
    const sum = (
      metricName: string,
      extra: Record<string, string> = {},
      label?: string,
    ) =>
      new cloudwatch.Metric({
        namespace: "LightbulbAI",
        metricName,
        statistic: "Sum",
        dimensionsMap: { ...dims, ...extra },
        label: label ?? Object.values(extra).join(" ") ?? "total",
      });
    const splitBy = (
      metricName: string,
      key: string,
      values: string[],
    ): cloudwatch.IMetric[] =>
      values.map((value) => sum(metricName, { [key]: value }));

    const appsync = (metricName: string) =>
      new cloudwatch.Metric({
        namespace: "AWS/AppSync",
        metricName,
        statistic: "Sum",
        // Event APIs publish under EventAPIId, not GraphQLAPIId.
        dimensionsMap: { EventAPIId: props.messaging.appsyncEventApiId },
        label: metricName,
      });
    const sqsVisible = (queueName: string, label: string) =>
      new cloudwatch.Metric({
        namespace: "AWS/SQS",
        metricName: "ApproximateNumberOfMessagesVisible",
        statistic: "Maximum",
        dimensionsMap: { QueueName: queueName },
        label,
      });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        // `author_kind` has no per-dimension rollup — its only published
        // identity is the emitter's full set {author_kind, room_kind,
        // has_targets}, which would mean enumerating every combination. SEARCH
        // returns one line per published set instead; it is rejected on alarms
        // but fine on a widget. The schema is spelled in CloudWatch's
        // alphabetical dimension order, which is what it matches on.
        title: "Messages created by author",
        left: [
          new cloudwatch.SearchExpression({
            expression:
              "SEARCH('{LightbulbAI,author_kind,env,has_targets,room_kind,service} " +
              `MetricName="messaging.message.created" env="${this.envName}"', 'Sum')`,
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Dispatch targets per message",
        left: splitBy("messaging.dispatch.targets", "count_bucket", [
          "0",
          "1",
          "many",
        ]),
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Work queue depth & wait",
        left: [
          sqsVisible(props.messaging.workQueueName, "work queue"),
          sqsVisible(props.messaging.workDlqName, "DLQ"),
        ],
        right: [
          new cloudwatch.Metric({
            namespace: "LightbulbAI",
            metricName: "messaging.work.queue_wait",
            statistic: "p99",
            dimensionsMap: dims,
            label: "queue wait p99",
          }),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Ingress & reply latency",
        left: (["p50", "p99"] as const).map(
          (statistic) =>
            new cloudwatch.Metric({
              namespace: "LightbulbAI",
              metricName: "messaging.ingress.latency",
              statistic,
              dimensionsMap: dims,
              label: `ingress ${statistic}`,
            }),
        ),
        right: (["p50", "p95"] as const).map(
          (statistic) =>
            new cloudwatch.Metric({
              namespace: "LightbulbAI",
              metricName: "messaging.reply.latency",
              statistic,
              dimensionsMap: dims,
              label: `reply ${statistic}`,
            }),
        ),
        width: 6,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Container ingress by status class",
        left: splitBy("messaging.ingress.post", "status_class", [
          "202",
          "409",
          "4xx",
          "5xx",
          "timeout",
          "transport",
          "other",
        ]),
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Work items by outcome",
        left: splitBy("messaging.worker.message", "outcome", ["ok", "retry"]),
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "AppSync live delivery",
        left: [
          appsync("BroadcastDroppedEvents"),
          appsync("FailedEvents"),
        ],
        right: [appsync("ActiveConnections")],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Projector push & lag",
        // service=messaging-projector: the Lambda emits its own EMF and is not
        // the backend. Its only extra dimension is `outcome`, so unlike the
        // widgets above this one binds a set that really is published.
        left: ["ok", "send_error", "token_read_error", "capped", "muted"].map(
          (outcome) =>
            sum(
              "messaging.projector.push",
              { service: "messaging-projector", outcome },
              outcome,
            ),
        ),
        right: [
          new cloudwatch.Metric({
            namespace: "AWS/Lambda",
            metricName: "IteratorAge",
            statistic: "Maximum",
            dimensionsMap: {
              FunctionName: props.messaging.projectorFunctionName,
            },
            label: "iterator age",
          }),
        ],
        width: 6,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Ambient gate outcomes",
        left: splitBy("messaging.ambient_gate", "outcome", [
          "dispatched",
          "ignored",
          "replayed",
          "not_admitted",
          "gate_error",
          "unknown",
        ]),
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Container wake outcomes",
        left: splitBy("messaging.wake.outcome", "outcome", [
          "dispatched",
          "already_running",
          "in_flight",
          "converting",
          "blocked",
          "operator_stopped",
          "no_row",
          "error",
          "unknown",
        ]),
        width: 6,
        height: 6,
      }),
    );
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
    //
    // REPOINTED at the messaging product. The headline used to be webchat
    // success rate + webchat p99 — a surface that is now one entry point among
    // rooms, DMs and channels, and one that says nothing about whether an
    // agent actually answered anybody. The two questions that matter at the
    // top of the ORR dashboard are: did the message reach an agent, and how
    // long did the human wait for the reply. The old webchat pair was Row 3;
    // it was deleted with the gateway connection pool (Task 10,
    // stateless-backend) — see the note where Row 3 used to be.
    const agentAnsweredRate = new cloudwatch.MathExpression({
      // FILL(zero, 0): a period in which NO message missed its targets
      // publishes no `count_bucket=0` datapoint at all, and CloudWatch
      // arithmetic yields nothing for a period unless every operand has data —
      // so without FILL the widget goes blank exactly when things are healthy.
      expression: "100 * (1 - FILL(zero, 0) / IF(total > 0, total, 1))",
      usingMetrics: {
        // count_bucket=0 is "select_dispatch_targets returned nothing", i.e.
        // the message was addressed to an agent and woke none.
        zero: new cloudwatch.Metric({
          namespace: "LightbulbAI",
          metricName: "messaging.dispatch.targets",
          statistic: "Sum",
          dimensionsMap: { ...dims, count_bucket: "0" },
        }),
        // The [env, service] rollup — every dispatch decision, whatever its
        // bucket, room kind or source.
        total: new cloudwatch.Metric({
          namespace: "LightbulbAI",
          metricName: "messaging.dispatch.targets",
          statistic: "Sum",
          dimensionsMap: dims,
        }),
      },
      period: cdk.Duration.hours(1),
    });

    const replyLatencyP95 = new cloudwatch.Metric({
      namespace: "LightbulbAI",
      metricName: "messaging.reply.latency",
      statistic: "p95",
      period: cdk.Duration.hours(1),
      dimensionsMap: dims,
    });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title:
          "Agent answered (share of mentioning messages with >=1 dispatch target)",
        left: [agentAnsweredRate],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Agent reply latency p95 (SLO < 5 min)",
        left: [replyLatencyP95],
        width: 12,
        height: 6,
      }),
    );

    // ----- Row 2: Containers (2 widgets) -----
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
    );

    // Row 3 (Chat pipeline: "Chat messages & errors", "Webchat E2E latency",
    // "Webchat success rate") DELETED with the gateway connection pool (Task
    // 10, stateless-backend) — all three plotted the pool-era webchat surface
    // (chat.e2e.latency/chat.error scoped to source=webchat, matching the
    // deleted W9/W10 alarms), which no longer exists as a distinct RPC path.

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

    this.addMessagingDashboardRows(dashboard, props, dims);

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

    // ----- Row 5: Auth & Security (2 widgets) -----
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

    // ----- Row 7: Backend ECS (2 widgets) -----
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
    );

    // ----- Row 8: DynamoDB + Update worker + Bedrock (3 widgets) -----
    // "Proxy upstream errors" widget removed — proxy.upstream is never emitted.
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        // Repointed off the never-emitted custom Isol8/dynamodb.throttle to the
        // AWS-native AWS/DynamoDB ThrottledRequests (summed across our tables),
        // matching the P8 alarm.
        title: "DynamoDB throttled requests (AWS-native)",
        left: alarmedTables(props.databaseTables).map(
          ([label, table]) =>
            new cloudwatch.Metric({
              namespace: "AWS/DynamoDB",
              metricName: "ThrottledRequests",
              statistic: "Sum",
              dimensionsMap: { TableName: table },
              label,
            }),
        ),
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
    dashboard.addWidgets(
      ...(
        [
          ["Jev decisions by outcome", "messaging.jev.decision", "outcome", ["ok", "error", "timeout", "rate_limited", "disabled"]],
          ["Rejected hook tokens by route", "internal_hook.auth_failed", "route", ["other"]],
        ] as const
      ).map(
        ([title, metricName, dim, values]) =>
          new cloudwatch.GraphWidget({
            title,
            left: values.map(
              (value) =>
                new cloudwatch.Metric({
                  namespace: "LightbulbAI",
                  metricName,
                  statistic: "Sum",
                  period: cdk.Duration.hours(1),
                  dimensionsMap: { ...dims, [dim]: value },
                  label: value,
                }),
            ),
            width: 8,
            height: 6,
          }),
      ),
    );

    // ----- Local-session relay (Claude Code session sharing; load-test scenario 3) -----
    const relayMetric = (
      metricName: string,
      statistic: string,
      label: string,
    ) =>
      new cloudwatch.Metric({
        namespace: "LightbulbAI",
        metricName,
        statistic,
        period: cdk.Duration.minutes(1),
        dimensionsMap: dims,
        label,
      });
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Session relay frames + connections",
        left: [
          ...["register", "heartbeat", "events", "ended", "receipt", "screen"].map(
            (type) => relayMetric(`session.relay.message.${type}`, "Sum", type),
          ),
          relayMetric("session.relay.connection.open", "Sum", "open"),
          relayMetric("session.relay.connection.close", "Sum", "close"),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Session relay latency (ms)",
        left: [
          relayMetric("session.relay.heartbeat.latency", "p50", "heartbeat ack p50"),
          relayMetric("session.relay.heartbeat.latency", "p99", "heartbeat ack p99"),
          relayMetric("session.command.dispatch.latency", "p50", "command dispatch p50"),
          relayMetric("session.command.dispatch.latency", "p99", "command dispatch p99"),
        ],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Session screen frames",
        left: [
          relayMetric("session.screen.frame.forwarded", "Sum", "forwarded"),
          relayMetric("session.screen.frame.dropped", "Sum", "dropped (budget)"),
        ],
        right: [relayMetric("session.screen.bytes", "Sum", "bytes forwarded")],
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Session screen viewers per frame",
        left: [
          relayMetric("session.screen.viewers", "Average", "avg"),
          relayMetric("session.screen.viewers", "Maximum", "max"),
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
