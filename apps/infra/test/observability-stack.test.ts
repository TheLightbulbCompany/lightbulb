import {
  resourcePrefix,
  tablePrefix,
} from "../lib/table-names";
import { LightbulbStage } from "../lib/lightbulb-stage";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as rds from "aws-cdk-lib/aws-rds";
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
        usersTable: `${tablePrefix(envName)}users`,
        containersTable: `${tablePrefix(envName)}containers`,
        billingTable: `${tablePrefix(envName)}billing-accounts`,
        usageCountersTable: `${tablePrefix(envName)}usage-counters`,
        pendingUpdatesTable: `${tablePrefix(envName)}pending-updates`,
        channelLinksTable: `${tablePrefix(envName)}channel-links`,
        companySlugsTable: `${tablePrefix(envName)}company-slugs`,
        agentEmailAddressesTable: `${tablePrefix(envName)}agent-email-addresses`,
      },
      connectionsTableName: `${tablePrefix(envName)}ws-connections`,
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
  "chat.e2e.latency": [[]],
  "chat.error": [["reason", "reclassified"]],
  "chat.message.count": [[]],
  "chat.session_usage.fetch.error": [[]],
  "company_stripe.account.created": [[]],
  "container.efs.access_point": [["op", "status"]],
  "container.error_state": [[]],
  "container.lifecycle.latency": [["op"]],
  "container.provision": [["status"]],
  "container.task_def.register": [["status"]],
  "debug.endpoint.prod_hit": [["endpoint"]],
  "gateway.cold_start.count": [["outcome"]],
  "gateway.cold_start.latency": [[]],
  "gateway.connection": [["event"], ["active_runs", "event"], ["error_class", "event"]],
  "gateway.connection.open": [[]],
  "gateway.frontend.prune": [[]],
  "gateway.health_check.timeout": [[]],
  "gateway.rpc.error": [["method"]],
  "gateway.running.count": [[]],
  "marketplace.path_traversal_attempt": [[]],
  "metronome.gate.refresh_error": [[]],
  "metronome.webhook": [["result"], ["blocked", "result"]],
  "operator.upstream.closed": [[]],
  "paperclip.upstream.error": [["kind"]],
  "provision.gate.blocked": [["code"]],
  "stripe.api.latency": [["op"]],
  "stripe.webhook.received": [["event_type"]],
  "stripe.webhook.sig_fail": [[]],
  "update.fleet_patch.invoked": [[]],
  "update.scheduled_worker.error": [[]],
  "update.scheduled_worker.heartbeat": [[]],
  "webhook.clerk.sig_fail": [[]],
  "workspace.file.write.error": [[]],
  "workspace.path_traversal.attempt": [[]],
};

type MetricBinding = { alarm: string; metricName: string; extraDims: string[] };

/**
 * Every Isol8-namespace metric an alarm binds, from BOTH shapes CloudFormation
 * renders: a plain metric alarm (Namespace/MetricName/Dimensions at the top
 * level) and a metric-math alarm (nested under Metrics[].MetricStat.Metric —
 * where W1 and W10 live). Search-expression alarms carry no MetricStat and are
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
    // 82 alarms: 11 page + 28 warn custom + 3 x 7 per-table DDB
    // + remaining AWS-native + 2 cost
    expect(count).toBeGreaterThanOrEqual(65);
  });

  test("creates 1 CloudWatch dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
  });

  // GuardDuty + Access Analyzer already exist at account level — not created by CDK.
  // We only create EventBridge rules to route their findings to SNS. Both
  // rules are PROD-GATED (see ObservabilityStack.createAccountHardening) —
  // account-wide findings, so dev doesn't get a duplicate copy of them.
  test("creates EventBridge rules for security findings (dev, no account-wide rules)", () => {
    // ECS TaskStopped (W39) + ECS TaskRunning (W59 multi-task detector) +
    // Aurora failover/failure (W61b). GuardDuty + Access Analyzer are prod-only.
    template.resourceCountIs("AWS::Events::Rule", 3);
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

  test("page-tier alarm P1 exists with correct name", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-P1-container-error-state",
    });
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

  // W65 is the availability half of "is Paperclip up?" — W63 only fires when a
  // real backend call fails, so it is blind to an outage in a quiet window.
  // Every property below is load-bearing; see the comment block on the alarm:
  //   - LiveTaskCount, NOT RunningTaskCount. The latter is an
  //     ECS/ContainerInsights metric and Container Insights is off on this
  //     cluster, so it would never receive a datapoint.
  //   - TreatMissingData=breaching. ECS stops publishing LiveTaskCount at zero
  //     tasks instead of reporting 0, so absence IS the outage. Flipping this
  //     to notBreaching silently restores the blind spot the alarm closes.
  //   - Both ClusterName and ServiceName. LiveTaskCount is per-service; an
  //     alarm scoped to the cluster alone would read the whole per-user fleet.
  test("W65 alarms on the Paperclip service's native ECS task count", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-W65-paperclip-no-running-task",
      Namespace: "AWS/ECS",
      MetricName: "LiveTaskCount",
      Statistic: "Minimum",
      Period: 60,
      Threshold: 1,
      EvaluationPeriods: 3,
      ComparisonOperator: "LessThanThreshold",
      TreatMissingData: "breaching",
      Dimensions: Match.arrayWith([
        Match.objectLike({ Name: "ClusterName" }),
        Match.objectLike({ Name: "ServiceName" }),
      ]),
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
        // Likewise the [env, service, status] rollup, emitted for any metric
        // whose full set carries a status dimension (put_metric's
        // _STATUS_ROLLUP_KEYS). This is what lets an error alarm aggregate
        // across a qualifier it should not enumerate — every op, every
        // provider — without a SEARCH expression, which CloudWatch rejects on
        // alarms outright. Mirrors the emitter's condition exactly: the rollup
        // exists if and only if the emitter tags status.
        if (
          b.extraDims.length === 1 &&
          b.extraDims[0] === "status" &&
          emitted.some((set) => set.includes("status"))
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

  // The three alarms issue #1486 found dead. They are correct BECAUSE
  // put_metric emits the [env, service] rollup — pin the binding so a future
  // "let me add a dimension to this alarm" quietly kills them again.
  test("W2/W8/W10 watch the env+service rollup, with no extra dimensions", () => {
    const rollupOnly = isol8MetricBindings(template).filter(
      (b) =>
        ["container.lifecycle.latency", "gateway.rpc.error", "chat.error"].includes(
          b.metricName,
        ),
    );
    expect(rollupOnly.map((b) => b.metricName).sort()).toEqual([
      "chat.error",
      "container.lifecycle.latency",
      "gateway.rpc.error",
    ]);
    for (const b of rollupOnly) {
      expect(b.extraDims).toEqual([]);
    }
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

  test("creates the reaper heartbeat alarm on SampleCount(gateway.running.count)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "gateway.running.count",
      Namespace: "LightbulbAI",
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
  // The grant lives in observability-stack, but FOUR other stacks import these
  // topics by ARN and attach alarm actions: bedrock-gate, bedrock-meter,
  // service and agent-mail-outbound. A test that only inspected
  // observability-stack's own alarms would not notice those drifting outside
  // the granted ARN pattern — #1947's failure relocated one stack over.
  //
  // The invariant is NOT "every alarm matches its stage's prefix". The gate and
  // meter stacks deliberately render BOTH a dev and a prod instance from one
  // stage and wire each instance to ITS OWN env's topics, so a prod stage
  // legitimately contains `lightbulb-dev-*` alarms pointing at dev topics.
  //
  // The real invariant is the PAIRING: an alarm publishing to some env's alert
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
    // ECS TaskStopped (W39) + ECS TaskRunning (W59) + Aurora failover (W61b)
    // + GuardDuty + Access Analyzer.
    template.resourceCountIs("AWS::Events::Rule", 5);
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
});
