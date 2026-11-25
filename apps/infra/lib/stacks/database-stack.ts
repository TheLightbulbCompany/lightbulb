import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as kms from "aws-cdk-lib/aws-kms";
import { tablePrefix } from "../table-names";
import { Construct } from "constructs";

export interface DatabaseStackProps extends cdk.StackProps {
  environment: string;
  kmsKey: kms.IKey;
}

const ENV_CONFIG: Record<string, { removalPolicy: cdk.RemovalPolicy }> = {
  // Dev data is expendable (owner, 2026-08-28): the table-prefix rename
  // REPLACES every table, so dev's legacy tables are deleted and dev starts
  // empty on the new prefix. No dev copy step, and no lingering RETAIN to undo.
  dev: { removalPolicy: cdk.RemovalPolicy.DESTROY },
  prod: { removalPolicy: cdk.RemovalPolicy.RETAIN },
};

export class DatabaseStack extends cdk.Stack {
  public readonly usersTable: dynamodb.Table;
  public readonly containersTable: dynamodb.Table;
  public readonly webhookDedupTable: dynamodb.Table;
  public readonly adminActionsTable: dynamodb.Table;


  constructor(scope: Construct, id: string, props: DatabaseStackProps) {
    super(scope, id, props);

    const config = ENV_CONFIG[props.environment] ?? ENV_CONFIG.dev;
    const env = props.environment;

    // Every DynamoDB table in this stack shares the same invariants:
    // `isol8-{env}-` name prefix, on-demand billing, customer-managed KMS
    // encryption, and point-in-time recovery. The factory pins them so a
    // future table can't silently regress durability by forgetting
    // pointInTimeRecovery or the CMK. `ephemeral: true` is the deliberate
    // opt-out for TTL'd cache/dedup tables: DESTROY + no PITR.
    //
    // The construct `id` is the CloudFormation logical ID — renaming an
    // existing one REPLACES the table (data loss). Never change ids here.
    const createTable = (
      id: string,
      suffix: string,
      schema: {
        partitionKey: dynamodb.Attribute;
        sortKey?: dynamodb.Attribute;
        timeToLiveAttribute?: string;
        ephemeral?: boolean;
      },
    ): dynamodb.Table =>
      new dynamodb.Table(this, id, {
        tableName: `${tablePrefix(env)}${suffix}`,
        partitionKey: schema.partitionKey,
        ...(schema.sortKey ? { sortKey: schema.sortKey } : {}),
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        // NOT `ephemeral ? DESTROY : ...` during the table-prefix rename.
        // Changing `tableName` REPLACES the table, and DESTROY deletes the old
        // one immediately -- but every READER still names the legacy table
        // until the atomic reader flip. So an ephemeral table would be deleted
        // out from under bedrock-gate (INVOCATION_DEDUP_TABLE) and the WS
        // Lambdas (ws-connections) for the whole window between the two
        // deploys. Ephemeral means "safe to lose eventually", not "safe to
        // delete while something is still reading it".
        //
        // Restore the ephemeral DESTROY when the legacy tables are dropped.
        removalPolicy: config.removalPolicy,
        ...(schema.ephemeral ? {} : { pointInTimeRecovery: true }),
        encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: props.kmsKey,
        ...(schema.timeToLiveAttribute
          ? { timeToLiveAttribute: schema.timeToLiveAttribute }
          : {}),
      });

    this.usersTable = createTable("UsersTable", "users", {
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
    });

    this.containersTable = createTable("ContainersTable", "containers", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      // Spark's voice-session rows (core/voice/os_voice.py) live here, found through status-index.
      timeToLiveAttribute: "session_ttl",
    });
    this.containersTable.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
    });

    // Webhook event dedup table — shared by Stripe and Clerk webhooks
    // PK: event_id (prefixed: "stripe:{id}" or "clerk:{id}")
    // TTL: 30-day auto-expiry via "ttl" attribute
    this.webhookDedupTable = createTable("WebhookEventDedup", "webhook-event-dedup", {
      partitionKey: { name: "event_id", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
      ephemeral: true,
    });

    new cdk.CfnOutput(this, "WebhookDedupTableName", {
      value: this.webhookDedupTable.tableName,
      exportName: `${this.stackName}-webhook-dedup-table`,
    });

    // Admin-actions audit table — every platform-admin write to /api/v1/admin/*
    // appends a row via @audit_admin_action. PK admin_user_id + SK timestamp#uuidv7
    // for chronological-per-admin queries; GSI flips to target_user_id for the
    // "show me all actions taken against this user" view. No TTL — audit rows
    // kept forever. Customer-managed KMS encryption like every other table in
    // this stack.
    this.adminActionsTable = createTable("AdminActionsTable", "admin-actions", {
      partitionKey: { name: "admin_user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp_action_id", type: dynamodb.AttributeType.STRING },
    });
    this.adminActionsTable.addGlobalSecondaryIndex({
      indexName: "target-timestamp-index",
      partitionKey: { name: "target_user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp_action_id", type: dynamodb.AttributeType.STRING },
    });

    new cdk.CfnOutput(this, "AdminActionsTableName", {
      value: this.adminActionsTable.tableName,
      exportName: `${this.stackName}-admin-actions-table`,
    });

    // Spark voice transcripts (os_voice): one row per turn, keyed by person, `<startedAt>#<sessionId>#<seq>`.
    // Text only, expired after 90 days by TTL; written while SPARK_TRANSCRIPTS_ENABLED is on.
    // Covered by the backend wildcard table grant; deleted with the person's Spark account.
    createTable("SparkTranscriptsTable", "spark-transcripts", {
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
    });

    new cdk.CfnOutput(this, "DynamoTablePrefix", {
      value: tablePrefix(env),
      exportName: `${this.stackName}-table-prefix`,
    });
  }
}
