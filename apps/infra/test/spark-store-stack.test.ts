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

  test("one bucket: public access blocked, KMS, not versioned", () => {
    t.resourceCountIs("AWS::S3::Bucket", 1);
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
    });
    const [bucket] = Object.values(t.findResources("AWS::S3::Bucket")) as any[];
    expect(bucket.Properties.VersioningConfiguration).toBeUndefined();
    expect(bucket.DeletionPolicy).toBe(environment === "prod" ? "Retain" : "Delete");
  });

  test("everything under data/ expires after 90 days", () => {
    t.hasResourceProperties("AWS::S3::Bucket", {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([Match.objectLike({ Prefix: "data/", ExpirationInDays: 90, Status: "Enabled" })]),
      },
    });
  });

  test("the retired prefixes expire after a day and no other rule matches data/", () => {
    const [bucket] = Object.values(t.findResources("AWS::S3::Bucket")) as any[];
    const rules = bucket.Properties.LifecycleConfiguration.Rules as any[];
    for (const prefix of ["warehouse/", "errors/", "jobs/", "athena-results/"]) {
      expect(rules).toContainEqual(expect.objectContaining({ Prefix: prefix, ExpirationInDays: 1 }));
    }
    const hitsData = rules.filter((r) => r.ExpirationInDays && "data/x".startsWith(r.Prefix ?? ""));
    expect(hitsData).toHaveLength(1);
  });

  test("no delivery stream, workgroup or Glue resource", () => {
    for (const type of [
      "AWS::KinesisFirehose::DeliveryStream",
      "AWS::Athena::WorkGroup",
      "AWS::Glue::Database",
      "AWS::Glue::Table",
    ]) {
      t.resourceCountIs(type, 0);
    }
  });
});
