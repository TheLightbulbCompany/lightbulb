import { tablePrefix } from "../lib/table-names";
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
  });
  return Template.fromStack(dbStack);
}

/**
 * Builds a real ServiceStack wired to real upstream stacks (Network, Database,
 * Container). Mirrors the wiring in lightbulb-stage.ts so the synthesized template
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
  });

  const container = new ContainerStack(app, `Container-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
  });

  const service = new ServiceStack(app, `Service-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    targetGroup: network.targetGroup,
    albSecurityGroup: network.albSecurityGroup,
    secretNames: {
      clerkIssuer: `isol8/${environment}/clerk_issuer`,
      clerkSecretKey: `isol8/${environment}/clerk_secret_key`,
      clerkWebhookSecret: `isol8/${environment}/clerk_webhook_secret`,
      stripeSecretKey: `isol8/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `isol8/${environment}/stripe_webhook_secret`,
      encryptionKey: `isol8/${environment}/encryption_key`,
      posthogProjectApiKey: `isol8/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `isol8/${environment}/paperclip_service_token_key`,
      paperclipEventsWebhookSecret: `isol8/${environment}/paperclip_events_webhook_secret`,
      gogKeyringPassword: `isol8/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      openaiApiKey: `lightbulb/${environment}/openai_api_key`,
      livekitApiKey: `lightbulb/${environment}/livekit_api_key`,
      livekitApiSecret: `lightbulb/${environment}/livekit_api_secret`,
      recallApiKey: `lightbulb/${environment}/recall_api_key`,
      recallWebhookSecret: `lightbulb/${environment}/recall_webhook_secret`,
      photonApiKey: `lightbulb/${environment}/photon_api_key`,
      photonProjectId: `lightbulb/${environment}/photon_project_id`,
      photonWebhookSecret: `lightbulb/${environment}/photon_webhook_secret`,
      huddleWorkerSecret: `lightbulb/${environment}/huddle_worker_secret`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
      wsInternalSecret: `isol8/${environment}/ws_internal_secret`,
      networkProofKey: `isol8/${environment}/network_proof_key`,
      sesIngestSecret: `isol8/${environment}/ses_ingest_secret`,
    },
    kmsKeyArn: kmsKey.keyArn,
    container: {
      cluster: container.cluster,
      cloudMapNamespace: container.cloudMapNamespace,
      containerSecurityGroup: container.containerSecurityGroup,
      ownerBedrockBasePolicy: container.ownerBedrockBasePolicy,
      ownerBedrockBoundary: container.ownerBedrockBoundary,
      workspaceMemoryId: container.workspaceMemory.attrMemoryId,
      sessionsMemoryId: container.sessionsMemory.attrMemoryId,
      workspaceBrowserId: container.workspaceBrowser.attrBrowserId,
    },
    managementApiUrl: "https://example.execute-api.us-east-1.amazonaws.com",
    webSocketUrl: "wss://example.execute-api.us-east-1.amazonaws.com",
    gatewayRelayUrl: "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws",
    connectionsTableName: `${tablePrefix(environment)}ws-connections`,
    wsApiId: "test-ws-api-id",
    wsStage: "prod",
    messagingTableName: `lightbulb-${environment}-messaging`,
    openClawWorkQueueUrl: `https://sqs.us-east-1.amazonaws.com/877352799272/lightbulb-${environment}-openclaw-work.fifo`,
    openClawWorkQueueArn: `arn:aws:sqs:us-east-1:877352799272:lightbulb-${environment}-openclaw-work.fifo`,
    appsyncEventsHttpEndpoint: `https://events-${environment}.appsync-api.us-east-1.amazonaws.com/event`,
    appsyncEventsRealtimeEndpoint: `wss://events-${environment}.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime`,
    appsyncEventsApiArn: `arn:aws:appsync:us-east-1:877352799272:apis/events-${environment}`,
    appsyncEventsRegion: "us-east-1",
  });

  return Template.fromStack(service);
}

describe("DatabaseStack — integrations status table", () => {
  const template = buildDbStack("dev");

  test("creates isol8-dev-integrations table with owner_id/provider composite key + PAY_PER_REQUEST", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-integrations",
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
      TableName: "lightbulb-dev-integrations",
      SSESpecification: Match.objectLike({ SSEEnabled: true, SSEType: "KMS" }),
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain(":table/*-dev-");
    expect(json).not.toContain(":table/lightbulb-prod-");
  });
});

// Hoist a single ServiceStack synthesis and reuse across describe blocks.
const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");

describe("ServiceStack — integration secret + DDB IAM (dev)", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("task role has an IntegrationSecretsRW statement scoped to isol8/dev/integrations/*", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
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
              ".*secret:lightbulb/dev/integrations/\\*",
            ),
          }),
        ]),
      },
    });
  });

  test("no RestoreSecret grant: a sealed put leaves the legacy secret alone, so nothing restores one", () => {
    expect(JSON.stringify(template.toJSON())).not.toContain("secretsmanager:RestoreSecret");
  });

  test("DynamoDbOwnedTables statement covers the lightbulb-dev-integrations table and its indexes", () => {
    // The by-name DynamoDbIntegrations statement was deleted 2026-09: exactly
    // subsumed by DynamoDbOwnedTables, which grants a superset of actions on
    // every table under the env-prefixed wildcard — integrations included.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "DynamoDbOwnedTables",
            Action: Match.arrayWith([
              "dynamodb:GetItem",
              "dynamodb:PutItem",
              "dynamodb:Query",
            ]),
            Resource: Match.arrayWith([
              Match.stringLikeRegexp(".*:table/lightbulb-dev-\\*$"),
              Match.stringLikeRegexp(".*:table/lightbulb-dev-\\*/index/\\*$"),
            ]),
          }),
        ]),
      },
    });
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain(":table/*-dev-");
    expect(json).not.toContain(":table/lightbulb-prod-");
  });
});

describe("ServiceStack — integration secret + DDB IAM (prod)", () => {
  const template = buildServiceStack("prod");

  test("task role IntegrationSecretsRW statement is scoped to isol8/prod/integrations/*", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "IntegrationSecretsRW",
            Effect: "Allow",
            Resource: Match.stringLikeRegexp(
              ".*secret:lightbulb/prod/integrations/\\*",
            ),
          }),
        ]),
      },
    });
  });

  test("DynamoDbOwnedTables statement covers the lightbulb-prod-integrations table and its indexes", () => {
    // The by-name DynamoDbIntegrations statement was deleted 2026-09: exactly
    // subsumed by DynamoDbOwnedTables, which grants a superset of actions on
    // every table under the env-prefixed wildcard — integrations included.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "DynamoDbOwnedTables",
            Resource: Match.arrayWith([
              Match.stringLikeRegexp(".*:table/lightbulb-prod-\\*$"),
              Match.stringLikeRegexp(".*:table/lightbulb-prod-\\*/index/\\*$"),
            ]),
          }),
        ]),
      },
    });
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain(":table/*-prod-");
    expect(json).not.toContain(":table/lightbulb-dev-");
  });
});
