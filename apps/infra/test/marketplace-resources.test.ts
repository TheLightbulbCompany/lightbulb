import { tablePrefix } from "../lib/table-names";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { ROOT_DOMAIN } from "../lib/domains";
import { ContainerStack } from "../lib/stacks/container-stack";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/**
 * Builds a real ServiceStack wired to real upstream stacks (Network, Database,
 * Container). Mirrors the wiring in lightbulb-stage.ts so the synthesized template
 * matches what gets deployed.
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
      gogKeyringPassword: `lightbulb/${environment}/gog-keyring-password`,
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

const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");

describe("ServiceStack — retired marketplace bucket", () => {
  test("no marketplace-artifacts bucket is defined", () => {
    SERVICE_TEMPLATE_DEV.resourcePropertiesCountIs(
      "AWS::S3::Bucket",
      { BucketName: "lightbulb-dev-marketplace-artifacts" },
      0,
    );
  });
});

describe("ServiceStack — gog file-keyring password", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("backend task def injects GOG_KEYRING_PASSWORD from the shared secret", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Secrets: Match.arrayWith([
            Match.objectLike({
              Name: "GOG_KEYRING_PASSWORD",
              // fromSecretNameV2 renders ValueFrom as a Fn::Join building the
              // secret ARN; assert the secret name appears inside that join.
              ValueFrom: Match.objectLike({
                "Fn::Join": Match.arrayWith([
                  Match.arrayWith([
                    Match.stringLikeRegexp("lightbulb/dev/gog-keyring-password"),
                  ]),
                ]),
              }),
            }),
          ]),
        }),
      ]),
    });
  });
});
