import * as fs from "fs";
import * as path from "path";
import { tablePrefix } from "../lib/table-names";
import * as cdk from "aws-cdk-lib";
import { AssetManifestArtifact } from "aws-cdk-lib/cx-api";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { vi } from "vitest";
import { ContainerStack } from "../lib/stacks/container-stack";
import { LAUNCH_DOMAIN, ROOT_DOMAIN } from "../lib/domains";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/**
 * Builds a real ServiceStack wired to real upstream stacks (Network, Database,
 * Container). Mirrors the wiring in lightbulb-stage.ts (and marketplace-resources.test.ts)
 * so the synthesized template matches what gets deployed.
 */
function buildServiceStack(environment: "dev" | "prod", inspectAssets?: (images: { hash: string; platform: string }[]) => void): Template {
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
      clerkIssuer: `lightbulb/${environment}/clerk_issuer`,
      clerkSecretKey: `lightbulb/${environment}/clerk_secret_key`,
      clerkWebhookSecret: `lightbulb/${environment}/clerk_webhook_secret`,
      stripeSecretKey: `lightbulb/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `lightbulb/${environment}/stripe_webhook_secret`,
      encryptionKey: `lightbulb/${environment}/encryption_key`,
      posthogProjectApiKey: `lightbulb/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `lightbulb/${environment}/paperclip_service_token_key`,
      gogKeyringPassword: `lightbulb/${environment}/gog-keyring-password`,
      metronomeApiKey: `lightbulb/${environment}/metronome_api_key`,
      openaiApiKey: `lightbulb/${environment}/openai_api_key`,
      livekitApiKey: `lightbulb/${environment}/livekit_api_key`,
      livekitApiSecret: `lightbulb/${environment}/livekit_api_secret`,
      recallApiKey: `lightbulb/${environment}/recall_api_key`,
      recallWebhookSecret: `lightbulb/${environment}/recall_webhook_secret`,
      photonApiKey: `lightbulb/${environment}/photon_api_key`,
      photonProjectId: `lightbulb/${environment}/photon_project_id`,
      photonWebhookSecret: `lightbulb/${environment}/photon_webhook_secret`,
      huddleWorkerSecret: `lightbulb/${environment}/huddle_worker_secret`,
      metronomeWebhookSecret: `lightbulb/${environment}/metronome_webhook_secret`,
      wsInternalSecret: `lightbulb/${environment}/ws_internal_secret`,
      networkProofKey: `lightbulb/${environment}/network_proof_key`,
      sesIngestSecret: `lightbulb/${environment}/ses_ingest_secret`,
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
    // Mirrors lightbulb-stage.ts's VENDOR_ORG_ID_BY_ENV["prod"]; left unset
    // for dev here so the untested env keeps exercising the "" default.
    vendorOrgId: environment === "prod" ? "org_3H3viGAfq2SXc9dIdcatmGXg8Td" : undefined,
  });

  if (inspectAssets) {
    const assembly = app.synth();
    const platforms = assembly.artifacts.filter(AssetManifestArtifact.isAssetManifestArtifact).flatMap((artifact) => {
      const file = path.isAbsolute(artifact.file) ? artifact.file : path.join(assembly.directory, artifact.file);
      return Object.entries(JSON.parse(fs.readFileSync(file, "utf8")).dockerImages ?? {}).map(([hash, image]: [string, any]) => ({ hash, platform: image.source.platform }));
    });
    inspectAssets(platforms);
  }
  return Template.fromStack(service);
}

// Hoist a single ServiceStack synthesis and reuse across tests.
const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");

describe("ServiceStack — certified phone provisioning", () => {
  test("prod enables explicit Go live purchases and injects scoped provider configuration without plaintext credentials", () => {
    const prod = buildServiceStack("prod");
    const tasks = prod.findResources("AWS::ECS::TaskDefinition");
    const backend = Object.values(tasks).flatMap((r: any) => r.Properties.ContainerDefinitions).find((c: any) => c.Name === "backend");
    expect(backend.Environment).toContainEqual({ Name: "VOICE_BUY_NUMBERS", Value: "true" });
    for (const name of ["TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET", "TWILIO_TRUNK_SID", "VOICE_INBOUND_TRUNK_ID", "VOICE_OUTBOUND_TRUNK_ID"]) {
      const secret = backend.Secrets.find((entry: any) => entry.Name === name);
      expect(JSON.stringify(secret.ValueFrom)).toContain("lightbulb/prod/voice_telephony");
      expect(backend.Environment.some((entry: any) => entry.Name === name)).toBe(false);
    }
  });

  test("dev cannot purchase production numbers or receive production telephony credentials", () => {
    const tasks = SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::TaskDefinition");
    const backend = Object.values(tasks).flatMap((r: any) => r.Properties.ContainerDefinitions).find((c: any) => c.Name === "backend");
    expect(backend.Environment).toContainEqual({ Name: "VOICE_BUY_NUMBERS", Value: "false" });
    expect(backend.Secrets.some((entry: any) => entry.Name === "TWILIO_API_KEY_SECRET")).toBe(false);
  });
});

describe("ServiceStack — desktop execution rollout", () => {
  test("defaults closed without an owner", () => {
    vi.stubEnv("DESKTOP_EXECUTION_OWNER_DEV", "");
    vi.stubEnv("DESKTOP_EXECUTION_OWNER_PROD", "");
    try {
      buildServiceStack("dev").hasResourceProperties("AWS::ECS::TaskDefinition", {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Name: "backend",
            Environment: Match.arrayWith([
              { Name: "DESKTOP_EXECUTION_ENABLED", Value: "false" },
              { Name: "DESKTOP_EXECUTION_OWNER_ALLOWLIST", Value: "" },
            ]),
          }),
        ]),
      });
    } finally {
      vi.unstubAllEnvs();
    }
  });

  test("admits exactly the configured dev owner without enabling prod", () => {
    vi.stubEnv("DESKTOP_EXECUTION_OWNER_DEV", "user_disposable");
    vi.stubEnv("DESKTOP_EXECUTION_OWNER_PROD", "");
    try {
      const dev = buildServiceStack("dev");
      const prod = buildServiceStack("prod");
      for (const template of [dev, prod]) {
        const enabled = template === dev;
        template.hasResourceProperties("AWS::ECS::TaskDefinition", {
          ContainerDefinitions: Match.arrayWith([
            Match.objectLike({
              Name: "backend",
              Environment: Match.arrayWith([
                { Name: "DESKTOP_EXECUTION_ENABLED", Value: enabled ? "true" : "false" },
                { Name: "DESKTOP_EXECUTION_OWNER_ALLOWLIST", Value: enabled ? "user_disposable" : "" },
              ]),
            }),
          ]),
        });
      }
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("ServiceStack — clean handoff / rolling deploy", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("backend can only read and retire legacy per-owner OpenRouter secrets", () => {
    const statements = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].flatMap(
      (type) => Object.values(template.findResources(type)).flatMap(
        (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
      ),
    );
    const grant = statements.find((statement: any) => statement.Sid === "OpenRouterOwnerSecretsRW");
    expect(grant).toMatchObject({
      Effect: "Allow",
      Resource: "arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/openrouter/owners/*",
    });
    expect([...grant.Action].sort()).toEqual([
      "secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue",
    ]);
    const gateUrl = statements.find((statement: any) => statement.Sid === "OpenRouterGateUrlParamRead");
    expect(gateUrl).toMatchObject({
      Action: "ssm:GetParameter",
      Resource: "arn:aws:ssm:us-east-1:877352799272:parameter/lightbulb/dev/openrouter-gate-url",
    });
    const hermesBoot = statements.find((statement: any) => statement.Sid === "HermesBootSecretsRW");
    expect(hermesBoot.Action).toContain("secretsmanager:UpdateSecretVersionStage");
    expect(hermesBoot.Resource).toBe(
      "arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/hermes/*",
    );
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([Match.objectLike({
        Name: "backend",
        Environment: Match.arrayWith([
          Match.objectLike({ Name: "OPENROUTER_WORKSPACE_ID", Value: "" }),
          Match.objectLike({ Name: "OPENROUTER_MANAGED_MODEL_ID", Value: "" }),
        ]),
      })]),
    });
  });

  test("backend receives the dedicated gate bearer HMAC key as a secret", () => {
    const backend = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .find((c: any) => c.Name === "backend");
    const entry = backend.Secrets.find((sec: any) => sec.Name === "GATE_BEARER_HMAC_KEY");
    expect(JSON.stringify(entry.ValueFrom)).toContain("secret:lightbulb/dev/gate_bearer_hmac_key");
  });

  test("wires the LiveKit secrets and a dark-by-default LIVEKIT_URL", () => {
    const backend = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .find((c: any) => c.Name === "backend");
    const names = backend.Secrets.map((s: any) => s.Name);
    expect(names).toEqual(expect.arrayContaining(["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "HUDDLE_WORKER_SECRET"]));
    const env = Object.fromEntries(backend.Environment.map((e: any) => [e.Name, e.Value]));
    expect(env.LIVEKIT_URL).toBe("");
  });

  test("wires the Recall secrets into the backend task", () => {
    const backend = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .find((c: any) => c.Name === "backend");
    const names = backend.Secrets.map((s: any) => s.Name);
    expect(names).toEqual(expect.arrayContaining(["RECALL_API_KEY", "RECALL_WEBHOOK_SECRET"]));
  });

  test("backend task carries the Photon secrets", () => {
    const secrets = JSON.stringify(template.findResources("AWS::ECS::TaskDefinition"));
    for (const name of ["PHOTON_API_KEY", "PHOTON_PROJECT_ID", "PHOTON_WEBHOOK_SECRET"]) expect(secrets).toContain(name);
    expect(secrets).not.toContain("SENDBLUE_");
  });

  test("wires the voice key and the inference markup into the backend task", () => {
    const backend = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .find((c: any) => c.Name === "backend");
    expect(backend.Secrets.map((s: any) => s.Name)).toContain("OPENAI_API_KEY");
    const env = Object.fromEntries(backend.Environment.map((e: any) => [e.Name, e.Value]));
    expect(env.OPENROUTER_MARKUP).toBe(
      JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "..", "openrouter-pricing.json"), "utf8")).markup,
    );
  });

  test("backend receives the hook grant key as a secret", () => {
    const backend = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .find((c: any) => c.Name === "backend");
    const entry = backend.Secrets.find((sec: any) => sec.Name === "HOOK_GRANT_SIGNING_KEY");
    expect(JSON.stringify(entry.ValueFrom)).toContain("secret:lightbulb/dev/hook_grant_signing_key");

    const lambdas = Object.values(template.findResources("AWS::Lambda::Function")) as any[];
    for (const lambda of lambdas) {
      expect(JSON.stringify(lambda.Properties?.Environment?.Variables?.LAMBDA_SECRET_ENV_MAP ?? ""))
        .not.toContain("hook_grant_signing_key");
    }

    const statements = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].flatMap((type) =>
      Object.values(template.findResources(type)).flatMap(
        (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
      ),
    );
    const deny = statements.find((statement: any) => statement.Sid === "DenyHookGrantKeyRead");
    expect(deny.Effect).toBe("Deny");
    expect([deny.Action].flat()).toContain("secretsmanager:GetSecretValue");
    expect([deny.Resource].flat()).toContain(
      "arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/hook_grant_signing_key-*",
    );
  });

  test("backend can manage install metadata outside runtime owner prefixes", () => {
    const statements = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].flatMap(
      (type) => Object.values(template.findResources(type)).flatMap(
        (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
      ),
    );
    const grant = statements.find((statement: any) => statement.Sid === "HermesProfilesRW");
    expect(grant.Effect).toBe("Allow");
    expect(grant.Action).toEqual(expect.arrayContaining(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]));
    expect(grant.Resource).toContain("arn:aws:s3:::lightbulb-dev-openclaw-replicas/marketplace-installs/*");
  });

  test("backend reads and tags super-agent task overlays, and nothing more there", () => {
    // Lane V: LearningWorkflow reads <owner>/hermes-tasks/<task>/<agent>/ (base, seal, learning
    // files) and, at task end, tags the overlay lb-kind=task-overlay so the 14-day rule expires it.
    const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
    );
    const grant = statements.find((statement: any) => statement.Sid === "HermesTaskOverlaysReadTag");
    expect(grant.Effect).toBe("Allow");
    expect([grant.Action].flat().sort()).toEqual(["s3:GetObject", "s3:PutObjectTagging"]);
    expect(grant.Resource).toBe("arn:aws:s3:::lightbulb-dev-openclaw-replicas/*/hermes-tasks/*");
  });

  test("backend can write the workspace member's network prefix (sessions.json, state.db cleanup)", () => {
    // The first dev roll of h-2026.10.24-9566ac3 failed every sessions.json write with
    // AccessDenied: the grant covered */hermes/* only, so no workspace member could start.
    const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
    );
    const grant = statements.find((statement: any) => statement.Sid === "HermesProfilesRW");
    expect(grant.Action).toEqual(expect.arrayContaining(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]));
    expect(grant.Resource).toContain("arn:aws:s3:::lightbulb-dev-openclaw-replicas/*/network/*");
  });

  test("lets StartBrowserSession read workspace browser extensions with the backend's credentials", () => {
    const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
    );
    const grant = statements.find((statement: any) => statement.Sid === "BrowserExtensionsRW");
    expect(grant.Action).toEqual(expect.arrayContaining(["s3:GetObject", "s3:GetObjectVersion"]));
    expect(grant.Resource).toBe("arn:aws:s3:::lightbulb-dev-openclaw-replicas/browser-state/*");
    const legacy = statements.find((statement: any) => statement.Sid === "BrowserExtensionsLegacyMove");
    expect(legacy.Action).toEqual(["s3:GetObject", "s3:DeleteObject"]);
    expect(legacy.Resource).toBe("arn:aws:s3:::lightbulb-dev-openclaw-replicas/*/browser-extensions/*");
  });

  test("keeps session pick-up transcripts outside the owner-readable S3 prefix", () => {
    const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
    );
    const grant = statements.find((statement: any) => statement.Sid === "SessionPickupRW");
    expect(grant.Action).toEqual(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]);
    expect(grant.Resource).toBe("arn:aws:s3:::lightbulb-dev-openclaw-replicas/session-pickup/*");
  });

  test("keeps the AgentCore runtime's VPC placement wired to the backend", () => {
    // ECS_SUBNETS + ECS_SECURITY_GROUP_ID are ECS-era NAMES but a LIVE Hermes
    // dependency: hermes_provisioning.build_runtime_request_for_owner reads both
    // into CreateAgentRuntime's networkConfiguration.networkModeConfig, on the
    // fresh provision AND on roll_runtime_image. Dropping either does not fail
    // loudly -- it hands the next runtime an empty VPC config, so pin them here.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "backend",
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "ECS_SUBNETS" }),
            Match.objectLike({ Name: "ECS_SECURITY_GROUP_ID" }),
          ]),
        }),
      ]),
    });
  });

  test("injects no OpenClaw image identity — nothing runs that image", () => {
    const rendered = JSON.stringify(template.findResources("AWS::ECS::TaskDefinition"));
    expect(rendered).not.toContain("OPENCLAW_UPSTREAM_VERSION");
    expect(rendered).not.toContain("OPENCLAW_EXTENDED_ARTIFACT_DIGEST");
    expect(rendered).not.toContain("AGENTCORE_GATEWAY_IMAGE");
    expect(rendered).not.toContain("EFS_MOUNT_PATH");
    expect(rendered).not.toContain("CLOUD_MAP_SERVICE_ARN");
    // The dispatch bus keeps its name; it is not an image reference.
    expect(rendered).toContain("OPENCLAW_WORK_QUEUE_URL");
  });

  test("does not wire the retired platform-ops owner or token into backend tasks", () => {
    const taskDefinitions = template.findResources("AWS::ECS::TaskDefinition");
    const rendered = JSON.stringify(taskDefinitions);
    expect(rendered).not.toContain("PLATFORM_OPS_OWNER_ID");
    expect(rendered).not.toContain("PLATFORM_OPS_TOKEN_KEY");
    expect(rendered).not.toContain("platform_ops_token_key");
  });

  test("retains AWS reads required by the human operator console", () => {
    const policies = {
      ...template.findResources("AWS::IAM::Policy"),
      ...template.findResources("AWS::IAM::ManagedPolicy"),
    };
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain("cloudformation:DescribeStacks");
    expect(rendered).toContain("cloudformation:ListStacks");
    expect(rendered).toContain("cloudwatch:DescribeAlarms");
  });

  test("backend task role can read CloudWatch metric data for the admin growth read", () => {
    const policies = {
      ...template.findResources("AWS::IAM::Policy"),
      ...template.findResources("AWS::IAM::ManagedPolicy"),
    };
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain("cloudwatch:GetMetricData");
  });

  test("backend service uses rolling deploy that starts new before stopping old", () => {
    // MinimumHealthyPercent=100 forces ECS to keep the old generation healthy
    // until the new generation is healthy (MaximumPercent=200 gives it the
    // headroom to run both at once), so a deploy is a clean handoff with no
    // outage window. Circuit breaker stays on so a bad rollout auto-rolls back.
    // Holds with dev's single task too: the new task starts before the old one stops.
    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: 1,
      DeploymentConfiguration: {
        MinimumHealthyPercent: 100,
        MaximumPercent: 200,
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
      },
    });
  });

  test("dev backend is one 0.5 vCPU / 2 GB task on Fargate Spot; prod is two 1 vCPU / 2 GB on-demand", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      CapacityProviderStrategy: [
        { CapacityProvider: "FARGATE_SPOT", Weight: 1 },
        { CapacityProvider: "FARGATE", Weight: 0 },
      ],
    });
    template.hasResourceProperties("AWS::ECS::TaskDefinition", { Family: "lightbulb-dev-backend", Cpu: "512", Memory: "2048" });
    const prod = buildServiceStack("prod");
    prod.hasResourceProperties("AWS::ECS::Service", { DesiredCount: 2, LaunchType: "FARGATE" });
    prod.hasResourceProperties("AWS::ECS::TaskDefinition", { Family: "lightbulb-prod-backend", Cpu: "1024", Memory: "2048" });
    prod.hasResourceProperties("AWS::ApplicationAutoScaling::ScalableTarget", { MinCapacity: 2, MaxCapacity: 4 });
  });

  test("backend service autoscales 1-4 (dev) on CPU", () => {
    // Sub-project D moved the remaining per-connection state that made a
    // second instance unsafe (node/operator sockets, RPC waiters) out of
    // process and onto DynamoDB, so the backend can run N>=2.
    template.hasResourceProperties("AWS::ApplicationAutoScaling::ScalableTarget", {
      MinCapacity: 1,
      MaxCapacity: 4,
      ServiceNamespace: "ecs",
    });
    template.hasResourceProperties("AWS::ApplicationAutoScaling::ScalingPolicy", {
      PolicyType: "TargetTrackingScaling",
      TargetTrackingScalingPolicyConfiguration: Match.objectLike({
        TargetValue: 60,
        PredefinedMetricSpecification: Match.objectLike({
          PredefinedMetricType: "ECSServiceAverageCPUUtilization",
        }),
      }),
    });
  });

  test("backend task carries the gateway relay URL", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "backend",
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "GATEWAY_RELAY_URL",
              Value: "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws",
            }),
          ]),
        }),
      ]),
    });
  });

  test("backend container has a bounded stopTimeout (SIGTERM->SIGKILL window)", () => {
    // 30s gives the app its 20s graceful-shutdown budget plus uvicorn's 25s
    // window before ECS hard-kills. Must be >= the deregistration delay.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({ Name: "backend", StopTimeout: 30 }),
      ]),
    });
  });

  test("backend task definition carries STRIPE_WORKSPACE_PRICE_ID", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "STRIPE_WORKSPACE_PRICE_ID" }),
          ]),
        }),
      ]),
    });
  });

  test("backend task definition carries STRIPE_FRONT_DESK_PRICE_ID", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "STRIPE_FRONT_DESK_PRICE_ID" }),
          ]),
        }),
      ]),
    });
  });

  test("escalation overrides route the founder org to Forge in prod only", () => {
    const envOf = (environment: "dev" | "prod") =>
      Match.objectLike({
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Name: "backend",
            Environment: Match.arrayWith([
              Match.objectLike({
                Name: "PAPERCLIP_ESCALATION_AGENT_OVERRIDES",
                Value: environment === "prod"
                  ? '{"org_3H3viGAfq2SXc9dIdcatmGXg8Td":"f293693d-1ec4-4dc3-bd3c-e54109e324bf"}'
                  : "{}",
              }),
            ]),
          }),
        ]),
      });
    template.hasResourceProperties("AWS::ECS::TaskDefinition", envOf("dev"));
    buildServiceStack("prod").hasResourceProperties("AWS::ECS::TaskDefinition", envOf("prod"));
  });

  test("the Accounts v2 version pin defaults empty, and no Stripe rail flag reaches the task env", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", Match.objectLike({
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: "STRIPE_ACCOUNTS_V2_VERSION", Value: "" })]),
        }),
      ]),
    }));
    const taskDefs = JSON.stringify(template.findResources("AWS::ECS::TaskDefinition"));
    expect(taskDefs).not.toContain("STRIPE_PROVISIONING_ENABLED");
    expect(taskDefs).not.toContain("STRIPE_PROJECTS_DEVELOPER_CONFIG");
  });

  test("backend task env carries neither LAUNCH_SCHEDULE_JSON nor STRIPE_FREE_MONTH_COUPON_ID nor any retired per-field LAUNCH_* name — settings now live in the launch-settings DynamoDB row, not synth-time env", () => {
    const backendTaskDefs = Object.values(
      template.findResources("AWS::ECS::TaskDefinition", {
        Properties: { Family: "lightbulb-dev-backend" },
      }),
    );
    expect(backendTaskDefs).toHaveLength(1);
    const containerDefs = (backendTaskDefs[0] as any).Properties
      ?.ContainerDefinitions as any[];
    const backendContainer = containerDefs.find((c) => c.Name === "backend");
    expect(backendContainer).toBeDefined();
    const backendEnvNames = (backendContainer?.Environment ?? []).map(
      (e: any) => e.Name,
    );

    // The six retired per-field env names, plus the two names this task
    // retires (the schedule blob and the coupon id) — asserted on the
    // located backend container, because a `Match.not` inside `arrayWith`
    // is satisfied by any other container in the template.
    const retiredLaunchEnvNames = [
      "LAUNCH_DROP_AT",
      "LAUNCH_COMP_SLOTS",
      "LAUNCH_ACTIVATION_WINDOW_HOURS",
      "LAUNCH_COMPETITION_DAYS",
      "LAUNCH_PRIZE_FRONT_CENTS",
      "LAUNCH_POT_PER_ENTRANT_CENTS",
      "LAUNCH_SCHEDULE_JSON",
      "STRIPE_FREE_MONTH_COUPON_ID",
    ];
    for (const retired of retiredLaunchEnvNames) {
      expect(backendEnvNames).not.toContain(retired);
    }
  });

  test("backend role's wildcard table statement admits the launch tables without a new grant", () => {
    // allTableArns(env) → table/lightbulb-{env}-* plus its indexes.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["dynamodb:PutItem", "dynamodb:Query"]),
            Resource: Match.arrayWith([
              Match.stringLikeRegexp("table/lightbulb-dev-\\*$"),
              Match.stringLikeRegexp("table/lightbulb-dev-\\*/index/\\*$"),
            ]),
          }),
        ]),
      }),
    });
  });

  test("backend role can pass per-owner bedrock roles to AgentCore Runtime, and only there", () => {
    // The owner-bedrock prefix ARN is built from ${this.account}; in this
    // concrete-account synth CDK renders it as a literal string (it becomes an
    // Fn::Join only when the account is an unresolved token). The PassRole
    // statement is PassedToService-conditioned to the one service that receives
    // the role -- never a wildcard, and no longer ecs-tasks.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "iam:PassedToService": ["bedrock-agentcore.amazonaws.com"],
              }),
            }),
            // One ARN left, so CDK renders Resource as a bare string, not an array.
            Resource: "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-*",
          }),
        ]),
      }),
    });
  });

  // The per-owner role-management + Bedrock-control-plane grants live in a
  // dedicated ManagedPolicy (lightbulb-dev-backend-owner-bedrock-manage), NOT the
  // inline default policy, so the inline policy stays under CloudFormation's
  // 10 KB limit and does not spill other statements into an overflow policy.
  test("backend role can create boundary-bounded per-owner roles", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:CreateRole",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "iam:PermissionsBoundary": Match.anyValue(),
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend role can mint Bedrock application inference profiles, scoped by the ManagedBy request tag", () => {
    // CreateInferenceProfile is gated on aws:RequestTag/ManagedBy so the
    // backend can only mint profiles it marks as its own (the AIP ARN cannot be
    // pre-scoped at synth time). Without the condition this would be an
    // unconditioned Resource:"*" create grant — see I-1.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:CreateInferenceProfile"]),
            Resource: "*",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                // Both values during the rename migration: the legacy tag on the 39
                // existing profiles MUST keep matching, or the backend
                // AccessDenies on every one of them.
                "aws:RequestTag/ManagedBy": ["lightbulb-backend"],
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend role can act on its own Bedrock profiles, scoped by the ManagedBy resource tag", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:DeleteInferenceProfile"]),
            Resource: "*",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                // Both values during the rename migration: the legacy tag on the 39
                // existing profiles MUST keep matching, or the backend
                // AccessDenies on every one of them.
                "aws:ResourceTag/ManagedBy": ["lightbulb-backend"],
              }),
            }),
          }),
        ]),
      }),
    });
  });

  // Account-wipe tool (Task 5): the backend needs to delete the per-owner
  // Bedrock role once its policies are detached. Scoped to the same
  // owner-bedrock-* role-name pattern as the rest of OwnerBedrockRoleManage —
  // never a bare "*", and never reaching the shared base managed policy.
  test("backend role can delete per-owner bedrock roles, scoped to the owner-bedrock-* role pattern", () => {
    // OwnerBedrockRoleManage has a single-element `resources` array, so CDK
    // synthesizes it as a bare Resource string (not a list) — unlike the
    // multi-resource IamPassRole statement asserted with arrayWith() above.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "OwnerBedrockRoleManage",
            // ListRolePolicies: teardown ENUMERATES inline policies (owner-browser,
            // owner-memory) before DeleteRole; without it every owner-role delete
            // AccessDenies (dev, 2026-09-22).
            Action: Match.arrayWith(["iam:ListRolePolicies", "iam:DeleteRole"]),
            Resource: "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-*",
          }),
        ]),
      }),
    });
  });

  // Paperclip exit: wipes no longer touch the Paperclip Aurora cluster, which is
  // being removed, so the Data API and DB-secret grants are gone.
  test("backend role has no Paperclip Aurora or admin-credentials grants", () => {
    const rendered = JSON.stringify(SERVICE_TEMPLATE_DEV.toJSON());
    for (const sid of ["AccountWipePaperclipAuroraExec", "AccountWipePaperclipDbSecretRead", "PaperclipAdminCredentialsWrite"]) {
      expect(rendered).not.toContain(sid);
    }
    expect(rendered).not.toContain("rds-data:ExecuteStatement");
  });

  // Lead ruling (task 10): CDK stops handing the backend PAPERCLIP_PUBLIC_URL and the
  // Paperclip admin credentials (task 9 removed both reads); the three it still reads stay.
  test("backend gets no PAPERCLIP_PUBLIC_URL and is denied the Paperclip admin credentials", () => {
    const env = Object.values(SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .flatMap((c: any) => [...(c.Environment ?? []), ...(c.Secrets ?? [])].map((e: any) => e.Name));
    expect(env).not.toContain("PAPERCLIP_PUBLIC_URL");
    for (const kept of ["PAPERCLIP_SERVICE_TOKEN_KEY", "PAPERCLIP_INTERNAL_URL"]) {
      expect(env).toContain(kept);
    }
    expect(env).not.toContain("PAPERCLIP_EVENTS_WEBHOOK_SECRET");
    const statements = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].flatMap((type) =>
      Object.values(SERVICE_TEMPLATE_DEV.findResources(type)).flatMap(
        (resource: any) => resource.Properties?.PolicyDocument?.Statement ?? [],
      ),
    );
    const deny = statements.find((st: any) => st.Sid === "DenyPaperclipAdminCredentialsRead");
    expect(deny.Effect).toBe("Deny");
    expect([deny.Action].flat()).toContain("secretsmanager:GetSecretValue");
    expect([deny.Resource].flat()).toContain(
      "arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb/dev/paperclip_admin_credentials-*",
    );
  });

  test("backend role can list secrets (metadata-only, resource '*' — the paginated ARN discovery)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipeListSecrets",
            Action: "secretsmanager:ListSecrets",
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("backend STS lives in a dedicated ManagedPolicy: AssumeRole scoped to the connector role, GetCallerIdentity unscoped", () => {
    // A ManagedPolicy (not DefaultPolicy) so the near-10 KB inline document's
    // packing stays stable; the AssumeRole resource pin is the security fence
    // (an unscoped * let a compromised backend assume any trusted role).
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "StsAccess",
            Action: "sts:GetCallerIdentity",
            Resource: "*",
          }),
          Match.objectLike({
            Sid: "StsAssumeConnectorRole",
            Action: "sts:AssumeRole",
            // Exactly one name. The transitional legacy ARN is gone: an
            // account still holding the retired role must relaunch the
            // quick-create stack, and routers/connections.py rejects the old
            // name at connect time rather than letting it AccessDeny here.
            Resource: "arn:aws:iam::*:role/lightbulb-connector",
          }),
        ]),
      }),
    });
  });

  test("stamps the vendor org id into the prod backend task and leaves it empty where unset", () => {
    buildServiceStack("prod").hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "backend",
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "VENDOR_ORG_ID", Value: "org_3H3viGAfq2SXc9dIdcatmGXg8Td" }),
          ]),
        }),
      ]),
    });
  });
});

describe("ServiceStack — openclaw-work consumer Lambda", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("runs scripts.work_lambda on the backend image with the provisioner's env and role", () => {
    const fns = Object.values(
      template.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: "lightbulb-dev-openclaw-work-consumer" },
      }),
    );
    expect(fns).toHaveLength(1);
    const fn = (fns[0] as any).Properties;
    expect(fn.ImageConfig.EntryPoint).toEqual(["/app/.venv/bin/python", "-m", "awslambdaric"]);
    expect(fn.ImageConfig.Command).toEqual(["scripts.work_lambda.handler"]);
    // <= the queue's 240 s visibility timeout (api-stack), or Lambda refuses the mapping.
    expect(fn.Timeout).toBe(180);
    expect(fn.ReservedConcurrentExecutions).toBe(20);
    expect(fn.VpcConfig.SecurityGroupIds).toHaveLength(1);
    expect(fn.Role["Fn::GetAtt"][0]).toMatch(/^TaskRole/);
    expect(fn.Environment.Variables.LAMBDA_ENV_PARAMETER).toBeDefined();
    expect(fn.Environment.Variables.LAMBDA_SECRET_ENV_MAP).toBeDefined();
  });

  test("drains openclaw-work.fifo one message per invoke with partial batch failures", () => {
    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      EventSourceArn: "arn:aws:sqs:us-east-1:877352799272:lightbulb-dev-openclaw-work.fifo",
      BatchSize: 1,
      FunctionResponseTypes: ["ReportBatchItemFailures"],
      ScalingConfig: { MaximumConcurrency: 20 },
    });
  });

  test("dev drains through a live alias with one provisioned (warm) consumer", () => {
    template.hasResourceProperties("AWS::Lambda::Alias", {
      Name: "live",
      ProvisionedConcurrencyConfig: { ProvisionedConcurrentExecutions: 1 },
    });
    const mapping = Object.values(template.findResources("AWS::Lambda::EventSourceMapping"))[0] as any;
    expect(JSON.stringify(mapping.Properties.FunctionName)).toMatch(/WorkConsumerLive/);
  });

  test("prod keeps the plain on-demand consumer: no alias, no provisioned concurrency", () => {
    const prod = buildServiceStack("prod");
    prod.resourceCountIs("AWS::Lambda::Alias", 0);
    const fns = Object.values(
      prod.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: "lightbulb-prod-openclaw-work-consumer" },
      }),
    );
    expect(fns).toHaveLength(1);
    expect((fns[0] as any).Properties.ProvisionedConcurrencyConfig).toBeUndefined();
    const mapping = Object.values(prod.findResources("AWS::Lambda::EventSourceMapping"))[0] as any;
    expect(JSON.stringify(mapping.Properties.FunctionName)).not.toMatch(/WorkConsumerLive/);
    expect(JSON.stringify(mapping.Properties.FunctionName)).toMatch(/WorkConsumer/);
  });

  test("starts the SQS mapping after ECS replaces old in-process pollers", () => {
    const serviceId = Object.keys(template.findResources("AWS::ECS::Service"))[0];
    const mapping = Object.values(template.findResources("AWS::Lambda::EventSourceMapping"))[0] as any;
    expect(mapping.DependsOn).toContain(serviceId);
  });
});

describe("ServiceStack — provisioning executor (state machine, live)", () => {
  const template = SERVICE_TEMPLATE_DEV;

  const provisionerFunction = (): any => {
    const fns = Object.values(
      template.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: "lightbulb-dev-provisioner" },
      }),
    );
    expect(fns).toHaveLength(1);
    return (fns[0] as any).Properties;
  };

  test("provision state machine exists and the ECS provisioner task-def is gone", () => {
    template.resourceCountIs("AWS::StepFunctions::StateMachine", 1);
    expect(
      Object.keys(
        template.findResources("AWS::ECS::TaskDefinition", {
          Properties: { Family: "lightbulb-dev-provisioner" },
        }),
      ),
    ).toHaveLength(0);
  });

  test("the provisioner is a VPC DockerImageFunction on the backend image running awslambdaric", () => {
    const fn = provisionerFunction();
    expect(fn.PackageType).toBe("Image");
    expect(fn.Architectures).toEqual(["arm64"]);
    expect(fn.ImageConfig.EntryPoint).toEqual(["/app/.venv/bin/python", "-m", "awslambdaric"]);
    expect(fn.ImageConfig.Command).toEqual(["scripts.provision_lambda.handler"]);
    expect(fn.Timeout).toBe(300);
    expect(fn.MemorySize).toBe(2048);
    expect(fn.ReservedConcurrentExecutions).toBeUndefined();
    expect(fn.VpcConfig.SubnetIds.length).toBeGreaterThan(0);
    expect(fn.VpcConfig.SecurityGroupIds).toHaveLength(1);
    // The same role (and so the same grants) the ECS provisioner task ran with.
    expect(fn.Role["Fn::GetAtt"][0]).toMatch(/^TaskRole/);
  });

  test("the provisioner env is the backend env (via SSM) plus the backend's secret map", () => {
    const backend = (
      Object.values(
        template.findResources("AWS::ECS::TaskDefinition", {
          Properties: { Family: "lightbulb-dev-backend" },
        }),
      )[0] as any
    ).Properties.ContainerDefinitions.find((c: any) => c.Name === "backend");
    const fnEnv = provisionerFunction().Environment.Variables;
    const params = template.findResources("AWS::SSM::Parameter", {
      Properties: { Name: "/lightbulb/dev/provisioner-env" },
    });
    const [paramId] = Object.keys(params);
    expect(fnEnv.LAMBDA_ENV_PARAMETER).toEqual({ Ref: paramId });
    const param = params[paramId] as any;
    expect(param.Properties.Tier).toBe("Advanced");
    const plain = JSON.stringify(param.Properties.Value);
    // Lambda reserves AWS_REGION (and sets it itself); everything else is shared.
    for (const { Name, Value } of backend.Environment) {
      if (Name === "PROVISION_STATE_MACHINE_ARN") continue;
      if (Name === "AWS_REGION") {
        expect(plain).not.toContain(`${Name}\\"`);
        continue;
      }
      expect(plain).toContain(`\\"${Name}\\":`);
      if (typeof Value === "string") expect(plain).toContain(JSON.stringify(JSON.stringify(Value)).slice(3, -3));
    }
    const secretMap = JSON.stringify(fnEnv.LAMBDA_SECRET_ENV_MAP);
    // The grant key is injected only into ECS; all other secrets feed the Lambda map.
    for (const { Name, ValueFrom } of backend.Secrets.filter((secret: any) => secret.Name !== "HOOK_GRANT_SIGNING_KEY")) {
      expect(secretMap).toContain(Name);
      for (const part of ValueFrom["Fn::Join"]?.[1] ?? [ValueFrom]) {
        if (typeof part === "string") expect(secretMap).toContain(part);
      }
    }
  });

  test("the provisioner reads every injected secret through the lightbulb/{env}/* grant", () => {
    const backend = (
      Object.values(
        template.findResources("AWS::ECS::TaskDefinition", {
          Properties: { Family: "lightbulb-dev-backend" },
        }),
      )[0] as any
    ).Properties.ContainerDefinitions.find((c: any) => c.Name === "backend");
    for (const secret of backend.Secrets.filter((s: any) => s.Name !== "HOOK_GRANT_SIGNING_KEY")) {
      expect(JSON.stringify(secret.ValueFrom), secret.Name).toContain(":secret:lightbulb/dev/");
    }
    const grant = Object.values(template.findResources("AWS::IAM::ManagedPolicy"))
      .flatMap((r: any) => r.Properties.PolicyDocument.Statement)
      .find((s: any) => s.Sid === "SecretsAccess");
    expect(grant).toBeDefined();
    expect(grant.Action).toContain("secretsmanager:GetSecretValue");
    expect(JSON.stringify(grant.Resource)).toMatch(/lightbulb\/dev\/\*"/);
  });

  test("backend task definition carries PROVISION_STATE_MACHINE_ARN and no PROVISION_EXECUTOR flag", () => {
    // No flag, no dual-run window (spec §7 / PR-10) — the executor is active
    // on merge. Regression guard: a re-introduced flag would silently
    // reopen the inline/SFN split.
    const backendTaskDefs = Object.values(
      template.findResources("AWS::ECS::TaskDefinition", {
        Properties: { Family: "lightbulb-dev-backend" },
      }),
    );
    expect(backendTaskDefs).toHaveLength(1);
    const containerDefs = (backendTaskDefs[0] as any).Properties
      ?.ContainerDefinitions as any[];
    const backendContainer = containerDefs.find((c) => c.Name === "backend");
    const envNames = (backendContainer.Environment ?? []).map(
      (e: any) => e.Name,
    );
    expect(envNames).toContain("PROVISION_STATE_MACHINE_ARN");
    expect(envNames).not.toContain("PROVISION_EXECUTOR");
  });

  const stateMachineResource = (): any => {
    const stateMachines = Object.values(
      template.findResources("AWS::StepFunctions::StateMachine"),
    );
    expect(stateMachines).toHaveLength(1);
    return stateMachines[0];
  };

  const flatDefinition = (): string => {
    const def = stateMachineResource().Properties?.DefinitionString;
    // DefinitionString synthesizes as an Fn::Join of literal strings and Refs;
    // flatten the literal parts to assert on the ASL content.
    return (def?.["Fn::Join"]?.[1] ?? [def])
      .map((part: any) => (typeof part === "string" ? part : ""))
      .join("");
  };

  test("state machine definition pins the load-bearing ASL (status condition, typed-error Retry/Catch)", () => {
    const flat = flatDefinition();
    // (a) Status-conditioned DDB writes — the crash backstop. No claim:
    // a RUNNING execution owns the provision.
    expect(flat).toContain('"ConditionExpression":"#s = :prov OR #s = :err"');
    expect(flat).not.toContain("provision_claim_id");
    // (b) RunProvisioner is a Lambda invoke retried on the runner's typed
    // transient error and Lambda's own service errors — 3 attempts, 10 s, x2.
    // payloadResponseOnly: the Resource IS the function (a GetAtt, blank when
    // flattened) and the Lambda receives the execution input unwrapped.
    expect(flat).toContain('"TimeoutSeconds":360,"ResultPath":null,"Resource":""}');
    expect(JSON.stringify(stateMachineResource().Properties.DefinitionString)).toContain("Provisioner");
    expect(flat).toContain(
      '"ErrorEquals":["ProvisionTransient","Lambda.ServiceException","Lambda.AWSLambdaException","Lambda.SdkClientException","Lambda.TooManyRequestsException"],"IntervalSeconds":10,"MaxAttempts":3,"BackoffRate":2',
    );
    // The task outlives the 5-min function.
    expect(flat).toContain('"TimeoutSeconds":360');
    // (c) Exhausted transients stay retryable; the runner's own deterministic
    // failure keeps the last_error it wrote; anything else (timeout, crash) is
    // deterministic and the machine writes last_error itself.
    expect(flat).toContain(
      '"Catch":[{"ErrorEquals":["ProvisionTransient","Lambda.ServiceException","Lambda.AWSLambdaException","Lambda.SdkClientException","Lambda.TooManyRequestsException"],"ResultPath":"$.error","Next":"RouteExhausted"},{"ErrorEquals":["ProvisionFailed"],"ResultPath":"$.error","Next":"RouteDeterministic"},{"ErrorEquals":["States.ALL"],"ResultPath":"$.error","Next":"RouteUnhandled"}]',
    );
    expect(flat).toContain("last_error = :le");
    // The ECS exit-code plumbing is gone.
    for (const gone of [
      "ClassifyFailure",
      "ParseFailureCause",
      "ShapeForRetry",
      "AttemptsRemain",
      "BumpAttempt",
      "WaitBeforeRetry",
      "MarkRetrying",
      "ExitCode",
      "ecs:runTask",
      "$.command",
    ]) {
      expect(flat).not.toContain(gone);
    }
    // (d) Terminal-write backstop resilience: the DDB writes retry transient
    // failures, with the condition miss (ConditionalCheckFailed) excluded
    // FIRST so a moved-on row falls to the Superseded catch, never a retry.
    expect(flat).toContain(
      '"ErrorEquals":["DynamoDB.ConditionalCheckFailedException"],"MaxAttempts":0',
    );
    // (e) No lease inputs survive anywhere in the machine.
    expect(flat).not.toContain("attempt_token");
    expect(flat).not.toContain("claim_id");
    expect(flat).not.toContain("PROVISION_ATTEMPT_TOKEN");
  });

  test("machine runs the provisioner at once — no watch loop", () => {
    // The backend no longer runs an inline attempt 0, so there is nothing to
    // watch first; the old watch loop keyed on the provision claim.
    const flat = flatDefinition();
    expect(flat).toContain('"StartAt":"RunProvisioner"');
    expect(flat).not.toContain("WatchRow");
    expect(flat).not.toContain("watch_ticks");
  });

  test("a reconcile execution's failure never reaches the row writes", () => {
    // dispatch_reconcile executions carry {mode: "reconcile"}. Every RunProvisioner catch goes
    // through a Choice that sends them to ReconcileGaveUp: the MarkFailed* writes admit a
    // PROVISIONING row, which a concurrent provision execution may own.
    const def = JSON.parse(flatDefinition());
    const catches = def.States.RunProvisioner.Catch.map((c: { Next: string }) => c.Next);
    expect(catches).toEqual(["RouteExhausted", "RouteDeterministic", "RouteUnhandled"]);
    for (const route of catches) {
      const choice = def.States[route];
      expect(choice.Type).toBe("Choice");
      expect(JSON.stringify(choice.Choices)).toContain('"StringEquals":"reconcile"');
      expect(choice.Choices[0].Next).toBe("ReconcileGaveUp");
      expect(choice.Default).toMatch(/^MarkFailed/);
    }
  });

  test("machine timeout is 30 minutes", () => {
    // The timeout lives INSIDE the ASL (TimeoutSeconds), not as a CFN prop.
    expect(flatDefinition()).toMatch(/"TimeoutSeconds":1800}$/);
  });

  test("state machine may write the containers row it marks", () => {
    // The MarkFailed* writes need dynamodb:UpdateItem AND the
    // CMK grant on the SAME role: the containers table is CUSTOMER_MANAGED-
    // encrypted and the key crosses stacks as a bare ARN, so nothing
    // auto-grants it (the 2026-08-10 KMS class of bug).
    const policies = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ) as any[];
    const machinePolicy = policies.find((p) =>
      (p.Properties?.PolicyDocument?.Statement ?? []).some(
        (s: any) => s.Sid === "ContainersTableKmsAccess",
      ),
    );
    expect(machinePolicy).toBeDefined();
    const statements = machinePolicy.Properties.PolicyDocument
      .Statement as any[];
    const kmsStatement = statements.find(
      (s) => s.Sid === "ContainersTableKmsAccess",
    );
    expect(kmsStatement.Action).toEqual(
      expect.arrayContaining(["kms:Decrypt", "kms:GenerateDataKey"]),
    );
    const canWrite = statements.some((s) =>
      Array.isArray(s.Action)
        ? s.Action.includes("dynamodb:UpdateItem")
        : s.Action === "dynamodb:UpdateItem",
    );
    expect(canWrite).toBe(true);
  });

  test("backend task role may start provision executions", () => {
    // The task role's inline default policy is at CloudFormation's ~10 KB
    // ceiling, so statements added late in the constructor spill into a
    // TaskRoleOverflowPolicy ManagedPolicy attached to the same role (see the
    // overflow-packing comment at the companySlugsTable grant in
    // service-stack.ts). Functionally identical either way, so this assertion
    // accepts the statement in EITHER the inline AWS::IAM::Policy or an
    // AWS::IAM::ManagedPolicy — but if it's in a ManagedPolicy, that policy
    // must be attached to the task role.
    // ListExecutions (RUNNING, by name prefix) is dispatch's single-flight check.
    const wantedActions = ["states:StartExecution", "states:ListExecutions"];
    const hasDispatchStatement = (doc: any): boolean =>
      (doc?.Statement ?? []).some(
        (s: any) =>
          s.Sid === "ProvisionStateMachineDispatch" &&
          JSON.stringify(s.Action) === JSON.stringify(wantedActions) &&
          // Pin the Resource scope: exactly the state machine ARN (a Ref to
          // the SM resource) — a future widening to "*" must fail this test.
          typeof s.Resource?.Ref === "string" &&
          s.Resource.Ref.startsWith("ProvisionStateMachine"),
      );

    const inline = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ).some((r: any) => hasDispatchStatement(r.Properties?.PolicyDocument));
    const managed = Object.values(
      template.findResources("AWS::IAM::ManagedPolicy"),
    ).some(
      (r: any) =>
        hasDispatchStatement(r.Properties?.PolicyDocument) &&
        (r.Properties?.Roles ?? []).some(
          (role: any) => typeof role?.Ref === "string" && role.Ref.startsWith("TaskRole"),
        ),
    );
    expect(inline || managed).toBe(true);
  });

  // These two alarms shipped with NO AlarmActions at all — they watched the
  // signup path and notified nobody. An alarm without an action is decoration
  // and it fails silently, so assert the wiring, not the alarm's existence.
  test("provisioning failure alarms page the alerts topic", () => {
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    const provisioning = Object.values(alarms).filter((a: any) =>
      String(a.Properties?.AlarmName ?? "").includes("provision-executions"),
    );

    // failed + timed-out + slow (the 2026-08-20 founder outage was an
    // execution that SUCCEEDED after 35 min, which the first two cannot see).
    expect(provisioning).toHaveLength(3);
    for (const alarm of provisioning as any[]) {
      expect(alarm.Properties.AlarmActions).toEqual([
        expect.stringContaining("lightbulb-dev-alerts-page"),
      ]);
    }
  });
});

describe("ServiceStack — Clerk azp (SEC-8) per-env wiring", () => {
  // Clerk session tokens carry no `aud`; the token-origin fence is the azp
  // allowlist. Pin the per-env origins and the audit rollout default — a
  // dropped var here silently disables the check (backend treats empty as
  // off), and a premature "enforce" would reject before audit validated the
  // list in the wild.
  test.each([
    [
      "dev",
      `https://dev.${ROOT_DOMAIN},https://admin.dev.${ROOT_DOMAIN},https://dev.company.${ROOT_DOMAIN},http://localhost:3000`,
    ],
    [
      "prod",
      `https://${ROOT_DOMAIN},https://www.${ROOT_DOMAIN},https://admin.${ROOT_DOMAIN},https://company.${ROOT_DOMAIN}`,
    ],
  ] as const)("%s backend gets azp allowlist in audit mode", (envName, parties) => {
    const template = envName === "dev" ? SERVICE_TEMPLATE_DEV : buildServiceStack("prod");
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "CLERK_AUTHORIZED_PARTIES", Value: parties }),
            Match.objectLike({ Name: "CLERK_AZP_MODE", Value: "audit" }),
          ]),
        }),
      ]),
    });
  });
});


describe("ServiceStack — production HTTP CORS origins", () => {
  test("prod backend accepts apex and www frontend origins", () => {
    const template = buildServiceStack("prod");
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "CORS_ORIGINS",
              Value: `https://${ROOT_DOMAIN},https://www.${ROOT_DOMAIN},https://${LAUNCH_DOMAIN},https://www.${LAUNCH_DOMAIN}`,
            }),
          ]),
        }),
      ]),
    });
  });
});

describe("ServiceStack — Metronome per-env wiring", () => {
  // METRONOME_LIFECYCLE_ENABLED is GONE (it defaulted false and gated only the
  // ledger-CREATING paths, never the ones that depend on a ledger, so any
  // fail-closed credit check became unrecoverable wherever it was off). What
  // still needs pinning is the per-env ids: they are console-created values
  // with no schema to catch a mix-up, and swapping dev/prod would bill against
  // the wrong rate card.
  test("dev references the sandbox rate card", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "METRONOME_RATE_CARD_ID",
              Value: "99576f5a-37d2-4983-ba64-fea9bad96e08",
            }),
          ]),
        }),
      ]),
    });
  });

  test("prod references the prod rate card + credits product", () => {
    // Prod's runbook completed 2026-06-12: prod token in Secrets Manager,
    // rate card + credits product created via API (rates verified 12/12
    // against sandbox), Stripe live product wired, webhook secret set.
    const prodTemplate = buildServiceStack("prod");
    prodTemplate.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "METRONOME_RATE_CARD_ID",
              Value: "fdac2edd-f207-4f11-b65b-01fad6fbbdbc",
            }),
            Match.objectLike({
              Name: "METRONOME_CREDITS_PRODUCT_ID",
              Value: "876e1694-9d76-4c37-9814-c957dbd9370f",
            }),
          ]),
        }),
      ]),
    });
  });
});

describe("ServiceStack — agent-mail outbound wiring", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("backend task definition carries AGENT_MAIL_CONFIGURATION_SET", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "AGENT_MAIL_CONFIGURATION_SET",
              Value: "isol8-test-agent-mail",
            }),
          ]),
        }),
      ]),
    });
  });

  test("the backend-ses-send managed policy grants per-owner SES tenant creation", () => {
    // Per-owner SES tenants are created lazily by the backend at first send
    // (core/services/agent_mail_tenant.py) — this is the IAM side of that.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-backend-ses-send",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentMailTenants",
            Action: Match.arrayWith(["ses:CreateTenant"]),
            Resource: Match.arrayWith([
              "arn:aws:ses:us-east-1:877352799272:tenant/owner-*",
              "arn:aws:ses:us-east-1:aws:reputation-policy/standard",
            ]),
          }),
        ]),
      }),
    });
  });

  test("the AgentMailTenants tenant resource is tightened to owner-* (not a bare wildcard)", () => {
    // Tenant names are deterministic `owner-{owner_id}` — a bare `tenant/*`
    // would also authorize actions against tenants outside this fleet's
    // naming convention.
    const policies = template.findResources("AWS::IAM::ManagedPolicy");
    const sesSend = Object.values(policies).find(
      (p: any) => p.Properties?.ManagedPolicyName === "lightbulb-dev-backend-ses-send",
    ) as any;
    expect(sesSend).toBeDefined();
    const statement = sesSend.Properties.PolicyDocument.Statement.find(
      (s: any) => s.Sid === "AgentMailTenants",
    );
    expect(statement.Resource).not.toContain(
      "arn:aws:ses:us-east-1:877352799272:tenant/*",
    );
  });
});

describe("ServiceStack — messaging environment and least privilege", () => {
  test("backend receives the exact messaging environment contract", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-backend",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "backend",
          Environment: Match.arrayWith([
            { Name: "MESSAGING_TABLE", Value: "lightbulb-dev-messaging" },
            {
              Name: "OPENCLAW_WORK_QUEUE_URL",
              Value:
                "https://sqs.us-east-1.amazonaws.com/877352799272/lightbulb-dev-openclaw-work.fifo",
            },
            {
              Name: "APPSYNC_EVENTS_HTTP_ENDPOINT",
              Value:
                "https://events-dev.appsync-api.us-east-1.amazonaws.com/event",
            },
            {
              Name: "APPSYNC_EVENTS_REALTIME_ENDPOINT",
              Value:
                "wss://events-dev.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime",
            },
            { Name: "APPSYNC_EVENTS_REGION", Value: "us-east-1" },
          ]),
        }),
      ]),
    });
  });

  test("backend consumes only the work FIFO and publishes only to messaging namespaces", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-backend-messaging",
      PolicyDocument: {
        Statement: Match.arrayWith([
          {
            Sid: "ConsumeOpenClawWork",
            Effect: "Allow",
            Action: [
              "sqs:ReceiveMessage",
              "sqs:DeleteMessage",
              "sqs:ChangeMessageVisibility",
              "sqs:GetQueueAttributes",
            ],
            Resource:
              "arn:aws:sqs:us-east-1:877352799272:lightbulb-dev-openclaw-work.fifo",
          },
          {
            Sid: "PublishMessagingEvents",
            Effect: "Allow",
            Action: "appsync:EventPublish",
            Resource: [
              "arn:aws:appsync:us-east-1:877352799272:apis/events-dev/channelNamespace/ws",
              "arn:aws:appsync:us-east-1:877352799272:apis/events-dev/channelNamespace/pub",
            ],
          },
        ]),
      },
    });
    const policies = JSON.stringify({
      ...SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::Policy"),
      ...SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::ManagedPolicy"),
    });
    expect(policies).not.toContain("appsync:EventSubscribe");
    expect(policies).not.toContain("dynamodb:DescribeStream");
  });

  test("owned-table policy supports transactions without stream permissions", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "DynamoDbOwnedTables",
            Action: Match.arrayWith(["dynamodb:TransactWriteItems"]),
          }),
        ]),
      }),
    });
  });

  test("machine WebSocket environment and ManageConnections permission remain", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-backend",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "WS_MANAGEMENT_API_URL" }),
            Match.objectLike({ Name: "WS_GATEWAY_URL" }),
            Match.objectLike({ Name: "WS_CONNECTIONS_TABLE" }),
          ]),
        }),
      ]),
    });
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "WebSocketManageConnections",
            Action: "execute-api:ManageConnections",
          }),
        ]),
      }),
    });
  });

  test("backend role can manage per-workspace AgentCore runtimes, scoped to this account", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreRuntimeManage",
            // CreateAgentRuntime also creates the DEFAULT endpoint on the caller's behalf.
            Action: Match.arrayWith([
              "bedrock-agentcore:CreateAgentRuntime",
              "bedrock-agentcore:CreateAgentRuntimeEndpoint",
              // The create request tags the runtime; Tag is a separate action (fourth dev flip).
              "bedrock-agentcore:TagResource",
            ]),
            Resource: Match.arrayWith([
              "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/*",
              "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/*/runtime-endpoint/*",
            ]),
          }),
          Match.objectLike({
            Sid: "AgentCoreWorkloadIdentityManage",
            // ...and the runtime's workload identity, which the denial names if it is missing.
            Action: Match.arrayWith(["bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore:DeleteWorkloadIdentity"]),
          }),
        ]),
      }),
    });
  });

  test("AGENTCORE_HERMES_IMAGE is the only runtime image env the backend gets", () => {
    // provision_hermes_runtime refuses when this is empty, so an empty value is what
    // stops a workspace being provisioned before the image is promoted deliberately.
    const envOf = (t: Template): Record<string, string> => {
      const defs = t.findResources("AWS::ECS::TaskDefinition");
      const backend = Object.values(defs).find((d: any) =>
        JSON.stringify(d).includes("AGENTCORE_HERMES_IMAGE"),
      ) as any;
      const container = backend.Properties.ContainerDefinitions[0];
      return Object.fromEntries((container.Environment ?? []).map((e: any) => [e.Name, e.Value]));
    };
    expect(envOf(SERVICE_TEMPLATE_DEV).AGENTCORE_HERMES_IMAGE).toMatch(
      /^877352799272\.dkr\.ecr\.us-east-1\.amazonaws\.com\/lightbulb\/hermes-agent-arm64:/,
    );
    expect(envOf(SERVICE_TEMPLATE_DEV).AGENTCORE_GATEWAY_IMAGE).toBeUndefined();
  });
});

describe("ServiceStack — peer task forwarding (node invoke, M2 task 27)", () => {
  const template = SERVICE_TEMPLATE_DEV;

  it("lets backend tasks reach each other on 8000 so a node invoke can be forwarded", () => {
    // The socket is pinned to the task that accepted it; the bridge's call lands anywhere.
    template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
      FromPort: 8000,
      ToPort: 8000,
      Description: "Peer backend task forwarding (node invoke)",
    });
  });

  it("lets AgentCore runtimes reach the Mac CDP door on 8000", () => {
    // The ticket names the task holding the Mac's socket; the runtime dials it directly.
    template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
      FromPort: 8000,
      ToPort: 8000,
      GroupId: { "Fn::GetAtt": [Match.stringLikeRegexp("^ServiceSecurityGroup"), "GroupId"] },
      SourceSecurityGroupId: {
        "Fn::ImportValue": Match.stringLikeRegexp("ContainerSecurityGroup"),
      },
    });
  });
});

describe("ServiceStack — task role inline policy size", () => {
  // IAM caps a role's INLINE policies at 10,240 bytes in aggregate, measured on the RESOLVED
  // document -- which runs longer than the template (cross-stack ARNs), and CDK's own overflow
  // split misjudges it. #2929's dev deploy failed on this (ServiceLimitExceeded). Keep the
  // template-measured default policy well below the cap; new grants go in a ManagedPolicy.
  test.each(["dev", "prod"] as const)("%s default policy stays under 9,000 template bytes", (env) => {
    const policies = buildServiceStack(env).findResources("AWS::IAM::Policy");
    const inline = Object.entries(policies)
      .filter(([, r]) => JSON.stringify(r.Properties.Roles ?? []).includes("TaskRole"))
      .map(([, r]) => JSON.stringify(r.Properties.PolicyDocument).length);
    expect(inline.length).toBeGreaterThan(0);
    expect(inline.reduce((a, b) => a + b, 0)).toBeLessThan(9000);
  });
});

describe("ServiceStack — provision status events", () => {
  test("every provision execution status change reaches the internal publish route", () => {
    const t = SERVICE_TEMPLATE_DEV;
    t.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: Match.objectLike({
        source: ["aws.states"],
        "detail-type": ["Step Functions Execution Status Change"],
        detail: { stateMachineArn: [Match.anyValue()] },
      }),
    });
    t.hasResourceProperties("AWS::Events::ApiDestination", {
      HttpMethod: "POST",
      InvocationEndpoint: "https://api-dev.thelightbulb.company/api/v1/internal/provisioning/execution-events",
    });
    t.hasResourceProperties("AWS::Events::Connection", {
      AuthorizationType: "API_KEY",
      AuthParameters: Match.objectLike({
        ApiKeyAuthParameters: Match.objectLike({ ApiKeyName: "x-lightbulb-ws-internal" }),
      }),
    });
  });
});

describe("Apple IAP env", () => {
  it.each([
    ["dev", "true", "true"],
    ["prod", "false", "false"],
  ] as const)("%s: selling=%s, xcode=%s", (env, selling, xcode) => {
    const template = buildServiceStack(env);
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: "APPLE_IAP_SELLING", Value: selling },
            { Name: "APPLE_IAP_ACCEPT_XCODE", Value: xcode },
          ]),
        }),
      ]),
    });
  });

  it("prod never accepts Xcode transactions", () => {
    const json = JSON.stringify(buildServiceStack("prod").toJSON());
    expect(json).not.toMatch(/"APPLE_IAP_ACCEPT_XCODE","Value":"true"/);
  });
});

describe("ServiceStack — workspace memory recall", () => {
  test("backend task role can rerank", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ["bedrock:Rerank", "bedrock:InvokeModel"],
            Effect: "Allow",
            Resource: "arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0",
          }),
        ]),
      },
    });
  });

  test("backend task role can retrieve records and write events on the workspace memory", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ["bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:CreateEvent"],
            Effect: "Allow",
          }),
        ]),
      },
    });
  });

  test("backend task role can mark mirrored records replaced", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Sid: "AgentCoreMemorySync", Action: "bedrock-agentcore:BatchUpdateMemoryRecords" }),
        ]),
      },
    });
  });

  test("backend task role reaches the sessions memory, and its id is in the backend env", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreSessionsMemory",
            Action: Match.arrayWith([
              "bedrock-agentcore:CreateEvent",
              "bedrock-agentcore:ListMemoryRecords",
              "bedrock-agentcore:BatchDeleteMemoryRecords",
              "bedrock-agentcore:DeleteEvent",
            ]),
          }),
        ]),
      },
    });
    expect(JSON.stringify(SERVICE_TEMPLATE_DEV.toJSON())).toContain('"Name":"AGENTCORE_SESSIONS_MEMORY_ID"');
  });

  test("backend task role reads, writes and deletes uploaded sessions in S3", () => {
    const policies = SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::Policy");
    const statements = Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    const rw = statements.find((s: any) => s.Sid === "SessionsRW");
    expect(rw.Action).toEqual(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]);
    expect(rw.Resource).toBe("arn:aws:s3:::lightbulb-dev-openclaw-replicas/sessions/*");
  });
});

describe("ServiceStack — super-agent temporal-worker (dev + prod, contracts-v2 §16)", () => {
  const PROD = buildServiceStack("prod");

  const containers = (template: Template) =>
    Object.values(template.findResources("AWS::ECS::TaskDefinition")).flatMap(
      (td: any) => td.Properties.ContainerDefinitions,
    ) as any[];

  test("dev synth has temporal-worker service and TEMPORAL env", () => {
    const services = Object.values(
      SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::Service", {
        Properties: { ServiceName: "lightbulb-dev-temporal-worker" },
      }),
    ) as any[];
    expect(services).toHaveLength(1);
    const svc = services[0].Properties;
    expect(svc.DesiredCount).toBe(1);
    expect(svc.LoadBalancers).toBeUndefined();
    expect(svc.DeploymentConfiguration.DeploymentCircuitBreaker).toEqual({ Enable: true, Rollback: true });
    expect(svc.CapacityProviderStrategy.map((s: any) => s.CapacityProvider)).toContain("FARGATE_SPOT");
    expect(svc.NetworkConfiguration.AwsvpcConfiguration.AssignPublicIp).toBe("ENABLED"); // dev has no NAT gateway (no-nat-gateway.test.ts)

    const [tdId] = Object.keys(
      SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::TaskDefinition", {
        Properties: { Family: "lightbulb-dev-temporal-worker" },
      }),
    );
    expect(svc.TaskDefinition).toEqual({ Ref: tdId });
    const worker = containers(SERVICE_TEMPLATE_DEV).find((c) => c.Name === "temporal-worker");
    expect(worker.Command).toEqual(["/app/.venv/bin/python", "-m", "scripts.temporal_worker"]);
    expect(worker.StopTimeout).toBe(30);
    expect(worker.HealthCheck).toBeUndefined();

    for (const c of [worker, containers(SERVICE_TEMPLATE_DEV).find((c) => c.Name === "backend")]) {
      const envOf = Object.fromEntries(c.Environment.map((e: any) => [e.Name, e.Value]));
      expect(envOf).toMatchObject({
        TEMPORAL_ADDRESS: "lightbulb-dev.nv5xx.tmprl.cloud:7233",
        TEMPORAL_NAMESPACE: "lightbulb-dev.nv5xx",
        TEMPORAL_TASK_QUEUE: "lightbulb",
      });
      const key = c.Secrets.find((s: any) => s.Name === "TEMPORAL_API_KEY");
      expect(JSON.stringify(key.ValueFrom)).toContain("secret:lightbulb/dev/temporal_api_key");
    }
  });

  test("dev synth has temporal-watchdog service with --queue lightbulb-watchdog", () => {
    const services = Object.values(
      SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::Service", {
        Properties: { ServiceName: "lightbulb-dev-temporal-watchdog" },
      }),
    ) as any[];
    expect(services).toHaveLength(1);
    const svc = services[0].Properties;
    expect(svc.DesiredCount).toBe(1);
    expect(svc.LoadBalancers).toBeUndefined();
    expect(svc.DeploymentConfiguration.DeploymentCircuitBreaker).toEqual({ Enable: true, Rollback: true });
    expect(svc.CapacityProviderStrategy.map((s: any) => s.CapacityProvider)).toContain("FARGATE_SPOT");

    const [tdId] = Object.keys(
      SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::TaskDefinition", {
        Properties: { Family: "lightbulb-dev-temporal-watchdog", Cpu: "256", Memory: "512" },
      }),
    );
    expect(svc.TaskDefinition).toEqual({ Ref: tdId });
    const wd = containers(SERVICE_TEMPLATE_DEV).find((c) => c.Name === "temporal-watchdog");
    expect(wd.Command.slice(-2)).toEqual(["--queue", "lightbulb-watchdog"]);
    expect(wd.Command.slice(0, 3)).toEqual(["/app/.venv/bin/python", "-m", "scripts.temporal_worker"]);
    expect(wd.StopTimeout).toBe(30);
    const envOf = Object.fromEntries(wd.Environment.map((e: any) => [e.Name, e.Value]));
    expect(envOf).toMatchObject({
      TEMPORAL_WATCHDOG_TASK_QUEUE: "lightbulb-watchdog",
      TEMPORAL_ADDRESS: "lightbulb-dev.nv5xx.tmprl.cloud:7233",
    });
    expect(Object.keys(envOf)).toEqual(expect.arrayContaining(["WATCHDOG_OWNER_ID", "WATCHDOG_ROOM_ID"]));
    expect(wd.Secrets.find((s: any) => s.Name === "TEMPORAL_API_KEY")).toBeDefined();
    // the main worker is untouched: no --queue arg, same image
    const worker = containers(SERVICE_TEMPLATE_DEV).find((c) => c.Name === "temporal-worker");
    expect(worker.Command).toEqual(["/app/.venv/bin/python", "-m", "scripts.temporal_worker"]);
    expect(wd.Image).toEqual(worker.Image);
  });

  test("prod synth has the temporal-watchdog and temporal-worker services, on-demand", () => {
    const services = Object.values(PROD.findResources("AWS::ECS::Service")) as any[];
    for (const name of ["lightbulb-prod-temporal-worker", "lightbulb-prod-temporal-watchdog"]) {
      const svc = services.find((s) => s.Properties.ServiceName === name);
      expect(svc).toBeDefined();
      expect(JSON.stringify(svc.Properties.CapacityProviderStrategy ?? [])).not.toMatch(/FARGATE_SPOT/);
    }
    expect(JSON.stringify(PROD.toJSON())).toMatch(/TEMPORAL_NAMESPACE/);
  });

  test("every Lambda's env stays under the 4 KB limit with the Temporal secret added", () => {
    // Resolve intrinsics to their deployed length: an ARN Join renders its literal parts plus the
    // partition/region/account, a bare Ref/GetAtt is a resource id or ARN (bounded at 128 here).
    const render = (v: any): string => {
      if (typeof v === "string") return v;
      if (v?.["Fn::Join"]) return v["Fn::Join"][1].map(render).join(v["Fn::Join"][0]);
      if (v?.Ref === "AWS::Partition") return "aws";
      if (v?.Ref === "AWS::Region") return "us-east-1";
      if (v?.Ref === "AWS::AccountId") return "877352799272";
      return "x".repeat(128);
    };
    const fns = Object.values(SERVICE_TEMPLATE_DEV.findResources("AWS::Lambda::Function")) as any[];
    const sizes = fns.map((fn) =>
      Object.entries(fn.Properties?.Environment?.Variables ?? {}).reduce(
        (n, [k, v]) => n + k.length + render(v).length,
        0,
      ),
    );
    expect(sizes.length).toBeGreaterThan(0);
    for (const size of sizes) expect(size).toBeLessThan(4096);
    // the provisioner/work-consumer map now carries the key; it must actually be measured here
    expect(JSON.stringify(fns)).toContain("temporal_api_key");
  });
});

describe("ServiceStack — credential envelope store (phase 2 task 5)", () => {
  test("backend gets the stage KMS key", () => {
    const backend = Object.values(SERVICE_TEMPLATE_DEV.findResources("AWS::ECS::TaskDefinition"))
      .flatMap((td: any) => td.Properties.ContainerDefinitions)
      .find((c: any) => c.Name === "backend");
    const envNames = backend.Environment.map((e: any) => e.Name);
    expect(envNames).toContain("CREDENTIAL_KMS_KEY_ARN");
  });
});

describe("ServiceStack — Hermes tenant vending role (#5088)", () => {
  for (const environment of ["dev", "prod"] as const) {
    const template = environment === "dev" ? SERVICE_TEMPLATE_DEV : buildServiceStack("prod");
    const bucket = `lightbulb-${environment}-openclaw-replicas`;
    const roles = template.findResources("AWS::IAM::Role", {
      Properties: { RoleName: `lightbulb-${environment}-hermes-tenant` },
    });
    const [[roleId, role]] = Object.entries(roles) as [string, any][];
    const taskRoleId = Object.keys(
      template.findResources("AWS::IAM::Role", { Properties: { RoleName: `lightbulb-${environment}-backend-task` } }),
    )[0];

    test(`${environment}: trusted by the backend task role only, 1 h max`, () => {
      expect(Object.keys(roles)).toHaveLength(1);
      expect(role.Properties.MaxSessionDuration).toBe(3600);
      expect(role.Properties.AssumeRolePolicyDocument.Statement).toEqual([
        {
          Action: "sts:AssumeRole",
          Effect: "Allow",
          Principal: { AWS: { "Fn::GetAtt": [taskRoleId, "Arn"] } },
        },
      ]);
    });

    test(`${environment}: grants only the owner-data S3 and workspace-memory actions`, () => {
      expect(role.Properties.ManagedPolicyArns).toBeUndefined();
      expect(role.Properties.Policies).toHaveLength(1);
      const statements = role.Properties.Policies[0].PolicyDocument.Statement;
      expect(statements.map((s: any) => [s.Sid, s.Effect, s.Action])).toEqual([
        ["OwnerStateBucketList", "Allow", ["s3:GetBucketLocation", "s3:ListBucket"]],
        ["OwnerStateObjects", "Allow", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]],
        [
          "WorkspaceMemoryUse",
          "Allow",
          [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:GetEvent",
            "bedrock-agentcore:ListSessions",
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:ListMemoryRecords",
          ],
        ],
      ]);
      expect(statements[0].Resource).toBe(`arn:aws:s3:::${bucket}`);
      expect(statements[1].Resource).toBe(`arn:aws:s3:::${bucket}/*`);
      // The one shared workspace memory, never a memory/* wildcard.
      expect(JSON.stringify(statements[2].Resource)).toContain(":memory/");
      expect(JSON.stringify(statements[2].Resource)).not.toContain("memory/*");
    });

    test(`${environment}: backend may assume it and gets its ARN as HERMES_TENANT_ROLE_ARN`, () => {
      template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
        ManagedPolicyName: `lightbulb-${environment}-backend-sts`,
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "StsAssumeHermesTenantRole",
              Action: "sts:AssumeRole",
              Resource: { "Fn::GetAtt": [roleId, "Arn"] },
            }),
          ]),
        }),
      });
      template.hasResourceProperties("AWS::ECS::TaskDefinition", {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Name: "backend",
            Environment: Match.arrayWith([
              { Name: "HERMES_TENANT_ROLE_ARN", Value: { "Fn::GetAtt": [roleId, "Arn"] } },
            ]),
          }),
        ]),
      });
    });
  }
});

describe("ServiceStack — the tier's shared Hermes runtime (shared runtime step 3)", () => {
  const HERMES_VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, "../../../hermes-version.json"), "utf8"));
  for (const environment of ["dev", "prod"] as const) {
    const template = environment === "dev" ? SERVICE_TEMPLATE_DEV : buildServiceStack("prod");
    const runtimes = template.findResources("AWS::BedrockAgentCore::Runtime");
    const [[runtimeId, runtime]] = Object.entries(runtimes) as [string, any][];
    const roles = template.findResources("AWS::IAM::Role", {
      Properties: { RoleName: `lightbulb-${environment}-hermes-pool` },
    });
    const [[roleId, role]] = Object.entries(roles) as [string, any][];

    test(`${environment}: one runtime, on the pinned image, booted with no owner`, () => {
      expect(Object.keys(runtimes)).toHaveLength(1);
      const p = runtime.Properties;
      expect(p.AgentRuntimeName).toBe(`lightbulb_${environment}_hermes_pool`);
      expect(JSON.stringify(p.AgentRuntimeArtifact)).toContain(
        `hermes-agent-arm64:${environment === "dev" ? HERMES_VERSION.dev.tag : HERMES_VERSION.prod.tag}`,
      );
      expect(p.RoleArn).toEqual({ "Fn::GetAtt": [roleId, "Arn"] });
      expect(p.NetworkConfiguration.NetworkMode).toBe("VPC");
      expect(p.LifecycleConfiguration).toEqual({ IdleRuntimeSessionTimeout: 300, MaxLifetime: 28800 });
      const envKeys = Object.keys(p.EnvironmentVariables);
      for (const perOwner of ["ISOL8_OWNER_ID", "S3_PREFIX", "LIGHTBULB_SECRET_ID", "AGENTCORE_PROFILE_ID"]) {
        expect(envKeys).not.toContain(perOwner);
      }
      expect(p.EnvironmentVariables.S3_BUCKET).toBe(`lightbulb-${environment}-openclaw-replicas`);
    });

    test(`${environment}: callers may invoke only the canonical pool and its endpoints`, () => {
      const policies = template.findResources("AWS::IAM::ManagedPolicy");
      const statements = Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
      const invokes = statements.filter((s: any) => [s.Action].flat().includes("bedrock-agentcore:InvokeAgentRuntime"));
      expect(invokes).toHaveLength(1);
      expect(invokes[0].Action).toBe("bedrock-agentcore:InvokeAgentRuntime");
      expect(invokes[0].Resource).toEqual([
        { "Fn::GetAtt": [runtimeId, "AgentRuntimeArn"] },
        { "Fn::Join": ["", [{ "Fn::GetAtt": [runtimeId, "AgentRuntimeArn"] }, "/runtime-endpoint/*"]] },
      ]);
      const lifecycle = statements.find((s: any) => s.Sid === "AgentCoreRuntimeManage");
      expect(lifecycle.Action).not.toContain("bedrock-agentcore:InvokeAgentRuntime");
      expect(lifecycle.Action).toContain("bedrock-agentcore:DeleteAgentRuntime");
      expect(lifecycle.Action).toContain("bedrock-agentcore:StopRuntimeSession");
    });

    test(`${environment}: its execution role reaches no tenant data`, () => {
      const actions = JSON.stringify(role.Properties.Policies);
      for (const denied of ["s3:", "secretsmanager:", "CreateEvent", "RetrieveMemoryRecords", "BrowserProfile", "BrowserSession", "kms:"]) {
        expect(actions).not.toContain(denied);
      }
      expect(actions).toContain("appsync:EventPublish");
      expect(actions).toContain("bedrock-agentcore:InvokeBrowser"); // the screen driver, as each owner role has it
      expect(role.Properties.ManagedPolicyArns).toBeUndefined();
    });

    test(`${environment}: the backend gets its ARN as HERMES_SHARED_RUNTIME_ARN`, () => {
      template.hasResourceProperties("AWS::ECS::TaskDefinition", {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Name: "backend",
            Environment: Match.arrayWith([
              { Name: "HERMES_SHARED_RUNTIME_ARN", Value: { "Fn::GetAtt": [runtimeId, "AgentRuntimeArn"] } },
            ]),
          }),
        ]),
      });
    });
  }
});


describe("ServiceStack — search consumer detached before API retirement", () => {
  test.each(["dev", "prod"] as const)("%s has no derived search endpoint, permission or cross-stack import", (env) => {
    const template = env === "dev" ? SERVICE_TEMPLATE_DEV : buildServiceStack(env);
    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).not.toContain("SEARCH_ENDPOINT");
    expect(rendered).not.toContain("SearchMessages");
    expect(rendered).not.toContain("aoss:APIAccessAll");
    expect(rendered).not.toContain("MessageSearchCollection");
    const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition"));
    const backend = tasks.flatMap((r: any) => r.Properties.ContainerDefinitions).find((c: any) => c.Name === "backend");
    expect(backend.Environment).toContainEqual({ Name: "MESSAGING_TABLE", Value: "lightbulb-" + env + "-messaging" });
  });
});

describe("ServiceStack — coherent ARM64 backend image consumers", () => {
  test.each(["dev", "prod"] as const)("%s publishes ARM64 and runs all five consumers on that architecture", (env) => {
    let assetHash = "";
    const template = buildServiceStack(env, (images) => {
      expect(images).toHaveLength(1);
      expect(images[0].platform).toBe("linux/arm64");
      assetHash = images[0].hash;
    });
    const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
    expect(tasks).toHaveLength(3);
    for (const task of tasks) expect(task.Properties.RuntimePlatform).toEqual({ CpuArchitecture: "ARM64", OperatingSystemFamily: "LINUX" });
    const sizes = Object.fromEntries(tasks.map((task) => [task.Properties.Family, [task.Properties.Cpu, task.Properties.Memory]]));
    expect(sizes).toEqual({
      [`lightbulb-${env}-backend`]: [env === "prod" ? "1024" : "512", "2048"],
      [`lightbulb-${env}-temporal-worker`]: ["512", "1024"],
      [`lightbulb-${env}-temporal-watchdog`]: ["256", "512"],
    });
    const lambdas = Object.values(template.findResources("AWS::Lambda::Function")).filter((fn: any) => fn.Properties.PackageType === "Image") as any[];
    expect(lambdas).toHaveLength(2);
    for (const fn of lambdas) expect(fn.Properties.Architectures).toEqual(["arm64"]);
    const images = [...tasks.flatMap((task) => task.Properties.ContainerDefinitions.map((c: any) => JSON.stringify(c.Image))), ...lambdas.map((fn) => JSON.stringify(fn.Properties.Code.ImageUri))];
    for (const image of images) expect(image).toContain(assetHash);
  });
});
