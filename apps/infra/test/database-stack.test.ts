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
      TableName: "isol8-dev-admin-actions",
      KeySchema: [
        { AttributeName: "admin_user_id", KeyType: "HASH" },
        { AttributeName: "timestamp_action_id", KeyType: "RANGE" },
      ],
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("admin-actions has target-timestamp GSI for per-target audit queries", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-admin-actions",
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
      TableName: "isol8-dev-admin-actions",
      SSESpecification: Match.objectLike({
        SSEEnabled: true,
        SSEType: "KMS",
      }),
    });
  });

  test("admin-actions has point-in-time recovery enabled", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-admin-actions",
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test("admin-actions has NO TTL (audit rows kept forever)", () => {
    // CDK omits the TimeToLiveSpecification entirely when no TTL is set.
    // Assert it's absent on this specific table.
    const tables = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "isol8-dev-admin-actions" },
    });
    const tableLogicalIds = Object.keys(tables);
    expect(tableLogicalIds).toHaveLength(1);
    const props = tables[tableLogicalIds[0]].Properties;
    expect(props.TimeToLiveSpecification).toBeUndefined();
  });

  test("dev environment uses DESTROY removal policy (matches other dev tables)", () => {
    const tables = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "isol8-dev-admin-actions" },
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
      Properties: { TableName: "isol8-prod-admin-actions" },
    });
    const logicalId = Object.keys(tables)[0];
    expect(tables[logicalId].DeletionPolicy).toBe("Retain");
  });
});

describe("DatabaseStack — Paperclip Aurora cluster", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates Aurora Serverless v2 Postgres cluster with scale-to-zero", () => {
    template.hasResourceProperties("AWS::RDS::DBCluster", {
      DBClusterIdentifier: "isol8-dev-paperclip-db",
      Engine: "aurora-postgresql",
      DatabaseName: "paperclip",
      StorageEncrypted: true,
      ServerlessV2ScalingConfiguration: Match.objectLike({
        MinCapacity: 0,
        MaxCapacity: 4,
      }),
    });
  });

  test("Aurora cluster uses generated secret with deterministic name", () => {
    template.hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "isol8-dev-paperclip-db-credentials",
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
      DBClusterIdentifier: "isol8-dev-paperclip-db",
      BackupRetentionPeriod: 7,
    });
  });

  test("Aurora cluster uses SNAPSHOT removal policy regardless of env", () => {
    const clusters = template.findResources("AWS::RDS::DBCluster", {
      Properties: { DBClusterIdentifier: "isol8-dev-paperclip-db" },
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
      TableName: "isol8-dev-paperclip-companies",
      KeySchema: [{ AttributeName: "user_id", KeyType: "HASH" }],
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("paperclip-companies has by-status-purge-at GSI for the cleanup cron", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-paperclip-companies",
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
      TableName: "isol8-dev-paperclip-companies",
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
      TableName: "isol8-dev-paperclip-companies",
      SSESpecification: Match.objectLike({
        SSEEnabled: true,
        SSEType: "KMS",
      }),
    });
  });
});
