import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { sparkStoreNames } from "../table-names";

export interface SparkStoreStackProps extends cdk.StackProps {
  environment: string;
  kmsKey: kms.IKey;
}

/**
 * Spark data store: one S3 bucket of gzip JSON-lines objects the backend writes at
 * data/<table>/dt=<day>/<user_id>__<name>.json.gz (core/observability/spark_store.py).
 * Storage only: the 90-day expiry is the lifecycle rule below.
 */
export class SparkStoreStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SparkStoreStackProps) {
    super(scope, id, props);
    const prod = props.environment === "prod";
    const retired = (prefix: string): s3.LifecycleRule => ({
      id: `Expire${prefix.replace("/", "")}`,
      prefix,
      expiration: cdk.Duration.days(1),
    });

    new s3.Bucket(this, "SparkStoreBucket", {
      bucketName: sparkStoreNames(props.environment).bucket,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: props.kmsKey,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      // Unversioned on purpose: a delete must not leave noncurrent copies of user text.
      versioned: false,
      removalPolicy: prod ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !prod,
      lifecycleRules: [
        { id: "AbortIncompleteUploads", abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) },
        { id: "ExpireData", prefix: "data/", expiration: cdk.Duration.days(90) },
        // The rules below clear prefixes of the old Iceberg/Firehose/Athena layout (dev holds test files there).
        // They can be deleted after December 2026.
        retired("warehouse/"),
        retired("errors/"),
        retired("jobs/"),
        retired("athena-results/"),
      ],
    });
  }
}
