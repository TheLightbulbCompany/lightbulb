import {
  resourcePrefix,
  tablePrefix,
} from "../lib/table-names";
import { LightbulbStage } from "../lib/lightbulb-stage";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../lib/domains";
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
      envName,
      backendService: service,
      backendLogGroupName: `/ecs/isol8-${envName}`,
      alb,
      targetGroup: tg,
      wsApiId: "test-ws-api-id",
      cluster,
      databaseTables: {
        usersTable: `${tablePrefix(envName)}users`,
        containersTable: `${tablePrefix(envName)}containers`,
        billingTable: `${tablePrefix(envName)}billing-accounts`,
        usageCountersTable: `${tablePrefix(envName)}usage-counters`,
        pendingUpdatesTable: `${tablePrefix(envName)}pending-updates`,
        channelLinksTable: `${tablePrefix(envName)}channel-links`,
        companySlugsTable: `${tablePrefix(envName)}company-slugs`,
        agentEmailAddressesTable: `${tablePrefix(envName)}agent-email-addresses`,
        messagingTable: `${tablePrefix(envName)}messaging`,
        wsConnectionsTable: `${tablePrefix(envName)}ws-connections`,
      },
      authorizerFunctionName: `isol8-${envName}-ws-authorizer`,
      messaging: {
        workQueueName: `${resourcePrefix(envName)}openclaw-work.fifo`,
        workDlqName: `${resourcePrefix(envName)}openclaw-work-dlq.fifo`,
        projectorFunctionName: `${resourcePrefix(envName)}messaging-projector`,
        appsyncEventApiId: "test-event-api-id",
      },
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

// ---------------------------------------------------------------------------
// Emitter registry — the dimension sets the backend actually publishes
// ---------------------------------------------------------------------------

/**
 * Every Isol8-namespace metric an alarm in this stack binds, mapped to the
 * EXTRA dimension key sets its backend emitter publishes (env + service are
 * on everything and are excluded here).
 *
 * Why this exists: CloudWatch treats each dimension set as a separate metric
 * identity, not a filterable view. An alarm bound to a set no emitter produces
 * receives zero datapoints and, with NOT_BREACHING, sits green forever. Three
 * alarms (W2, W8, W10) shipped that way and were discovered only by reading
 * the code. `put_metric` now always emits an [env, service] rollup alongside
 * the full set, so the empty set below is universally valid; anything else has
 * to match an emitter exactly.
 *
 * Maintenance: grep the metric name in apps/backend for its put_metric /
 * timing / gauge call and copy the dimension keys. A new alarm on a metric
 * that is not listed fails this test — which is the point: if you cannot name
 * the emitter, the alarm has nothing to watch.
 */
const EMITTED_DIMENSION_SETS: Record<string, string[][]> = {
  "auth.jwks.refresh": [["status"]],
  "auth.jwt.fail": [["reason"]],
  "auth.org_admin.denied": [[]],
  "billing.pricing.missing_model": [[]],
  // core/repositories/paperclip_repo.py — mark_bootstrap_delivered /
  // mark_bootstrap_failed, on the conditional write that lands the terminal
  // transition. W66 binds the "failed" status rollup.
  "bootstrap.state": [["status"]],
  "channel.configure": [["provider", "status"]],
  "channel.rpc": [["provider", "status"]],
  "company_stripe.account.created": [[]],
  "container.efs.access_point": [["op", "status"]],
  "container.error_state": [[]],
  "container.lifecycle.latency": [["op"]],
  "container.provision": [["status"]],
  "container.task_def.register": [["status"]],
  "debug.endpoint.prod_hit": [["endpoint"]],
  "gateway.cold_start.count": [["outcome"]],
  "gateway.cold_start.latency": [[]],
  // main.py:_running_count_gauge_loop. P12 divides the fleet sample count by
  // HealthyHostCount (C5) but binds only {env, service} -- the emitting ECS
  // task id is a log field (running_count), not a metric dimension, since it
  // is a new identity per task per deploy (the _DENIED_DIMENSIONS class).
  "gateway.running.count": [[]],
  // core/services/voice_calls.py sweep_calls_once; W84 binds the rollup.
  "voice.calls.open": [[]],
  // core/services/voice_onboarding.py _provision_number (LINE write failed after a purchase), no dims; W86 binds the rollup.
  "voice.number.orphaned": [[]],
  // core/services/watchdog.py (spec 11): tick_metric {Mode}; every repo write; cap refusals {Kind};
  // the judge's Jev cost via jev.decide(metric_prefix="watchdog.judge"), no dims. P48-P51 bind the rollup.
  "watchdog.tick": [["Mode"]],
  "watchdog.write": [[]],
  "watchdog.cap_hit": [["Kind"]],
  "watchdog.judge.cost": [[]],
  "marketplace.path_traversal_attempt": [[]],
  // routers/paperclip_events.py receive_paperclip_event, before the push
  // decision. No alarm since the Paperclip exit.
  "paperclip.run.failed": [["error_code"]],
  // core/services/paperclip_escalation.py escalate; no alarm since the Paperclip exit.
  "paperclip.escalation.exhausted": [[]],
  // routers/messaging.py _count_hook_auth_failure; dashboard only.
  "internal_hook.auth_failed": [["route"]],
  // core/services/jev.py decide(); P26 binds the rollup and outcome=error|forbidden|timeout|rate_limited.
  "messaging.jev.decision": [["outcome"]],
  // core/services/run_analytics.py _enrich_and_capture; no alarm since the Paperclip exit.
  "nightly_ceo.run": [["outcome"]],
  // core/services/messaging.py count_first_turn_failure; P29 binds the bare rollup.
  "messaging.run.first_turn_failed": [[]],
  // apps/infra/lambda/openrouter-settlement/index.py Store.commit (EMF, own service); W81.
  "openrouter.settlement.overshoot_microcents": [[]],
  // apps/infra/lambda/openrouter-gate/core.mjs (EMF, own service); P34.
  "openrouter.gate.account_refused": [[]],
  // apps/infra/hermes/bridge/network.py (EMF, service hermes-bridge); P35.
  "network.member_key_contested": [[]],
  // --- messaging platform (emitters ship with the 2026-09-12 backend tasks) ---
  // core/services/messaging.py — create_human_message / create_agent_message.
  "messaging.message.created": [["author_kind", "has_targets", "room_kind"]],
  // core/services/messaging.py — after each select_dispatch_targets call.
  "messaging.dispatch.targets": [["count_bucket", "room_kind", "source"]],
  "messaging.mention.unresolved": [["reason"]],
  "messaging.reply.latency": [["event_kind", "matched", "room_kind"]],
  // core/services/container_rpc.py — one HTTP RPC per call to the container
  // plugin. "method" is not a rollup dimension, so it only publishes with the
  // full set or the outcome-only rollup, never alone.
  "messaging.rpc": [["outcome", "method"], ["outcome"]],
  // core/services/openclaw_work.py — the work-queue consumer.
  "messaging.work.queue_wait": [["kind"]],
  "messaging.worker.message": [["error", "kind", "outcome"]],
  "messaging.ingress.post": [["event_kind", "status_class"]],
  "messaging.wake.outcome": [["outcome"]],
  // core/services/live_events.py
  "messaging.live.publish": [["outcome", "status_class"]],
  // apps/infra/lambda/messaging-projector/index.py — note this one emits with
  // service="messaging-projector", not the backend's service dimension.
  "messaging.projector.push": [["outcome"]],
  "metronome.gate.refresh_error": [[]],
  "metronome.webhook": [["result"], ["blocked", "result"]],
  // core/services/openrouter_keys.py — check_account_balance, from the
  // running-gauges loop in main.py. Prod-only alarms (W79/P25); dev and prod
  // share one OpenRouter account.
  "openrouter.account.balance_usd": [[]],
  "operator.upstream.closed": [[]],
  "paperclip.upstream.error": [["kind"]],
  // core/services/push_service.py — send_push_to_user.
  "push.send": [["error_kind", "outcome"]],
  "provision.gate.blocked": [["code"]],
  // core/services/hermes_provisioning.py — reconcile_runtime's except arm.
  "provision.reconcile.failed": [[]],
  // run_analytics.schedule_run_completed — full set {outcome, source}.
  "run.completed": [["outcome", "source"]],
  "stripe.api.latency": [["op"]],
  "stripe.webhook.received": [["event_type"]],
  "stripe.webhook.sig_fail": [[]],
  "update.scheduled_worker.error": [[]],
  "update.scheduled_worker.heartbeat": [[]],
  "webhook.clerk.sig_fail": [[]],
  "workspace.file.write.error": [[]],
  "workspace.path_traversal.attempt": [[]],
};

/**
 * The single dimensions `put_metric` publishes an [env, service, d] rollup for.
 *
 * This is the whole reason an alarm can say "errors, across every value of the
 * qualifier" — every op, every provider, every `kind` — without a SEARCH
 * expression, which CloudWatch rejects on alarms outright. An alarm naming one
 * dimension OUTSIDE this set (`event_kind`, `room_kind`, `frame_type`,
 * `error_kind`, `author_kind`, `kind`) binds an identity nothing publishes
 * unless it happens to be that metric's complete set.
 *
 * Keep in lockstep with `_ROLLUP_KEYS` / the per-dimension rollups in
 * apps/backend/core/observability/metrics.py.
 */
const ROLLUP_DIMENSIONS = [
  "status",
  "outcome",
  "status_class",
  "count_bucket",
  "source",
];

/**
 * The `service` dimension value each LightbulbAI metric is published under.
 *
 * Everything is the backend except the projector Lambda, which emits its own
 * EMF. This is a separate check from the dimension-SET contract below because
 * the two fail differently: a wrong service is a right dimension key with a
 * wrong VALUE, which the set check cannot see at all — the alarm looks
 * perfectly formed and receives nothing forever. W75 is the live case.
 */
const EMITTING_SERVICE: Record<string, string> = {
  "messaging.projector.push": "messaging-projector",
  "openrouter.settlement.overshoot_microcents": "openrouter-settlement",
  "openrouter.gate.account_refused": "openrouter-gate",
  "network.member_key_contested": "hermes-bridge",
};
const DEFAULT_EMITTING_SERVICE = "lightbulb-backend";

type MetricBinding = {
  alarm: string;
  metricName: string;
  service: string | undefined;
  extraDims: string[];
};

/**
 * Every Isol8-namespace metric an alarm binds, from BOTH shapes CloudFormation
 * renders: a plain metric alarm (Namespace/MetricName/Dimensions at the top
 * level) and a metric-math alarm (nested under Metrics[].MetricStat.Metric —
 * where W1 lives). Search-expression alarms carry no MetricStat and are
 * skipped: they match on dimension VALUES, so there is no set to check.
 */
function isol8MetricBindings(template: Template): MetricBinding[] {
  const bindings: MetricBinding[] = [];
  for (const [logicalId, res] of Object.entries(
    template.findResources("AWS::CloudWatch::Alarm"),
  ) as [string, any][]) {
    const props = res.Properties ?? {};
    const alarm =
      typeof props.AlarmName === "string" ? props.AlarmName : logicalId;
    const collect = (namespace: any, metricName: any, dimensions: any) => {
      if (namespace !== "LightbulbAI" || typeof metricName !== "string") return;
      bindings.push({
        alarm,
        metricName,
        service: (dimensions ?? []).find((d: any) => d.Name === "service")
          ?.Value,
        extraDims: (dimensions ?? [])
          .map((d: any) => d.Name)
          .filter((k: string) => k !== "env" && k !== "service")
          .sort(),
      });
    };
    collect(props.Namespace, props.MetricName, props.Dimensions);
    for (const m of props.Metrics ?? []) {
      const metric = m.MetricStat?.Metric;
      if (metric) collect(metric.Namespace, metric.MetricName, metric.Dimensions);
    }
  }
  return bindings;
}

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
    // 76 alarms: 11 page + 22 warn custom (28 minus the 7 gateway-pool-era ids
    // deleted in Task 10, plus W77-messaging-rpc-errors added in the
    // whole-branch review fix round) + 3 x 7 per-table DDB + remaining
    // AWS-native + 2 cost
    expect(count).toBeGreaterThanOrEqual(65);
  });

  test("P35 pages on the bridge's member-key-contested stream", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-P35-network-member-key-contested",
      Namespace: "LightbulbAI",
      MetricName: "network.member_key_contested",
      Threshold: 1,
      Period: 600,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      Dimensions: Match.arrayWith([
        Match.objectLike({ Name: "service", Value: "hermes-bridge" }),
      ]),
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
    });
  });

  test("creates 1 CloudWatch dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
  });

  // GuardDuty + Access Analyzer already exist at account level — not created by CDK.
  // We only create EventBridge rules to route their findings to SNS. Both
  // rules are PROD-GATED (see ObservabilityStack.createAccountHardening) —
  // account-wide findings, so dev doesn't get a duplicate copy of them.
  test("creates EventBridge rules for security findings (dev, no account-wide rules)", () => {
    // ECS TaskStopped (W39) + the hourly alert-subscription monitor.
    // GuardDuty + Access Analyzer are prod-only.
    template.resourceCountIs("AWS::Events::Rule", 2);
  });

  // The $1,500 account-wide Budget is PROD-GATED (see createCostAlarms) —
  // dev and prod share one AWS account, so an ungated budget would render
  // twice and double-email admin@ for the same account-wide spend number.
  test("does NOT create an AWS Budget in dev", () => {
    template.resourceCountIs("AWS::Budgets::Budget", 0);
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
      expect(s.endpoint).toBe(`admin@${PLATFORM_MAIL_DOMAIN}`);
    }
  });

  // The only outside-in signal in the stack. Every other alarm is internal
  // and can look healthy while users cannot reach the product at all.
  test("creates Route53 health checks for the app and API front doors", () => {
    const checks = template.findResources("AWS::Route53::HealthCheck");
    const hosts = Object.values(checks).map(
      (c: any) => c.Properties.HealthCheckConfig.FullyQualifiedDomainName,
    );
    expect(hosts.sort()).toEqual([`api-dev.${ROOT_DOMAIN}`, `dev.${ROOT_DOMAIN}`]);

    for (const c of Object.values(checks) as any[]) {
      expect(c.Properties.HealthCheckConfig.Type).toBe("HTTPS");
    }
  });

  test("uptime alarms treat missing data as breaching", () => {
    // Route53 publishes HealthCheckStatus every minute unconditionally, so
    // absent data means the check is gone — indistinguishable from an
    // outage, and must never be read as healthy.
    for (const alarmName of [
      "lightbulb-dev-P13-app-unreachable",
      "lightbulb-dev-P14-api-unreachable",
    ]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: alarmName,
        ComparisonOperator: "LessThanThreshold",
        TreatMissingData: "breaching",
      });
    }
  });

  test("no alarm is left bound to a per-user ECS/EFS metric", () => {
    // Every emitter under core/containers/ went with the runtime, so an alarm
    // on one of these could never fire again.
    const rendered = JSON.stringify(template.findResources("AWS::CloudWatch::Alarm"));
    for (const dead of [
      "container.error_state",
      "container.efs.access_point",
      "container.task_def.register",
      "container.lifecycle.latency",
      "workspace.file.write.error",
      "workspace.path_traversal.attempt",
      "AWS/EFS",
    ]) {
      expect(rendered, `alarm still bound to ${dead}`).not.toContain(dead);
    }
  });

  // P15 exists because its exact absence let a 30-minute single-owner outage
  // page nobody: the inline-timeout emission landed on a dimension stream no
  // alarm watched. Pin the binding (metric + status dimension), not just the
  // name, so the alarm cannot silently drift off the emitter's stream.
  test("P15 pages on the inline-provision-timeout stream", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-P15-provision-inline-timeout",
      Namespace: "LightbulbAI",
      MetricName: "container.provision",
      Threshold: 0,
      Period: 60,
      Dimensions: Match.arrayWith([
        Match.objectLike({ Name: "status", Value: "inline_timeout" }),
      ]),
    });
  });

  // The company Stripe create door is live in prod and owner-accepted, but
  // was otherwise silent — this is how we learn when a real owner first
  // mints a company Stripe account through it.
  // WARNS rather than pages, on purpose: minting a company Stripe account is a
  // desirable business event that recurs on every future success, and an alarm
  // that pages on success is how a team learns to ignore pages. The topic is
  // asserted so that intent cannot be flipped back silently.
  test("P16 warns (does not page) when a company Stripe account is minted", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-P16-company-stripe-account-created",
      Namespace: "LightbulbAI",
      MetricName: "company_stripe.account.created",
      Threshold: 0,
      ComparisonOperator: "GreaterThanThreshold",
      TreatMissingData: "notBreaching",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsWarn") }],
    });
  });

  // The prod subscriptions CloudFormation believed it owned had been deleted
  // by SNS, and CFN still reported them CREATE_COMPLETE — only a NEW construct
  // id recreates them. Pin the ids so a later "tidy up" rename does not
  // silently recreate the same dead-letterbox situation.
  test("alert subscriptions use the recreated V3 construct ids", () => {
    const logicalIds = Object.keys(
      template.findResources("AWS::SNS::Subscription"),
    );
    for (const id of ["AlertsPageEmailV3", "AlertsWarnEmailV3"]) {
      // CDK appends a uniqueness hash to the logical id, so match the prefix.
      expect(logicalIds.some((logicalId) => logicalId.startsWith(id))).toBe(
        true,
      );
    }
  });

  // Nothing else watches whether the alert topics have a live destination, and
  // the failure mode is total silence, so missing monitor data must read as
  // failure rather than health.
  test("subscription-confirmation alarms exist and treat missing data as breaching", () => {
    for (const [name, tier] of [
      ["lightbulb-dev-P17-alert-subscriptions-page-topic", "page"],
      ["lightbulb-dev-P18-alert-subscriptions-warn-topic", "warn"],
    ]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: name,
        Namespace: "LightbulbAI/Alerting",
        MetricName: "SubscriptionsConfirmed",
        ComparisonOperator: "LessThanThreshold",
        TreatMissingData: "breaching",
        EvaluationPeriods: 2,
        Period: 3600,
        // Dev has only the mailbox on both tiers.
        Threshold: 1,
        Dimensions: [{ Name: "topic", Value: tier }],
        // Both page: a warn-tier alarm about a dead warn topic would deliver
        // to the topic it is reporting on.
        AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
      });
    }
  });

  // The consumer is a Lambda; per-message failures are batch items, so Errors
  // means the function cannot run at all.
  test("messaging work consumer pages on Lambda errors", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-P19-messaging-worker-errors",
      Namespace: "AWS/Lambda",
      MetricName: "Errors",
      Dimensions: [{ Name: "FunctionName", Value: "lightbulb-dev-openclaw-work-consumer" }],
      Statistic: "Sum",
      Period: 60,
      Threshold: 1,
      EvaluationPeriods: 5,
      DatapointsToAlarm: 3,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      TreatMissingData: "notBreaching",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
    });
  });

  test("warns at 70% of the GPT-Live concurrent-session tier", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-W84-voice-sessions-near-tier",
      Namespace: "LightbulbAI",
      MetricName: "voice.calls.open",
      Statistic: "Maximum",
      Period: 60,
      Threshold: 17.5,
      EvaluationPeriods: 3,
      DatapointsToAlarm: 2,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsWarn") }],
    });
  });

  // The messaging table is the product's busiest table and had no DynamoDB
  // alarm at all until it joined the shared table set.
  test("the messaging table is in the DynamoDB alarm set", () => {
    for (const prefix of ["W40-ddb-read", "W41-ddb-write", "W42-ddb-errors"]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: `lightbulb-dev-${prefix}-messaging`,
        Namespace: "AWS/DynamoDB",
        Dimensions: [
          { Name: "TableName", Value: `${tablePrefix("dev")}messaging` },
        ],
      });
    }
  });

  test("dashboard has correct name", () => {
    template.hasResourceProperties("AWS::CloudWatch::Dashboard", {
      DashboardName: "lightbulb-dev-orr",
    });
  });

  test("ALB host-count alarms (W28/W48) carry BOTH LoadBalancer and TargetGroup dimensions", () => {
    // HealthyHostCount / UnHealthyHostCount are per-target-group metrics: an
    // alarm scoped to LoadBalancer alone receives no datapoints, so W48
    // (TreatMissingData=breaching) sits in ALARM forever. Guard both alarms so
    // the TargetGroup dimension cannot be dropped again.
    for (const alarmName of [
      "lightbulb-dev-W48-alb-healthy-hosts-drop",
      "lightbulb-dev-W28-alb-unhealthy-hosts",
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

  test("no Paperclip alarms or Aurora rule remain (Paperclip exit)", () => {
    for (const t of [template, buildTemplate("prod")]) {
      const names = Object.values(t.findResources("AWS::CloudWatch::Alarm")).map((a: any) => a.Properties.AlarmName);
      expect(names.filter((n) => /paperclip|nightly-ceo|agent-run-failure-storm|aurora/.test(n))).toEqual([]);
      const rendered = JSON.stringify(t.toJSON());
      expect(rendered).not.toContain("AWS/RDS");
      expect(rendered).not.toContain("aws.rds");
      expect(rendered).not.toContain("nightly_ceo.run");
    }
  });

  test("W85 warns on ERROR lines from the voice routers", () => {
    template.hasResourceProperties("AWS::Logs::MetricFilter", {
      LogGroupName: "/ecs/isol8-dev",
      FilterPattern: '{ $.level = "ERROR" && $.logger = "routers.voice*" }',
      MetricTransformations: [
        Match.objectLike({
          MetricNamespace: "LightbulbAI/Voice/dev",
          MetricName: "voice.errors",
        }),
      ],
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-W85-voice-errors",
      Namespace: "LightbulbAI/Voice/dev",
      MetricName: "voice.errors",
      Threshold: 3,
      TreatMissingData: "notBreaching",
      AlarmActions: [Match.anyValue()],
    });
  });

  // -------------------------------------------------------------------
  // Alarm <-> emitter dimension contract
  // -------------------------------------------------------------------

  test("every Isol8-namespace alarm binds a dimension set an emitter publishes", () => {
    const bindings = isol8MetricBindings(template);
    // Anti-vacuity: a CloudFormation shape change that stops the walker from
    // finding anything would otherwise make this test pass on an empty list.
    expect(bindings.length).toBeGreaterThan(30);

    const offenders = bindings
      .map((b) => {
        const emitted = EMITTED_DIMENSION_SETS[b.metricName];
        if (!emitted) {
          return `${b.alarm}: no emitter registered for "${b.metricName}"`;
        }
        // The [env, service] rollup is emitted for every metric, so an alarm
        // that adds no dimensions of its own is always watching real data.
        if (b.extraDims.length === 0) return null;
        // Likewise a per-dimension rollup, emitted for any metric whose full
        // set carries one of ROLLUP_DIMENSIONS. Mirrors the emitter's
        // condition exactly: the rollup exists if and only if the emitter
        // tags that dimension.
        if (
          b.extraDims.length === 1 &&
          ROLLUP_DIMENSIONS.includes(b.extraDims[0]) &&
          emitted.some((set) => set.includes(b.extraDims[0]))
        ) {
          return null;
        }
        const matches = emitted.some(
          (set) => [...set].sort().join(",") === b.extraDims.join(","),
        );
        return matches
          ? null
          : `${b.alarm}: binds "${b.metricName}" {${b.extraDims.join(", ")}} ` +
              `but emitters publish ${JSON.stringify(emitted)}`;
      })
      .filter(Boolean);

    expect(offenders).toEqual([]);
  });

  // The dimension-SET contract above is blind to this: a wrong `service` VALUE
  // is a correctly-shaped alarm that receives nothing forever. W75 watches the
  // projector Lambda, which emits under its own service name.
  test("every alarm binds the service dimension value its emitter publishes", () => {
    const offenders = isol8MetricBindings(template)
      .filter(
        (b) =>
          b.service !==
          (EMITTING_SERVICE[b.metricName] ?? DEFAULT_EMITTING_SERVICE),
      )
      .map(
        (b) =>
          `${b.alarm}: binds "${b.metricName}" with service=${b.service}, ` +
          `emitter publishes ${EMITTING_SERVICE[b.metricName] ?? DEFAULT_EMITTING_SERVICE}`,
      );
    expect(offenders).toEqual([]);

    // Anti-vacuity, and the point of the whole test: the non-default case is
    // actually present, so the map is not dead weight.
    expect(
      isol8MetricBindings(template).filter(
        (b) => b.service === "messaging-projector",
      ),
    ).toHaveLength(1);
  });

  // The LightbulbAI dimension-contract test above cannot catch this: it only
  // covers our own namespace. P10 (PAGE) and W30 read 5XXError/4XXError/Count on
  // the WS ApiId — REST-API metrics a WebSocket API never publishes (it emits
  // only ClientError/ExecutionError/IntegrationError/IntegrationLatency/
  // ConnectCount/MessageCount), so both alarms could never fire. Guard every
  // alarm, both envs, so a nonexistent AWS-native metric can't slip back in.
  test("no alarm reads a WS-API metric that does not exist (P10/W30 blind guard)", () => {
    const FORBIDDEN = new Set(["5XXError", "4XXError", "Count"]);
    for (const env of ["dev", "prod"] as const) {
      const alarms = buildTemplate(env).findResources("AWS::CloudWatch::Alarm");
      const offenders: string[] = [];
      for (const [logicalId, res] of Object.entries(alarms) as [string, any][]) {
        const props = res.Properties ?? {};
        const check = (ns: any, mn: any) => {
          if (ns === "AWS/ApiGateway" && FORBIDDEN.has(mn)) {
            offenders.push(`${env}:${logicalId}:${mn}`);
          }
        };
        check(props.Namespace, props.MetricName);
        for (const m of props.Metrics ?? []) {
          if (m.MetricStat?.Metric) {
            check(m.MetricStat.Metric.Namespace, m.MetricStat.Metric.MetricName);
          }
        }
      }
      expect(offenders).toEqual([]);
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

  // The construct is named BackendLivenessAlarm; the LOGICAL id must stay the
  // one already deployed, hash and all. A CloudWatch alarm's physical id is its
  // AlarmName — unchanged here — so a different logical id makes CloudFormation
  // create the alarm and then delete it again in the cleanup phase, taking the
  // live page-tier liveness alarm with it. The hash is not decoration: CDK
  // derives `<construct id><path hash>`, so pinning the bare
  // "ReaperHeartbeatAlarm" would cause the very deletion the pin prevents.
  test("the backend-liveness alarm keeps its deployed logical id", () => {
    for (const env of ["dev", "prod"] as const) {
      expect(
        Object.keys(
          buildTemplate(env).findResources("AWS::CloudWatch::Alarm"),
        ),
      ).toContain("ReaperHeartbeatAlarm31FA055D");
    }
  });

  test("P12 divides the heartbeat by the live instance count", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-P12-backend-liveness",
      ComparisonOperator: "LessThanThreshold",
      Threshold: 8,
      TreatMissingData: "breaching",
      Metrics: Match.arrayWith([
        Match.objectLike({ Id: "beatsPerInstance", Expression: "beats / hosts" }),
        Match.objectLike({
          Id: "beats",
          MetricStat: Match.objectLike({
            Stat: "SampleCount",
            Metric: Match.objectLike({
              Namespace: "LightbulbAI",
              MetricName: "gateway.running.count",
            }),
          }),
        }),
        Match.objectLike({
          Id: "hosts",
          MetricStat: Match.objectLike({
            Stat: "Minimum",
            Metric: Match.objectLike({
              Namespace: "AWS/ApplicationELB",
              MetricName: "HealthyHostCount",
            }),
          }),
        }),
      ]),
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

describe("ObservabilityStack — watchdog alarms P48-P51 (dev + prod, spec 11)", () => {
  const dev = buildTemplate("dev");
  const alarm = (id: string) =>
    (Object.values(dev.findResources("AWS::CloudWatch::Alarm")) as any[])
      .map((a) => a.Properties)
      .find((p) => String(p.AlarmName).startsWith(`lightbulb-dev-${id}-`));

  test("dev synth has P48-P51 with thresholds, all warn", () => {
    const p48 = alarm("P48");
    expect(p48).toMatchObject({
      AlarmName: "lightbulb-dev-P48-watchdog-silent",
      MetricName: "watchdog.tick",
      Statistic: "SampleCount",
      Threshold: 0,
      Period: 900,
      EvaluationPeriods: 1,
      ComparisonOperator: "LessThanOrEqualToThreshold",
      TreatMissingData: "breaching",
    });
    expect(alarm("P49")).toMatchObject({ MetricName: "watchdog.write", Threshold: 300, Period: 3600 });
    expect(alarm("P50")).toMatchObject({ MetricName: "watchdog.cap_hit", Threshold: 0, Period: 3600 });
    expect(alarm("P51")).toMatchObject({ MetricName: "watchdog.judge.cost", Threshold: 5, Period: 86400 });
    for (const id of ["P49", "P50", "P51"]) {
      expect(alarm(id).ComparisonOperator).toBe("GreaterThanThreshold");
      expect(alarm(id).TreatMissingData).toBe("notBreaching");
    }
    for (const id of ["P48", "P49", "P50", "P51"]) {
      expect(alarm(id).AlarmActions).toEqual([{ Ref: expect.stringMatching(/^AlertsWarn/) }]);
      const dims = Object.fromEntries(alarm(id).Dimensions.map((d: any) => [d.Name, d.Value]));
      expect(dims).toEqual({ env: "dev", service: "lightbulb-backend" });
    }
  });

  test("prod synth has the watchdog alarms", () => {
    expect(JSON.stringify(PROD_TEMPLATE.findResources("AWS::CloudWatch::Alarm"))).toMatch(/watchdog/i);
  });
});

describe("ObservabilityStack — CloudWatch alarms can publish", () => {
  // A CloudWatch alarm action publishes as the CloudWatch SERVICE principal.
  // Attaching an EventBridge rule to a topic makes CDK emit an
  // AWS::SNS::TopicPolicy, which REPLACES the topic's default account-owner
  // statement — so without an explicit grant the alarm silently fails its
  // action with `CloudWatch Alarms is not authorized to perform: SNS:Publish`.
  // A failed alarm action is not itself an alarm, so nothing surfaces it.
  test.each(["dev", "prod"] as const)(
    "%s: both alert topics grant CloudWatch sns:Publish",
    (env) => {
      const statements = Object.values(
        buildTemplate(env).findResources("AWS::SNS::TopicPolicy"),
      ).flatMap((p: any) => p.Properties.PolicyDocument.Statement ?? []);

      const grants = statements.filter(
        (st: any) =>
          st.Principal?.Service === "cloudwatch.amazonaws.com" &&
          st.Action === "sns:Publish" &&
          st.Effect === "Allow",
      );

      // One for the page topic, one for the warn topic.
      expect(grants).toHaveLength(2);
      for (const grant of grants) {
        expect(grant.Condition.StringEquals["AWS:SourceAccount"]).toEqual({
          Ref: "AWS::AccountId",
        });
      }
    },
  );

  // THIS is the test that matters. A previous attempt at this fix hard-coded
  // an `isol8-${env}-*` source-ARN pattern AND hard-coded the same string in
  // its assertion. The rebrand later renamed every alarm to `lightbulb-*`, the
  // grant stopped matching anything, and the test stayed green — a passing
  // suite blessing a permission that granted nothing.
  //
  // So do not assert a literal prefix here. Derive it from the alarm names the
  // template actually renders, so any future rename that misses one side fails
  // this test instead of silently disarming production alerting.
  test.each(["dev", "prod"] as const)(
    "%s: the granted alarm-ARN pattern actually matches the rendered alarm names",
    (env) => {
      const built = buildTemplate(env);

      const patterns = Object.values(
        built.findResources("AWS::SNS::TopicPolicy"),
      )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement ?? [])
        .filter(
          (st: any) => st.Principal?.Service === "cloudwatch.amazonaws.com",
        )
        .map((st: any) => {
          const join = st.Condition.ArnLike["AWS:SourceArn"]["Fn::Join"][1];
          const tail = join[join.length - 1] as string;
          // e.g. ":alarm:lightbulb-prod-*" -> "lightbulb-prod-"
          const match = /:alarm:(.*)\*$/.exec(tail);
          expect(match).not.toBeNull();
          return match![1];
        });

      expect(patterns.length).toBeGreaterThan(0);

      const alarmNames = Object.values(
        built.findResources("AWS::CloudWatch::Alarm"),
      ).map((a: any) => a.Properties.AlarmName as string);

      // Every alarm this stack creates must fall inside every granted pattern.
      expect(alarmNames.length).toBeGreaterThan(0);
      for (const prefix of patterns) {
        const outside = alarmNames.filter((n) => !n.startsWith(prefix));
        expect(outside).toEqual([]);
      }
    },
  );
});

describe("alert topic grant covers alarms in EVERY stack", () => {
  // The grant lives in observability-stack, but other stacks import these
  // topics by ARN and attach alarm actions (service, agent-mail-outbound). A test that only inspected
  // observability-stack's own alarms would not notice those drifting outside
  // the granted ARN pattern — #1947's failure relocated one stack over.
  //
  // The invariant is the PAIRING: an alarm publishing to some env's alert
  // topic must fall inside the ARN prefix that env's topic policy grants.
  test.each(["dev", "prod"] as const)(
    "%s stage: every alarm is inside the granted prefix of the topic it pages",
    (envName) => {
      const app = new cdk.App();
      const stage = new LightbulbStage(app, envName, {
        environment: envName,
        env: { account: "123456789012", region: "us-east-1" },
      } as any);

      const alarms = stage.node
        .findAll()
        .filter((c): c is cdk.Stack => c instanceof cdk.Stack)
        .flatMap((stack) =>
          Object.values(
            Template.fromStack(stack).findResources("AWS::CloudWatch::Alarm"),
          ),
        );
      expect(alarms.length).toBeGreaterThan(0);

      // "arn:aws:sns:<region>:<account>:lightbulb-<env>-alerts-<page|warn>"
      const topicEnv = /:([a-z0-9-]+)-alerts-(?:page|warn)$/;

      const mismatched: string[] = [];
      let checked = 0;
      for (const alarm of alarms as any[]) {
        const name = alarm.Properties?.AlarmName;
        if (typeof name !== "string") continue;
        const actions = [
          ...(alarm.Properties?.AlarmActions ?? []),
          ...(alarm.Properties?.OKActions ?? []),
        ];
        for (const action of actions) {
          if (typeof action !== "string") continue; // same-stack Ref
          const match = topicEnv.exec(action);
          if (!match) continue;
          checked += 1;
          // match[1] is "lightbulb-dev" / "lightbulb-prod"; the granted prefix
          // for that env is resourcePrefix(env) === `${match[1]}-`.
          if (!name.startsWith(`${match[1]}-`)) {
            mismatched.push(`${name} -> ${action}`);
          }
        }
      }

      // Guard the guard: if nothing resolved to a literal topic ARN the loop
      // above would vacuously pass.
      expect(checked).toBeGreaterThan(0);
      expect(mismatched).toEqual([]);
    },
  );
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

  // Account-wide resources (GuardDuty/Access Analyzer routing rules, the
  // $1,500 Budget) are PROD-GATED — dev and prod share one AWS account, so
  // these render exactly once, on the prod stack.
  test("prod creates EventBridge rules for security findings", () => {
    // ECS TaskStopped (W39) + the alert-subscription monitor + GuardDuty +
    // Access Analyzer.
    template.resourceCountIs("AWS::Events::Rule", 4);
  });

  test("prod creates the account-wide AWS Budget", () => {
    template.resourceCountIs("AWS::Budgets::Budget", 1);
  });

  test("prod uptime alarms page rather than warn", () => {
    for (const name of [
      "lightbulb-prod-P13-app-unreachable",
      "lightbulb-prod-P14-api-unreachable",
    ]) {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: name,
        AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
      });
    }
  });

  // CloudWatch rejects SEARCH() on alarms outright -- "SEARCH is not supported
  // on Metric Alarms" -- so a SEARCH expression here is not a degraded alarm,
  // it is a stack that fails to deploy. It shipped once (three alarms), rolled
  // isol8-dev-observability back on every deploy, and because the prod stages
  // depend on dev succeeding it silently blocked prod deploys entirely.
  //
  // Asserted across BOTH environments and every alarm, not just the three that
  // regressed: the cost of a wrong answer here is the whole pipeline, and
  // nothing else in CI deploys a template to find out.
  test("no alarm uses a SEARCH expression, in either environment", () => {
    for (const env of ["dev", "prod"] as const) {
      const alarms = Object.entries(
        buildTemplate(env).findResources("AWS::CloudWatch::Alarm"),
      );
      // Anti-vacuity, same reasoning as the dimension-contract test below: this
      // only inspects alarms built from metric math, so if none existed (or the
      // rendered shape changed) it would pass by looking at nothing at all.
      const withExpressions = alarms.filter(([, alarm]: [string, any]) =>
        (alarm.Properties?.Metrics ?? []).some((m: any) => typeof m?.Expression === "string"),
      );
      expect(withExpressions.length).toBeGreaterThan(0);

      const offenders = alarms
        .filter(([, alarm]: [string, any]) =>
          (alarm.Properties?.Metrics ?? []).some((m: any) =>
            typeof m?.Expression === "string" && m.Expression.includes("SEARCH("),
          ),
        )
        .map(([logicalId]) => `${env}:${logicalId}`);
      expect(offenders).toEqual([]);
    }
  });

  test("prod health checks target the real public hostnames", () => {
    const hosts = Object.values(template.findResources("AWS::Route53::HealthCheck"))
      .map((c: any) => c.Properties.HealthCheckConfig.FullyQualifiedDomainName);
    expect(hosts.sort()).toEqual([`api.${ROOT_DOMAIN}`, ROOT_DOMAIN]);
  });

  // W79 (warn, <$5) + P25 (page, <$1) — dev and prod share ONE OpenRouter
  // account, so these are account-wide and PROD-GATED like the Bedrock/Budget
  // alarms above (an ungated pair would double-page on the same balance).
  test("prod pages on OpenRouter balance <$1 and warns <$5", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-prod-W79-openrouter-balance-low",
      Namespace: "LightbulbAI",
      MetricName: "openrouter.account.balance_usd",
      Threshold: 5,
      ComparisonOperator: "LessThanThreshold",
      TreatMissingData: "notBreaching",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsWarn") }],
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-prod-P25-openrouter-balance-critical",
      Namespace: "LightbulbAI",
      MetricName: "openrouter.account.balance_usd",
      Threshold: 1,
      ComparisonOperator: "LessThanThreshold",
      TreatMissingData: "notBreaching",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
    });
  });

  test.each(["dev", "prod"] as const)("%s warns when the OpenRouter settlement worker keeps failing", (env) => {
    buildTemplate(env).hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `lightbulb-${env}-W80-openrouter-settlement-errors`,
      Namespace: "AWS/Lambda",
      MetricName: "Errors",
      Dimensions: [{ Name: "FunctionName", Value: `lightbulb-${env}-openrouter-settlement` }],
      Threshold: 3,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsWarn") }],
    });
  });

  test.each(["dev", "prod"] as const)("%s warns when OpenRouter settlement overshoots its reservation", (env) => {
    buildTemplate(env).hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `lightbulb-${env}-W81-openrouter-settlement-overshoot`,
      Namespace: "LightbulbAI",
      MetricName: "openrouter.settlement.overshoot_microcents",
      Dimensions: Match.arrayWith([{ Name: "service", Value: "openrouter-settlement" }]),
      Threshold: 0,
      ComparisonOperator: "GreaterThanThreshold",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsWarn") }],
    });
  });

  test("dev does NOT get the OpenRouter balance alarms", () => {
    const rendered = JSON.stringify(
      buildTemplate("dev").findResources("AWS::CloudWatch::Alarm"),
    );
    expect(rendered).not.toContain("openrouter.account.balance_usd");
  });

  test("P29 pages on 2+ free-tier first-turn failures in 2 h", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-prod-P29-free-tier-first-turn-failures",
      MetricName: "messaging.run.first_turn_failed",
      Period: 7200,
      Threshold: 2,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
    });
  });

  test("P26 pages when most Jev decisions fail in an hour, from 3 decisions up", () => {
    // Prod peaks at 12-29 decisions per hour: a floor above that never fires (2026-09-27).
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-prod-P26-jev-decision-error-rate",
      Threshold: 50,
      AlarmActions: [{ Ref: Match.stringLikeRegexp("^AlertsPage") }],
      Metrics: Match.arrayWith([
        Match.objectLike({
          Expression: "IF(all >= 3, 100 * (FILL(e, 0) + FILL(f, 0) + FILL(t, 0) + FILL(r, 0)) / all, 0)",
        }),
        Match.objectLike({ Id: "all", MetricStat: Match.objectLike({ Period: 3600 }) }),
        // The gateway's 403 (the 2026-09-27 outage) is metered as "forbidden", not "error".
        Match.objectLike({
          Id: "f",
          MetricStat: Match.objectLike({
            Metric: Match.objectLike({
              Dimensions: Match.arrayWith([{ Name: "outcome", Value: "forbidden" }]),
            }),
          }),
        }),
      ]),
    });
  });
});
