import * as cdk from "aws-cdk-lib";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct, IConstruct } from "constructs";
import {
  CLERK_DOMAIN,
  DEV_ROOT_DOMAIN,
  LEGACY_ROOT_DOMAIN,
  PLATFORM_MAIL_DOMAIN,
  ROOT_DOMAIN,
} from "./domains";
import { AgentMailOutboundStack } from "./stacks/agent-mail-outbound-stack";
import { ApiStack } from "./stacks/api-stack";
import { AuthStack } from "./stacks/auth-stack";
import { OpenRouterGateStack } from "./stacks/openrouter-gate-stack";
import { ContainerStack } from "./stacks/container-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { DnsStack } from "./stacks/dns-stack";
import { NetworkStack } from "./stacks/network-stack";
import { ObservabilityStack } from "./stacks/observability-stack";
import { PaperclipStack } from "./stacks/paperclip-stack";
import { SesInboundStack } from "./stacks/ses-inbound-stack";
import { ServiceStack } from "./stacks/service-stack";
import { resourcePrefix, tableName } from "./table-names";

// Connect platform client id (ca_…) per environment, for the company Stripe
// account's Standard-OAuth "connect an existing account" flow. Public, not a
// secret — safe as a plain constant (contrast the STRIPE_*_PRICE_ID_{DEV,PROD}
// values in app.ts, which are actual account identifiers read from GitHub
// secrets at synth time). Read from the live dashboard 2026-09-01 (platform
// acct_1TBLi5I54BysGS3r); empty would mean STRIPE_CONNECT_CLIENT_ID ships as
// "" and that OAuth flow can't start.
//
// ⚠️ The client id ALONE does not make live OAuth work. Two dashboard settings
// on Connect → Onboarding options → OAuth are still required, and both were
// unset when this value was read:
//   1. "Enable OAuth" (OAuth for Stripe Dashboard accounts) — toggle is OFF.
//   2. At least one redirect URI. The dashboard refuses the flow outright:
//      "You must define a redirect URI to onboard users via OAuth." Ours is
//      STRIPE_COMPANY_OAUTH_REDIRECT_URL below —
//      https://api.thelightbulb.company/api/v1/company/stripe/connect/callback (prod).
// Docs: "You must specify all redirect URLs in your platform settings"
// (docs.stripe.com/connect/oauth-standard-accounts). Until both are set, the
// authorize URL this id builds will be rejected by Stripe.
const STRIPE_CONNECT_CLIENT_ID_BY_ENV: Record<string, string> = {
  dev: "ca_VAYlHjvDMa4nm0M4C3uEen6Hk7cfpBBO",
  prod: "ca_USVqSxboff54k8ETEYsNtlJK62FiFI7S",
};

// One LiveKit project ("Lightbulb") serves both envs; "" keeps an env dark.
const LIVEKIT_URL_BY_ENV: Record<string, string> = {
  dev: "wss://lightbulb-svx3a1rx.livekit.cloud",
  prod: "wss://lightbulb-svx3a1rx.livekit.cloud",
};
// Sharing the project, dev must never dispatch to prod's deployed worker (lightbulb-huddle, the
// backend default): dev's huddles and meetings go to the Mac worker started as lightbulb-meet-dev.
const LIVEKIT_AGENT_NAME_BY_ENV: Record<string, string> = { dev: "lightbulb-meet-dev" };

// The Lightbulb workspace per environment — the org every customer's shared
// `lightbulb-<slug>` channel is shared with. Dev is the founder's dev org.
const VENDOR_ORG_ID_BY_ENV: Record<string, string> = {
  prod: "org_3H3viGAfq2SXc9dIdcatmGXg8Td",
  dev: "org_3JISOofvGAOehDRhYwzZ33C50aw",
};

export interface LightbulbStageProps extends cdk.StageProps {
  environment: string;
  /**
   * Product root domain — every frontend, API, WebSocket, browser-view, Clerk
   * and Paperclip-company hostname in the stage derives from it. Per-stage so
   * one environment can move independently of the other. Defaults to
   * ROOT_DOMAIN (see lib/domains.ts).
   */
  rootDomain?: string;
  /**
   * Domain the platform sender + agent mail live on. Defaults to `rootDomain`,
   * which is where mail lives today; it is a separate parameter so mail can be
   * moved to its own domain without touching the product hostnames.
   */
  platformMailDomain?: string;
}

/** Caps every log group's retention (unset = forever) at dev 14 / prod 30 days. Cost review
 *  2026-10-27: no alarm reads logs older than that. */
class CapLogRetention implements cdk.IAspect {
  constructor(private readonly maxDays: number) {}
  visit(node: IConstruct): void {
    if (node instanceof logs.CfnLogGroup && !((node.retentionInDays ?? Infinity) <= this.maxDays)) {
      node.retentionInDays = this.maxDays;
    }
  }
}

export class LightbulbStage extends cdk.Stage {
  constructor(scope: Construct, id: string, props: LightbulbStageProps) {
    super(scope, id, props);

    const env = props.environment;
    cdk.Aspects.of(this).add(new CapLogRetention(env === "prod" ? 30 : 14));
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const platformMailDomain =
      props.platformMailDomain ?? props.rootDomain ?? PLATFORM_MAIL_DOMAIN;

    const auth = new AuthStack(this, `lightbulb-${env}-auth`, {
      stackName: `lightbulb-${env}-auth`,
      environment: env,
    });

    const dns = new DnsStack(this, `lightbulb-${env}-dns`, {
      stackName: `lightbulb-${env}-dns`,
      environment: env,
      rootDomain,
      legacyRootDomain: LEGACY_ROOT_DOMAIN,
    });

    // SesInboundStack — SES inbound agent-mail receiving (domain identity,
    // wildcard MX/SPF, S3 buckets, receipt rule set + activation, ingest
    // Lambda). The STACK is PROD-ONLY — SetActiveReceiptRuleSet is an
    // account+region-scoped "one active rule set" operation, so it must be a
    // singleton, never one-per-env.
    //
    // Agent email itself is live in BOTH envs. SES matches a recipient
    // condition against the domain and its SUBDOMAINS, so the single
    // prod-owned catch-all rule already receives `{slug}.dev.{mailDomain}` mail;
    // this stack publishes the dev DNS and the ingest Lambda routes by
    // recipient suffix to the dev backend. That is why dev needs no stack of
    // its own — and why the dev wiring is configured here rather than in the
    // dev stage.
    let sesInbound: SesInboundStack | undefined;
    if (env === "prod") {
      sesInbound = new SesInboundStack(this, `lightbulb-${env}-ses-inbound`, {
        stackName: `lightbulb-${env}-ses-inbound`,
        envName: env,
        // By-name import — same cross-stack-cycle-avoidance posture as every
        // other secret name threaded into ServiceStack below.
        sesIngestSecretName: auth.sesIngestSecret.secretName,
        mailDomain: platformMailDomain,
        devMailDomain: `dev.${platformMailDomain}`,
        rootDomain,
        devRootDomain: DEV_ROOT_DOMAIN,
        // Deterministic by-name reference to the DEV stage's secret. AuthStack
        // creates it unconditionally in every env; before dev routing existed,
        // only prod's was ever read.
        devSesIngestSecretName: "lightbulb/dev/ses_ingest_secret",
      });
      sesInbound.addDependency(auth);
      // No DnsStack dependency: mail lives on its own domain now, and this
      // stack looks that zone (and the legacy mail zone) up by name itself —
      // DnsStack's zone follows `rootDomain`, which is a different domain.
    }

    // Outbound agent-mail plumbing exists wherever agent email is on (dev + prod).
    const agentEmailDomain =
      env === "prod"
        ? platformMailDomain
        : env === "dev"
          ? `dev.${platformMailDomain}`
          : "";
    let agentMailOutbound: AgentMailOutboundStack | undefined;
    if (agentEmailDomain) {
      agentMailOutbound = new AgentMailOutboundStack(this, `lightbulb-${env}-agent-mail-outbound`, {
        stackName: `lightbulb-${env}-agent-mail-outbound`,
        envName: env,
        apiDomain:
          env === "prod" ? `api.${rootDomain}` : `api-${env}.${rootDomain}`,
        sesIngestSecretName: `lightbulb/${env}/ses_ingest_secret`,
      });
      agentMailOutbound.addDependency(auth);
    }

    const network = new NetworkStack(this, `lightbulb-${env}-network`, {
      stackName: `lightbulb-${env}-network`,
      environment: env,
      certificate: dns.certificate,
    });

    const database = new DatabaseStack(this, `lightbulb-${env}-database`, {
      stackName: `lightbulb-${env}-database`,
      environment: env,
      kmsKey: auth.kmsKey,
      vpc: network.vpc,
    });

    const container = new ContainerStack(this, `lightbulb-${env}-container`, {
      stackName: `lightbulb-${env}-container`,
      environment: env,
      vpc: network.vpc,
    });

    // ApiStack deploys BEFORE ServiceStack (no circular dependency)
    const api = new ApiStack(this, `lightbulb-${env}-api`, {
      stackName: `lightbulb-${env}-api`,
      environment: env,
      rootDomain,
      vpc: network.vpc,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
      alb: network.albV2,
      albHttpListenerArn: network.albHttpListenerArnV2,
      albV2: network.albV2,
      albSecurityGroup: network.albSecurityGroup,
      clerkIssuerSecretName: `lightbulb/${env}/clerk_issuer`,
      // Prod's issuer is a literal so a Clerk domain move reaches AppSync in the
      // deploy that flips CLERK_DOMAIN: a secret reference is only re-read when
      // the resource changes for some other reason. Dev is a Clerk dev instance
      // whose issuer is not on our domain, so it stays on the secret.
      clerkIssuerUrl: env === "prod" ? `https://clerk.${CLERK_DOMAIN}` : undefined,
      // Pass secret NAME (not ISecret) — same pattern as ServiceStack uses
      // for cross-stack secret refs to avoid KMS auto-grant cycles.
      paperclipServiceTokenKeySecretName:
        auth.paperclipServiceTokenKey.secretName,
      // Pass KMS key ARN (not Key object) so ApiStack can grant kms:Decrypt
      // on the authorizer's role without triggering CDK's auto-grant cycle.
      paperclipKmsKeyArn: auth.kmsKey.keyArn,
      wsInternalSecretSecretName: auth.wsInternalSecret.secretName,
      // Authorizer reads paired_devices[deviceId].publicKey for device-key auth.
      containersTableName: tableName(env, "containers"),
    });

    // ServiceStack replaces ComputeStack
    const service = new ServiceStack(this, `lightbulb-${env}-service`, {
      stackName: `lightbulb-${env}-service`,
      environment: env,
      rootDomain,
      platformMailDomain,
      vpc: network.vpc,
      targetGroup: network.targetGroupV2,
      albSecurityGroup: network.albSecurityGroup,
      // Pass secret names as plain strings to avoid cross-stack refs to AuthStack.
      // These names match the secretName used in AuthStack's createSecret helper.
      secretNames: {
        clerkIssuer: `lightbulb/${env}/clerk_issuer`,
        clerkSecretKey: `lightbulb/${env}/clerk_secret_key`,
        clerkWebhookSecret: `lightbulb/${env}/clerk_webhook_secret`,
        stripeSecretKey: `lightbulb/${env}/stripe_secret_key`,
        stripeWebhookSecret: `lightbulb/${env}/stripe_webhook_secret`,
        encryptionKey: `lightbulb/${env}/encryption_key`,
        posthogProjectApiKey: `lightbulb/${env}/posthog_project_api_key`,
        openaiApiKey: `lightbulb/${env}/openai_api_key`,
        livekitApiKey: `lightbulb/${env}/livekit_api_key`,
        livekitApiSecret: `lightbulb/${env}/livekit_api_secret`,
        recallApiKey: `lightbulb/${env}/recall_api_key`,
        recallWebhookSecret: `lightbulb/${env}/recall_webhook_secret`,
        photonApiKey: `lightbulb/${env}/photon_api_key`,
        photonProjectId: `lightbulb/${env}/photon_project_id`,
        photonWebhookSecret: `lightbulb/${env}/photon_webhook_secret`,
        huddleWorkerSecret: `lightbulb/${env}/huddle_worker_secret`,
        paperclipServiceTokenKey: auth.paperclipServiceTokenKey.secretName,
        paperclipEventsWebhookSecret: auth.paperclipEventsWebhookSecret.secretName,
        gogKeyringPassword: auth.gogKeyringPassword.secretName,
        metronomeApiKey: auth.metronomeApiKey.secretName,
        metronomeWebhookSecret: auth.metronomeWebhookSecret.secretName,
        wsInternalSecret: auth.wsInternalSecret.secretName,
        networkProofKey: auth.networkProofKey.secretName,
        sesIngestSecret: auth.sesIngestSecret.secretName,
      },
      kmsKeyArn: auth.kmsKey.keyArn,
      container: {
        cluster: container.cluster,
        cloudMapNamespace: container.cloudMapNamespace,
        // Live on the Hermes path: the AgentCore runtime ENI's security group.
        containerSecurityGroup: container.containerSecurityGroup,
        ownerBedrockBasePolicy: container.ownerBedrockBasePolicy,
        ownerBedrockBoundary: container.ownerBedrockBoundary,
        workspaceMemoryId: container.workspaceMemory.attrMemoryId,
        sessionsMemoryId: container.sessionsMemory.attrMemoryId,
        workspaceBrowserId: container.workspaceBrowser.attrBrowserId,
      },
      managementApiUrl: api.managementApiUrl,
      webSocketUrl: api.webSocketUrl,
      gatewayRelayUrl: api.gatewayRelayUrl,
      connectionsTableName: api.connectionsTableName,
      wsApiId: api.wsApiId,
      wsStage: api.wsStage,
      messagingTableName: api.messagingTableName,
      messageSearchEndpoint: api.messageSearchEndpoint,
      messageSearchCollectionArn: api.messageSearchCollectionArn,
      openClawWorkQueueUrl: api.openClawWorkQueueUrl,
      openClawWorkQueueArn: api.openClawWorkQueueArn,
      appsyncEventsHttpEndpoint: api.appsyncEventsHttpEndpoint,
      appsyncEventsRealtimeEndpoint: api.appsyncEventsRealtimeEndpoint,
      appsyncEventsApiArn: api.appsyncEventsApiArn,
      appsyncEventsRegion: api.appsyncEventsRegion,
      // Agent-email base domain. Prod owns the apex; dev gets its own
      // subdomain so addresses can never collide and a dev message can never
      // be mistaken for a real one. Both resolve through the single
      // prod-owned receipt rule (SES matches subdomains), and the ingest
      // Lambda routes each to its own backend.
      //
      // Every OTHER env stays empty, which keeps the backend's guards inert —
      // there is no DNS, and nothing would route.
      agentEmailDomain,
      // Undefined wherever AgentMailOutboundStack isn't instantiated —
      // ServiceStack defaults this to an empty string, which stamps no
      // configuration set on outbound sends (core/services/ses_email.py).
      agentMailConfigurationSet: agentMailOutbound?.configurationSetName,
      stripeConnectClientId: STRIPE_CONNECT_CLIENT_ID_BY_ENV[env],
      vendorOrgId: VENDOR_ORG_ID_BY_ENV[env],
      // Dev provisions out of Stripe's `testing` catalog partition so a dev
      // experiment can never create a real provider resource out of the prod
      // catalog. Prod reads and writes the real one.
      stripeProjectsCatalog: env === "prod" ? "prod" : "testing",
      // App hostnames live in the root zone (dev and prod share it, so dev gets its own subdomain).
      appsDomain: env === "prod" ? "apps.thelightbulb.company" : "apps.dev.thelightbulb.company",
      appsHostedZoneId: dns.hostedZone.hostedZoneId,
      livekitUrl: LIVEKIT_URL_BY_ENV[env],
      livekitAgentName: LIVEKIT_AGENT_NAME_BY_ENV[env],
    });

    // The service ships AGENT_MAIL_CONFIGURATION_SET, and ses_email.py stamps
    // it on EVERY platform send (agent mail, welcome, support tickets) — SES
    // rejects a send naming a configuration set that does not exist, so the
    // outbound stack has to be deployed before the service that references it.
    if (agentMailOutbound) service.addDependency(agentMailOutbound);
    // EXPLICIT, and load-bearing. The service stack used to take a `database`
    // prop, which gave CDK an implicit ordering. Decoupling the exports removed
    // the prop AND that ordering — but the backend still reads every table, now
    // BY NAME, so it must not start before the database stack has created them.
    // Without this the generated workflow drops database from service's `needs`.
    service.addDependency(database);

    // PaperclipStack — runs the pinned Paperclip image (extended overlay,
    // apps/infra/paperclip-extended/) as a single Fargate service. Reachable from
    // FastAPI via Cloud Map at
    // `http://paperclip.lightbulb-${env}.local:3100/`. T6 wires the public
    // host route on the existing ALB; T14 wires the FastAPI proxy router.
    const paperclip = new PaperclipStack(this, `lightbulb-${env}-paperclip`, {
      stackName: `lightbulb-${env}-paperclip`,
      environment: env,
      rootDomain,
      vpc: network.vpc,
      cluster: container.cluster,
      // Paperclip registers as `paperclip.<namespace>` and
      // PAPERCLIP_INTERNAL_URL is derived from namespaceName, so these must
      // always name the same namespace ServiceStack gets.
      cloudMapNamespace: container.cloudMapNamespace,
      paperclipDbCluster: database.paperclipDbCluster,
      paperclipDbSecurityGroup: database.paperclipDbSecurityGroup,
      paperclipBetterAuthSecretName: auth.paperclipBetterAuthSecret.secretName,
      paperclipEventsWebhookSecretName: auth.paperclipEventsWebhookSecret.secretName,
      // Pass KMS key ARN (not Key object) so PaperclipStack can grant
      // kms:Decrypt on its task execution roles without triggering CDK's
      // cross-stack auto-grant cycle (same pattern as ApiStack).
      paperclipKmsKeyArn: auth.kmsKey.keyArn,
    });
    paperclip.addDependency(database);
    paperclip.addDependency(auth);
    paperclip.addDependency(container);
    paperclip.addDependency(network);

    // ObservabilityStack — alarms, dashboard, canaries, account hardening
    const observability = new ObservabilityStack(
      this,
      `lightbulb-${env}-observability`,
      {
        stackName: `lightbulb-${env}-observability`,
        envName: env,
        rootDomain,
        platformMailDomain,
        backendService: service.service,
        backendLogGroupName: `/ecs/lightbulb-${env}`,
        alb: network.albV2,
        targetGroup: network.targetGroupV2,
        wsApiId: api.wsApiId,
        cluster: container.cluster,
        databaseTables: {
          usersTable: tableName(env, "users"),
          containersTable: tableName(env, "containers"),
          billingTable: tableName(env, "billing-accounts"),
          usageCountersTable: tableName(env, "usage-counters"),
          pendingUpdatesTable: tableName(env, "pending-updates"),
          channelLinksTable: tableName(env, "channel-links"),
          companySlugsTable: tableName(env, "company-slugs"),
          agentEmailAddressesTable: tableName(env, "agent-email-addresses"),
          // Both owned by ApiStack, not DatabaseStack (see api-stack.ts) —
          // still plain literal names, for the same no-cross-stack-export
          // reason as the eight above.
          messagingTable: api.messagingTableName,
          wsConnectionsTable: api.connectionsTableName,
        },
        authorizerFunctionName: `${resourcePrefix(env)}ws-authorizer`,
        messaging: {
          workQueueName: api.openClawWorkQueueName,
          workDlqName: api.openClawWorkDlqName,
          projectorFunctionName: api.messagingProjectorFunctionName,
          appsyncEventApiId: api.appsyncEventApiId,
        },
        paperclipDbCluster: database.paperclipDbCluster,
        paperclipService: paperclip.service,
      },
    );
    observability.addDependency(database);
    observability.addDependency(paperclip);

    // Both envs: a runtime resolved to openrouter_managed needs this stack's gate URL,
    // so the stack itself can't be env-gated.
    const openrouterGate = new OpenRouterGateStack(this, `lightbulb-${env}-openrouter-gate`, {
      stackName: `lightbulb-${env}-openrouter-gate`,
      environment: env,
      apiDomain: env === "prod" ? `api.${rootDomain}` : `api-${env}.${rootDomain}`,
      wsInternalSecretName: auth.wsInternalSecret.secretName,
    });
    openrouterGate.addDependency(database);

    // --- Tags ---
    cdk.Tags.of(this).add("Project", "lightbulb");
    cdk.Tags.of(this).add("Environment", env);
  }


}
