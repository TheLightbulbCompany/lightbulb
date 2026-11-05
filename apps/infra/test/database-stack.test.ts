import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import { Match, Template } from "aws-cdk-lib/assertions";
import { DatabaseStack } from "../lib/stacks/database-stack";

function buildStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");
  const vpc = new ec2.Vpc(supportStack, "Vpc", { maxAzs: 2 });
  const databaseStack = new DatabaseStack(app, `Database-${environment}`, {
    env,
    environment,
    kmsKey,
    vpc,
  });
  return Template.fromStack(databaseStack);
}

describe("DatabaseStack — admin-actions table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates isol8-{env}-admin-actions with composite key", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-admin-actions",
      KeySchema: [
        { AttributeName: "admin_user_id", KeyType: "HASH" },
        { AttributeName: "timestamp_action_id", KeyType: "RANGE" },
      ],
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("admin-actions has target-timestamp GSI for per-target audit queries", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-admin-actions",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "target-timestamp-index",
          KeySchema: [
            { AttributeName: "target_user_id", KeyType: "HASH" },
            { AttributeName: "timestamp_action_id", KeyType: "RANGE" },
          ],
        }),
      ]),
    });
  });

  test("admin-actions uses customer-managed KMS encryption (matches other tables)", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-admin-actions",
      SSESpecification: Match.objectLike({
        SSEEnabled: true,
        SSEType: "KMS",
      }),
    });
  });

  test("admin-actions has point-in-time recovery enabled", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-admin-actions",
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test("admin-actions has NO TTL (audit rows kept forever)", () => {
    // CDK omits the TimeToLiveSpecification entirely when no TTL is set.
    // Assert it's absent on this specific table.
    const tables = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "lightbulb-dev-admin-actions" },
    });
    const tableLogicalIds = Object.keys(tables);
    expect(tableLogicalIds).toHaveLength(1);
    const props = tables[tableLogicalIds[0]].Properties;
    expect(props.TimeToLiveSpecification).toBeUndefined();
  });

  test("dev environment uses DESTROY removal policy (matches other dev tables)", () => {
    const tables = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "lightbulb-dev-admin-actions" },
    });
    const logicalId = Object.keys(tables)[0];
    expect(tables[logicalId].DeletionPolicy).toBe("Delete");
  });
});

describe("DatabaseStack — admin-actions table — prod", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("prod");
  });

  test("prod environment uses RETAIN removal policy", () => {
    const tables = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "lightbulb-prod-admin-actions" },
    });
    const logicalId = Object.keys(tables)[0];
    expect(tables[logicalId].DeletionPolicy).toBe("Retain");
  });
});

describe("DatabaseStack — native room read cursors", () => {
  test("creates one durable owner/profile/room cursor table without transcript indexes", () => {
    const template = buildStack("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-prod-room-read-cursors",
      KeySchema: [
        { AttributeName: "owner_id", KeyType: "HASH" },
        { AttributeName: "cursor_key", KeyType: "RANGE" },
      ],
      BillingMode: "PAY_PER_REQUEST",
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
    const tables = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "lightbulb-prod-room-read-cursors" },
    });
    const table = Object.values(tables)[0] as { Properties: Record<string, unknown> };
    expect(table.Properties.GlobalSecondaryIndexes).toBeUndefined();
  });
});

describe("DatabaseStack — Paperclip Aurora cluster", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates Aurora Serverless v2 Postgres cluster with scale-to-zero", () => {
    template.hasResourceProperties("AWS::RDS::DBCluster", {
      DBClusterIdentifier: "lightbulb-dev-paperclip-db",
      Engine: "aurora-postgresql",
      SnapshotIdentifier: "lightbulb-dev-paperclip-db-rename",
      StorageEncrypted: true,
      ServerlessV2ScalingConfiguration: Match.objectLike({
        MinCapacity: 0,
        MaxCapacity: 4,
      }),
    });
  });

  test("Aurora cluster uses generated secret with deterministic name", () => {
    template.hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "lightbulb-dev-paperclip-db-credentials",
    });
  });

  test("Aurora cluster gets a serverless v2 writer instance", () => {
    template.hasResourceProperties("AWS::RDS::DBInstance", {
      DBInstanceClass: "db.serverless",
      Engine: "aurora-postgresql",
    });
  });

  test("Paperclip security group has no ingress rules at this layer", () => {
    // Ingress is granted later by the consuming stacks (backend SG +
    // Paperclip task SG). The SG here must start closed so a misconfigured
    // consumer cannot accidentally make the DB world-reachable.
    //
    // Assert directly on SecurityGroupIngress (the real contract). The
    // older check on the allowAllOutbound:false egress sentinel was an
    // implementation-detail proxy that silently passed any SG — including
    // wide-open ones — if CDK ever changed the sentinel.
    const sgs = template.findResources("AWS::EC2::SecurityGroup", {
      Properties: {
        GroupDescription: Match.stringLikeRegexp("Paperclip Aurora cluster"),
      },
    });
    expect(Object.keys(sgs)).toHaveLength(1);
    const paperclipSg = Object.values(sgs)[0] as { Properties: any };
    expect(paperclipSg.Properties.SecurityGroupIngress ?? []).toEqual([]);
  });

  test("Aurora cluster has 7-day backup retention", () => {
    template.hasResourceProperties("AWS::RDS::DBCluster", {
      DBClusterIdentifier: "lightbulb-dev-paperclip-db",
      BackupRetentionPeriod: 7,
    });
  });

  test("Aurora cluster uses SNAPSHOT removal policy regardless of env", () => {
    const clusters = template.findResources("AWS::RDS::DBCluster", {
      Properties: { DBClusterIdentifier: "lightbulb-dev-paperclip-db" },
    });
    const logicalId = Object.keys(clusters)[0];
    expect(clusters[logicalId].DeletionPolicy).toBe("Snapshot");
  });
});

describe("DatabaseStack — paperclip-companies table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates isol8-{env}-paperclip-companies with user_id PK and PAY_PER_REQUEST", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-paperclip-companies",
      KeySchema: [{ AttributeName: "user_id", KeyType: "HASH" }],
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("paperclip-companies has by-status-purge-at GSI for the cleanup cron", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-paperclip-companies",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-status-purge-at",
          KeySchema: [
            { AttributeName: "status", KeyType: "HASH" },
            { AttributeName: "scheduled_purge_at", KeyType: "RANGE" },
          ],
          Projection: Match.objectLike({ ProjectionType: "KEYS_ONLY" }),
        }),
      ]),
    });
  });

  test("paperclip-companies has by-org-id GSI with ALL projection", () => {
    // get_org_company_id, count_org_members, _find_org_owner, and the
    // organization.deleted webhook sweep all query this GSI. Projection
    // must be ALL because the org-owner lookup needs full row data
    // (paperclip_password_encrypted etc).
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-paperclip-companies",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-org-id",
          KeySchema: [{ AttributeName: "org_id", KeyType: "HASH" }],
          Projection: Match.objectLike({ ProjectionType: "ALL" }),
        }),
      ]),
    });
  });

  test("paperclip-companies uses customer-managed KMS encryption (matches stack posture)", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-paperclip-companies",
      SSESpecification: Match.objectLike({
        SSEEnabled: true,
        SSEType: "KMS",
      }),
    });
  });
});

describe("DatabaseStack — bedrock-aip-owners reverse table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates the bedrock-aip-owners reverse table keyed on aip_arn", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-bedrock-aip-owners",
      KeySchema: [{ AttributeName: "aip_arn", KeyType: "HASH" }],
      AttributeDefinitions: Match.arrayWith([
        { AttributeName: "aip_arn", AttributeType: "S" },
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });
});

describe("DatabaseStack — metronome fast-gate table (Phase 2)", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates the metronome-fast-gate table keyed on owner_id", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-metronome-fast-gate",
      KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
      AttributeDefinitions: Match.arrayWith([
        { AttributeName: "owner_id", AttributeType: "S" },
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("metronome-fast-gate uses customer-managed KMS encryption (matches stack posture)", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-metronome-fast-gate",
      SSESpecification: Match.objectLike({
        SSEEnabled: true,
        SSEType: "KMS",
      }),
    });
  });
});

describe("DatabaseStack — agent email + company identity tables", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates the company-slugs table with a by-owner GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("company-slugs$"),
      KeySchema: [{ AttributeName: "slug", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner",
          KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
        }),
      ]),
    });
  });

  test("creates the agent-email-addresses table with a by-owner GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("agent-email-addresses$"),
      KeySchema: [{ AttributeName: "address", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner",
          KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
        }),
      ]),
    });
  });
});

describe("DatabaseStack — launch competition tables", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates launch-waitlist keyed by email with a referral-code GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-launch-waitlist",
      KeySchema: [{ AttributeName: "email", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "referral-code-index",
          KeySchema: [{ AttributeName: "referral_code", KeyType: "HASH" }],
        }),
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("creates launch-entries keyed by owner_id", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-launch-entries",
      KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
    });
  });

  test("creates setup-requests keyed by id", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-setup-requests",
      KeySchema: [{ AttributeName: "id", KeyType: "HASH" }],
    });
  });

  test("creates launch-settings keyed by id", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-launch-settings",
      KeySchema: [{ AttributeName: "id", KeyType: "HASH" }],
    });
  });
});

describe("DatabaseStack — billing-accounts table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("billing-accounts has the company-stripe-account-index GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("billing-accounts$"),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "company-stripe-account-index",
          KeySchema: [{ AttributeName: "company_stripe_account_id", KeyType: "HASH" }],
        }),
      ]),
    });
  });
});

describe("DatabaseStack — provisioning-workflows table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  // No open-row index: workflows advance on read, so nothing ever sweeps the
  // table for open rows.
  test("provisioning-workflows table with the owner index and no sweep index", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", Match.objectLike({
      TableName: Match.stringLikeRegexp("provisioning-workflows$"),
      KeySchema: [{ AttributeName: "workflow_id", KeyType: "HASH" }],
      GlobalSecondaryIndexes: [Match.objectLike({ IndexName: "owner-state-index" })],
    }));
  });
});

describe("DatabaseStack — apps table", () => {
  test("apps keyed by app_id with owner-index and name-index", () => {
    buildStack("dev").hasResourceProperties("AWS::DynamoDB::Table", Match.objectLike({
      TableName: Match.stringLikeRegexp("apps$"),
      KeySchema: [{ AttributeName: "app_id", KeyType: "HASH" }],
      GlobalSecondaryIndexes: [
        Match.objectLike({
          IndexName: "owner-index",
          KeySchema: [
            { AttributeName: "owner_id", KeyType: "HASH" },
            { AttributeName: "name", KeyType: "RANGE" },
          ],
        }),
        Match.objectLike({ IndexName: "name-index", KeySchema: [{ AttributeName: "name", KeyType: "HASH" }] }),
      ],
    }));
  });
});

describe("DatabaseStack — slack-installs table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates slack-installs keyed by team_id with a by-owner GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("slack-installs$"),
      KeySchema: [{ AttributeName: "team_id", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner",
          KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
          Projection: { ProjectionType: "ALL" },
        }),
      ]),
    });
  });
});

describe("DatabaseStack — workspace join links", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates workspace-join-links keyed by org_id, durable, no TTL", () => {
    // One row per org IS the link, which is what makes regenerate atomic — a
    // sort key or a token-hash GSI would reintroduce the orphaned-credential
    // race the repo module documents. And the owner's decision is that join
    // links never expire, so a TTL attribute would be a silent revoke.
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-workspace-join-links",
      KeySchema: [{ AttributeName: "org_id", KeyType: "HASH" }],
      BillingMode: "PAY_PER_REQUEST",
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      TimeToLiveSpecification: Match.absent(),
      GlobalSecondaryIndexes: Match.absent(),
    });
  });
});

describe("DatabaseStack — containers table", () => {
  test("per-task AgentCore session rows expire by TTL (session_ttl), in every env", () => {
    // Only `__session__#<owner>#task#<task>` rows carry session_ttl (container_repo): workspace
    // rows and container rows never do, so the TTL is inert for them.
    for (const environment of ["dev", "prod"] as const) {
      buildStack(environment).hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: `lightbulb-${environment}-containers`,
        TimeToLiveSpecification: { AttributeName: "session_ttl", Enabled: true },
      });
    }
  });
});
