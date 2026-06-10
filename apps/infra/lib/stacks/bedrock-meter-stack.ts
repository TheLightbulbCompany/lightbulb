import * as path from "path";
import * as cdk from "aws-cdk-lib";
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
import { Construct } from "constructs";

// =============================================================================
// BedrockMeterStack — account-scoped Bedrock usage meter, direct Metronome
// ingest. ONE codebase -> TWO function instances (dev + prod).
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
    this.invocationLogGroup = new logs.LogGroup(this, "InvocationLogGroup", {
      logGroupName: "/isol8/bedrock-invocation",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Bedrock's delivery role — assumed by the bedrock.amazonaws.com service
    // to write to the CloudWatch log group.
    const deliveryRole = new iam.Role(this, "BedrockLoggingRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    });
    this.invocationLogGroup.grantWrite(deliveryRole);

    // No L1 for PutModelInvocationLoggingConfiguration — use AwsCustomResource.
    const loggingConfig = {
      cloudWatchConfig: {
        logGroupName: this.invocationLogGroup.logGroupName,
        roleArn: deliveryRole.roleArn,
      },
      textDataDeliveryEnabled: true,
      embeddingDataDeliveryEnabled: true,
      imageDataDeliveryEnabled: false,
    };
    new cr.AwsCustomResource(this, "BedrockLoggingConfig", {
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
          // Per-env attribution table: aip_arn -> {owner_id, tier}.
          AIP_OWNERS_TABLE: `isol8-${meterEnv}-bedrock-aip-owners`,
          // The env dimension on the Lambda's EMF metrics — both instances
          // share namespace Isol8 + service=isol8-bedrock-meter, so this is
          // what keeps dev noise out of the prod alarms.
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
      // from this prod-stage stack (cross-stage). Grant kms:Decrypt broadly
      // but confined by kms:ViaService to decryption mediated by Secrets
      // Manager in this region — the role holds no GetSecretValue beyond its
      // own metronome_api_key secret, so the effective reach is just that
      // secret's CMK.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "KmsDecryptViaSecretsManager",
          effect: iam.Effect.ALLOW,
          actions: ["kms:Decrypt"],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "kms:ViaService": `secretsmanager.${this.region}.amazonaws.com`,
            },
          },
        }),
      );

      // Attribution read: one GetItem per record on the env's aip-owners
      // reverse table (owned by that env's database stack; ARN-by-convention
      // for the same no-cross-stage-ref reason as the secret above).
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AipOwnersGetItem",
          effect: iam.Effect.ALLOW,
          actions: ["dynamodb:GetItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${meterEnv}-bedrock-aip-owners`,
          ],
        }),
      );

      // Env-split subscription filter. identity.arn carries the per-owner
      // role name (isol8-{env}-owner-bedrock-{owner}) — that role-name prefix
      // IS the env marker, so a term filter cleanly routes each env's records
      // to its own Lambda. The "application-inference-profile" term scopes to
      // Isol8 AIP traffic so unrelated invocations (e.g. GooseTown) drop at
      // the source.
      // CONSTRAINT: these two filters consume BOTH of CloudWatch's
      // subscription-filter slots on the group (hard limit 2/group). Any
      // third consumer must fan out behind one of the Lambdas, not add a
      // filter.
      new logs.SubscriptionFilter(this, `InvocationSubscription${idSuffix}`, {
        logGroup: this.invocationLogGroup,
        destination: new logsDestinations.LambdaDestination(fn),
        filterPattern: logs.FilterPattern.literal(
          `"application-inference-profile" "isol8-${meterEnv}-owner-bedrock"`,
        ),
      });

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

      // One alarm per skip/failure metric the Lambda emits. Both instances
      // emit into namespace Isol8 with service=isol8-bedrock-meter, so each
      // alarm scopes to ITS instance via the env dimension — otherwise dev
      // noise would page prod.
      const skipMetrics: Array<[string, string]> = [
        ["meter.unattributed", "MeterUnattributedAlarm"],
        ["meter.unknown_model", "MeterUnknownModelAlarm"],
        ["meter.transport_error", "MeterTransportErrorAlarm"],
        ["meter.parse_failed", "MeterParseFailedAlarm"],
      ];
      for (const [metricName, alarmId] of skipMetrics) {
        const alarm = new cloudwatch.Alarm(this, `${alarmId}${idSuffix}`, {
          alarmName: `isol8-${meterEnv}-${metricName.replace(/\./g, "-")}`,
          alarmDescription: `Bedrock meter (${meterEnv}) emitted ${metricName} - invocations are being skipped or failed in transport (potential revenue leak). Investigate.`,
          metric: new cloudwatch.Metric({
            namespace: "Isol8",
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
        alarm.addAlarmAction(alarmAction);
      }
    }

    this.meterFnDev = fns["dev"];
    this.meterFnProd = fns["prod"];

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }
}
