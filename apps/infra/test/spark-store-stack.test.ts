import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import { Match, Template } from "aws-cdk-lib/assertions";
import { SparkStoreStack } from "../lib/stacks/spark-store-stack";

function build(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const key = new kms.Key(new cdk.Stack(app, `Support-${environment}`, { env }), "KmsKey");
  return Template.fromStack(new SparkStoreStack(app, `Spark-${environment}`, { env, environment, kmsKey: key }));
}

describe.each(["dev", "prod"] as const)("SparkStoreStack %s", (environment) => {
  const t = build(environment);

  test("bucket: public access blocked, KMS, not versioned, four lifecycle rules", () => {
    t.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: `lightbulb-${environment}-spark-store`,
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          Match.objectLike({
            BucketKeyEnabled: true,
            ServerSideEncryptionByDefault: { SSEAlgorithm: "aws:kms", KMSMasterKeyID: Match.anyValue() },
          }),
        ],
      },
      LifecycleConfiguration: {
        Rules: [
          Match.objectLike({ Id: "AbortIncompleteUploads", AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 } }),
          Match.objectLike({ Id: "ExpireAthenaResults", Prefix: "athena-results/", ExpirationInDays: 7 }),
          Match.objectLike({ Id: "ExpireJobState", Prefix: "jobs/", ExpirationInDays: 7 }),
          Match.objectLike({ Id: "ExpireFirehoseErrors", Prefix: "errors/", ExpirationInDays: 3 }),
        ],
      },
    });
    const [bucket] = Object.values(t.findResources("AWS::S3::Bucket"));
    expect(bucket.Properties.VersioningConfiguration).toBeUndefined();
    expect(bucket.DeletionPolicy).toBe(environment === "prod" ? "Retain" : "Delete");
  });

  test("workgroups: 1 GB app cap and 20 GB adhoc cap, configuration enforced", () => {
    for (const [name, cutoff] of [
      [`lightbulb-${environment}-spark`, 1073741824],
      [`lightbulb-${environment}-spark-adhoc`, 21474836480],
    ] as const) {
      t.hasResourceProperties("AWS::Athena::WorkGroup", {
        Name: name,
        WorkGroupConfiguration: Match.objectLike({
          EnforceWorkGroupConfiguration: true,
          BytesScannedCutoffPerQuery: cutoff,
        }),
      });
    }
  });

  test("workgroup results are SSE_KMS encrypted", () => {
    const wgs = Object.values(t.findResources("AWS::Athena::WorkGroup")) as any[];
    expect(wgs).toHaveLength(2);
    for (const w of wgs) {
      expect(w.Properties.WorkGroupConfiguration.ResultConfiguration.EncryptionConfiguration.EncryptionOption).toBe(
        "SSE_KMS",
      );
    }
  });

  test("no lifecycle rule touches warehouse/", () => {
    const [bucket] = Object.values(t.findResources("AWS::S3::Bucket")) as any[];
    for (const r of bucket.Properties.LifecycleConfiguration.Rules) expect(r.Prefix ?? "").not.toMatch(/^warehouse/);
  });

  test("firehose KMS use is limited to S3 via kms:ViaService", () => {
    t.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ["kms:GenerateDataKey", "kms:Decrypt"],
            Condition: { StringEquals: { "kms:ViaService": "s3.us-east-1.amazonaws.com" } },
          }),
        ]),
      }),
    });
  });

  test("firehose role and log group exist", () => {
    t.hasResourceProperties("AWS::IAM::Role", { RoleName: `lightbulb-${environment}-spark-firehose` });
    t.hasResourceProperties("AWS::Logs::LogGroup", { LogGroupName: `/aws/firehose/lightbulb-${environment}-spark` });
  });

  test.each([
    ["turns", "turns"],
    ["steps", "task_steps"],
    ["events", "events"],
  ])("delivery stream %s writes append-only to table %s", (suffix, table) => {
    t.hasResourceProperties("AWS::KinesisFirehose::DeliveryStream", {
      DeliveryStreamName: `lightbulb-${environment}-spark-${suffix}`,
      DeliveryStreamType: "DirectPut",
      DeliveryStreamEncryptionConfigurationInput: { KeyType: "CUSTOMER_MANAGED_CMK", KeyARN: Match.anyValue() },
      IcebergDestinationConfiguration: Match.objectLike({
        AppendOnly: true,
        s3BackupMode: "FailedDataOnly", // CloudFormation spells this one with a lowercase s
        BufferingHints: { IntervalInSeconds: environment === "prod" ? 900 : 60, SizeInMBs: 128 },
        RetryOptions: { DurationInSeconds: 3600 },
        RoleARN: { "Fn::GetAtt": [Match.stringLikeRegexp("^FirehoseRole"), "Arn"] },
        CatalogConfiguration: { CatalogArn: Match.anyValue() },
        DestinationTableConfigurationList: [
          {
            DestinationDatabaseName: `lightbulb_${environment}_spark`,
            DestinationTableName: table,
            S3ErrorOutputPrefix: `errors/${table}/`,
          },
        ],
        S3Configuration: Match.objectLike({ ErrorOutputPrefix: `errors/${table}/` }),
        CloudWatchLoggingOptions: {
          Enabled: true,
          LogGroupName: `/aws/firehose/lightbulb-${environment}-spark`,
          LogStreamName: table,
        },
      }),
    });
    t.hasResourceProperties("AWS::Logs::LogStream", { LogStreamName: table });
  });

  test("exactly three streams, each waiting for the role policy", () => {
    const streams = Object.values(t.findResources("AWS::KinesisFirehose::DeliveryStream")) as any[];
    expect(streams).toHaveLength(3);
    for (const s of streams) expect(s.DependsOn.some((d: string) => d.startsWith("FirehoseRoleDefaultPolicy"))).toBe(true);
  });
});
