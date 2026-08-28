import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN, DEV_ROOT_DOMAIN } from "./domains";
import { AgentMailOutboundStack } from "./stacks/agent-mail-outbound-stack";
import { ApiStack } from "./stacks/api-stack";
import { AuthStack } from "./stacks/auth-stack";
import { BedrockGateStack } from "./stacks/bedrock-gate-stack";
import { BedrockMeterStack } from "./stacks/bedrock-meter-stack";
import { ContainerStack } from "./stacks/container-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { DnsStack } from "./stacks/dns-stack";
import { NetworkStack } from "./stacks/network-stack";
import { ObservabilityStack } from "./stacks/observability-stack";
import { PaperclipStack } from "./stacks/paperclip-stack";
import { SesInboundStack } from "./stacks/ses-inbound-stack";
import { ServiceStack } from "./stacks/service-stack";
import { tableName } from "./table-names";

export interface Isol8StageProps extends cdk.StageProps {
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

export class Isol8Stage extends cdk.Stage {
  constructor(scope: Construct, id: string, props: Isol8StageProps) {
    super(scope, id, props);

    const env = props.environment;
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const platformMailDomain =
      props.platformMailDomain ?? props.rootDomain ?? PLATFORM_MAIL_DOMAIN;

    const auth = new AuthStack(this, `isol8-${env}-auth`, {
      stackName: `isol8-${env}-auth`,
      environment: env,
    });

    const dns = new DnsStack(this, `isol8-${env}-dns`, {
      stackName: `isol8-${env}-dns`,
      environment: env,
      rootDomain,
    });

    // SesInboundStack — SES inbound agent-mail receiving (domain identity,
    // wildcard MX/SPF, S3 buckets, receipt rule set + activation, ingest
    // Lambda). The STACK is PROD-ONLY — SetActiveReceiptRuleSet is an
    // account+region-scoped "one active rule set" operation, exactly like the
    // bedrock-meter/bedrock-gate account-scoped stacks below, so it must be a
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
      sesInbound = new SesInboundStack(this, `isol8-${env}-ses-inbound`, {
        stackName: `isol8-${env}-ses-inbound`,
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
        devSesIngestSecretName: "isol8/dev/ses_ingest_secret",
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
      agentMailOutbound = new AgentMailOutboundStack(this, `isol8-${env}-agent-mail-outbound`, {
        stackName: `isol8-${env}-agent-mail-outbound`,
        envName: env,
        apiDomain:
          env === "prod" ? `api.${rootDomain}` : `api-${env}.${rootDomain}`,
        sesIngestSecretName: `isol8/${env}/ses_ingest_secret`,
      });
      agentMailOutbound.addDependency(auth);
    }

    const network = new NetworkStack(this, `isol8-${env}-network`, {
      stackName: `isol8-${env}-network`,
      environment: env,
      certificate: dns.certificate,
    });

    const database = new DatabaseStack(this, `isol8-${env}-database`, {
      stackName: `isol8-${env}-database`,
      environment: env,
      kmsKey: auth.kmsKey,
      vpc: network.vpc,
    });

    const container = new ContainerStack(this, `isol8-${env}-container`, {
      stackName: `isol8-${env}-container`,
      environment: env,
      vpc: network.vpc,
      kmsKeyArn: auth.kmsKey.keyArn,
      // Pass secret NAME (not ISecret) — same cross-stack pattern as the
      // service-stack secretNames, so ContainerStack imports it via
      // fromSecretNameV2 without triggering a KMS auto-grant cycle.
      gogKeyringPasswordSecretName: auth.gogKeyringPassword.secretName,
      cloudflareWarpEnrollmentSecretName:
        auth.cloudflareWarpEnrollment.secretName,
    });

    // ApiStack deploys BEFORE ServiceStack (no circular dependency)
    const api = new ApiStack(this, `isol8-${env}-api`, {
      stackName: `isol8-${env}-api`,
      environment: env,
      rootDomain,
      vpc: network.vpc,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
      alb: network.alb,
      albHttpListenerArn: network.albHttpListenerArn,
      albSecurityGroup: network.albSecurityGroup,
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
    const service = new ServiceStack(this, `isol8-${env}-service`, {
      stackName: `isol8-${env}-service`,
      environment: env,
      rootDomain,
      platformMailDomain,
      vpc: network.vpc,
      targetGroup: network.targetGroup,
      albSecurityGroup: network.albSecurityGroup,
      // Pass secret names as plain strings to avoid cross-stack refs to AuthStack.
      // These names match the secretName used in AuthStack's createSecret helper.
      secretNames: {
        clerkIssuer: `isol8/${env}/clerk_issuer`,
        clerkSecretKey: `isol8/${env}/clerk_secret_key`,
        clerkWebhookSecret: `isol8/${env}/clerk_webhook_secret`,
        stripeSecretKey: `isol8/${env}/stripe_secret_key`,
        stripeWebhookSecret: `isol8/${env}/stripe_webhook_secret`,
        encryptionKey: `isol8/${env}/encryption_key`,
        posthogProjectApiKey: `isol8/${env}/posthog_project_api_key`,
        paperclipServiceTokenKey: auth.paperclipServiceTokenKey.secretName,
        paperclipEventsWebhookSecret: auth.paperclipEventsWebhookSecret.secretName,
        gogKeyringPassword: auth.gogKeyringPassword.secretName,
        metronomeApiKey: auth.metronomeApiKey.secretName,
        metronomeWebhookSecret: auth.metronomeWebhookSecret.secretName,
        wsInternalSecret: auth.wsInternalSecret.secretName,
        sesIngestSecret: auth.sesIngestSecret.secretName,
      },
      kmsKeyArn: auth.kmsKey.keyArn,
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
      managementApiUrl: api.managementApiUrl,
      webSocketUrl: api.webSocketUrl,
      connectionsTableName: api.connectionsTableName,
      wsApiId: api.wsApiId,
      wsStage: api.wsStage,
      // Agent-email base domain. Prod owns the apex; dev gets its own
      // subdomain so addresses can never collide and a dev message can never
      // be mistaken for a real one. Both resolve through the single
      // prod-owned receipt rule (SES matches subdomains), and the ingest
      // Lambda routes each to its own backend.
      //
      // Every OTHER env stays empty, which keeps the backend's guards inert —
      // there is no DNS, and nothing would route.
      agentEmailDomain,
      // Undefined in every env/deploy where SesInboundStack isn't
      // instantiated (non-prod stage) — ServiceStack defaults this to an
      // empty string, which is inert without AGENT_EMAIL_DOMAIN.
      agentEmailAttachmentsBucket: sesInbound?.attachmentsBucketName,
      // Undefined wherever AgentMailOutboundStack isn't instantiated —
      // ServiceStack defaults this to an empty string, which stamps no
      // configuration set on outbound sends (core/services/ses_email.py).
      agentMailConfigurationSet: agentMailOutbound?.configurationSetName,
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
    // `http://paperclip.isol8-${env}.local:3100/`. T6 wires the public
    // host route on the existing ALB; T14 wires the FastAPI proxy router.
    const paperclip = new PaperclipStack(this, `isol8-${env}-paperclip`, {
      stackName: `isol8-${env}-paperclip`,
      environment: env,
      rootDomain,
      vpc: network.vpc,
      cluster: container.cluster,
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
      `isol8-${env}-observability`,
      {
        stackName: `isol8-${env}-observability`,
        envName: env,
        rootDomain,
        platformMailDomain,
        backendService: service.service,
        backendLogGroupName: `/ecs/isol8-${env}`,
        alb: network.alb,
        targetGroup: network.targetGroup,
        wsApiId: api.wsApiId,
        cluster: container.cluster,
        efsFileSystem: container.efsFileSystem,
        databaseTables: {
          usersTable: tableName(env, "users"),
          containersTable: tableName(env, "containers"),
          billingTable: tableName(env, "billing-accounts"),
          usageCountersTable: tableName(env, "usage-counters"),
          pendingUpdatesTable: tableName(env, "pending-updates"),
          channelLinksTable: tableName(env, "channel-links"),
          companySlugsTable: tableName(env, "company-slugs"),
          agentEmailAddressesTable: tableName(env, "agent-email-addresses"),
        },
        connectionsTableName: api.connectionsTableName,
        authorizerFunctionName: `isol8-${env}-ws-authorizer`,
        paperclipDbCluster: database.paperclipDbCluster,
        paperclipService: paperclip.service,
      },
    );
    observability.addDependency(database);
    observability.addDependency(paperclip);

    // BedrockMeterStack — account-scoped usage meter, direct Metronome
    // ingest. Bedrock model-invocation logging is account+region-global (one
    // config per region, one invocation-log group per region per account), so
    // this stack is instantiated ONCE — prod stage only — and contains BOTH
    // env Lambda instances; the dev/prod split happens at the subscription
    // filters inside the stack. No stack dependencies: the per-env Metronome
    // secrets are referenced by NAME (fromSecretNameV2), which creates no
    // CFN-level coupling to either env's auth stack, and the Lambdas have no
    // VPC, so the network stack is not involved at all.
    if (env === "prod") {
      new BedrockMeterStack(this, `isol8-${env}-bedrock-meter`, {
        stackName: `isol8-${env}-bedrock-meter`,
        envName: env,
      });

      // BedrockGateStack — account-scoped synchronous Bedrock metering gateway.
      // Instantiated ONCE (prod stage only) for the SAME reason as the meter:
      // both env instances (dev + prod) live in one account-scoped stack and
      // the split is pure env-var config, so a per-env duplicate would collide
      // on the fixed function/table names. No stack dependencies — the billing,
      // fast-gate and dedup tables are referenced by NAME (ARN-by-convention),
      // and the Lambdas have no VPC (a Function URL cannot live in one).
      new BedrockGateStack(this, `isol8-${env}-bedrock-gate`, {
        stackName: `isol8-${env}-bedrock-gate`,
        envName: env,
      });
    }

    // --- Tags ---
    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", env);
  }
}
