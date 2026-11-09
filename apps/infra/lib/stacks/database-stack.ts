import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as kms from "aws-cdk-lib/aws-kms";
import { superAgentEnabled } from "../super-agent";
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
  public readonly billingTable: dynamodb.Table;
  public readonly usageCountersTable: dynamodb.Table;
  public readonly pendingUpdatesTable: dynamodb.Table;
  public readonly channelLinksTable: dynamodb.Table;
  public readonly companySlugsTable: dynamodb.Table;
  public readonly agentEmailAddressesTable: dynamodb.Table;
  public readonly slackInstallsTable: dynamodb.Table;
  public readonly agentMailInboxTable: dynamodb.Table;
  public readonly webhookDedupTable: dynamodb.Table;
  public readonly adminActionsTable: dynamodb.Table;
  public readonly metronomeFastGateTable: dynamodb.Table;
  public readonly openrouterKeysTable: dynamodb.Table;
  public readonly openrouterRequestsTable: dynamodb.Table;
  public readonly bedrockAipOwnersTable: dynamodb.Table;
  public readonly oauthTokensTable: dynamodb.Table;
  public readonly oauthStateTable: dynamodb.Table;
  public readonly integrationsTable: dynamodb.Table;
  public readonly provisioningWorkflowsTable: dynamodb.Table;
  public readonly appsTable: dynamodb.Table;
  public readonly paperclipCompaniesTable: dynamodb.Table;
  public readonly roomReadCursorsTable: dynamodb.Table;
  public readonly workspaceJoinLinksTable: dynamodb.Table;

  public readonly launchWaitlistTable: dynamodb.Table;
  public readonly launchEntriesTable: dynamodb.Table;
  public readonly launchSettingsTable: dynamodb.Table;

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
      // Only per-task AgentCore session rows (`__session__#<owner>#task#<task>`, super-agent
      // per-task VMs) carry it: a task's warm estimate is dead 8 h after its claim. Behind the
      // super-agent gate, so the prod table's CloudFormation does not change.
      ...(superAgentEnabled(props.environment) ? { timeToLiveAttribute: "session_ttl" } : {}),
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

    // Company Stripe account (the owner's own business account on the same
    // Connect platform). Webhooks for that account (account.updated, Standard
    // OAuth deauthorize) identify the account only by its acct_… id, so this
    // resolves the owning row the same way apple-original-transaction-index
    // resolves App Store notifications above.
    this.billingTable.addGlobalSecondaryIndex({
      indexName: "company-stripe-account-index",
      partitionKey: { name: "company_stripe_account_id", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
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

    // One Lightbulb Slack app serves every workspace; Slack posts to ONE URL
    // with team_id in the body, so this is the team_id → owner routing table.
    // Same shape as agent-email-addresses (address → owner). The bot token is
    // NOT here — it lives in Secrets Manager under the owner's integrations
    // prefix; this row carries only its secret name.
    this.slackInstallsTable = createTable("SlackInstallsTable", "slack-installs", {
      partitionKey: { name: "team_id", type: dynamodb.AttributeType.STRING },
    });
    this.slackInstallsTable.addGlobalSecondaryIndex({
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

    this.openrouterKeysTable = createTable("OpenRouterKeysTable", "openrouter-keys", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });

    this.openrouterRequestsTable = createTable("OpenRouterRequestsTable", "openrouter-requests", {
      partitionKey: { name: "request_id", type: dynamodb.AttributeType.STRING },
    });
    this.openrouterRequestsTable.addGlobalSecondaryIndex({
      indexName: "state-created-index",
      partitionKey: { name: "state", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created_at", type: dynamodb.AttributeType.NUMBER },
    });

    // Per-owner Bedrock AIP -> owner reverse index (Plan 2). PK aip_arn.
    // Written by ensure_owner_aips; read by the bedrock-usage-meter Lambda's
    // own boto3 GetItem on its hot path (no backend code reads this table).
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

    // The provisioning rail's durable workflow record. Stripe's Provisioning
    // API has no webhooks and no idempotency contract, so a browser session,
    // provider redirect, backend restart or deploy must never lose an account
    // request id, a resource id, or an uncertain write (spec §3.5).
    this.provisioningWorkflowsTable = createTable(
      "ProvisioningWorkflowsTable",
      "provisioning-workflows",
      { partitionKey: { name: "workflow_id", type: dynamodb.AttributeType.STRING } },
    );
    // The owner's list, newest first within a state.
    this.provisioningWorkflowsTable.addGlobalSecondaryIndex({
      indexName: "owner-state-index",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "state", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // One row per app, plus one NAME#<name> guard row per app that makes names
    // globally unique (the guard has no owner_id or name, so neither index holds it).
    this.appsTable = createTable("AppsTable", "apps", {
      partitionKey: { name: "app_id", type: dynamodb.AttributeType.STRING },
    });
    this.appsTable.addGlobalSecondaryIndex({
      indexName: "owner-index",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "name", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.appsTable.addGlobalSecondaryIndex({
      indexName: "name-index",
      partitionKey: { name: "name", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Product-only per-human sequence state. Native OpenClaw SQLite remains
    // canonical for every room message, sender, and search result.
    this.roomReadCursorsTable = createTable("RoomReadCursorsTable", "room-read-cursors", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "cursor_key", type: dynamodb.AttributeType.STRING },
    });

    // Workspace join links — ONE row per Clerk org (PK org_id), holding the
    // SHA-256 of that workspace's live join token and nothing else that could
    // reconstruct it. Hash-only at rest, same posture as the bedrock-gate
    // tokens table; the plaintext exists once, in the mint response.
    //
    // No GSI and no TTL, both deliberate. Redeem is told which org to look up
    // (the join URL carries the org id beside the secret), so a token-hash
    // index would only add a second row that a concurrent regenerate could
    // orphan as an unrevokable live credential. And the owner's decision is
    // that join links do not expire — they end when an admin regenerates
    // (one PutItem over this row) or revokes (one DeleteItem).
    //
    // NOT ephemeral: losing these rows silently breaks every link an admin has
    // already shared, so it takes PITR like the rest of the durable tables.
    this.workspaceJoinLinksTable = createTable("WorkspaceJoinLinksTable", "workspace-join-links", {
      partitionKey: { name: "org_id", type: dynamodb.AttributeType.STRING },
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

    // Launch Competition waitlist.
    // Waitlist rows are keyed by lowercased email — the join to an account is
    // users.email (written by the Clerk user.created webhook). The counter row
    // (email = "__counter__") hands out positions atomically.
    this.launchWaitlistTable = createTable("LaunchWaitlistTable", "launch-waitlist", {
      partitionKey: { name: "email", type: dynamodb.AttributeType.STRING },
    });
    this.launchWaitlistTable.addGlobalSecondaryIndex({
      indexName: "referral-code-index",
      partitionKey: { name: "referral_code", type: dynamodb.AttributeType.STRING },
    });
    this.launchEntriesTable = createTable("LaunchEntriesTable", "launch-entries", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
    // Launch admin settings — a small single-row-per-id config table replacing
    // the retired schedule file + env vars; the backend reads it via
    // get_table("launch-settings"). Covered by the existing backend wildcard
    // table grant in service-stack.ts (table/*-{env}-*) — no IAM change needed.
    this.launchSettingsTable = createTable("LaunchSettingsTable", "launch-settings", {
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
    });

    // Guided-setup requests from the public /request-setup form, one row per
    // submission keyed by a random id; the backend writes via
    // get_table("setup-requests"). Covered by the backend wildcard table grant.
    createTable("SetupRequestsTable", "setup-requests", {
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
    });

    // Integration credentials under a KMS envelope (integration_secret_store, phase 2 task 5):
    // one row per owner + provider instead of one $0.40/mo Secrets Manager secret each. The row
    // holds ciphertext only; the data key is wrapped by the stage CMK with {owner_id, provider}
    // as encryption context. Covered by the backend wildcard table grant; swept by the owner wipe.
    createTable("CredentialEnvelopesTable", "credential-envelopes", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "provider", type: dynamodb.AttributeType.STRING },
    });

    new cdk.CfnOutput(this, "DynamoTablePrefix", {
      value: tablePrefix(env),
      exportName: `${this.stackName}-table-prefix`,
    });
  }
}
