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
 * These tests lock in the two invariants that make the export retirement safe:
 *  1. The database stack still publishes the two auto-generated ARN exports
 *     under their EXACT existing names (so the deploy that drops the import does
 *     not try to delete an in-use export — CloudFormation would refuse).
 *  2. The service stack grants the tables by name and no longer imports either
 *     ARN export.
 *
 * The export names are the live names published by isol8-<env>-database,
 * verified against deployed CloudFormation. The database stack is named
 * `isol8-<env>-database` here so `this.stackName` resolves to those names.
 */

const CONNECTIONS_ARN_EXPORT = (env: string) =>
  `isol8-${env}-database:ExportsOutputFnGetAttConnectionsTable8000B8A1Arn6FD6777C`;
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
      creditsTable: database.creditsTable,
      creditTransactionsTable: database.creditTransactionsTable,
      oauthTokensTable: database.oauthTokensTable,
      webhookDedupTable: database.webhookDedupTable,
      paperclipCompaniesTable: database.paperclipCompaniesTable,
      marketplaceListingsTable: database.marketplaceListingsTable,
      marketplacePurchasesTable: database.marketplacePurchasesTable,
      marketplacePayoutAccountsTable: database.marketplacePayoutAccountsTable,
      marketplaceTakedownsTable: database.marketplaceTakedownsTable,
      marketplaceSellersTable: database.marketplaceSellersTable,
      hookEventsTable: database.hookEventsTable,
    },
    secretNames: {
      clerkIssuer: `isol8/${environment}/clerk_issuer`,
      clerkSecretKey: `isol8/${environment}/clerk_secret_key`,
      stripeSecretKey: `isol8/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `isol8/${environment}/stripe_webhook_secret`,
      encryptionKey: `isol8/${environment}/encryption_key`,
      posthogProjectApiKey: `isol8/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `isol8/${environment}/paperclip_service_token_key`,
      gogKeyringPassword: `isol8/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
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

    test("database stack retains the connections ARN export under its exact live name (literal, not GetAtt — the table is gone)", () => {
      // Pinned with a literal ARN (Fn::Join over region/account) rather than
      // Fn::GetAtt, because the connections table was deleted out-of-band and a
      // GetAtt would fail at deploy time. The literal must end in the table name.
      database.hasOutput("*", {
        Export: { Name: CONNECTIONS_ARN_EXPORT(environment) },
        Value: `arn:aws:dynamodb:us-east-1:877352799272:table/isol8-${environment}-connections`,
      });
    });

    test("database stack retains the oauth-state ARN export under its exact live name", () => {
      database.hasOutput("*", {
        Export: { Name: OAUTH_STATE_ARN_EXPORT(environment) },
        Value: { "Fn::GetAtt": ["OauthStateTable1D8479B9", "Arn"] },
      });
    });

    test("service task role grants connections + oauth-state by name", () => {
      service.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({ Sid: "DynamoDbIntegrations" }),
          ]),
        },
      });
      const json = JSON.stringify(service.toJSON());
      expect(json).toContain(`:table/isol8-${environment}-connections`);
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
