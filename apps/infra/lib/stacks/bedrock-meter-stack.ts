import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
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

  constructor(scope: Construct, id: string, props: BedrockMeterStackProps) {
    super(scope, id, props);

    // Dead-letter queue for at-least-once retries. The endpoint is idempotent
    // (deplete() exactly-once on requestId) so replays are safe.
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
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

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

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }
}
