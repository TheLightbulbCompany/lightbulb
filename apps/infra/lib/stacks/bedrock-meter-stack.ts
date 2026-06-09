import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as logsDestinations from "aws-cdk-lib/aws-logs-destinations";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

// =============================================================================
// BedrockMeterStack — account-scoped, SINGLE-OWNER Bedrock usage meter.
// =============================================================================
// Instantiated ONCE (prod stage only). Bedrock model-invocation logging is an
// ACCOUNT+REGION-level config (one per region) and there is ONE invocation-log
// group per region per account — so a per-env duplicate would collide, exactly
// like the Cost Anomaly Detection tombstone in observability-stack.ts:1609-1616.
// Dev validation uses temporary manual enablement (capture plan), not a second
// permanent stack.
// =============================================================================

export interface BedrockMeterStackProps extends cdk.StackProps {
  envName: string;
  vpc: ec2.IVpc;
  alb: elbv2.IApplicationLoadBalancer;
  albSecurityGroup: ec2.ISecurityGroup;
  /** Secrets Manager NAME of the pre-minted bedrock_meter token (not ISecret). */
  meterTokenSecretName: string;
  /** AuthStack KMS key ARN — the Lambda needs kms:Decrypt to read the token. */
  meterKmsKeyArn: string;
  /** Bedrock fan-out regions to enable logging + a filter in (Task 13). */
  fanoutRegions: string[];
}

export class BedrockMeterStack extends cdk.Stack {
  public readonly meterFn: lambda.Function;
  public readonly invocationLogGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: BedrockMeterStackProps) {
    super(scope, id, props);

    // Fail-safe durability queue. A CloudWatch Logs subscription invokes the
    // meter Lambda SYNCHRONOUSLY, so Lambda's async-invocation DLQ (the
    // `deadLetterQueue` prop below) NEVER fires on a per-record failure — a
    // backend 5xx, an unparseable S3 ref, or an S3 fetch error would otherwise
    // silently drop the record (or POST a zeroed-cache record → ~30× under-bill).
    // So the Lambda code (index.handler) explicitly `sqs:SendMessage`s each
    // failed raw log event here (via DLQ_URL) and continues. The endpoint is
    // idempotent (deplete() exactly-once on requestId), so redriving this queue
    // is safe. We ALSO wire it as the Lambda's async DLQ to catch the rare
    // platform-level async failure path, but the synchronous per-record sends
    // are the load-bearing durability mechanism.
    const dlq = new sqs.Queue(this, "MeterDlq", {
      queueName: `isol8-${props.envName}-bedrock-meter-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });

    // Lambda SG: egress to the ALB on 80 (internal endpoint) + 443 (S3 fetch).
    const meterSg = new ec2.SecurityGroup(this, "MeterLambdaSg", {
      vpc: props.vpc,
      description: "Bedrock meter Lambda — egress to ALB + S3",
      allowAllOutbound: false,
    });
    meterSg.addEgressRule(
      props.albSecurityGroup,
      ec2.Port.tcp(80),
      "Allow meter Lambda to reach ALB on port 80",
    );
    meterSg.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "Allow HTTPS for S3 + AWS APIs",
    );

    this.meterFn = new lambda.Function(this, "MeterFn", {
      functionName: `isol8-${props.envName}-bedrock-meter`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "bedrock-usage-meter"),
      ),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [meterSg],
      deadLetterQueueEnabled: true,
      deadLetterQueue: dlq,
      environment: {
        ALB_DNS_NAME: props.alb.loadBalancerDnsName,
        // The synchronous per-record fail-safe sink the Lambda code POSTs to.
        DLQ_URL: dlq.queueUrl,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // The Lambda code sends failed records to the DLQ synchronously (the
    // subscription-filter invocation is synchronous, so the async DLQ wiring
    // above does not cover per-record failures). Grant the send.
    dlq.grantSendMessages(this.meterFn);

    // Inject the pre-minted meter token from Secrets Manager (cold-start fetch
    // not needed — passing the secret ARN + an env mapping keeps the value out
    // of CFN; the function reads it via the SDK extension or, simpler, the CDK
    // resolves the secret value at deploy into a non-logged env. We use the
    // ARN + a kms:Decrypt grant, mirroring the WS authorizer pattern.)
    const meterSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "MeterTokenSecret",
      props.meterTokenSecretName,
    );
    meterSecret.grantRead(this.meterFn);
    // grantRead only adds GetSecretValue — KMS-encrypted secrets also need
    // kms:Decrypt on the CMK (documented WS-authorizer footgun).
    this.meterFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForMeterToken",
        effect: iam.Effect.ALLOW,
        actions: ["kms:Decrypt"],
        resources: [props.meterKmsKeyArn],
      }),
    );
    this.meterFn.addEnvironment("METER_TOKEN_SECRET_ARN", meterSecret.secretArn);

    // --- Bedrock model-invocation logging sink (this stack's region) ---
    // S3 sink for both the full log copy and the >100KB body offload.
    const logSink = new s3.Bucket(this, "InvocationLogSink", {
      bucketName: `isol8-${props.envName}-bedrock-invocation-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    this.invocationLogGroup = new logs.LogGroup(this, "InvocationLogGroup", {
      logGroupName: `/isol8/${props.envName}/bedrock-invocation`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Bedrock's delivery role — assumed by the bedrock.amazonaws.com service
    // to write to the log group + S3 sink.
    const deliveryRole = new iam.Role(this, "BedrockLoggingRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    });
    this.invocationLogGroup.grantWrite(deliveryRole);
    logSink.grantWrite(deliveryRole);

    // No L1 for PutModelInvocationLoggingConfiguration — use AwsCustomResource.
    const loggingConfig = {
      cloudWatchConfig: {
        logGroupName: this.invocationLogGroup.logGroupName,
        roleArn: deliveryRole.roleArn,
        largeDataDeliveryS3Config: { bucketName: logSink.bucketName },
      },
      s3Config: { bucketName: logSink.bucketName },
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

    // Subscription filter — drive the meter Lambda, scoped to the Isol8
    // AIP-ARN shape so unrelated traffic (e.g. GooseTown) is dropped at the
    // source. Bedrock records carry the AIP ARN in `modelId`; the term filter
    // requires the invocation-profile substring to be present.
    new logs.SubscriptionFilter(this, "InvocationSubscription", {
      logGroup: this.invocationLogGroup,
      destination: new logsDestinations.LambdaDestination(this.meterFn),
      filterPattern: logs.FilterPattern.literal('"application-inference-profile"'),
    });

    // Fan-out logging configs — Bedrock writes to a per-region invocation-log
    // group, so enable logging in EVERY fan-out region. The home region
    // (this.region) is configured by the Task-12 block above; this loop covers
    // the remaining regions. Each is an account-level config in its region.
    for (const region of props.fanoutRegions) {
      if (region === this.region) continue;
      new cr.AwsCustomResource(this, `BedrockLoggingConfig-${region}`, {
        onCreate: {
          service: "Bedrock",
          action: "putModelInvocationLoggingConfiguration",
          region,
          parameters: { loggingConfig: this._regionLoggingConfig(region, props) },
          physicalResourceId: cr.PhysicalResourceId.of(
            `bedrock-logging-${region}`,
          ),
        },
        onUpdate: {
          service: "Bedrock",
          action: "putModelInvocationLoggingConfiguration",
          region,
          parameters: { loggingConfig: this._regionLoggingConfig(region, props) },
          physicalResourceId: cr.PhysicalResourceId.of(
            `bedrock-logging-${region}`,
          ),
        },
        onDelete: {
          service: "Bedrock",
          action: "deleteModelInvocationLoggingConfiguration",
          region,
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
              "bedrock:PutModelInvocationLoggingConfiguration",
              "bedrock:DeleteModelInvocationLoggingConfiguration",
            ],
            resources: ["*"],
          }),
          // PutModelInvocationLoggingConfiguration with a cloudWatchConfig.roleArn
          // requires iam:PassRole on that role (same as the home-region config
          // above). The role ARN must match _regionLoggingConfig's roleArn.
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: ["iam:PassRole"],
            resources: [
              `arn:aws:iam::${this.account}:role/isol8-${props.envName}-bedrock-logging-${region}`,
            ],
          }),
        ]),
      });
    }

    // --- Alarms (review I2) ---------------------------------------------------
    // A silent revenue leak (records DLQ'd on transport/parse failure, or
    // skipped as unattributed/unknown-model) would be invisible until a manual
    // ledger-vs-Bedrock reconciliation. Alarm on BOTH the DLQ depth and the
    // skip/failure metrics so a sustained miss-storm pages instead of hiding.
    // These alarms live in this prod-only, account-scoped stack, so the W53
    // account-global-collision constraint is honored (no per-env duplicate).
    new cloudwatch.Alarm(this, "MeterDlqNotEmptyAlarm", {
      alarmName: `isol8-${props.envName}-bedrock-meter-dlq-not-empty`,
      alarmDescription:
        "Bedrock meter DLQ has messages — a record failed transport/parse/S3-merge and was held for redrive. Investigate before billing reconciles.",
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

    // One alarm per skip/failure metric. Each is emitted in the Isol8 EMF
    // namespace (the backend emits unattributed/unknown_model; the Lambda emits
    // s3_merge_failed/transport_error/parse_failed). Sum across dimensions so a
    // burst from any region/service trips it.
    const skipMetrics: Array<[string, string]> = [
      ["meter.unattributed", "MeterUnattributedAlarm"],
      ["meter.unknown_model", "MeterUnknownModelAlarm"],
      ["meter.s3_merge_failed", "MeterS3MergeFailedAlarm"],
      ["meter.transport_error", "MeterTransportErrorAlarm"],
      ["meter.parse_failed", "MeterParseFailedAlarm"],
    ];
    for (const [metricName, alarmId] of skipMetrics) {
      new cloudwatch.Alarm(this, alarmId, {
        alarmName: `isol8-${props.envName}-${metricName.replace(/\./g, "-")}`,
        alarmDescription: `Bedrock meter emitted ${metricName} — invocations are being skipped or failed in transport (potential revenue leak). Investigate.`,
        metric: new cloudwatch.Metric({
          namespace: "Isol8",
          metricName,
          period: cdk.Duration.minutes(15),
          statistic: "Sum",
        }),
        threshold: 0,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }

  private _regionLoggingConfig(
    region: string,
    props: BedrockMeterStackProps,
  ): Record<string, unknown> {
    // The other-region groups/buckets are pre-provisioned out-of-band (one-time
    // `aws logs create-log-group` + `aws s3 mb` per region) because a stack in
    // us-east-1 cannot create resources in another region without a cross-region
    // sub-stack. Phase 1 logs to S3 in those regions and sweeps daily until the
    // per-region Lambda lands; the config below points Bedrock at those sinks.
    const bucket = `isol8-${props.envName}-bedrock-invocation-${region}`;
    return {
      cloudWatchConfig: {
        logGroupName: `/isol8/${props.envName}/bedrock-invocation`,
        roleArn: `arn:aws:iam::${this.account}:role/isol8-${props.envName}-bedrock-logging-${region}`,
        largeDataDeliveryS3Config: { bucketName: bucket },
      },
      s3Config: { bucketName: bucket },
      textDataDeliveryEnabled: true,
      embeddingDataDeliveryEnabled: true,
      imageDataDeliveryEnabled: false,
    };
  }
}
