import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { ContainerStack } from "../lib/stacks/container-stack";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/**
 * Phase 3 of the credential-layer redesign: the api-keys DynamoDB table is
 * retired. Its secrets moved to Secrets Manager (per-user LLM keys under
 * isol8/<env>/user-keys/* and per-agent Paperclip keys under
 * isol8/<env>/paperclip-agent-keys/*). The backend no longer reads or writes
 * the table, so the service stack no longer imports its ARN via
 * grantReadWriteData.
 *
 * These tests lock in the invariants that make dropping the import safe — the
 * same pattern proven for the connections/oauth-state retirement:
 *  1. The database stack still publishes BOTH auto-generated api-keys exports
 *     (Arn via GetAtt, and the table name via Ref) under their EXACT existing
 *     names. CloudFormation refuses to delete an export another stack still
 *     imports, so the export must outlive the import-removal deploy (Pass-1).
 *  2. The service stack grants the Paperclip agent keys via a scoped
 *     Secrets Manager statement (the api-keys replacement) and no longer
 *     imports the api-keys table ARN.
 *
 * The export names are the live names published by isol8-<env>-database,
 * verified against the exportValue calls pinned in database-stack.ts. The
 * database stack is named `isol8-<env>-database` here so `this.stackName`
 * resolves to those names.
 */

const API_KEYS_ARN_EXPORT = (env: string) =>
  `isol8-${env}-database:ExportsOutputFnGetAttApiKeysTable9F4DC7E7ArnE84F3BB0`;
const API_KEYS_REF_EXPORT = (env: string) =>
  `isol8-${env}-database:ExportsOutputRefApiKeysTable9F4DC7E79648F444`;

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
  "api-keys export decouple — %s",
  (environment) => {
    let database: Template;
    let service: Template;

    beforeAll(() => {
      ({ database, service } = build(environment));
    });

    test("database stack retains the api-keys ARN export under its exact live name", () => {
      database.hasOutput("*", {
        Export: { Name: API_KEYS_ARN_EXPORT(environment) },
        Value: { "Fn::GetAtt": ["ApiKeysTable9F4DC7E7", "Arn"] },
      });
    });

    test("database stack retains the api-keys Ref (table-name) export under its exact live name", () => {
      database.hasOutput("*", {
        Export: { Name: API_KEYS_REF_EXPORT(environment) },
        Value: { Ref: "ApiKeysTable9F4DC7E7" },
      });
    });

    test("service task role grants paperclip-agent-keys secrets (the api-keys replacement)", () => {
      service.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({ Sid: "PaperclipAgentKeysRW" }),
          ]),
        },
      });
      const json = JSON.stringify(service.toJSON());
      expect(json).toContain(
        `secret:isol8/${environment}/paperclip-agent-keys/*`,
      );
    });

    test("service stack no longer imports the api-keys table ARN", () => {
      const json = JSON.stringify(service.toJSON());
      // grantReadWriteData on the api-keys table exported its ARN from the
      // database stack and imported it here as a GetAtt...Arn export. That
      // import is what blocked retiring the table; it must be gone now that
      // the backend reads/writes Secrets Manager instead.
      expect(json).not.toContain("ExportsOutputFnGetAttApiKeysTable");
    });
  },
);
