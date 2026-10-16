import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as ecr from "aws-cdk-lib/aws-ecr";
import { Construct } from "constructs";
import * as fs from "fs";
import * as path from "path";
import { ROOT_DOMAIN } from "../domains";
import { logGroupPath } from "../table-names";

// Single source of truth for the pinned (extended) Paperclip image. Bump
// paperclip-version.json at the repo root; the overlay it patches lives at
// apps/infra/paperclip-extended/. Mirrors the openclaw-version.json pattern.
type PaperclipVersionConfig = {
  upstream: string;
  extendedImage: string;
  dev: { tag: string };
  prod: { tag: string };
  notes?: string;
};
const PAPERCLIP_VERSION: PaperclipVersionConfig = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "..", "paperclip-version.json"),
    "utf8",
  ),
);

/**
 * PaperclipStack — runs the pinned Paperclip image (an extended overlay of the
 * official image; see apps/infra/paperclip-extended/) as a single Fargate
 * service inside the Isol8 VPC.
 *
 * Reachability: the FastAPI backend talks to Paperclip over Cloud Map
 * service discovery. The service registers as `paperclip` inside the
 * existing private DNS namespace (`lightbulb-${env}.local`) created by
 * ContainerStack — same pattern OpenClaw containers use. Backend resolves
 * `http://paperclip.lightbulb-${env}.local:3100/`.
 *
 * Why Cloud Map and not an internal ALB listener?
 *  - The repo's NetworkStack only exposes a single public ALB (HTTP+HTTPS
 *    listeners on isol8.co / dev.isol8.co). Adding an "internal" listener
 *    would require new SG rules + a private Route 53 record, neither of
 *    which exist today.
 *  - Cloud Map is already the canonical service-discovery surface for the
 *    fleet (OpenClaw containers), which keeps T14's proxy router code
 *    consistent across providers.
 *  - The public host route (`company.isol8.co` → Paperclip) is wired in T6
 *    on the existing public ALB and is independent of how FastAPI reaches
 *    Paperclip internally.
 *
 * Cross-stack KMS posture: AuthStack's BetterAuth secret is encrypted with
 * the shared CMK. We import it by *name* (NOT ISecret) to avoid the same
 * cross-stack auto-grant cycle that service-stack.ts mitigates with
 * `secretNames`. The Aurora cluster's master secret is owned by
 * DatabaseStack and reaches us via `props.paperclipDbCluster.secret`,
 * which is fine because there is no ambient KMS dependency on AuthStack.
 */
export interface PaperclipStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  vpc: ec2.IVpc;
  cluster: ecs.ICluster;
  /** Cloud Map private DNS namespace owned by ContainerStack. */
  cloudMapNamespace: servicediscovery.IPrivateDnsNamespace;
  /**
   * Concrete cluster (not IDatabaseCluster) — we read `.secret` to wire
   * PGPASSWORD, and that property only exists on the concrete base class
   * `DatabaseClusterBase` exposed by `rds.DatabaseCluster`.
   */
  paperclipDbCluster: rds.DatabaseClusterFromSnapshot;
  paperclipDbSecurityGroup: ec2.ISecurityGroup;
  /**
   * Pass the secret *name* (not an ISecret) to avoid CDK auto-granting
   * KMS decrypt on AuthStack's CMK, which would create a cross-stack
   * dependency cycle. Same pattern as service-stack.ts.
   */
  paperclipBetterAuthSecretName: string;
  /**
   * Bearer secret the isol8.notifications plugin worker attaches to its
   * outbound POSTs to the backend /webhooks/paperclip-events receiver.
   * Injected as PAPERCLIP_ISOL8_NOTIFY_TOKEN (name-only, same cross-stack
   * KMS posture as `paperclipBetterAuthSecretName`).
   */
  paperclipEventsWebhookSecretName: string;
  /**
   * AuthStack KMS key ARN (NOT a Key object) used to encrypt the Paperclip
   * BetterAuth secret. The task execution roles (main + migrate) need
   * `kms:Decrypt` on this key in order for `secretsmanager:GetSecretValue`
   * to succeed at task launch — `secret.grantRead()` only adds the
   * SecretsManager action, not the underlying KMS one. Same pattern as
   * api-stack.ts uses for the Lambda authorizer (T9). Passed as a string
   * for the same cross-stack reason as `paperclipBetterAuthSecretName`.
   */
  paperclipKmsKeyArn: string;
}

export class PaperclipStack extends cdk.Stack {
  public readonly service: ecs.FargateService;
  // T14 NOTE: T14's proxy router must add an ingress rule on this SG
  // allowing port 3100 from FastAPI's service SG. PaperclipStack
  // deliberately does NOT open ingress here — it doesn't have FastAPI's
  // SG handle, and granting it would create a cross-stack SG cycle.
  // T14 should add the rule from the service stack side using
  // `ec2.CfnSecurityGroupIngress` (matching the Aurora-from-Paperclip
  // pattern below) once it has both SG IDs in scope.
  public readonly taskSecurityGroup: ec2.SecurityGroup;
  /**
   * Internal URL FastAPI uses to reach Paperclip (T14 reads this).
   *
   * T14 NOTE: this URL resolves via Cloud Map A records with a 10-second
   * TTL (see `cloudMapOptions` on the FargateService below). During
   * rolling ECS deploys, task IPs change, so T14's HTTP client MUST NOT
   * cache DNS forever. With `aiohttp`, use
   * `aiohttp.TCPConnector(ttl_dns_cache=10)` (or `use_dns_cache=False`).
   * With `httpx`, ensure no Python-layer DNS cache is wrapping the client
   * — httpx itself doesn't cache DNS, but a custom transport might.
   */
  public readonly internalUrl: string;
  /**
   * One-shot ECS task definition that runs Drizzle migrations + creates
   * the pgvector extension. NOT bound to a service; operators invoke it
   * via `aws ecs run-task` after each deploy. See
   * apps/infra/paperclip/RUNBOOK.md.
   *
   * Exposed publicly so a future custom resource (or admin tooling) can
   * reference the task-definition ARN without re-deriving it.
   */
  public readonly migrateTaskDefinition: ecs.FargateTaskDefinition;

  constructor(scope: Construct, id: string, props: PaperclipStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;

    // Extended Paperclip image (mirrors openclaw-extended): use the patched ECR
    // image once the per-env tag is promoted past "bootstrap"; until then fall
    // back to the pinned upstream image so the env keeps deploying. The overlay
    // patches the openclaw-gateway adapter to gateway protocol v4 — see
    // apps/infra/paperclip-extended/.
    const paperclipExtendedRepo = ecr.Repository.fromRepositoryName(
      this,
      "PaperclipExtendedRepo",
      "lightbulb/paperclip-extended",
    );
    const paperclipEnvCfg =
      env === "dev" || env === "prod" ? PAPERCLIP_VERSION[env] : undefined;
    const paperclipEnvTag = paperclipEnvCfg?.tag ?? "bootstrap";
    const paperclipImage =
      paperclipEnvTag === "bootstrap"
        ? ecs.ContainerImage.fromRegistry(PAPERCLIP_VERSION.upstream)
        : ecs.ContainerImage.fromEcrRepository(
            paperclipExtendedRepo,
            paperclipEnvTag,
          );

    // ─────────────────────────────────────────────────────────────────
    // Log group
    // ─────────────────────────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, "PaperclipLogs", {
      logGroupName: logGroupPath(env, "paperclip"),
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // ─────────────────────────────────────────────────────────────────
    // Task security group
    // Egress is open (Aurora + Secrets Manager + ECR + CloudWatch).
    // Ingress is granted by T6 (public ALB host rule for company.isol8.co)
    // and by Cloud Map peers (FastAPI backend SG → Paperclip:3100). We add
    // the FastAPI ingress in T14 / future wiring; nothing else has the
    // SG handle today, so leaving that to the consumer keeps blast radius
    // tight.
    // ─────────────────────────────────────────────────────────────────
    this.taskSecurityGroup = new ec2.SecurityGroup(this, "PaperclipTaskSg", {
      vpc: props.vpc,
      // ASCII-only: EC2 GroupDescription rejects non-ASCII (em-dashes, arrows, etc.)
      description: `Isol8 ${env} Paperclip task SG - Aurora egress + ALB/FastAPI ingress`,
      allowAllOutbound: true,
    });

    // Intra-VPC ingress on port 3100. The backend FastAPI proxy connects
    // directly to the Paperclip task via Cloud Map at
    // http://paperclip.isol8-{env}.local:3100 (per ServiceStack's
    // PAPERCLIP_INTERNAL_URL env var) — that bypasses the ALB and hits
    // this task SG directly. Without this rule the connection times out
    // and provisioning fails. The earlier comment "Ingress is granted by
    // T6 (public ALB host rule for company.isol8.co)" was wrong: the ALB
    // rule only covers ALB-initiated traffic, not direct intra-VPC
    // connections from the FastAPI service. Scope to the VPC CIDR
    // instead of the FastAPI SG to avoid a cross-stack reference (the
    // task lives in a private subnet so the practical reachability is
    // identical, and the Aurora pattern below shows why we avoid
    // cross-stack SG refs).
    this.taskSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(3100),
      "FastAPI proxy via Cloud Map (intra-VPC)",
    );

    // Aurora ingress: the Paperclip task must reach Postgres on 5432.
    // Use CfnSecurityGroupIngress (NOT addIngressRule) for the same reason
    // service-stack.ts does — addIngressRule on a cross-stack SG creates
    // mutual Refs that CDK reports as a cyclic stack dependency. The Cfn
    // form takes raw IDs and emits a single-direction reference.
    new ec2.CfnSecurityGroupIngress(this, "AuroraFromPaperclipIngress", {
      groupId: props.paperclipDbSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 5432,
      toPort: 5432,
      sourceSecurityGroupId: this.taskSecurityGroup.securityGroupId,
      // ASCII-only: EC2 SecurityGroupIngress description rejects non-ASCII
      description: "Paperclip task to Aurora",
    });

    // ─────────────────────────────────────────────────────────────────
    // IAM: task execution role (ECR pull + CloudWatch + secrets fetch)
    // ─────────────────────────────────────────────────────────────────
    const executionRole = new iam.Role(this, "PaperclipTaskExecutionRole", {
      roleName: `lightbulb-${env}-paperclip-task-execution`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Paperclip Fargate task execution role`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy",
        ),
      ],
    });

    // Grant secret read for the two we hand to the container. Scoped
    // tightly to the secret ARNs we actually consume — no `isol8/${env}/*`
    // wildcard.
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipExecutionSecretsRead",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/paperclip_better_auth_secret-*`,
          // Aurora's master secret is named "isol8-${env}-paperclip-db-credentials"
          // (see database-stack.ts), generated under the AWS-managed prefix.
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb-${env}-paperclip-db-credentials-*`,
        ],
      }),
    );

    // KMS decrypt grant on AuthStack's CMK. The BetterAuth secret is
    // encrypted with the shared customer-managed key, and Secrets Manager
    // requires the caller to have `kms:Decrypt` on the underlying CMK in
    // addition to `secretsmanager:GetSecretValue`. CDK's `secret.grantRead`
    // only adds the SecretsManager action — without this explicit KMS
    // grant, ECS task launch fails with:
    //   ResourceInitializationError: AccessDeniedException: Access to KMS
    //   is not allowed ... fetching arn:aws:secretsmanager:.../paperclip_
    //   better_auth_secret
    // Same fix as api-stack.ts T9 (commit 9188442) for the Lambda
    // authorizer's service-token secret.
    //
    // Note: the Aurora master secret uses the AWS-managed
    // `aws/secretsmanager` KMS key (we never passed an explicit
    // `encryptionKey` to `rds.Credentials.fromGeneratedSecret`), so it
    // doesn't need an explicit grant — only the BetterAuth secret does.
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForPaperclipBetterAuthSecret",
        effect: iam.Effect.ALLOW,
        actions: ["kms:Decrypt"],
        resources: [props.paperclipKmsKeyArn],
      }),
    );

    // ─────────────────────────────────────────────────────────────────
    // IAM: task role (the container itself; Paperclip doesn't call AWS
    // APIs in normal operation, so this stays minimal — log writes only)
    // ─────────────────────────────────────────────────────────────────
    const taskRole = new iam.Role(this, "PaperclipTaskRole", {
      roleName: `lightbulb-${env}-paperclip-task`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Paperclip Fargate task role`,
    });
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipCloudWatchLogs",
        actions: [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: [logGroup.logGroupArn],
      }),
    );

    // ─────────────────────────────────────────────────────────────────
    // S3 attachment storage. Paperclip's pluggable StorageService defaults
    // to the local-disk provider (ephemeral Fargate disk, lost on every
    // redeploy); its s3-provider builds an S3Client with NO static
    // credentials, so it picks up THIS task role via the SDK default
    // credential chain. The provider uses only object-level ops
    // (Put/Get/Head/Delete), which grantReadWrite covers.
    // ─────────────────────────────────────────────────────────────────
    const attachmentsBucket = new s3.Bucket(this, "PaperclipAttachmentsBucket", {
      bucketName: `lightbulb-${env}-paperclip-attachments`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
    });
    attachmentsBucket.grantReadWrite(taskRole);

    // ─────────────────────────────────────────────────────────────────
    // Task definition: 1 vCPU / 4 GB Fargate, upstream image. v2026.916 OOM-killed at 1 GB on
    // prod's data during startup recovery (2026-09-24), so the floor is 4 GB.
    // ─────────────────────────────────────────────────────────────────
    const taskDefinition = new ecs.FargateTaskDefinition(this, "PaperclipTaskDef", {
      family: `lightbulb-${env}-paperclip-server`,
      cpu: 1024,
      memoryLimitMiB: 4096,
      executionRole,
      taskRole,
    });

    // Aurora cluster's CDK-managed master secret. Required for PGPASSWORD.
    const dbCredsSecret = props.paperclipDbCluster.secret;
    if (!dbCredsSecret) {
      throw new Error(
        "PaperclipStack: paperclipDbCluster.secret is undefined; " +
          "DatabaseStack must use rds.Credentials.fromGeneratedSecret(...) " +
          "(see database-stack.ts: paperclip Aurora wiring).",
      );
    }

    // Import BetterAuth secret by name to avoid cross-stack KMS auto-grant.
    const betterAuthSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "PaperclipBetterAuthSecretRef",
      props.paperclipBetterAuthSecretName,
    );

    // Bearer token for the bundled isol8.notifications plugin's outbound
    // POSTs to the backend paperclip-events receiver. Same name-only import
    // posture as betterAuthSecret; same CMK, so the execution role's
    // kms:Decrypt grant below covers it.
    const notifyWebhookSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "PaperclipEventsWebhookSecretRef",
      props.paperclipEventsWebhookSecretName,
    );

    // Paperclip seals every local_encrypted company secret (each agent's hermes_gateway
    // apiKey among them) with this key. Unset, Paperclip generates one into a key file on
    // the task's EPHEMERAL disk, so every task replacement orphans every stored secret —
    // 2026-09-23: one replacement left 124 secrets in 12 companies undecryptable and every
    // Paperclip-woken run failed at setup. RETAIN: losing this key loses every secret.
    const secretsMasterKey = new secretsmanager.Secret(this, "PaperclipSecretsMasterKey", {
      secretName: `lightbulb/${env}/paperclip_secrets_master_key`,
      description: `Lightbulb ${env} Paperclip local_encrypted secrets master key (raw 32 chars)`,
      generateSecretString: { passwordLength: 32, excludePunctuation: true, includeSpace: false },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Upstream Decisions HMAC-sign every decision spec and verify it on decide/execute. Unset,
    // Paperclip generates this key into the task's EPHEMERAL disk too, so each task replacement
    // turns every pending decision into a 403 "signature verification failed". RETAIN.
    const decisionSigningSecret = new secretsmanager.Secret(this, "PaperclipDecisionSigningSecret", {
      secretName: `lightbulb/${env}/paperclip_decision_signing_secret`,
      description: `Lightbulb ${env} Paperclip decision-spec HMAC secret`,
      generateSecretString: { passwordLength: 48, excludePunctuation: true, includeSpace: false },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // PAPERCLIP_PUBLIC_URL is Better Auth's ORIGIN IDENTITY — it feeds the auth
    // baseURL, the trustedOrigins set, the secure-cookie scheme, and the `Origin`
    // header the backend BFF sends on /api/auth/* calls (server/src/auth/better-auth.ts).
    // It is NEVER dereferenced over the network (the Teams UI is native in the isol8
    // frontend; the backend reaches Paperclip over PAPERCLIP_INTERNAL_URL), so the
    // fact that `company.isol8.co` is NXDOMAIN does not matter here — it just has to
    // be a STABLE https origin that the backend's Origin header matches. Pointing it
    // at the http in-VPC host (PR #1073) broke Better Auth's secure-cookie sign-in
    // with a 403, so keep it https.
    //
    // It is NOT the agent's Paperclip API URL. The deployed image derives the agent's
    // injected URL from PAPERCLIP_RUNTIME_API_URL (= origin(authPublicBaseUrl)), BUT
    // the per-agent adapterConfig `paperclipApiUrl` override takes precedence
    // (openclaw-gateway execute.ts). The backend sets that override to the in-VPC
    // host in synthesize_openclaw_adapter, so agents reach Paperclip independently of
    // this value. That decoupling is what lets PUBLIC_URL stay the auth origin.
    const paperclipPublicUrl =
      env === "prod"
        ? `https://company.${rootDomain}`
        : `https://${env}.company.${rootDomain}`;

    // In-VPC Cloud Map host. Since upstream's PAPERCLIP_API_URL-first precedence
    // change (adapter-utils buildPaperclipEnv), this env WINS over
    // PAPERCLIP_RUNTIME_API_URL for any agent lacking the adapterConfig
    // `paperclipApiUrl` override — but our backend always sets that override
    // (synthesize_openclaw_adapter), and it is applied last, so the live agent
    // path remains the override. This value is the same in-VPC host either way.
    const paperclipInternalApiUrl = `http://paperclip.${props.cloudMapNamespace.namespaceName}:3100`;

    // Receiver for the bundled isol8.notifications plugin. MUST be the PUBLIC
    // backend host: the plugin outbound-fetch SSRF filter rejects private/
    // reserved IPs, so an in-VPC hostname can never work here.
    const notifyReceiverUrl =
      env === "prod"
        ? `https://api.${rootDomain}/api/v1/webhooks/paperclip-events`
        : `https://api-${env}.${rootDomain}/api/v1/webhooks/paperclip-events`;

    taskDefinition.addContainer("paperclip", {
      // Paperclip publishes to GitHub Container Registry, not Docker Hub
      // (see paperclip's `.github/workflows/docker.yml`). Without the
      // explicit `ghcr.io/` prefix, Docker resolves the bare name to
      // `docker.io/paperclipai/paperclip` which doesn't exist and ECS
      // fails task launch with `CannotPullContainerError: pull access
      // denied`.
      //
      // GHCR-auth assumption: paperclipai/paperclip is a public image
      // (the GitHub repo itself is public), so ECS Fargate platform
      // version 1.4+ pulls it without `repositoryCredentials`. If a
      // future rebuild flips visibility we'll wire a Secrets Manager
      // secret holding a GitHub PAT and reference it via
      // `repositoryCredentials.credentialsParameter`; not needed today.
      image: paperclipImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "paperclip",
        logGroup,
      }),
      environment: {
        PORT: "3100",
        // Defensive parity with upstream's docker/.env.aws.example. The
        // Paperclip Dockerfile sets these in ENV already, but pinning them
        // here documents intent and survives an upstream image change.
        NODE_ENV: "production",
        HOST: "0.0.0.0",
        SERVE_UI: "true",
        PAPERCLIP_DEPLOYMENT_MODE: "authenticated",
        // Better Auth's sign-in limiter (3 per 10s, keyed by client IP) is a
        // browser brute-force guard. It defaults ON in authenticated mode, but
        // here it only ever throttled US: the backend signs in as each owner
        // over the in-VPC Cloud Map host with no X-Forwarded-For, so every
        // fleet sign-in shares ONE bucket, and one owner's onboarding brief
        // 429s another owner's Teams session. Nothing outside the VPC can
        // reach port 3100 (no public IP, no ALB rule). Inside it, the other
        // callers are the per-owner agent containers, which run third-party
        // marketplace code -- but every Paperclip password is a backend-minted
        // secrets.token_urlsafe(32), so a guess is infeasible at any rate and
        // the limiter never defended that class. Upstream's own knob; "false"
        // disables it. Asserted in paperclip-stack.test.ts for both envs.
        PAPERCLIP_AUTH_RATE_LIMIT_ENABLED: "false",
        // Lightbulb owns the fresh-workspace starter roster. The fork (v2026.7-isol8.5+) gates its built-in
        // starter-agent auto-provisioning (Reflection Coach / Summarizer)
        // behind this env var — covering BOTH company-create and the
        // unconditional startup reconcile that would otherwise re-create
        // them on every BFF restart. Built-ins are undeletable by design
        // (409 built_in_agent_undeletable), so not creating them is the
        // only path. Inert on images older than isol8.5 (unread env).
        PAPERCLIP_SKIP_BUILTIN_AGENTS: "1",
        // The server sends usage telemetry to Paperclip unless this is exactly
        // "1" (fork packages/shared/src/telemetry/config.ts
        // resolveTelemetryConfig). A customer fleet must not report upstream.
        PAPERCLIP_TELEMETRY_DISABLED: "1",
        PAPERCLIP_DEPLOYMENT_EXPOSURE: "public",
        // Receiver URL for the bundled isol8.notifications plugin (worker env
        // passthrough in the fork's buildPluginWorkerEnv; the token half is a
        // secret below). Inert on images without the plugin (unread env).
        PAPERCLIP_ISOL8_NOTIFY_URL: notifyReceiverUrl,
        PAPERCLIP_PUBLIC_URL: paperclipPublicUrl,
        // The API host injected into agent heartbeat runs as PAPERCLIP_API_URL.
        // Must be the in-VPC Cloud Map host (not PAPERCLIP_PUBLIC_URL) so the
        // per-user agent containers can resolve + reach it. See the
        // paperclipInternalApiUrl definition above for the full rationale.
        PAPERCLIP_API_URL: paperclipInternalApiUrl,
        // CRITICAL: without this, server/src/index.ts refuses to boot on a
        // fresh Aurora cluster with the error "Refusing to start against a
        // stale schema." Auto-apply runs Drizzle migrations on startup;
        // safe (idempotent) on warm clusters too. Matches upstream's
        // docker/.env.aws.example. The standalone migrate task is still
        // required for the pgvector CREATE EXTENSION step which lives
        // outside Drizzle.
        PAPERCLIP_MIGRATION_AUTO_APPLY: "true",
        // Durable attachment storage. Without these Paperclip falls back to
        // its local-disk provider on ephemeral Fargate storage (lost on every
        // task restart/redeploy). The s3-provider resolves credentials from
        // the task role (no static keys). Region defaults to the stack region.
        PAPERCLIP_STORAGE_PROVIDER: "s3",
        PAPERCLIP_STORAGE_S3_BUCKET: attachmentsBucket.bucketName,
        PAPERCLIP_STORAGE_S3_REGION: this.region,
        // Mirror run logs to S3 (fork run-log-store.ts). Without these they
        // live only on the task's local disk and vanish on replacement. Same
        // bucket as attachments (task role already has grantReadWrite), under
        // its own key prefix.
        RUN_LOG_S3_BUCKET: attachmentsBucket.bucketName,
        RUN_LOG_S3_REGION: this.region,
        RUN_LOG_S3_PREFIX: "run-logs",
        // Paperclip's own hourly pg_dump lands on the task's ephemeral disk -- useless there
        // (Aurora PITR is the backup) and a full DB dump on a container filesystem.
        PAPERCLIP_DB_BACKUP_ENABLED: "false",
        // Drives the remote-agent heartbeat loop. Upstream sets this for
        // public ECS deploys; without it some scheduler-dependent flows
        // never fire.
        HEARTBEAT_SCHEDULER_ENABLED: "true",
        // Better Auth's email/password signup endpoint
        // (`POST /api/auth/sign-up/email`). All "first-admin" bootstrap
        // paths in upstream Paperclip — direct signup, board-claim, and
        // the bootstrap_ceo invite — funnel through this same endpoint
        // (verified in paperclip/ui/src/pages/InviteLanding.tsx and
        // paperclip/server/src/auth/better-auth.ts). Setting this
        // to "true" before any instance_admin exists makes the deployment
        // un-bootstrappable, and no out-of-band path exists upstream.
        //
        // Practical exposure on this deployment is near zero regardless:
        // `company.isol8.co` has no DNS record, the Paperclip task is in
        // private subnets, and our backend's paperclip_proxy router
        // route-filters /api/auth/* (see docs/audit-2026-05-02-
        // paperclip-routes.md). Keeping signup enabled uniformly across
        // dev and prod gives every fresh env-stand-up the same
        // deterministic bootstrap path. If we later add public DNS for
        // Paperclip, revisit this as a deliberate security review post-
        // bootstrap rather than as a default.
        PAPERCLIP_AUTH_DISABLE_SIGN_UP: "false",
        PAPERCLIP_BIND: "lan",
        // Postgres connection split: host/port/user/db at synth time;
        // password injected from Secrets Manager at runtime. The entrypoint
        // shim assembles DATABASE_URL from these so we never have to hold
        // a constructed URL in plaintext at synth time.
        PGHOST: props.paperclipDbCluster.clusterEndpoint.hostname,
        PGPORT: "5432",
        PGUSER: "paperclip_admin",
        PGDATABASE: "paperclip",
      },
      secrets: {
        // Aurora's generated secret has top-level fields {username, password,
        // host, port, dbname, dbClusterIdentifier, engine}. Pull just the
        // password — we already baked the rest into env above.
        PGPASSWORD: ecs.Secret.fromSecretsManager(dbCredsSecret, "password"),
        BETTER_AUTH_SECRET: ecs.Secret.fromSecretsManager(betterAuthSecret),
        PAPERCLIP_ISOL8_NOTIFY_TOKEN: ecs.Secret.fromSecretsManager(notifyWebhookSecret),
        PAPERCLIP_SECRETS_MASTER_KEY: ecs.Secret.fromSecretsManager(secretsMasterKey),
        PAPERCLIP_DECISION_SIGNING_SECRET: ecs.Secret.fromSecretsManager(decisionSigningSecret),
      },
      // Paperclip's image entrypoint expects DATABASE_URL. The shim below
      // assembles it from PG* env at container start, then directly execs
      // node — bypassing the image's `docker-entrypoint.sh`.
      //
      // Why bypass: `docker-entrypoint.sh` ends with `exec gosu node "$@"`
      // to drop root → node. That requires setuid, which Fargate blocks
      // via `NoNewPrivs=1` (default, not opt-out-able on Fargate). Result:
      //   `error: failed switching to "node": operation not permitted`
      // and the task exits before our shell wrapper ever runs.
      //
      // The entrypoint's other job — `chown -R node:node /paperclip` after
      // a UID remap — is irrelevant on Fargate: no host bind mounts, no
      // UID mismatch source. Image build already chowns /paperclip and
      // /app to node, so running as node is safe out of the box.
      //
      // PGPASSWORD is URL-encoded via Node's `encodeURIComponent` before
      // interpolation: RDS-generated passwords routinely contain
      // url-special characters (`/`, `+`, `=`, `@`, `:`) that break
      // Postgres URL parsing.
      //
      // `set -eu` makes failures loud: without it, a failing `node -e`
      // (or unset PGPASSWORD secret) silently produces an empty
      // PGPASSWORD_ENC and the app dies on Postgres auth with no
      // breadcrumb in the wrapper layer.
      entryPoint: ["/bin/sh", "-c"],
      command: [
        // Refuse to start without the master key: Paperclip would otherwise mint an
        // ephemeral key file and silently orphan every stored secret (2026-09-23).
        "set -eu && : \"${PAPERCLIP_SECRETS_MASTER_KEY:?unset - refusing to start with an ephemeral secrets key}\" && : \"${PAPERCLIP_DECISION_SIGNING_SECRET:?unset - refusing to start with an ephemeral decision signing key}\" && PGPASSWORD_ENC=$(node -e 'process.stdout.write(encodeURIComponent(process.env.PGPASSWORD))') && export DATABASE_URL=\"postgres://${PGUSER}:${PGPASSWORD_ENC}@${PGHOST}:${PGPORT}/${PGDATABASE}\" && exec node --import ./server/node_modules/tsx/dist/loader.mjs server/dist/index.js",
      ],
      // Match the UID the image's Dockerfile chowns /paperclip + /app to.
      // Avoids running node as root in production for no upside.
      user: "1000:1000",
      healthCheck: {
        command: [
          "CMD-SHELL",
          "curl -fsS http://localhost:3100/api/health || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
      portMappings: [
        // `name` is required when using ECS Service Connect / Cloud Map
        // discovery — Cloud Map publishes A records that resolve to the
        // task ENI; the named port mapping is what the SDK ties the
        // discovery entry to.
        { containerPort: 3100, protocol: ecs.Protocol.TCP, name: "http" },
      ],
    });

    // ─────────────────────────────────────────────────────────────────
    // Fargate service
    //
    // Cloud Map registration: we use `cloudMapOptions` on the FargateService
    // (matching the OpenClaw fleet pattern). This is the single canonical
    // registration of `paperclip` inside the private namespace owned by
    // ContainerStack — declaring a standalone `servicediscovery.Service`
    // on the side would collide on the namespace Name and fail at first
    // deploy.
    // ─────────────────────────────────────────────────────────────────
    this.service = new ecs.FargateService(this, "PaperclipService", {
      cluster: props.cluster,
      taskDefinition,
      serviceName: `lightbulb-${env}-paperclip-server`,
      // Exactly one task, no autoscaling: the fork's orphan-run reaper is not
      // task-aware, so a second task reaps a sibling's in-flight runs as
      // process_lost, and live events + agent start locks are process-local.
      desiredCount: 1,
      securityGroups: [this.taskSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      circuitBreaker: { rollback: true },
      // Stop the old task BEFORE the new one starts: no old + new overlap.
      // The new task migrates the shared Aurora schema on boot, and since
      // upstream v2026.1012.0 restore/agent-file locks are OS file locks that
      // old and new processes must never share (doc/workspace-restore-locks.md).
      // Costs ~1-2 min of Paperclip downtime per task-def change.
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      // ECS refuses maximumPercent <= 100 while AZ rebalancing is on (the
      // Fargate default); it has nothing to rebalance with one task anyway.
      availabilityZoneRebalancing: ecs.AvailabilityZoneRebalancing.DISABLED,
      enableExecuteCommand: true,
      cloudMapOptions: {
        cloudMapNamespace: props.cloudMapNamespace,
        name: "paperclip",
        dnsRecordType: servicediscovery.DnsRecordType.A,
        dnsTtl: cdk.Duration.seconds(10),
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // One-shot migration task definition.
    //
    // NOT a service — never auto-runs. Operators invoke it manually via
    // `aws ecs run-task` after every cdk deploy that bumps the Paperclip
    // image. See apps/infra/paperclip/RUNBOOK.md for the run command +
    // log-tail incantations.
    //
    // The task:
    //   1. Builds DATABASE_URL from PG* env (PGPASSWORD URL-encoded with
    //      Node's encodeURIComponent, same as the main service entrypoint
    //      — RDS passwords routinely contain URL-special chars).
    //   2. Runs `CREATE EXTENSION IF NOT EXISTS vector;` against the
    //      `paperclip` database. Idempotent — safe to re-run.
    //   3. Runs `pnpm --filter @paperclipai/db migrate` from /app, which
    //      applies the Drizzle schema. Drizzle tracks applied migrations
    //      in `__drizzle_migrations`, so re-runs after partial failure
    //      pick up where they left off.
    //
    // Sized 256 / 512 — one-shot, doesn't need the full server slice.
    // Reuses the same task SG so it inherits the existing Aurora ingress
    // rule (CfnSecurityGroupIngress added above for the main service).
    // ─────────────────────────────────────────────────────────────────
    const migrateExecutionRole = new iam.Role(this, "PaperclipMigrateExecutionRole", {
      roleName: `lightbulb-${env}-paperclip-migrate-execution`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Paperclip migrate task execution role`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy",
        ),
      ],
    });
    // Same tight-scoped secret-read pattern as the main service — only the
    // Aurora master secret is needed (no BetterAuth here).
    migrateExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipMigrateExecutionSecretsRead",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb-${env}-paperclip-db-credentials-*`,
        ],
      }),
    );

    // Defensive parity with the main task execution role: grant
    // `kms:Decrypt` on AuthStack's CMK so a future change that surfaces
    // the BetterAuth secret to the migrate task (e.g., if migrations need
    // to seed BetterAuth state) doesn't silently fail at task launch with
    // the same KMS AccessDeniedException that bit the main task in this
    // PR. Today the migrate task only reads the Aurora master secret
    // (encrypted under AWS-managed `aws/secretsmanager`), so this grant
    // is currently unused — but mirroring the main role keeps the next
    // expansion safe.
    migrateExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForPaperclipBetterAuthSecret",
        effect: iam.Effect.ALLOW,
        actions: ["kms:Decrypt"],
        resources: [props.paperclipKmsKeyArn],
      }),
    );

    const migrateLogGroup = new logs.LogGroup(this, "PaperclipMigrateLogs", {
      logGroupName: logGroupPath(env, "paperclip-migrate"),
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    const migrateTaskDef = new ecs.FargateTaskDefinition(
      this,
      "PaperclipMigrateTaskDef",
      {
        family: `lightbulb-${env}-paperclip-migrate`,
        cpu: 256,
        memoryLimitMiB: 512,
        executionRole: migrateExecutionRole,
        taskRole: new iam.Role(this, "PaperclipMigrateTaskRole", {
          roleName: `lightbulb-${env}-paperclip-migrate-task`,
          assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        }),
      },
    );

    migrateTaskDef.addContainer("migrate", {
      // Same GHCR-vs-Docker-Hub fix as the main service container above.
      image: paperclipImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "paperclip-migrate",
        logGroup: migrateLogGroup,
      }),
      environment: {
        PGHOST: props.paperclipDbCluster.clusterEndpoint.hostname,
        PGPORT: "5432",
        PGUSER: "paperclip_admin",
        PGDATABASE: "paperclip",
      },
      secrets: {
        PGPASSWORD: ecs.Secret.fromSecretsManager(dbCredsSecret, "password"),
      },
      // The upstream image is debian-based (node:lts-trixie-slim) and does
      // NOT ship `psql`, so we apt-install postgresql-client at run time
      // for the CREATE EXTENSION step. Drizzle owns the rest of the
      // schema via its own migration runner.
      //
      // Same URL-encoding lesson as the main service: PGPASSWORD passes
      // through encodeURIComponent before interpolation, since RDS
      // passwords routinely contain `/`, `+`, `=`, `@`, `:`.
      //
      // TODO: bake postgresql-client into a custom migrate image extending
      // paperclipai/paperclip:latest so we don't apt-get-install on every run.
      // Acceptable v1 trade-off given low run frequency (once per deploy);
      // reconsider once schema-change cadence justifies the custom image.
      // Same gosu/Fargate-NoNewPrivs bypass as the main service container.
      // Migrate runs as root (default — no `user`) because apt-get requires
      // it for the postgresql-client install.
      entryPoint: ["/bin/sh", "-c"],
      command: [
        [
          // `set -eu` propagates failures from the URL-encoding subshell
          // and the apt/psql/pnpm chain. Same reasoning as the main
          // service wrapper above.
          "set -eu",
          'PGPASSWORD_ENC=$(node -e "process.stdout.write(encodeURIComponent(process.env.PGPASSWORD))")',
          'export DATABASE_URL="postgres://${PGUSER}:${PGPASSWORD_ENC}@${PGHOST}:${PGPORT}/${PGDATABASE}"',
          "apt-get update && apt-get install -y --no-install-recommends postgresql-client",
          'PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -c "CREATE EXTENSION IF NOT EXISTS vector;"',
          "cd /app && pnpm --filter @paperclipai/db migrate",
        ].join(" && "),
      ],
    });

    this.migrateTaskDefinition = migrateTaskDef;

    new cdk.CfnOutput(this, "PaperclipMigrateTaskDefArn", {
      value: migrateTaskDef.taskDefinitionArn,
      description:
        "One-shot migrate task - run via `aws ecs run-task`. See apps/infra/paperclip/RUNBOOK.md.",
      exportName: `lightbulb-${env}-paperclip-migrate-taskdef`,
    });

    // ─────────────────────────────────────────────────────────────────
    // Internal URL: FastAPI uses this to reach Paperclip (T14).
    //
    // The FargateService.cloudMapOptions block above publishes
    // `paperclip.<namespace>` A records pointing at task ENI IPs.
    // Service Connect / Cloud Map A records always resolve to the
    // container port directly — no separate target group needed.
    // ─────────────────────────────────────────────────────────────────
    this.internalUrl = `http://paperclip.${props.cloudMapNamespace.namespaceName}:3100`;

    new cdk.CfnOutput(this, "PaperclipInternalUrl", {
      value: this.internalUrl,
      description:
        "Internal URL FastAPI uses to reach Paperclip via Cloud Map",
      exportName: `lightbulb-${env}-paperclip-internal-url`,
    });

    new cdk.CfnOutput(this, "PaperclipTaskSecurityGroupId", {
      value: this.taskSecurityGroup.securityGroupId,
      description:
        "Paperclip task SG - granted ingress by T6 (ALB) and FastAPI peer",
      exportName: `lightbulb-${env}-paperclip-task-sg`,
    });

    // Tags
    cdk.Tags.of(this).add("Project", "lightbulb");
    cdk.Tags.of(this).add("Environment", env);
    cdk.Tags.of(this).add("Component", "paperclip");
  }
}
