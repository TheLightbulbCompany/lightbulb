import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import * as rds from "aws-cdk-lib/aws-rds";
import { Construct } from "constructs";

export interface DatabaseStackProps extends cdk.StackProps {
  environment: string;
  kmsKey: kms.IKey;
  /**
   * VPC for the Paperclip Aurora cluster (Task 1 of paperclip-rebuild).
   * The DynamoDB tables in this stack are VPC-less; the VPC is consumed
   * exclusively by the Aurora subnet group + security group.
   */
  vpc: ec2.IVpc;
}

const ENV_CONFIG: Record<string, { removalPolicy: cdk.RemovalPolicy }> = {
  dev: { removalPolicy: cdk.RemovalPolicy.DESTROY },
  prod: { removalPolicy: cdk.RemovalPolicy.RETAIN },
};

export class DatabaseStack extends cdk.Stack {
  public readonly usersTable: dynamodb.Table;
  public readonly containersTable: dynamodb.Table;
  public readonly billingTable: dynamodb.Table;
  public readonly apiKeysTable: dynamodb.Table;
  public readonly usageCountersTable: dynamodb.Table;
  public readonly pendingUpdatesTable: dynamodb.Table;
  public readonly channelLinksTable: dynamodb.Table;
  public readonly webhookDedupTable: dynamodb.Table;
  public readonly adminActionsTable: dynamodb.Table;
  public readonly creditsTable: dynamodb.Table;
  public readonly creditTransactionsTable: dynamodb.Table;
  public readonly oauthTokensTable: dynamodb.Table;
  public readonly paperclipCompaniesTable: dynamodb.Table;

  // Paperclip Aurora Serverless v2 cluster (Task 1 of paperclip-rebuild).
  // pgvector extension is created by the drizzle migrations runner in
  // paperclip-stack.ts (Task 5), not here.
  public readonly paperclipDbCluster: rds.DatabaseCluster;
  public readonly paperclipDbSecurityGroup: ec2.SecurityGroup;

  public readonly marketplaceListingsTable: dynamodb.Table;
  public readonly marketplacePurchasesTable: dynamodb.Table;
  public readonly marketplacePayoutAccountsTable: dynamodb.Table;
  public readonly marketplaceTakedownsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DatabaseStackProps) {
    super(scope, id, props);

    const config = ENV_CONFIG[props.environment] ?? ENV_CONFIG.dev;
    const env = props.environment;

    this.usersTable = new dynamodb.Table(this, "UsersTable", {
      tableName: `isol8-${env}-users`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    this.containersTable = new dynamodb.Table(this, "ContainersTable", {
      tableName: `isol8-${env}-containers`,
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });
    this.containersTable.addGlobalSecondaryIndex({
      indexName: "gateway-token-index",
      partitionKey: { name: "gateway_token", type: dynamodb.AttributeType.STRING },
    });
    this.containersTable.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
    });
    this.containersTable.addGlobalSecondaryIndex({
      indexName: "owner-type-index",
      partitionKey: { name: "owner_type", type: dynamodb.AttributeType.STRING },
    });

    this.billingTable = new dynamodb.Table(this, "BillingTable", {
      tableName: `isol8-${env}-billing-accounts`,
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });
    this.billingTable.addGlobalSecondaryIndex({
      indexName: "stripe-customer-index",
      partitionKey: { name: "stripe_customer_id", type: dynamodb.AttributeType.STRING },
    });

    this.apiKeysTable = new dynamodb.Table(this, "ApiKeysTable", {
      tableName: `isol8-${env}-api-keys`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "tool_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    this.usageCountersTable = new dynamodb.Table(this, "UsageCountersTable", {
      tableName: `isol8-${env}-usage-counters`,
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "period", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    this.pendingUpdatesTable = new dynamodb.Table(this, "PendingUpdatesTable", {
      tableName: `isol8-${env}-pending-updates`,
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "update_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
      timeToLiveAttribute: "ttl",
    });
    this.pendingUpdatesTable.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "scheduled_at", type: dynamodb.AttributeType.STRING },
    });

    this.channelLinksTable = new dynamodb.Table(this, "ChannelLinksTable", {
      tableName: `isol8-${env}-channel-links`,
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });
    this.channelLinksTable.addGlobalSecondaryIndex({
      indexName: "by-member",
      partitionKey: { name: "member_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "owner_provider_agent", type: dynamodb.AttributeType.STRING },
    });

    // Webhook event dedup table — shared by Stripe and Clerk webhooks
    // PK: event_id (prefixed: "stripe:{id}" or "clerk:{id}")
    // TTL: 30-day auto-expiry via "ttl" attribute
    this.webhookDedupTable = new dynamodb.Table(this, "WebhookEventDedup", {
      tableName: `isol8-${env}-webhook-event-dedup`,
      partitionKey: { name: "event_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    new cdk.CfnOutput(this, "WebhookDedupTableName", {
      value: this.webhookDedupTable.tableName,
      exportName: `${this.stackName}-webhook-dedup-table`,
    });

    // Admin-actions audit table — every platform-admin write to /api/v1/admin/*
    // appends a row via @audit_admin_action. PK admin_user_id + SK timestamp#uuidv7
    // for chronological-per-admin queries; GSI flips to target_user_id for the
    // "show me all actions taken against this user" view. No TTL (CEO review:
    // audit rows kept forever). Customer-managed KMS encryption like every
    // other table in this stack.
    this.adminActionsTable = new dynamodb.Table(this, "AdminActionsTable", {
      tableName: `isol8-${env}-admin-actions`,
      partitionKey: { name: "admin_user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp_action_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
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

    // Credits balance per user — atomic counter, deducted per Bedrock chat
    // (card 3 only). Single-key, plain item; per spec §6.1.
    this.creditsTable = new dynamodb.Table(this, "CreditsTable", {
      tableName: `isol8-${env}-credits`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    // Credit transactions audit log — immutable history of top-ups, deducts,
    // adjustments. PK user_id + SK tx_id (ULID, sortable by time).
    // Per spec §6.1.
    this.creditTransactionsTable = new dynamodb.Table(this, "CreditTransactionsTable", {
      tableName: `isol8-${env}-credit-transactions`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "tx_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    // ChatGPT OAuth bootstrap tokens — Fernet-encrypted access + refresh
    // tokens captured during signup, used once when staging the user's EFS
    // codex/auth.json file at container provision. Per spec §5.1.
    this.oauthTokensTable = new dynamodb.Table(this, "OAuthTokensTable", {
      tableName: `isol8-${env}-oauth-tokens`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    // Paperclip companies — maps Isol8 user_id → their Paperclip company
    // record + encrypted credentials. GSI by-status-purge-at lets the
    // cleanup cron find disabled rows past their grace window. Per the
    // paperclip-rebuild plan (Task 2). Customer-managed KMS to match the
    // rest of this stack (deviates from the plan template's AWS_MANAGED).
    this.paperclipCompaniesTable = new dynamodb.Table(this, "PaperclipCompaniesTable", {
      tableName: `isol8-${env}-paperclip-companies`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });
    this.paperclipCompaniesTable.addGlobalSecondaryIndex({
      indexName: "by-status-purge-at",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "scheduled_purge_at", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    // GSI for org-scoped lookups: get_org_company_id, count_org_members,
    // _find_org_owner, and the organization.deleted webhook sweep all
    // partition by org_id. Projection ALL because the org-owner lookup
    // and company_id resolution both consume full row data, not just keys.
    this.paperclipCompaniesTable.addGlobalSecondaryIndex({
      indexName: "by-org-id",
      partitionKey: { name: "org_id", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new cdk.CfnOutput(this, "CreditsTableName", {
      value: this.creditsTable.tableName,
      exportName: `${this.stackName}-credits-table`,
    });
    new cdk.CfnOutput(this, "CreditTransactionsTableName", {
      value: this.creditTransactionsTable.tableName,
      exportName: `${this.stackName}-credit-transactions-table`,
    });
    new cdk.CfnOutput(this, "OAuthTokensTableName", {
      value: this.oauthTokensTable.tableName,
      exportName: `${this.stackName}-oauth-tokens-table`,
    });
    new cdk.CfnOutput(this, "PaperclipCompaniesTableName", {
      value: this.paperclipCompaniesTable.tableName,
      exportName: `${this.stackName}-paperclip-companies-table`,
    });

    this.marketplaceListingsTable = new dynamodb.Table(this, "MarketplaceListingsTable", {
      tableName: `isol8-${env}-marketplace-listings`,
      partitionKey: { name: "listing_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "version", type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });
    this.marketplaceListingsTable.addGlobalSecondaryIndex({
      indexName: "slug-version-index",
      partitionKey: { name: "slug", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "version", type: dynamodb.AttributeType.NUMBER },
    });
    this.marketplaceListingsTable.addGlobalSecondaryIndex({
      indexName: "seller-created-index",
      partitionKey: { name: "seller_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created_at", type: dynamodb.AttributeType.STRING },
    });
    this.marketplaceListingsTable.addGlobalSecondaryIndex({
      indexName: "status-published-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "published_at", type: dynamodb.AttributeType.STRING },
    });
    this.marketplaceListingsTable.addGlobalSecondaryIndex({
      indexName: "tag-published-index",
      partitionKey: { name: "tag", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "published_at", type: dynamodb.AttributeType.STRING },
    });

    this.marketplacePurchasesTable = new dynamodb.Table(this, "MarketplacePurchasesTable", {
      tableName: `isol8-${env}-marketplace-purchases`,
      partitionKey: { name: "buyer_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "purchase_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });
    this.marketplacePurchasesTable.addGlobalSecondaryIndex({
      indexName: "listing-created-index",
      partitionKey: { name: "listing_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created_at", type: dynamodb.AttributeType.STRING },
    });
    this.marketplacePurchasesTable.addGlobalSecondaryIndex({
      indexName: "license-key-index",
      partitionKey: { name: "license_key", type: dynamodb.AttributeType.STRING },
    });
    // Used by the charge.refunded Stripe webhook to find the purchase row
    // matching a Stripe payment_intent ID without scanning the full table.
    // Sparse: only paid purchases (which all carry stripe_payment_intent_id)
    // appear in the index — refund/revoke writes don't add load.
    this.marketplacePurchasesTable.addGlobalSecondaryIndex({
      indexName: "payment-intent-index",
      partitionKey: {
        name: "stripe_payment_intent_id",
        type: dynamodb.AttributeType.STRING,
      },
    });

    this.marketplacePayoutAccountsTable = new dynamodb.Table(this, "MarketplacePayoutAccountsTable", {
      tableName: `isol8-${env}-marketplace-payout-accounts`,
      partitionKey: { name: "seller_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    this.marketplaceTakedownsTable = new dynamodb.Table(this, "MarketplaceTakedownsTable", {
      tableName: `isol8-${env}-marketplace-takedowns`,
      partitionKey: { name: "listing_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "takedown_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });

    new cdk.CfnOutput(this, "DynamoTablePrefix", {
      value: `isol8-${env}-`,
      exportName: `${this.stackName}-table-prefix`,
    });

    // ─────────────────────────────────────────────────────────────────
    // Paperclip Aurora Serverless v2 cluster
    //
    // Postgres 16.4 with pgvector available. Scale-to-zero (min 0 ACU,
    // max 4 ACU). Subnet group on private-with-egress subnets. Security
    // group is restrictive — ingress is granted later (in paperclip-stack
    // / service-stack) only from the backend SG and the Paperclip task SG.
    //
    // Cross-stack note: the props.vpc reference here is the only reason
    // DatabaseStack now depends on NetworkStack. See isol8-stage.ts for
    // the wiring.
    // ─────────────────────────────────────────────────────────────────
    const paperclipDbSubnetGroup = new rds.SubnetGroup(this, "PaperclipDbSubnets", {
      vpc: props.vpc,
      description: "Subnets for Paperclip Aurora cluster",
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    const paperclipDbSecurityGroup = new ec2.SecurityGroup(this, "PaperclipDbSg", {
      vpc: props.vpc,
      description:
        "Paperclip Aurora cluster - only backend SG and Paperclip task SG may reach 5432 (ASCII only - EC2 rejects non-ASCII)",
      allowAllOutbound: false,
    });

    this.paperclipDbCluster = new rds.DatabaseCluster(this, "PaperclipDb", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_4,
      }),
      serverlessV2MinCapacity: 0, // scale-to-zero
      serverlessV2MaxCapacity: 4,
      writer: rds.ClusterInstance.serverlessV2("writer"),
      vpc: props.vpc,
      subnetGroup: paperclipDbSubnetGroup,
      securityGroups: [paperclipDbSecurityGroup],
      defaultDatabaseName: "paperclip",
      credentials: rds.Credentials.fromGeneratedSecret("paperclip_admin", {
        secretName: `isol8-${env}-paperclip-db-credentials`,
      }),
      backup: { retention: cdk.Duration.days(7) },
      storageEncrypted: true,
      storageEncryptionKey: props.kmsKey,
      removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
      clusterIdentifier: `isol8-${env}-paperclip-db`,
      // Enable RDS Data API (HTTPS-callable SQL endpoint, no VPC required).
      // Lets operator scripts run one-off SQL — e.g.
      // `aws rds-data execute-statement` to seed a Paperclip bootstrap_ceo
      // invite row — from outside the VPC without standing up a bastion.
      // Aurora Postgres supports Data API on 14.7+/16+ provisioned clusters.
      // Auth is two-secret: caller's IAM principal + the cluster's master
      // credentials secret (already exists, generated above).
      enableDataApi: true,
    });

    this.paperclipDbSecurityGroup = paperclipDbSecurityGroup;

    new cdk.CfnOutput(this, "PaperclipDbEndpoint", {
      value: this.paperclipDbCluster.clusterEndpoint.hostname,
      exportName: `isol8-${env}-paperclip-db-endpoint`,
    });
  }
}
