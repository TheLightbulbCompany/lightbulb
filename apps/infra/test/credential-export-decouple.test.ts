import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { ContainerStack } from "../lib/stacks/container-stack";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/**
 * Phase 1 of the credential-layer redesign: the backend service stack must
 * grant the credential `connections` + `oauth-state` tables by NAME, not via
 * grantReadWriteData. grantReadWriteData exported each table's ARN from the
 * database stack and imported it into the service stack — a cross-stack export.
 * An export that another stack imports cannot be deleted, which is what blocked
 * renaming/replacing the table (the 2026-05-31 incident).
 *
 * The `connections` (credential-vault) table itself was retired 2026-07-14 —
 * it had no remaining consumers, so the table and its pinned ARN export were
 * deleted outright. The `oauth-state` TABLE is still live, but its pinned ARN
 * export was retired in #1291 (phase 2 of the two-phase removal), after
 * `cloudformation list-imports` confirmed the export had zero importers.
 *
 * So this file now locks in the END STATE of the decouple: neither credential
 * table publishes an ARN export, and the service stack grants oauth-state by
 * NAME rather than importing one. Re-introducing a cross-stack ARN reference
 * (e.g. grantReadWriteData on oauthStateTable) would make CDK regenerate the
 * auto-export and restore exactly the coupling that blocked renaming the table
 * in the 2026-05-31 incident — which is what the assertions below prevent.
 *
 * The export name below is the historical live name published by
 * isol8-<env>-database. The database stack is named `isol8-<env>-database`
 * here so `this.stackName` resolves to that name.
 */

const OAUTH_STATE_ARN_EXPORT = (env: string) =>
  `isol8-${env}-database:ExportsOutputFnGetAttOauthStateTable1D8479B9Arn3B610B82`;

function build(environment: "dev" | "prod"): {
  database: Template;
  service: Template;
} {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };

  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");

  const network = new NetworkStack(app, `Network-${environment}`, {
    env,
    environment,
  });

  // Name the stack exactly as it deploys so this.stackName-derived export
  // names match the live names asserted below.
  const database = new DatabaseStack(app, `isol8-${environment}-database`, {
    env,
    environment,
    kmsKey,
    vpc: network.vpc,
  });

  const container = new ContainerStack(app, `Container-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    kmsKeyArn: kmsKey.keyArn,
    gogKeyringPasswordSecretName: `isol8/${environment}/gog-keyring-password`,
  });

  const service = new ServiceStack(app, `isol8-${environment}-service`, {
    env,
    environment,
    vpc: network.vpc,
    targetGroup: network.targetGroup,
    albSecurityGroup: network.albSecurityGroup,
    database: {
      usersTable: database.usersTable,
      containersTable: database.containersTable,
      billingTable: database.billingTable,
      usageCountersTable: database.usageCountersTable,
      pendingUpdatesTable: database.pendingUpdatesTable,
      channelLinksTable: database.channelLinksTable,
      adminActionsTable: database.adminActionsTable,
      oauthTokensTable: database.oauthTokensTable,
      webhookDedupTable: database.webhookDedupTable,
      paperclipCompaniesTable: database.paperclipCompaniesTable,
      marketplaceListingsTable: database.marketplaceListingsTable,
      marketplacePurchasesTable: database.marketplacePurchasesTable,
      marketplacePayoutAccountsTable: database.marketplacePayoutAccountsTable,
      marketplaceTakedownsTable: database.marketplaceTakedownsTable,
      marketplaceSellersTable: database.marketplaceSellersTable,
      hookEventsTable: database.hookEventsTable,
      opsCacheTable: database.opsCacheTable,
    },
    secretNames: {
      clerkIssuer: `isol8/${environment}/clerk_issuer`,
      clerkSecretKey: `isol8/${environment}/clerk_secret_key`,
      stripeSecretKey: `isol8/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `isol8/${environment}/stripe_webhook_secret`,
      encryptionKey: `isol8/${environment}/encryption_key`,
      posthogProjectApiKey: `isol8/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `isol8/${environment}/paperclip_service_token_key`,
      platformOpsTokenKey: `isol8/${environment}/platform_ops_token_key`,
      gogKeyringPassword: `isol8/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
      resendApiKey: `isol8/${environment}/resend_api_key`,
      wsInternalSecret: `isol8/${environment}/ws_internal_secret`,
    },
    kmsKeyArn: kmsKey.keyArn,
    container: {
      cluster: container.cluster,
      cloudMapNamespace: container.cloudMapNamespace,
      cloudMapService: container.cloudMapService,
      efsFileSystem: container.efsFileSystem,
      efsSecurityGroup: container.efsSecurityGroup,
      containerSecurityGroup: container.containerSecurityGroup,
      taskExecutionRole: container.taskExecutionRole,
      taskRole: container.taskRole,
      openclawTaskDef: container.openclawTaskDef,
      ownerBedrockBasePolicy: container.ownerBedrockBasePolicy,
      ownerBedrockBoundary: container.ownerBedrockBoundary,
    },
    managementApiUrl: "https://example.execute-api.us-east-1.amazonaws.com",
    webSocketUrl: "wss://example.execute-api.us-east-1.amazonaws.com",
    connectionsTableName: `isol8-${environment}-ws-connections`,
    wsApiId: "test-ws-api-id",
    wsStage: "prod",
  });

  return {
    database: Template.fromStack(database),
    service: Template.fromStack(service),
  };
}

describe.each(["dev", "prod"] as const)(
  "credential export decouple — %s",
  (environment) => {
    let database: Template;
    let service: Template;

    beforeAll(() => {
      ({ database, service } = build(environment));
    });

    test("database stack no longer publishes the connections (credential-vault) ARN export or table", () => {
      const outputs = database.toJSON().Outputs ?? {};
      const connectionsOutputKeys = Object.keys(outputs).filter((key) =>
        key.includes("ConnectionsTable"),
      );
      expect(connectionsOutputKeys).toEqual([]);

      const connectionsResources = database.findResources(
        "AWS::DynamoDB::Table",
        { Properties: { TableName: `isol8-${environment}-connections` } },
      );
      expect(Object.keys(connectionsResources)).toEqual([]);
    });

    test("database stack no longer publishes the oauth-state ARN export", () => {
      // Retired in #1291 (phase 2) once list-imports showed zero importers.
      // Asserted on Outputs specifically: the table resource itself stays.
      const outputs = JSON.stringify(database.toJSON().Outputs ?? {});
      expect(outputs).not.toContain(OAUTH_STATE_ARN_EXPORT(environment));
      expect(outputs).not.toContain("ExportsOutputFnGetAttOauthStateTable");
    });

    test("database stack still owns the oauth-state table and its NAME export", () => {
      // The decouple removed the ARN export, not the table. The name export is
      // a stable literal (`<stack>-oauth-state-table`), not a GetAtt auto-export,
      // so it never created the rename-blocking coupling.
      database.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: `isol8-${environment}-oauth-state`,
      });
      database.hasOutput("*", {
        Export: { Name: `isol8-${environment}-database-oauth-state-table` },
      });
    });

    test("service task role grants oauth-state by name", () => {
      service.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({ Sid: "DynamoDbIntegrations" }),
          ]),
        },
      });
      const json = JSON.stringify(service.toJSON());
      expect(json).toContain(`:table/isol8-${environment}-oauth-state`);
    });

    test("service stack no longer imports either credential-table ARN", () => {
      const json = JSON.stringify(service.toJSON());
      // The credential tables were exported from the database stack as
      // GetAtt...Arn exports and imported here via grantReadWriteData. Those
      // imports are what blocked the rename; they must be gone. Match the
      // GetAtt-Arn export form specifically: the bare logical id
      // ConnectionsTable8000B8A1 also names the unrelated WebSocket connections
      // table in the api stack (same construct id), imported as a Ref — that
      // one legitimately stays and is out of scope here.
      expect(json).not.toContain("ExportsOutputFnGetAttConnectionsTable");
      expect(json).not.toContain("ExportsOutputFnGetAttOauthStateTable");
    });
  },
);
