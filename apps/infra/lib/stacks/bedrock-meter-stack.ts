import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as logsDestinations from "aws-cdk-lib/aws-logs-destinations";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subs from "aws-cdk-lib/aws-sns-subscriptions";
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
    // backend 5xx, an unparseable record, or a truncated output body would
    // otherwise silently drop the record (or POST a zeroed-cache record → ~30×
    // under-bill).
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

    // Lambda SG: egress to the ALB on 80 (internal endpoint) + 443 (AWS APIs:
    // Secrets Manager for the meter token, STS/KMS). No S3 — the meter is
    // CloudWatch-only and never fetches an offloaded body.
    const meterSg = new ec2.SecurityGroup(this, "MeterLambdaSg", {
      vpc: props.vpc,
      description: "Bedrock meter Lambda - egress to ALB + AWS APIs",
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
      "Allow HTTPS for AWS APIs (Secrets Manager, STS, KMS)",
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
    // CloudWatch-ONLY. The meter reads everything it needs from the CloudWatch
    // record: token counts are top-level inline and the cache counts live in the
    // <100KB output body (output.outputBodyJson.usage), which Bedrock inlines.
    // Bedrock offloads only >100KB bodies to S3 and the huge thing on cache-heavy
    // turns is the cached INPUT prompt (which we never need), so there is no S3
    // sink and no largeDataDeliveryS3Config / s3Config — the meter never touches
    // S3.
    this.invocationLogGroup = new logs.LogGroup(this, "InvocationLogGroup", {
      logGroupName: `/isol8/${props.envName}/bedrock-invocation`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
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
    // group, so enable CloudWatch logging in EVERY fan-out region. The home
    // region (this.region) is configured by the block above and its log group is
    // wired to the meter Lambda via the subscription filter; this loop only
    // ENABLES per-region CloudWatch logging.
    //
    // FOLLOW-UP (documented): the fan-out regions (us-east-2 / us-west-2) still
    // need their OWN per-region CloudWatch subscription filter + meter Lambda to
    // get full multi-region metering coverage — a stack in us-east-1 cannot
    // create a subscription/Lambda in another region without a cross-region
    // sub-stack. This PR covers the home region end-to-end. Do NOT re-introduce
    // S3 for the fan-out — the per-region Lambda is the path, not an S3 sweep.
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

    // --- Alarms (review I2 + I1) ----------------------------------------------
    // A silent revenue leak (records DLQ'd on transport/parse failure, or
    // skipped as unattributed/unknown-model) would be invisible until a manual
    // ledger-vs-Bedrock reconciliation. Alarm on BOTH the DLQ depth and the
    // skip/failure metrics so a sustained miss-storm pages instead of hiding.
    // These alarms live in this prod-only, account-scoped stack, so the W53
    // account-global-collision constraint is honored (no per-env duplicate).
    //
    // I1: an alarm with no action only flips a console dot — it never pages. Wire
    // every alarm to an SNS topic so a sustained miss-storm actually reaches
    // oncall. This stack is prod-only and account-scoped, and the observability
    // stack's page/warn topics are not exported across stacks (the codebase
    // deliberately avoids cross-stack export coupling), so the topic is created
    // here and subscribed to the same oncall address.
    const alarmTopic = new sns.Topic(this, "MeterAlerts", {
      topicName: `isol8-${props.envName}-bedrock-meter-alerts`,
      displayName: "Isol8 Bedrock Meter Alerts",
    });
    alarmTopic.addSubscription(new subs.EmailSubscription("oncall@isol8.co"));
    const alarmAction = new cloudwatchActions.SnsAction(alarmTopic);

    const dlqAlarm = new cloudwatch.Alarm(this, "MeterDlqNotEmptyAlarm", {
      alarmName: `isol8-${props.envName}-bedrock-meter-dlq-not-empty`,
      alarmDescription:
        "Bedrock meter DLQ has messages - a record failed transport/parse or had a truncated output body and was held for redrive. Investigate before billing reconciles.",
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

    // One alarm per skip/failure metric. Each is emitted in the Isol8 EMF
    // namespace (the backend emits unattributed/unknown_model; the Lambda emits
    // output_body_truncated/transport_error/parse_failed). Sum across dimensions
    // so a burst from any region/service trips it.
    const skipMetrics: Array<[string, string]> = [
      ["meter.unattributed", "MeterUnattributedAlarm"],
      ["meter.unknown_model", "MeterUnknownModelAlarm"],
      ["meter.output_body_truncated", "MeterOutputBodyTruncatedAlarm"],
      ["meter.transport_error", "MeterTransportErrorAlarm"],
      ["meter.parse_failed", "MeterParseFailedAlarm"],
    ];
    for (const [metricName, alarmId] of skipMetrics) {
      const alarm = new cloudwatch.Alarm(this, alarmId, {
        alarmName: `isol8-${props.envName}-${metricName.replace(/\./g, "-")}`,
        alarmDescription: `Bedrock meter emitted ${metricName} - invocations are being skipped or failed in transport (potential revenue leak). Investigate.`,
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
      alarm.addAlarmAction(alarmAction);
    }

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }

  private _regionLoggingConfig(
    region: string,
    props: BedrockMeterStackProps,
  ): Record<string, unknown> {
    // The other-region log group + delivery role are pre-provisioned out-of-band
    // (one-time `aws logs create-log-group` + role create per region) because a
    // stack in us-east-1 cannot create resources in another region without a
    // cross-region sub-stack. CloudWatch-only, like the home region: no S3 sink,
    // no largeDataDeliveryS3Config / s3Config. Full per-region metering still
    // needs a per-region subscription + meter Lambda (the FOLLOW-UP noted at the
    // fan-out loop above); this config only ENABLES CloudWatch logging there.
    return {
      cloudWatchConfig: {
        logGroupName: `/isol8/${props.envName}/bedrock-invocation`,
        roleArn: `arn:aws:iam::${this.account}:role/isol8-${props.envName}-bedrock-logging-${region}`,
      },
      textDataDeliveryEnabled: true,
      embeddingDataDeliveryEnabled: true,
      imageDataDeliveryEnabled: false,
    };
  }
}
