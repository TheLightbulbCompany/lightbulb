import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as logsDestinations from "aws-cdk-lib/aws-logs-destinations";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import * as cr from "aws-cdk-lib/custom-resources";
import { bothTableArns, legacyTablePrefix, tablePrefix } from "../table-names";
import { Construct } from "constructs";

// =============================================================================
// BedrockMeterStack — account-scoped Bedrock usage meter. ONE codebase -> TWO
// function instances (dev + prod). Paid owners are ingested to Metronome;
// TRIAL owners are priced locally, counted, and CUT OFF at the $3/day cap.
// =============================================================================
// The cap is the load-bearing part. The per-user container holds its own AWS
// credentials and calls Bedrock directly — the backend is not on the inference
// path — so the only thing that provably stops a free owner's spend is taking
// away the container's ability to invoke: delete the per-owner scoped inline
// IAM policy AND set its ECS service to desiredCount=0. Hence this Lambda's
// role carries iam:DeleteRolePolicy + ecs:UpdateService, tightly scoped to the
// per-owner role-name prefix and the per-user service-name prefix.
// =============================================================================
// Instantiated ONCE (prod stage only). Bedrock model-invocation logging is an
// ACCOUNT+REGION-level config (one per region) and there is ONE invocation-log
// group per region per account — so a per-env duplicate stack would collide,
// exactly like the Cost Anomaly Detection tombstone in
// observability-stack.ts. Instead, the single log group receives BOTH envs'
// records and the env split happens at the subscription filters: every record
// carries identity.arn = the per-owner role
// (...assumed-role/isol8-{env}-owner-bedrock-{owner}/...), so a CloudWatch
// term filter routes dev records to the dev Lambda (sandbox Metronome token)
// and prod records to the prod Lambda (live token). The two instances share
// one code asset and differ ONLY in env vars.
//
// No VPC anywhere: the Lambda talks only to Metronome, DynamoDB, SQS and
// Secrets Manager — all public HTTPS endpoints. (The VPC existed solely for
// the deleted internal-ALB hop to the backend.)
// =============================================================================

export interface BedrockMeterStackProps extends cdk.StackProps {
  /** Stage env name ("prod") — tagging only; both meter instances are fixed. */
  envName: string;
}

export class BedrockMeterStack extends cdk.Stack {
  public readonly meterFnDev: lambda.Function;
  public readonly meterFnProd: lambda.Function;
  public readonly invocationLogGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: BedrockMeterStackProps) {
    super(scope, id, props);

    // --- Bedrock model-invocation logging sink (this stack's region) ---
    // CloudWatch-ONLY. The meter reads everything it needs from the top level
    // of the CloudWatch record (token + cache counts are inline regardless of
    // body size), so there is no S3 sink and no largeDataDeliveryS3Config —
    // the meter never touches S3.
    // ENV-NEUTRAL NAME: this one group receives BOTH envs' records (the
    // logging config is account+region-level), so the old /isol8/prod/...
    // name was a prod-stage-instantiation artifact, not a statement of scope.
    // DESTROY (not RETAIN): the log group has a FIXED name, so a RETAIN policy
    // orphans it on any CREATE-rollback or stack delete, and the orphan then
    // blocks every subsequent CREATE with AlreadyExists (this bricked two prod
    // deploys). It is metering plumbing, not a system of record — the metering
    // happens in near-real-time via the subscription filters, and DESTROY only
    // acts on stack delete / CREATE-rollback (never on a steady-state UPDATE).
    // THREE_DAYS retention, deliberately short: text data delivery cannot be
    // disabled without blinding the meter (see the loggingConfig comment), so
    // every record carries full request/response bodies — live records were
    // observed containing a tenant's Paperclip API key and gateway JWT in
    // plaintext. The meter consumes each record within seconds (subscription
    // filter), and DLQ messages embed their own copy of the log event, so
    // nothing ever re-reads this group after delivery. Retention is purely a
    // debugging window; keep the credential-bearing history short.
    this.invocationLogGroup = new logs.LogGroup(this, "InvocationLogGroup", {
      logGroupName: "/isol8/bedrock-invocation",
      retention: logs.RetentionDays.THREE_DAYS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Bedrock's delivery role — assumed by the bedrock.amazonaws.com service
    // to write to the CloudWatch log group.
    const deliveryRole = new iam.Role(this, "BedrockLoggingRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    });
    this.invocationLogGroup.grantWrite(deliveryRole);

    // No L1 for PutModelInvocationLoggingConfiguration — use AwsCustomResource.
    //
    // textDataDeliveryEnabled MUST STAY TRUE. The modality flags select WHICH
    // MODELS get logged at all, not whether bodies are included: "Data will be
    // logged for all models that support the modalities ... that you choose"
    // (Bedrock user guide, model-invocation-logging). Deselecting Text stops
    // logging Claude/Converse invocations entirely — the meter goes blind, the
    // trial cap never fires, and paid usage stops ingesting. There is NO
    // metadata-only mode.
    //
    // The unavoidable consequence: records carry full request/response bodies
    // — observed live: a tenant's Paperclip API key and gateway JWT in
    // plaintext — so the log group is a credential-bearing surface. Exposure
    // is bounded by the log group's SHORT retention (see InvocationLogGroup):
    // the meter consumes each record within seconds via the subscription
    // filter, and DLQ messages embed their own copy of the log event, so
    // redrives never re-read the log group.
    const loggingConfig = {
      cloudWatchConfig: {
        logGroupName: this.invocationLogGroup.logGroupName,
        roleArn: deliveryRole.roleArn,
      },
      textDataDeliveryEnabled: true,
      embeddingDataDeliveryEnabled: true,
      imageDataDeliveryEnabled: false,
    };
    const loggingConfigCr = new cr.AwsCustomResource(this, "BedrockLoggingConfig", {
      onCreate: {
        service: "Bedrock",
        action: "putModelInvocationLoggingConfiguration",
        parameters: { loggingConfig },
        physicalResourceId: cr.PhysicalResourceId.of(
          `bedrock-logging-${this.region}`,
        ),
      },
      onUpdate: {
        service: "Bedrock",
        action: "putModelInvocationLoggingConfiguration",
        parameters: { loggingConfig },
        physicalResourceId: cr.PhysicalResourceId.of(
          `bedrock-logging-${this.region}`,
        ),
      },
      onDelete: {
        service: "Bedrock",
        action: "deleteModelInvocationLoggingConfiguration",
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            "bedrock:PutModelInvocationLoggingConfiguration",
            "bedrock:DeleteModelInvocationLoggingConfiguration",
            "bedrock:GetModelInvocationLoggingConfiguration",
          ],
          resources: ["*"], // account-level config — no resource ARN exists
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["iam:PassRole"],
          resources: [deliveryRole.roleArn],
        }),
      ]),
    });

    // ONE code asset shared by both function instances — the env split is
    // pure configuration, never code.
    const meterCode = lambda.Code.fromAsset(
      path.join(__dirname, "..", "..", "lambda", "bedrock-usage-meter"),
    );

    const fns: Record<string, lambda.Function> = {};
    const subscriptionFilters: logs.SubscriptionFilter[] = [];

    for (const meterEnv of ["dev", "prod"] as const) {
      // The prod instance keeps the construct ids from the original
      // single-function stack ("" suffix) so its fixed-name resources (the
      // DLQ queueName) update in place instead of CREATE-colliding with
      // themselves; the dev instance gets fresh "Dev"-suffixed ids.
      const idSuffix = meterEnv === "prod" ? "" : "Dev";

      // Fail-safe durability queue. A CloudWatch Logs subscription invokes the
      // meter Lambda SYNCHRONOUSLY, so Lambda's async-invocation DLQ (the
      // `deadLetterQueue` prop below) NEVER fires on a per-record failure — a
      // Metronome non-2xx, an unattributed record, or an unparseable record
      // would otherwise silently drop. So the Lambda code (index.handler)
      // explicitly `sqs:SendMessage`s each failed raw log event here (via
      // DLQ_URL) and continues. Redriving this queue is safe: Metronome dedups
      // on transaction_id (34-day window). We ALSO wire it as the Lambda's
      // async DLQ to catch the rare platform-level async failure path, but the
      // synchronous per-record sends are the load-bearing durability mechanism.
      const dlq = new sqs.Queue(this, `MeterDlq${idSuffix}`, {
        queueName: `isol8-${meterEnv}-bedrock-meter-dlq`,
        retentionPeriod: cdk.Duration.days(14),
      });

      // THE REPLAY GUARD for the trial counter. Metronome dedups the paid path
      // on transaction_id (34 days) — which is the ONLY reason CW-Logs batch
      // retries and DLQ redrives are safe to replay. A DynamoDB ADD has no such
      // dedup, so the daily-spend counter needs its own: a conditional PutItem
      // keyed on the Bedrock requestId. Without it a single batch retry
      // double-counts and false-locks-out a real user at half their real spend.
      //
      // TTL (see DEDUP_TTL_SECONDS in index.py) is 15 DAYS, deliberately LONGER
      // than this DLQ's 14-day retention. A shorter TTL does not "safely
      // under-count": once the claim row expires the conditional PutItem
      // succeeds again, so a redrive past the TTL RE-APPLIES the counter and
      // double-counts, false-locking a real user out early. Do not shorten it.
      // AWS-owned encryption, not the per-env CMK: the CMK is
      // created by each env's auth stack and its ARN is unknowable from this
      // prod-stage stack (cross-stage), and the rows hold nothing but a request
      // id and a timestamp. DESTROY for the same reason as the log group — a
      // RETAIN policy on a FIXED-name resource orphans it on any
      // CREATE-rollback and the orphan then blocks every subsequent CREATE with
      // AlreadyExists. This is 48h-ephemeral replay state, not a system of
      // record.
      const dedupTable = new dynamodb.Table(this, `MeterDedupTable${idSuffix}`, {
        tableName: `${tablePrefix(meterEnv)}bedrock-invocation-dedup`,
        partitionKey: {
          name: "request_id",
          type: dynamodb.AttributeType.STRING,
        },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        timeToLiveAttribute: "ttl",
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      // No explicit functionName: a fixed name would CREATE-before-DELETE
      // collide with the pre-rework function on the replacement update. CFN
      // names the functions; nothing references them by name (alarms and the
      // subscription filters use object refs).
      const fn = new lambda.Function(this, `MeterFn${idSuffix}`, {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "index.handler",
        code: meterCode,
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        deadLetterQueueEnabled: true,
        deadLetterQueue: dlq,
        environment: {
          // The synchronous per-record fail-safe sink the Lambda code sends to.
          DLQ_URL: dlq.queueUrl,
          // Per-env attribution table: aip_arn -> {owner_id, tier, plan}.
          AIP_OWNERS_TABLE: `${tablePrefix(meterEnv)}bedrock-aip-owners`,
          // Per-requestId replay guard for the trial counter.
          INVOCATION_DEDUP_TABLE: dedupTable.tableName,
          // The trial daily-spend counter lives on the billing row
          // (trial_daily_spend_microcents.{YYYY-MM-DD}); the revoked flag the
          // backend's daily restore reads lives on the containers row.
          BILLING_TABLE: `${tablePrefix(meterEnv)}billing-accounts`,
          CONTAINERS_TABLE: `${tablePrefix(meterEnv)}containers`,
          // The bedrock-gate's per-owner token table (owned by the gate stack;
          // ARN-by-convention like every other table here). The deny-flag kill
          // step flips denied=true on the owner's row so the gateway 403s the
          // cut-over owner — the gateway-path half of the kill.
          GATE_TOKENS_TABLE: `${tablePrefix(meterEnv)}bedrock-gate-tokens`,
          // The per-user ECS services live in the env's container-stack cluster,
          // whose physical name is CFN-generated (renaming it would REPLACE the
          // cluster and destroy every per-user service) and, for dev, unknowable
          // from this prod-stage stack. The Lambda resolves it once per cold
          // start by listing clusters and matching this prefix — keeping the
          // "instances differ ONLY in env vars" rule intact.
          ECS_CLUSTER_PREFIX: `isol8-${meterEnv}-container-`,
          // The env dimension on the Lambda's EMF metrics — both instances
          // share namespace Isol8 + service=isol8-bedrock-meter, so this is
          // what keeps dev noise out of the prod alarms. ALSO the env segment
          // of the per-owner IAM role name the kill deletes its policy from.
          METER_ENV: meterEnv,
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      fns[meterEnv] = fn;

      // The Lambda code sends failed records to the DLQ synchronously (the
      // subscription-filter invocation is synchronous, so the async DLQ wiring
      // above does not cover per-record failures). Grant the send.
      dlq.grantSendMessages(fn);

      // Per-env Metronome API key (sandbox token in dev, live token in prod;
      // Metronome is token-scoped — same base URL, the key decides the env).
      // Referenced by NAME: the secrets are created by EACH ENV'S auth-stack,
      // and this prod-stage stack cannot hold a cross-stage object ref to the
      // dev one — by-name import needs no stack dependency at all.
      const apiKeySecret = secretsmanager.Secret.fromSecretNameV2(
        this,
        `MetronomeApiKeySecret${idSuffix}`,
        `isol8/${meterEnv}/metronome_api_key`,
      );
      apiKeySecret.grantRead(fn);
      fn.addEnvironment("METRONOME_API_KEY_SECRET_ARN", apiKeySecret.secretArn);

      // grantRead only adds GetSecretValue — the secrets are CMK-encrypted
      // with each env's auth-stack key, and the dev CMK's ARN is unknowable
      // from this prod-stage stack (cross-stage). The aip-owners, billing and
      // containers DynamoDB tables are encrypted with the SAME per-env CMKs, so
      // reads need kms:Decrypt mediated by DynamoDB too (live-verified: without
      // it the attribution read dies with KMS AccessDenied and the batch sits
      // in CW Logs retry).
      //
      // kms:GenerateDataKey IS LOAD-BEARING, not padding. A CMK-encrypted table
      // needs Decrypt to READ and GenerateDataKey to WRITE; a Decrypt-only role
      // does GetItem happily and then fails EVERY UpdateItem — i.e. the trial
      // counter would work in every test and fail only in the deployed
      // environment, silently, with real money on the other side.
      //
      // Granted broadly but confined by kms:ViaService to Secrets Manager +
      // DynamoDB in this region — the role can only reach its own
      // metronome_api_key secret and its env's three tables, so the effective
      // reach is just those CMKs.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "KmsDecryptViaServices",
          effect: iam.Effect.ALLOW,
          actions: ["kms:Decrypt", "kms:GenerateDataKey"],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "kms:ViaService": [
                `secretsmanager.${this.region}.amazonaws.com`,
                `dynamodb.${this.region}.amazonaws.com`,
              ],
            },
          },
        }),
      );

      // Attribution read: one GetItem per record on the env's aip-owners
      // reverse table (owned by that env's database stack; ARN-by-convention
      // for the same no-cross-stage-ref reason as the secret above). That one
      // row carries owner_id + tier + plan, so the trial branch costs no extra
      // read.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AipOwnersGetItem",
          effect: iam.Effect.ALLOW,
          actions: ["dynamodb:GetItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${tablePrefix(meterEnv)}bedrock-aip-owners`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${legacyTablePrefix(meterEnv)}bedrock-aip-owners`,
          ],
        }),
      );

      // The replay guard's conditional PutItem. PutItem only: the Lambda never
      // reads or deletes a dedup row — a claim it cannot roll back is exactly
      // the point (at-most-once counting; we would rather under-count a record
      // whose ADD failed than double-count one whose ADD response was lost).
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "InvocationDedupPutItem",
          effect: iam.Effect.ALLOW,
          actions: ["dynamodb:PutItem"],
          resources: [dedupTable.tableArn],
        }),
      );

      // The trial counter (billing-accounts.trial_daily_spend_microcents) and
      // the revoked flag the backend's daily restore reads
      // (containers.bedrock_revoked). UpdateItem only — the meter never creates
      // or deletes either row.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "TrialCounterAndRevokedFlagUpdateItem",
          effect: iam.Effect.ALLOW,
          actions: ["dynamodb:UpdateItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${tablePrefix(meterEnv)}billing-accounts`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${legacyTablePrefix(meterEnv)}billing-accounts`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${tablePrefix(meterEnv)}containers`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${legacyTablePrefix(meterEnv)}containers`,
          ],
        }),
      );

      // THE KILL, half one: delete the per-owner role's scoped inline Bedrock
      // policy. Scoped to the owner-role name prefix the backend mints
      // (bedrock_aip._owner_role_name) — this role can strip Bedrock from a
      // per-owner task role and NOTHING else.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "RevokeOwnerBedrockPolicy",
          effect: iam.Effect.ALLOW,
          actions: ["iam:DeleteRolePolicy"],
          resources: [
            `arn:aws:iam::${this.account}:role/isol8-${meterEnv}-owner-bedrock-*`,
          ],
        }),
      );

      // THE KILL, half two: desiredCount=0 on the owner's own service. The IAM
      // revoke is ~5-15s eventually consistent and does NOT kill an
      // already-admitted streaming call — this is the instant stop, which is
      // why the two are fired independently and neither may depend on the
      // other. ListClusters carries no resource-level permissions (it is the
      // cold-start cluster discovery); UpdateService is scoped to the env's
      // cluster and the per-user openclaw-* service name, so the meter cannot
      // touch the backend's own service.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "StopOwnerContainer",
          effect: iam.Effect.ALLOW,
          actions: ["ecs:UpdateService"],
          resources: [
            `arn:aws:ecs:${this.region}:${this.account}:service/isol8-${meterEnv}-container-*/openclaw-*`,
          ],
        }),
      );
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "DiscoverEnvCluster",
          effect: iam.Effect.ALLOW,
          actions: ["ecs:ListClusters"],
          resources: ["*"],
        }),
      );

      // THE KILL, gateway half: flip denied=true on the owner's bedrock-gate
      // token row so the gateway 403s the cut-over owner (the gateway-path
      // equivalent of the IAM revoke). Reads the containers row for the
      // rotation pointer (bedrock_gate_token_hash) then writes the gate-tokens
      // row — GetItem + UpdateItem, scoped to the env's two tables. The
      // gate-tokens table itself is owned by the bedrock-gate stack;
      // ARN-by-convention keeps this stack free of a cross-stack ref.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "DenyGateTokenOnKill",
          effect: iam.Effect.ALLOW,
          actions: ["dynamodb:GetItem", "dynamodb:UpdateItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${tablePrefix(meterEnv)}containers`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${legacyTablePrefix(meterEnv)}containers`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${tablePrefix(meterEnv)}bedrock-gate-tokens`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${legacyTablePrefix(meterEnv)}bedrock-gate-tokens`,
          ],
        }),
      );

      // Env-split subscription filter. identity.arn carries the signing role
      // name, and TWO roles now sign Isol8 Bedrock traffic: the per-owner role
      // (isol8-{env}-owner-bedrock-{owner}, direct invoke) and the gateway role
      // (isol8-{env}-bedrock-gate, cut-over owners). Both markers carry the env
      // segment, so an OR of the two role names both scopes to Isol8 traffic
      // (unrelated invocations like GooseTown carry neither role → dropped at
      // the source, so the old "application-inference-profile" required term is
      // redundant) AND keeps the dev/prod split exact (a dev gateway record
      // matches only the dev filter, a prod one only prod).
      //
      // The `?` prefixes make the two terms OR'd. CloudWatch IGNORES `?` if any
      // non-`?` (required) term is present in the same pattern — which is why
      // the application-inference-profile required term is dropped, not kept:
      // keeping it would silently AND all three and match nothing.
      //
      // CONSTRAINT: these two filters consume BOTH of CloudWatch's
      // subscription-filter slots on the group (hard limit 2/group). Any
      // third consumer must fan out behind one of the Lambdas, not add a
      // filter.
      subscriptionFilters.push(
        new logs.SubscriptionFilter(this, `InvocationSubscription${idSuffix}`, {
          logGroup: this.invocationLogGroup,
          destination: new logsDestinations.LambdaDestination(fn),
          filterPattern: logs.FilterPattern.literal(
            `?"isol8-${meterEnv}-owner-bedrock" ?"isol8-${meterEnv}-bedrock-gate"`,
          ),
        }),
      );

      // --- Alarms (per instance) --------------------------------------------
      // A silent revenue leak (records DLQ'd on transport/parse failure, or
      // skipped as unattributed/unrateable) would be invisible until a manual
      // reconciliation. Alarm on BOTH the DLQ depth and the skip/failure
      // metrics so a sustained miss-storm pages instead of hiding.
      //
      // An alarm with no action only flips a console dot — it never pages.
      // Wire each instance's alarms to ITS env's existing alerts-warn topic
      // (created by that env's observability stack, subscriptions already
      // wired) so dev noise pages dev and prod noise pages prod. ARN
      // reference (not a cross-stack export) keeps the codebase's
      // no-export-coupling posture.
      const alarmTopic = sns.Topic.fromTopicArn(
        this,
        `MeterAlerts${idSuffix}`,
        `arn:aws:sns:${this.region}:${this.account}:isol8-${meterEnv}-alerts-warn`,
      );
      const alarmAction = new cloudwatchActions.SnsAction(alarmTopic);

      // A meter.kill_*_error means the cap FIRED and the owner is STILL ABLE TO
      // SPEND — the one failure in this system that burns money in real time,
      // and the only one with no self-healing path (the kill is now re-asserted
      // on every over-cap record, but if IAM/ECS is durably broken that retry
      // never lands). alerts-warn is email-only to alerts@; that is not a
      // response to unbounded spend. These go to the PAGE topic (SMS + oncall@),
      // which is what the comment above has always said they must do.
      const pageTopic = sns.Topic.fromTopicArn(
        this,
        `MeterPage${idSuffix}`,
        `arn:aws:sns:${this.region}:${this.account}:isol8-${meterEnv}-alerts-page`,
      );
      const pageAction = new cloudwatchActions.SnsAction(pageTopic);
      const PAGING_METRICS = new Set([
        "meter.kill_iam_error",
        "meter.kill_ecs_error",
        "meter.kill_deny_token_error",
      ]);

      const dlqAlarm = new cloudwatch.Alarm(this, `MeterDlqNotEmptyAlarm${idSuffix}`, {
        alarmName: `isol8-${meterEnv}-bedrock-meter-dlq-not-empty`,
        alarmDescription:
          "Bedrock meter DLQ has messages - a record failed Metronome ingest, was unattributed, or was unparseable and is held for redrive. Investigate before billing reconciles.",
        metric: dlq.metricApproximateNumberOfMessagesVisible({
          period: cdk.Duration.minutes(5),
          statistic: "Maximum",
        }),
        threshold: 0,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      dlqAlarm.addAlarmAction(alarmAction);

      // Whole-batch failures (a cold-start import error, a timeout, or
      // transport-retry exhaustion) raise OUT of the handler, so they never
      // reach the per-record DLQ path — CloudWatch Logs retries the batch a
      // few times and then drops it silently. The function's own Errors
      // metric is the only signal for that failure class.
      const errorsAlarm = new cloudwatch.Alarm(this, `MeterFnErrorsAlarm${idSuffix}`, {
        alarmName: `isol8-${meterEnv}-bedrock-meter-fn-errors`,
        alarmDescription:
          "Bedrock meter Lambda invocation errored - a WHOLE batch failed (cold-start import error, timeout, or transport-retry exhaustion) and never reached the per-record DLQ. CloudWatch Logs retries then drops the batch, so investigate before records age out.",
        metric: fn.metricErrors({
          period: cdk.Duration.minutes(5),
          statistic: "Sum",
        }),
        threshold: 0,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      errorsAlarm.addAlarmAction(alarmAction);

      // One alarm per skip/failure metric the Lambda emits. Both instances
      // emit into namespace Isol8 with service=isol8-bedrock-meter, so each
      // alarm scopes to ITS instance via the env dimension — otherwise dev
      // noise would page prod.
      //
      // The counter/kill metrics are the same class of problem as a revenue
      // leak, from the other direction: meter.counter_error means trial spend
      // is going uncounted (the cap will fire late or never), and a
      // meter.kill_*_error means the cap FIRED and the owner is still able to
      // spend — the one failure in this system that burns money in real time.
      // Both must page. (meter.cap_kill and meter.replay_skipped are NOT
      // alarmed: a trial owner hitting their cap, and a CW-Logs batch retry,
      // are both normal operation.)
      const skipMetrics: Array<[string, string]> = [
        ["meter.unattributed", "MeterUnattributedAlarm"],
        ["meter.unknown_model", "MeterUnknownModelAlarm"],
        ["meter.transport_error", "MeterTransportErrorAlarm"],
        ["meter.parse_failed", "MeterParseFailedAlarm"],
        ["meter.counter_error", "MeterCounterErrorAlarm"],
        ["meter.kill_iam_error", "MeterKillIamErrorAlarm"],
        ["meter.kill_ecs_error", "MeterKillEcsErrorAlarm"],
        ["meter.kill_stamp_error", "MeterKillStampErrorAlarm"],
        // A failed deny-gate-token step: the owner crossed the cap but their
        // gateway token stays valid. IAM revoke + ECS stop (the other two kill
        // steps) still fired, so it is not an immediate leak during the
        // transition — but once owner IAM is dropped (Phase 4) the deny flag IS
        // the fence, so this must page like the other kill_* failures.
        ["meter.kill_deny_token_error", "MeterKillDenyTokenErrorAlarm"],
        ["meter.malformed_record", "MeterMalformedRecordAlarm"],
      ];
      for (const [metricName, alarmId] of skipMetrics) {
        const alarm = new cloudwatch.Alarm(this, `${alarmId}${idSuffix}`, {
          alarmName: `isol8-${meterEnv}-${metricName.replace(/\./g, "-")}`,
          alarmDescription: `Bedrock meter (${meterEnv}) emitted ${metricName} - invocations are being skipped or failed in transport (potential revenue leak). Investigate.`,
          metric: new cloudwatch.Metric({
            namespace: "LightbulbAI",
            metricName,
            dimensionsMap: {
              service: "isol8-bedrock-meter",
              env: meterEnv,
            },
            period: cdk.Duration.minutes(15),
            statistic: "Sum",
          }),
          threshold: 0,
          comparisonOperator:
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          evaluationPeriods: 1,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
        alarm.addAlarmAction(
          PAGING_METRICS.has(metricName) ? pageAction : alarmAction,
        );
      }
    }

    // CUTOVER ORDERING: when the log group is REPLACED, the logging-config
    // custom resource repoints Bedrock at the new group. If that repoint ran
    // before the new group's subscription filters existed, records delivered
    // in the gap would land in the new group with nothing forwarding them —
    // filters only forward events ingested AFTER filter creation, so those
    // records would be unrecoverable. Depending on BOTH filters makes CFN
    // create/repoint the config only after both filters are live, and the
    // dependency re-arms on every future group replacement.
    loggingConfigCr.node.addDependency(...subscriptionFilters);

    this.meterFnDev = fns["dev"];
    this.meterFnProd = fns["prod"];

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }
}
