import * as cdk from "aws-cdk-lib";
import * as athena from "aws-cdk-lib/aws-athena";
import * as firehose from "aws-cdk-lib/aws-kinesisfirehose";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { sparkStoreNames } from "../table-names";

export interface SparkStoreStackProps extends cdk.StackProps {
  environment: string;
  kmsKey: kms.IKey;
}

const GB = 1024 ** 3;

/**
 * Spark data store: an S3 bucket holding Iceberg tables (created by
 * scripts/spark_store_ddl.py, not CDK: Firehose only delivers to tables made through
 * the Iceberg Glue catalog), three Athena workgroups, and the role and log group the
 * Firehose delivery streams use, plus one delivery stream per table.
 */
export class SparkStoreStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SparkStoreStackProps) {
    super(scope, id, props);
    const env = props.environment;
    const prod = env === "prod";
    const n = sparkStoreNames(env);
    const catalog = `arn:aws:glue:${this.region}:${this.account}`;

    const bucket = new s3.Bucket(this, "SparkStoreBucket", {
      bucketName: n.bucket,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: props.kmsKey,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      // Unversioned on purpose: VACUUM's deletes must not leave noncurrent copies of user text.
      versioned: false,
      removalPolicy: prod ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !prod,
      // Never a rule on warehouse/: Iceberg owns those files and expiring them corrupts tables.
      lifecycleRules: [
        { id: "AbortIncompleteUploads", abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) },
        { id: "ExpireAthenaResults", prefix: "athena-results/", expiration: cdk.Duration.days(7) },
        // Only the daily run markers expire; jobs/forget/ delete requests must never be dropped unrun.
        { id: "ExpireRetentionMarkers", prefix: "jobs/retention/", expiration: cdk.Duration.days(7) },
        { id: "ExpireFirehoseErrors", prefix: "errors/", expiration: cdk.Duration.days(3) },
      ],
    });

    const workgroup = (key: string, name: string, cutoffBytes: number, results: string) =>
      new athena.CfnWorkGroup(this, key, {
        name,
        recursiveDeleteOption: !prod,
        workGroupConfiguration: {
          enforceWorkGroupConfiguration: true,
          bytesScannedCutoffPerQuery: cutoffBytes,
          publishCloudWatchMetricsEnabled: true,
          engineVersion: { selectedEngineVersion: "Athena engine version 3" },
          resultConfiguration: {
            outputLocation: `s3://${bucket.bucketName}/athena-results/${results}/`,
            encryptionConfiguration: { encryptionOption: "SSE_KMS", kmsKey: props.kmsKey.keyArn },
          },
        },
      });
    // App workgroup: 1 GB cap, so a query missing its dt/user_bucket filter fails instead of billing.
    workgroup("AppWorkGroup", n.workgroup, 1 * GB, "app");
    // Team SQL and the DDL script: 20 GB cap.
    workgroup("AdhocWorkGroup", n.adhocWorkgroup, 20 * GB, "adhoc");
    // Backend jobs (user deletes, OPTIMIZE, VACUUM, retention): they read whole data files, so 100 GB.
    workgroup("JobsWorkGroup", n.jobsWorkgroup, 100 * GB, "jobs");

    const logGroup = new logs.LogGroup(this, "FirehoseLogGroup", {
      logGroupName: n.logGroup,
      retention: prod ? logs.RetentionDays.ONE_MONTH : logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const role = new iam.Role(this, "FirehoseRole", {
      roleName: n.firehoseRole,
      assumedBy: new iam.ServicePrincipal("firehose.amazonaws.com", {
        conditions: { StringEquals: { "aws:SourceAccount": this.account } },
      }),
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
        resources: [
          `${catalog}:catalog`,
          `${catalog}:database/${n.database}`,
          `${catalog}:table/${n.database}/*`,
        ],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:AbortMultipartUpload",
          "s3:ListBucketMultipartUploads",
        ],
        resources: [bucket.bucketArn, bucket.arnForObjects("*")],
      }),
    );
    // By ARN, not key.grant(): a grant on the auth stack's key would add a key-policy
    // statement there that points back at this stack.
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["kms:GenerateDataKey", "kms:Decrypt"],
        resources: [props.kmsKey.keyArn],
        conditions: { StringEquals: { "kms:ViaService": `s3.${this.region}.amazonaws.com` } },
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["logs:PutLogEvents", "logs:CreateLogStream"],
        resources: [logGroup.logGroupArn],
      }),
    );

    // One DirectPut stream per Iceberg table (never two on one table: Iceberg commits conflict).
    // Creating a stream does not need the table to exist (Firehose only checks it at create time
    // when UniqueKeys are set), so prod gets its streams before the prod DDL has run.
    // Firehose will not create the log streams, so they are created here, one per table.
    const tables = ["turns", "task_steps", "events"];
    tables.forEach((table, i) => {
      const logStream = new logs.LogStream(this, `${table}LogStream`, {
        logGroup,
        logStreamName: table,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      const stream = new firehose.CfnDeliveryStream(this, `${table}Stream`, {
        deliveryStreamName: n.streams[i],
        deliveryStreamType: "DirectPut",
        deliveryStreamEncryptionConfigurationInput: { keyType: "CUSTOMER_MANAGED_CMK", keyArn: props.kmsKey.keyArn },
        icebergDestinationConfiguration: {
          catalogConfiguration: { catalogArn: `${catalog}:catalog` },
          roleArn: role.roleArn,
          destinationTableConfigurationList: [
            { destinationDatabaseName: n.database, destinationTableName: table, s3ErrorOutputPrefix: `errors/${table}/` },
          ],
          appendOnly: true,
          bufferingHints: { intervalInSeconds: prod ? 900 : 60, sizeInMBs: 128 },
          retryOptions: { durationInSeconds: 3600 },
          s3BackupMode: "FailedDataOnly",
          s3Configuration: {
            bucketArn: bucket.bucketArn,
            roleArn: role.roleArn,
            errorOutputPrefix: `errors/${table}/`,
            encryptionConfiguration: { kmsEncryptionConfig: { awskmsKeyArn: props.kmsKey.keyArn } },
          },
          cloudWatchLoggingOptions: { enabled: true, logGroupName: n.logGroup, logStreamName: table },
        },
      });
      // The role's policy must exist first or the create fails on a role with no permissions.
      stream.node.addDependency(role, logStream);
    });
  }
}
