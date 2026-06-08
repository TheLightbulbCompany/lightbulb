import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { ContainerStack } from "../lib/stacks/container-stack";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

function buildDbStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");
  // DatabaseStack now requires a VPC for the Paperclip Aurora cluster.
  const network = new NetworkStack(app, `Network-${environment}`, {
    env,
    environment,
  });
  const dbStack = new DatabaseStack(app, `Database-${environment}`, {
    env,
    environment,
    kmsKey,
    vpc: network.vpc,
  });
  return Template.fromStack(dbStack);
}

/**
 * Builds a real ServiceStack wired to real upstream stacks (Network, Database,
 * Container). Mirrors the wiring in isol8-stage.ts so the synthesized template
 * matches what gets deployed. Copied verbatim from marketplace-resources.test.ts
 * — note the ServiceStack database props no longer include the connections /
 * oauth-state tables (credential-layer redesign).
 */
function buildServiceStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };

  // KMS key lives in a separate support stack (mirrors AuthStack).
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");

  const network = new NetworkStack(app, `Network-${environment}`, {
    env,
    environment,
  });

  const database = new DatabaseStack(app, `Database-${environment}`, {
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

  const service = new ServiceStack(app, `Service-${environment}`, {
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

  return Template.fromStack(service);
}

describe("DatabaseStack — integrations status table", () => {
  const template = buildDbStack("dev");

  test("creates isol8-dev-integrations table with owner_id/provider composite key + PAY_PER_REQUEST", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-integrations",
      KeySchema: [
        { AttributeName: "owner_id", KeyType: "HASH" },
        { AttributeName: "provider", KeyType: "RANGE" },
      ],
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("isol8-dev-integrations is customer-managed-KMS encrypted and PITR-enabled", () => {
    // A regression dropping CMK or PITR (e.g. swapping to AWS-managed or
    // removing point-in-time recovery) on the status index must fail here.
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-integrations",
      SSESpecification: Match.objectLike({ SSEEnabled: true, SSEType: "KMS" }),
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });
});

// Hoist a single ServiceStack synthesis and reuse across describe blocks.
const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");

describe("ServiceStack — integration secret + DDB IAM (dev)", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("task role has an IntegrationSecretsRW statement scoped to isol8/dev/integrations/*", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "IntegrationSecretsRW",
            Effect: "Allow",
            // CDK's arrayWith matches as an ordered subsequence, so list the
            // expected actions in the order the stack emits them.
            Action: Match.arrayWith([
              "secretsmanager:CreateSecret",
              "secretsmanager:PutSecretValue",
              "secretsmanager:DeleteSecret",
              "secretsmanager:GetSecretValue",
            ]),
            // stringLikeRegexp anchors with ^…$, so wrap with .* to assert the
            // ARN merely CONTAINS the integrations secret prefix.
            Resource: Match.stringLikeRegexp(
              ".*secret:isol8/dev/integrations/\\*",
            ),
          }),
        ]),
      },
    });
  });

  test("DynamoDbIntegrations statement covers the isol8-dev-integrations table", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "DynamoDbIntegrations",
            Action: Match.arrayWith([
              "dynamodb:GetItem",
              "dynamodb:PutItem",
              "dynamodb:Query",
            ]),
            Resource: Match.arrayWith([
              Match.stringLikeRegexp(".*:table/isol8-dev-integrations"),
            ]),
          }),
        ]),
      },
    });
  });
});

describe("ServiceStack — integration secret + DDB IAM (prod)", () => {
  const template = buildServiceStack("prod");

  test("task role IntegrationSecretsRW statement is scoped to isol8/prod/integrations/*", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "IntegrationSecretsRW",
            Effect: "Allow",
            Resource: Match.stringLikeRegexp(
              ".*secret:isol8/prod/integrations/\\*",
            ),
          }),
        ]),
      },
    });
  });

  test("DynamoDbIntegrations statement covers the isol8-prod-integrations table", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "DynamoDbIntegrations",
            Resource: Match.arrayWith([
              Match.stringLikeRegexp(".*:table/isol8-prod-integrations"),
            ]),
          }),
        ]),
      },
    });
  });
});
