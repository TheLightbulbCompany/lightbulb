import { readFileSync } from "node:fs";
import { join } from "node:path";
import { tablePrefix } from "../lib/table-names";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
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
    agentMailConfigurationSet: "isol8-test-agent-mail",
    vendorOrgId: environment === "prod" ? "org_3H3viGAfq2SXc9dIdcatmGXg8Td" : undefined,
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

// The service stack names the container stack's browser through a cross-stack import.
const CUSTOM_BROWSER_ARN = Match.objectLike({
  "Fn::Join": ["", Match.arrayWith(["arn:aws:bedrock-agentcore:us-east-1:877352799272:browser-custom/"])],
});

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

  it("names the state bucket for what it holds, and no longer ships the old name", () => {
    const env = backendContainerEnv(dev.serviceTemplate, "dev");
    // Every Hermes profile lives under <owner>/hermes/ in this bucket. The
    // litestream-era LITESTREAM_BUCKET alias is gone; HERMES_RUNTIME went with
    // it -- AgentCore is the only runtime, so there is no flag left to read.
    expect(env.OWNER_STATE_BUCKET).toBe("lightbulb-dev-openclaw-replicas");
    expect(env.LITESTREAM_BUCKET).toBeUndefined();
    expect(env.HERMES_RUNTIME).toBeUndefined();
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

  it("lets the per-owner role read only its own Hermes boot secret, in both the base policy and the boundary", () => {
    const baseStatements = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-base");
    const boundaryStatements = managedPolicyStatements(
      dev.containerTemplate,
      "lightbulb-dev-owner-bedrock-boundary",
    );
    const hermesSecretArn =
      "arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/hermes/${aws:PrincipalTag/owner_id}/boot-*";

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
    // placement shifts as grants change.
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
    // Profiles plus backend-owned install records; still no access to the old replicas.
    expect(flatten(profiles!.Resource)).toEqual([
      "arn:aws:s3:::lightbulb-dev-openclaw-replicas/*/hermes/*",
      "arn:aws:s3:::lightbulb-dev-openclaw-replicas/*/network/*",
      "arn:aws:s3:::lightbulb-dev-openclaw-replicas/marketplace-installs/*",
      "arn:aws:s3:::lightbulb-dev-openclaw-replicas/browser-state/*",
      "arn:aws:s3:::lightbulb-dev-openclaw-replicas/apps/*",
    ]);
  });

  // The built-in browser takes a new AWS address per session, and a moved session is signed out
  // by sites such as Google; this one leaves from the NAT's fixed address (dev 2026-10-26).
  it("starts every workspace session on one VPC-mode browser that may only leave on 80/443", () => {
    dev.containerTemplate.resourceCountIs("AWS::BedrockAgentCore::BrowserCustom", 1);
    const [browser] = Object.values(dev.containerTemplate.findResources("AWS::BedrockAgentCore::BrowserCustom"));
    expect(browser.Properties.Name).toBe("lightbulb_dev_workspace_browser");
    expect(browser.Properties.NetworkConfiguration.NetworkMode).toBe("VPC");
    const sgId = browser.Properties.NetworkConfiguration.VpcConfig.SecurityGroups[0]["Fn::GetAtt"][0];
    const sg = dev.containerTemplate.toJSON().Resources[sgId];
    expect(sg.Properties.SecurityGroupEgress.map((r: { IpProtocol: string; FromPort: number; CidrIp: string }) =>
      [r.IpProtocol, r.FromPort, r.CidrIp])).toEqual([["tcp", 443, "0.0.0.0/0"], ["tcp", 80, "0.0.0.0/0"]]);
    // Both envs are switched (VPC_BROWSER_ENVS): their runtimes run a bridge that accepts the
    // custom browser's CDP URL (prod rolled onto h-2026.10.26-4519359 first).
    expect(JSON.stringify(dev.serviceTemplate.findResources("AWS::ECS::TaskDefinition"))).toContain('"AGENTCORE_BROWSER_ID"');
    expect(JSON.stringify(prod.serviceTemplate.findResources("AWS::ECS::TaskDefinition"))).toContain('"AGENTCORE_BROWSER_ID"');
  });

  it("the owner boundary permits AgentCore Browser use, and the base policy grants none", () => {
    const boundary = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-boundary");
    const stmt = boundary.find((s) => s.Sid === "BoundaryAgentCoreBrowserUse");
    expect(flatten(stmt?.Action)).toEqual(expect.arrayContaining([
      "bedrock-agentcore:StartBrowserSession", "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:StopBrowserSession", "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:SaveBrowserSessionProfile", "bedrock-agentcore:InvokeBrowser"]));
    expect(flatten(stmt?.Action)).not.toContain("bedrock-agentcore:ListBrowserSessions"); // the §2c fence
    expect(flatten(stmt?.Resource).join(" ")).toContain(":aws:browser/aws.browser.v1");
    expect(JSON.stringify(stmt?.Resource)).toContain('"WorkspaceBrowser'); // the env's VPC browser
    const base = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-base");
    expect(base.some((s) => /Browser/.test(s.Sid ?? ""))).toBe(false); // the grant is inline, per owner
  });

  it("grants the backend task role browser profile, session and live-view management, and nothing more", () => {
    dev.serviceTemplate.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreBrowserManage",
            Action: Match.arrayEquals([
              "bedrock-agentcore:CreateBrowserProfile",
              "bedrock-agentcore:GetBrowserProfile",
              "bedrock-agentcore:ListBrowserProfiles",
              // Account wipe deletes the profile (backend PR #2793's reap_owner_bedrock_resources).
              "bedrock-agentcore:DeleteBrowserProfile",
            ]),
            // CFN collapses a single-element Resource array to a bare string.
            Resource: "arn:aws:bedrock-agentcore:us-east-1:877352799272:browser-profile/*",
          }),
          Match.objectLike({
            Sid: "AgentCoreBrowserSessionView",
            Action: Match.arrayEquals([
              "bedrock-agentcore:GetBrowserSession",
              "bedrock-agentcore:ConnectBrowserLiveViewStream",
            ]),
            Resource: Match.arrayEquals([
              CUSTOM_BROWSER_ARN,
              "arn:aws:bedrock-agentcore:us-east-1:aws:browser/aws.browser.v1",
            ]),
          }),
          Match.objectLike({
            Sid: "AgentCoreBrowserSessionStart",
            // CDK collapses a single action to a bare string.
            Action: "bedrock-agentcore:StartBrowserSession",
            Resource: Match.arrayEquals([
              CUSTOM_BROWSER_ARN,
              "arn:aws:bedrock-agentcore:us-east-1:aws:browser/aws.browser.v1",
              "arn:aws:bedrock-agentcore:us-east-1:877352799272:browser-profile/*",
            ]),
          }),
          Match.objectLike({
            Sid: "AgentCoreBrowserSessionControl",
            Action: Match.arrayEquals([
              "bedrock-agentcore:StopBrowserSession",
              "bedrock-agentcore:UpdateBrowserStream",
              "bedrock-agentcore:ConnectBrowserAutomationStream",
            ]),
            Resource: Match.arrayEquals([
              CUSTOM_BROWSER_ARN,
              "arn:aws:bedrock-agentcore:us-east-1:aws:browser/aws.browser.v1",
            ]),
          }),
        ]),
      }),
    });

    // Native SaveBrowserSessionProfile is deliberately absent: v2 persists via the released
    // agent-browser state save/load path instead.
    const doc = JSON.stringify([dev.serviceTemplate.findResources("AWS::IAM::Policy"), dev.serviceTemplate.findResources("AWS::IAM::ManagedPolicy")]);
    expect(doc).not.toContain("bedrock-agentcore:SaveBrowserSessionProfile");
  });

  // Workspace memory (Track G). One shared resource per env; tenants are isolated by the per-owner
  // inline policy, so the boundary must never admit the record get/delete/Batch* actions -- they
  // carry no condition keys and would let one owner reach another's records by id (dev spike 09-22).
  it("creates one workspace memory per env (facts namespaced by actor = owner) and a sessions memory (episodes by user)", () => {
    dev.containerTemplate.resourceCountIs("AWS::BedrockAgentCore::Memory", 2);
    dev.containerTemplate.hasResourceProperties("AWS::BedrockAgentCore::Memory", {
      Name: "lightbulb_dev_workspace_memory",
      EventExpiryDuration: 7,
      MemoryStrategies: [
        { CustomMemoryStrategy: Match.objectLike({ Name: "workspace_claims", Namespaces: ["/owners/{actorId}/"] }) },
      ],
    });
    dev.containerTemplate.hasResourceProperties("AWS::BedrockAgentCore::Memory", {
      Name: "lightbulb_dev_sessions_memory",
      MemoryStrategies: [{ EpisodicMemoryStrategy: Match.objectLike({ Name: "session_episodes" }) }],
    });
    // Prod memory is owner data: a later edit that drops applyRemovalPolicy must fail here.
    prod.containerTemplate.hasResource("AWS::BedrockAgentCore::Memory", {
      Properties: Match.objectLike({ Name: "lightbulb_prod_workspace_memory" }),
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
    });
  });

  it("the owner boundary permits only conditionable memory actions, and the base policy grants none", () => {
    const boundary = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-boundary");
    const stmt = boundary.find((s) => s.Sid === "BoundaryAgentCoreMemoryUse");
    expect(flatten(stmt?.Action).sort()).toEqual([
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:ListSessions",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]);
    // Scoped to THE memory (a GetAtt), never "*".
    expect(JSON.stringify(stmt?.Resource)).toContain("WorkspaceMemory");
    const all = JSON.stringify(boundary);
    for (const forbidden of ["GetMemoryRecord", "DeleteMemoryRecord", "BatchDeleteMemoryRecords",
      "BatchCreateMemoryRecords", "BatchUpdateMemoryRecords", "ListActors"]) {
      expect(all).not.toContain(`bedrock-agentcore:${forbidden}`);
    }
    const base = managedPolicyStatements(dev.containerTemplate, "lightbulb-dev-owner-bedrock-base");
    expect(JSON.stringify(base)).not.toContain("MemoryRecords");
  });

  it("grants the backend task role the memory wipe, review and recall actions", () => {
    dev.serviceTemplate.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreMemoryWipe",
            Action: Match.arrayEquals([
              "bedrock-agentcore:ListMemoryRecords",
              "bedrock-agentcore:BatchDeleteMemoryRecords",
              "bedrock-agentcore:ListSessions",
              "bedrock-agentcore:ListEvents",
              "bedrock-agentcore:DeleteEvent",
              "bedrock-agentcore:ListActors",
            ]),
          }),
        ]),
      }),
    });
    dev.serviceTemplate.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreMemoryReview",
            Action: Match.arrayEquals([
              "bedrock-agentcore:GetMemoryRecord",
              "bedrock-agentcore:BatchCreateMemoryRecords",
            ]),
          }),
        ]),
      }),
    });
    const env = backendContainerEnv(dev.serviceTemplate, "dev") as Record<string, unknown>;
    expect(env.AGENTCORE_MEMORY_ID).toBeDefined();
    // Recall (task 1 of the workspace memory plan): the backend now searches and writes events.
    dev.serviceTemplate.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreMemoryRecall",
            Action: ["bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:CreateEvent"],
          }),
        ]),
      }),
    });
  });
});
