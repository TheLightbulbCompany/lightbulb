import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import * as rds from "aws-cdk-lib/aws-rds";
import { legacyTablePrefix, tablePrefix } from "../table-names";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
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
  // Dev data is expendable (owner, 2026-08-28): the table-prefix rename
  // REPLACES every table, so dev's legacy tables are deleted and dev starts
  // empty on the new prefix. No dev copy step, and no lingering RETAIN to undo.
  dev: { removalPolicy: cdk.RemovalPolicy.DESTROY },
  prod: { removalPolicy: cdk.RemovalPolicy.RETAIN },
};

export class DatabaseStack extends cdk.Stack {
  public readonly usersTable: dynamodb.Table;
  public readonly containersTable: dynamodb.Table;
  public readonly billingTable: dynamodb.Table;
  public readonly usageCountersTable: dynamodb.Table;
  public readonly pendingUpdatesTable: dynamodb.Table;
  public readonly channelLinksTable: dynamodb.Table;
  public readonly companySlugsTable: dynamodb.Table;
  public readonly agentEmailAddressesTable: dynamodb.Table;
  public readonly agentMailInboxTable: dynamodb.Table;
  public readonly webhookDedupTable: dynamodb.Table;
  public readonly adminActionsTable: dynamodb.Table;
  public readonly metronomeFastGateTable: dynamodb.Table;
  public readonly bedrockAipOwnersTable: dynamodb.Table;
  public readonly oauthTokensTable: dynamodb.Table;
  public readonly oauthStateTable: dynamodb.Table;
  public readonly integrationsTable: dynamodb.Table;
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
  public readonly marketplaceSellersTable: dynamodb.Table;

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
    });
    this.containersTable.addGlobalSecondaryIndex({
      indexName: "gateway-token-index",
      partitionKey: { name: "gateway_token", type: dynamodb.AttributeType.STRING },
    });
    this.containersTable.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
    });

    this.billingTable = createTable("BillingTable", "billing-accounts", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
    this.billingTable.addGlobalSecondaryIndex({
      indexName: "stripe-customer-index",
      partitionKey: { name: "stripe_customer_id", type: dynamodb.AttributeType.STRING },
    });
    // App Store Server Notifications identify a subscription only by its
    // originalTransactionId — they carry no owner_id — so renewals, expiries
    // and refunds resolve the account through this index.
    //
    // SPARSE by construction: billing_repo.set_apple_subscription is the only
    // writer and refuses an empty value, because an empty string on a GSI key
    // attribute makes DynamoDB reject the ENTIRE PutItem. On this table's
    // read-then-put writers that would brick every unrelated billing write for
    // the owner — the shape of the 2026-08-02 paperclip-companies outage. Test
    // fixtures must mirror this index or they cannot catch that class at all.
    this.billingTable.addGlobalSecondaryIndex({
      indexName: "apple-original-transaction-index",
      partitionKey: {
        name: "apple_original_transaction_id",
        type: dynamodb.AttributeType.STRING,
      },
    });

    this.usageCountersTable = createTable("UsageCountersTable", "usage-counters", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "period", type: dynamodb.AttributeType.STRING },
    });

    this.pendingUpdatesTable = createTable("PendingUpdatesTable", "pending-updates", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "update_id", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
    });
    this.pendingUpdatesTable.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "scheduled_at", type: dynamodb.AttributeType.STRING },
    });

    this.channelLinksTable = createTable("ChannelLinksTable", "channel-links", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
    });
    this.channelLinksTable.addGlobalSecondaryIndex({
      indexName: "by-member",
      partitionKey: { name: "member_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "owner_provider_agent", type: dynamodb.AttributeType.STRING },
    });

    this.companySlugsTable = createTable("CompanySlugsTable", "company-slugs", {
      partitionKey: { name: "slug", type: dynamodb.AttributeType.STRING },
    });
    this.companySlugsTable.addGlobalSecondaryIndex({
      indexName: "by-owner",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });

    this.agentEmailAddressesTable = createTable(
      "AgentEmailAddressesTable",
      "agent-email-addresses",
      {
        partitionKey: { name: "address", type: dynamodb.AttributeType.STRING },
      },
    );
    this.agentEmailAddressesTable.addGlobalSecondaryIndex({
      indexName: "by-owner",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });

    // Stored inbound agent mail (inbox the agent reads on demand — OTPs etc.).
    // PK = the agent's address, SK = "{received_at}#{message_id}" so a query
    // is newest-first by ISO timestamp. 30-day TTL: the inbox is a working
    // buffer, not an archive.
    this.agentMailInboxTable = createTable("AgentMailInboxTable", "agent-mail-inbox", {
      partitionKey: { name: "address", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "received_key", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
      ephemeral: true,
    });
    this.agentMailInboxTable.addGlobalSecondaryIndex({
      indexName: "by-message-id",
      partitionKey: { name: "message_id", type: dynamodb.AttributeType.STRING },
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

    // The DDB credit ledger (credits + credit-transactions tables) was
    // retired 2026-07-14 — Metronome is the sole biller and these tables
    // had no remaining consumers.

    // Metronome local fast-gate flag. A disposable per-owner projection of
    // Metronome balance that the SYNCHRONOUS chat gate reads, since
    // Metronome's balance read is async with no low-latency SLA. Fed by the
    // Metronome low-balance webhook + the gate's lazy getNetBalance refresh
    // (Metronome fires no recovery webhook on a top-up). Carries no
    // money-correctness burden (Metronome is the SoR). The per-owner
    // metronome_customer_id lives on the billing row, not here — this
    // projection is wipeable cache state. PK owner_id.
    this.metronomeFastGateTable = createTable("MetronomeFastGateTable", "metronome-fast-gate", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });

    // Per-owner Bedrock AIP -> owner reverse index (Plan 2). PK aip_arn.
    // Written by ensure_owner_aips; read by resolve_owner_from_aip_arn on the
    // Plan-3 meter hot path (a single bounded GetItem per invocation record).
    this.bedrockAipOwnersTable = createTable("BedrockAipOwnersTable", "bedrock-aip-owners", {
      partitionKey: { name: "aip_arn", type: dynamodb.AttributeType.STRING },
    });

    // ChatGPT OAuth bootstrap tokens — Fernet-encrypted access + refresh
    // tokens captured during signup, used once when staging the user's EFS
    // codex/auth.json file at container provision. Per spec §5.1.
    this.oauthTokensTable = createTable("OAuthTokensTable", "oauth-tokens", {
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
    });

    // Short-lived OAuth dance state (PKCE verifier + state nonce). TTL'd, CMK, DESTROY (mirrors the webhook-event-dedup table).
    this.oauthStateTable = createTable("OauthStateTable", "oauth-state", {
      partitionKey: { name: "state", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
      ephemeral: true,
    });

    // Secret-free integration STATUS index (credential-layer redesign). Holds
    // which providers an owner has connected; the credential itself lives in
    // Secrets Manager (backend-custodian providers) or the container EFS
    // keyring (actor-refresh providers), never here. Same shape as the old
    // connections vault (owner_id PK + provider SK) minus any credential payload.
    this.integrationsTable = createTable("IntegrationsTable", "integrations", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "provider", type: dynamodb.AttributeType.STRING },
    });

    // Paperclip companies — maps Isol8 user_id → their Paperclip company
    // record + encrypted credentials. GSI by-status-purge-at lets the
    // cleanup cron find disabled rows past their grace window. Per the
    // paperclip-rebuild plan (Task 2). Customer-managed KMS to match the
    // rest of this stack (deviates from the plan template's AWS_MANAGED).
    this.paperclipCompaniesTable = createTable("PaperclipCompaniesTable", "paperclip-companies", {
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
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
    // Reverse lookup companyId -> member user_ids for the inbound Paperclip
    // plugin-event receiver (routers/paperclip_events.py): push-notification
    // audience resolution. INCLUDE status so the active-row filter needs no
    // base-table chase (rows also carry encrypted creds — do not project ALL).
    this.paperclipCompaniesTable.addGlobalSecondaryIndex({
      indexName: "by-company-id",
      partitionKey: { name: "company_id", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["status"],
    });

    new cdk.CfnOutput(this, "OAuthTokensTableName", {
      value: this.oauthTokensTable.tableName,
      exportName: `${this.stackName}-oauth-tokens-table`,
    });
    new cdk.CfnOutput(this, "OauthStateTableName", {
      value: this.oauthStateTable.tableName,
      exportName: `${this.stackName}-oauth-state-table`,
    });
    new cdk.CfnOutput(this, "PaperclipCompaniesTableName", {
      value: this.paperclipCompaniesTable.tableName,
      exportName: `${this.stackName}-paperclip-companies-table`,
    });
    new cdk.CfnOutput(this, "IntegrationsTableName", {
      value: this.integrationsTable.tableName,
      exportName: `${this.stackName}-integrations-table`,
    });

    this.marketplaceListingsTable = createTable("MarketplaceListingsTable", "marketplace-listings", {
      partitionKey: { name: "listing_id", type: dynamodb.AttributeType.STRING },
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

    this.marketplacePurchasesTable = createTable("MarketplacePurchasesTable", "marketplace-purchases", {
      partitionKey: { name: "buyer_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "purchase_id", type: dynamodb.AttributeType.STRING },
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

    this.marketplacePayoutAccountsTable = createTable("MarketplacePayoutAccountsTable", "marketplace-payout-accounts", {
      partitionKey: { name: "seller_id", type: dynamodb.AttributeType.STRING },
    });

    this.marketplaceTakedownsTable = createTable("MarketplaceTakedownsTable", "marketplace-takedowns", {
      partitionKey: { name: "listing_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "takedown_id", type: dynamodb.AttributeType.STRING },
    });

    // Marketplace seller profiles — per-seller chosen handle / display name.
    // The seller_id is the Clerk user_id; the handle is the public marketplace
    // identity surfaced on listing bylines and (future) star / credibility
    // surfaces. `handle-index` enforces uniqueness via conditional writes.
    this.marketplaceSellersTable = createTable("MarketplaceSellersTable", "marketplace-sellers", {
      partitionKey: { name: "seller_id", type: dynamodb.AttributeType.STRING },
    });
    this.marketplaceSellersTable.addGlobalSecondaryIndex({
      indexName: "handle-index",
      partitionKey: { name: "handle", type: dynamodb.AttributeType.STRING },
    });

    new cdk.CfnOutput(this, "DynamoTablePrefix", {
      value: tablePrefix(env),
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

    // Escape hatch, and the reason is not cosmetic. CDK's generated secret
    // gets the default description "Generated by the CDK for stack: {Ref:
    // AWS::StackName}". CloudFormation STACK REFACTORING refuses to move any
    // resource that references a pseudo-parameter whose value differs between
    // the source and destination stacks -- AWS::StackName is precisely that --
    // so this one auto-generated string made the entire database stack
    // ineligible to be renamed. Overriding it removes the Ref.
    //
    // Description is an updatable property on a Secrets Manager secret, so this
    // is an in-place update: it does NOT replace the secret or rotate the
    // Aurora master credentials.
    // `cluster.secret` resolves to the ATTACHED secret (a
    // SecretTargetAttachment), not the generated one -- overriding through it
    // is a no-op. The CfnSecret lives at the construct path
    // `PaperclipDb/Secret/Resource`, so reach it by node lookup and assert it
    // was found: a silent miss here would quietly leave the stack ineligible.
    const paperclipDbSecretNode =
      this.paperclipDbCluster.node.tryFindChild("Secret");
    const paperclipDbSecret = paperclipDbSecretNode?.node.defaultChild as
      | secretsmanager.CfnSecret
      | undefined;
    if (!paperclipDbSecret) {
      throw new Error(
        "PaperclipDb/Secret/Resource not found - the CDK internal path changed. " +
          "Without the Description override the generated secret references " +
          "AWS::StackName, which makes this stack ineligible for CloudFormation " +
          "stack refactoring.",
      );
    }
    paperclipDbSecret.addPropertyOverride(
      "Description",
      "Paperclip Aurora master credentials",
    );

    this.paperclipDbSecurityGroup = paperclipDbSecurityGroup;

    new cdk.CfnOutput(this, "PaperclipDbEndpoint", {
      value: this.paperclipDbCluster.clusterEndpoint.hostname,
      exportName: `isol8-${env}-paperclip-db-endpoint`,
    });

  }
}
