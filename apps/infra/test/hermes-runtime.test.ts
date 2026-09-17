import { readFileSync } from "node:fs";
import { join } from "node:path";
import { tablePrefix } from "../lib/table-names";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { describe, expect, it } from "vitest";
import { ContainerStack } from "../lib/stacks/container-stack";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

// hermes-version.json is the single source of truth for the per-env promotion tag
// (M4-R34), mirroring how openclaw-version.json's dev.tag/prod.tag are read.
const HERMES_VERSION: { dev: { tag: string }; prod: { tag: string } } = JSON.parse(
  readFileSync(join(__dirname, "../../../hermes-version.json"), "utf8"),
);

/**
 * Builds real Container + Service stacks wired the way lightbulb-stage.ts
 * wires them (mirrors the local `buildServiceStack` in service-stack.test.ts).
 * Returns both templates: the backend task def lives in ServiceStack, the
 * per-owner AgentCore base policy + boundary live in ContainerStack.
 */
function buildStacks(environment: "dev" | "prod"): {
  serviceTemplate: Template;
  containerTemplate: Template;
} {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };

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
    gogKeyringPasswordSecretName: `lightbulb/${environment}/gog-keyring-password`,
    cloudflareWarpEnrollmentSecretName: `lightbulb/${environment}/cloudflare-warp-enrollment`,
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
      gogKeyringPassword: `lightbulb/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
      wsInternalSecret: `isol8/${environment}/ws_internal_secret`,
      sesIngestSecret: `isol8/${environment}/ses_ingest_secret`,
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
    gatewayRelayUrl: "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws",
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
    agentMailConfigurationSet: "isol8-test-agent-mail",
    vendorOrgId: environment === "prod" ? "org_3H3viGAfq2SXc9dIdcatmGXg8Td" : undefined,
    stripeProjectsCatalog: environment === "prod" ? "prod" : "testing",
  });

  return {
    serviceTemplate: Template.fromStack(service),
    containerTemplate: Template.fromStack(container),
  };
}

function backendContainerEnv(serviceTemplate: Template, environment: "dev" | "prod"): Record<string, string> {
  const resources = Object.values(
    serviceTemplate.findResources("AWS::ECS::TaskDefinition"),
  ) as Array<{
    Properties?: {
      Family?: string;
      ContainerDefinitions?: Array<{
        Name?: string;
        Environment?: Array<{ Name: string; Value: string }>;
      }>;
    };
  }>;
  const taskDef = resources.find(
    (resource) => resource.Properties?.Family === `lightbulb-${environment}-backend`,
  );
  const container = taskDef?.Properties?.ContainerDefinitions?.find(({ Name }) => Name === "backend");
  return Object.fromEntries((container?.Environment ?? []).map((e) => [e.Name, e.Value]));
}

/** Finds a managed policy by its `ManagedPolicyName`, not by scanning the whole template. */
function managedPolicyStatements(
  containerTemplate: Template,
  managedPolicyName: string,
): Array<{ Sid?: string; Action?: string | string[]; Resource?: string | string[] }> {
  const resources = Object.values(
    containerTemplate.findResources("AWS::IAM::ManagedPolicy", {
      Properties: { ManagedPolicyName: managedPolicyName },
    }),
  ) as Array<{
    Properties?: { PolicyDocument?: { Statement?: Array<{ Sid?: string; Action?: unknown; Resource?: unknown }> } };
  }>;
  expect(resources).toHaveLength(1);
  return (resources[0].Properties?.PolicyDocument?.Statement ?? []) as Array<{
    Sid?: string;
    Action?: string | string[];
    Resource?: string | string[];
  }>;
}

function flatten(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

describe("hermes runtime wiring", () => {
  const dev = buildStacks("dev");
  const prod = buildStacks("prod");

  // Each env's AGENTCORE_HERMES_IMAGE follows its own pin in hermes-version.json, and an unset
  // tag yields an EMPTY image rather than a repo with a dangling colon -- provision_hermes_runtime
  // refuses to run without an image, which is what kept prod inert while prod.tag was "".
  // prod.tag was set on 2026-09-17 for the founder migration; asserting it is still "" pinned a
  // moment, not a contract, and failed the day the promotion became real (M6-R58).
  it("pins each env's Hermes image to its own tag, and yields an empty image when a tag is unset", () => {
    const image = (tag: string) =>
      tag ? `877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:${tag}` : "";
    expect(backendContainerEnv(dev.serviceTemplate, "dev").AGENTCORE_HERMES_IMAGE).toBe(
      image(HERMES_VERSION.dev.tag),
    );
    expect(backendContainerEnv(prod.serviceTemplate, "prod").AGENTCORE_HERMES_IMAGE).toBe(
      image(HERMES_VERSION.prod.tag),
    );
  });

  it("wires HERMES_RUNTIME off in both envs at this point", () => {
    expect(backendContainerEnv(dev.serviceTemplate, "dev").HERMES_RUNTIME).toBe("false");
    expect(backendContainerEnv(prod.serviceTemplate, "prod").HERMES_RUNTIME).toBe("false");
  });

  it("names the state bucket for what it holds, while still accepting the old name during the transition", () => {
    const env = backendContainerEnv(dev.serviceTemplate, "dev");
    // OWNER_STATE_BUCKET is the new name; LITESTREAM_BUCKET rides alongside it
    // (M4-R6) so a CDK deploy and a backend roll can land in either order.
    // A later cleanup drops LITESTREAM_BUCKET once the backend only reads the
    // new name.
    expect(env.OWNER_STATE_BUCKET).toBe("lightbulb-dev-openclaw-replicas");
    expect(env.LITESTREAM_BUCKET).toBe("lightbulb-dev-openclaw-replicas");
  });

  it("lets the per-owner role pull the Hermes image in both the base policy and the boundary", () => {
    const baseStatements = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-base");
    const boundaryStatements = managedPolicyStatements(
      dev.containerTemplate,
      "lightbulb-dev-owner-bedrock-boundary",
    );
    const hermesRepoArn = "arn:aws:ecr:us-east-1:877352799272:repository/lightbulb/hermes-agent-arm64";

    for (const statements of [baseStatements, boundaryStatements]) {
      const pullStatement = statements.find((s) => flatten(s.Resource).includes(hermesRepoArn));
      expect(pullStatement).toBeDefined();
      expect(flatten(pullStatement!.Action)).toEqual(
        expect.arrayContaining(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]),
      );
      const tokenStatement = statements.find((s) => flatten(s.Action).includes("ecr:GetAuthorizationToken"));
      expect(tokenStatement).toBeDefined();
      expect(flatten(tokenStatement!.Resource)).toEqual(["*"]);
    }
  });

  it("lets the per-owner role read its own Hermes secrets at boot, in both the base policy and the boundary", () => {
    const baseStatements = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-base");
    const boundaryStatements = managedPolicyStatements(
      dev.containerTemplate,
      "lightbulb-dev-owner-bedrock-boundary",
    );
    const hermesSecretArn = "arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/hermes/*";

    for (const statements of [baseStatements, boundaryStatements]) {
      const secretsStatement = statements.find((s) =>
        flatten(s.Action).includes("secretsmanager:GetSecretValue"),
      );
      expect(secretsStatement).toBeDefined();
      expect(flatten(secretsStatement!.Resource)).toEqual([hermesSecretArn]);
    }
  });

  it("lets the BACKEND task role write the per-workspace boot secret and read/write the S3 profiles (the first dev migration run failed on both)", () => {
    // Searched across BOTH inline and managed policies: CDK packs the task role's grants into
    // the inline default policy and overflows into a managed policy as it fills, so the exact
    // placement shifts as grants change (see service-stack.test.ts's BedrockGateUrlParamRead).
    const statements: Array<{ Sid?: string; Action: string | string[]; Resource: string | string[] }> = [
      "AWS::IAM::Policy",
      "AWS::IAM::ManagedPolicy",
    ].flatMap((type) =>
      Object.values(dev.serviceTemplate.findResources(type)).flatMap(
        (policy: any) => policy.Properties?.PolicyDocument?.Statement ?? [],
      ),
    );
    const bySid = (sid: string) => statements.find((s) => s.Sid === sid);
    const wi = bySid("AgentCoreWorkloadIdentityManage");
    expect(wi).toBeDefined();
    expect(flatten(wi!.Action)).toEqual(expect.arrayContaining(["bedrock-agentcore:TagResource"]));

    const boot = bySid("HermesBootSecretsRW");
    expect(boot).toBeDefined();
    expect(flatten(boot!.Action)).toEqual(
      expect.arrayContaining(["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret"]),
    );
    expect(flatten(boot!.Resource)).toEqual(["arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/hermes/*"]);

    const profiles = bySid("HermesProfilesRW");
    expect(profiles).toBeDefined();
    expect(flatten(profiles!.Action)).toEqual(expect.arrayContaining(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]));
    // Scoped to the hermes/ sub-prefix only: the backend must stay out of the litestream replicas.
    expect(flatten(profiles!.Resource)).toEqual(["arn:aws:s3:::lightbulb-dev-openclaw-replicas/*/hermes/*"]);
  });
});
