import * as path from "path";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";

// Single source of truth for the pinned OpenClaw container image.
// Bump openclaw-version.json at the repo root to upgrade.
type OpenClawVersionConfig = {
  upstream: string;
  image: string;
  tag: string;
  full: string;
  extendedImage: string;
  dev: { tag: string };
  prod: { tag: string };
  notes?: string;
};
const OPENCLAW_VERSION: OpenClawVersionConfig = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "..", "..", "..", "openclaw-version.json"), "utf8"),
);

export interface ContainerStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
  /** Pass as string ARN to avoid cross-stack dependency cycle. */
  kmsKeyArn: string;
}

const ENV_CONFIG: Record<
  string,
  { removalPolicy: cdk.RemovalPolicy }
> = {
  dev: { removalPolicy: cdk.RemovalPolicy.DESTROY },
  prod: { removalPolicy: cdk.RemovalPolicy.RETAIN },
};

export class ContainerStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly cloudMapNamespace: servicediscovery.PrivateDnsNamespace;
  public readonly cloudMapService: servicediscovery.Service;
  public readonly efsFileSystem: efs.FileSystem;
  public readonly efsSecurityGroup: ec2.SecurityGroup;
  public readonly containerSecurityGroup: ec2.SecurityGroup;
  public readonly taskExecutionRole: iam.Role;
  public readonly taskRole: iam.Role;
  public readonly openclawExtendedRepo: ecr.IRepository;
  // Exposed so service-stack can pin the backend's ECS_TASK_DEFINITION env to
  // a specific revision ARN — never the family name. The backend cloner
  // reads this ARN to build per-user task defs; pinning prevents per-user
  // clones (which register into the same family) from poisoning family-latest.
  public readonly openclawTaskDef: ecs.FargateTaskDefinition;
  // SSM parameter carrying the current task-def ARN. Consumed by service-stack
  // via ecs.Secret.fromSsmParameter so the value travels through SSM rather
  // than a cross-stack Fn::ImportValue — the Fn::ImportValue path made every
  // task-def bump hit CFN's "export is in use" lock (2026-04-20 incident).

  constructor(scope: Construct, id: string, props: ContainerStackProps) {
    super(scope, id, props);

    const config = ENV_CONFIG[props.environment] ?? ENV_CONFIG.dev;

    // Import KMS key by ARN to avoid cross-stack dependency cycle
    const kmsKey = kms.Key.fromKeyArn(this, "ImportedKmsKey", props.kmsKeyArn);

    // ECS Fargate cluster — Container Insights disabled to avoid per-service
    // metric explosion (~15 metrics × N user containers = thousands of
    // CloudWatch metric streams).  We use AWS/ECS standard metrics
    // (CPUUtilization, MemoryUtilization) for the backend service alarms
    // and custom EMF metrics for everything else.
    this.cluster = new ecs.Cluster(this, "Cluster", {
      vpc: props.vpc,
      containerInsights: false,
    });

    // Cloud Map private DNS namespace
    this.cloudMapNamespace = new servicediscovery.PrivateDnsNamespace(
      this,
      "Namespace",
      {
        name: `isol8-${props.environment}.local`,
        vpc: props.vpc,
        description: `Isol8 ${props.environment} service discovery namespace`,
      },
    );

    // Cloud Map service for container registration
    this.cloudMapService = this.cloudMapNamespace.createService(
      "ContainerService",
      {
        dnsRecordType: servicediscovery.DnsRecordType.A,
        dnsTtl: cdk.Duration.seconds(10),
        description: `Isol8 ${props.environment} container service discovery`,
      },
    );

    // EFS security group
    this.efsSecurityGroup = new ec2.SecurityGroup(this, "EfsSecurityGroup", {
      vpc: props.vpc,
      description: `Isol8 ${props.environment} EFS security group`,
      allowAllOutbound: false,
    });

    // EFS filesystem
    this.efsFileSystem = new efs.FileSystem(this, "FileSystem", {
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroup: this.efsSecurityGroup,
      encrypted: true,
      kmsKey,
      lifecyclePolicy: efs.LifecyclePolicy.AFTER_30_DAYS,
      performanceMode: efs.PerformanceMode.GENERAL_PURPOSE,
      throughputMode: efs.ThroughputMode.BURSTING,
      removalPolicy: config.removalPolicy,
    });

    // Container (Fargate task) security group
    this.containerSecurityGroup = new ec2.SecurityGroup(
      this,
      "ContainerSecurityGroup",
      {
        vpc: props.vpc,
        description: `Isol8 ${props.environment} Fargate container security group`,
        allowAllOutbound: true,
      },
    );

    // Allow containers to mount EFS
    this.efsSecurityGroup.addIngressRule(
      this.containerSecurityGroup,
      ec2.Port.tcp(2049),
      "Allow NFS from Fargate containers",
    );

    // ECS task execution role (used by ECS agent)
    this.taskExecutionRole = new iam.Role(this, "TaskExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Isol8 ${props.environment} ECS task execution role`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy",
        ),
      ],
    });

    this.taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "secretsmanager:GetSecretValue",
        ],
        resources: ["*"],
      }),
    );

    // ECS task role (used by the container itself)
    this.taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Isol8 ${props.environment} ECS task role`,
    });

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: ["*"],
      }),
    );

    // EFS client access — required for IAM-authenticated EFS mounts
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess",
        ],
        resources: ["*"],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: ["*"],
      }),
    );

    // ECS Exec — lets us aws ecs execute-command into per-user OpenClaw
    // containers for live debugging (skill installs, workspace state, etc.).
    // Paired with enableExecuteCommand=true on each per-user service in
    // ecs_manager.py.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ],
        resources: ["*"],
      }),
    );

    // Extended OpenClaw image — built and pushed by
    // .github/workflows/build-openclaw-image.yml. Per-env tags live in
    // openclaw-version.json (extendedImage + dev.tag/prod.tag). After the
    // extended-image migration completes, this stack reads the env-appropriate
    // tag and uses it as the per-user container image.
    //
    // The repository is account-scoped, so we create it only in the dev stack
    // and reference it by name in prod. This keeps a single source of truth
    // for image tags (one ECR repo, two env-tagged image references).
    //
    // Initialized BEFORE the container task def so the image-selection
    // expression below can dereference it.
    // The ECR repo `isol8/openclaw-extended` is account-scoped and holds
    // images for both dev and prod. It outlives any individual stack — when
    // we tear down container-stack to clean up state, the repo (and its
    // image history) MUST survive. Originally CDK created it with
    // `RemovalPolicy.RETAIN`, but that leaves an orphan that blocks later
    // recreation: ECR rejects the create with "already exists" because the
    // physical resource still exists outside any stack.
    //
    // Both dev and prod now treat the repo as an EXISTING resource via
    // `fromRepositoryName`. Lifecycle rules, immutability, and image-scan
    // settings are managed once at the AWS level (set on first creation,
    // not by CDK). To recreate from scratch you'd need to delete the ECR
    // repo + all images via the AWS CLI/console first.
    this.openclawExtendedRepo = ecr.Repository.fromRepositoryName(
      this,
      "OpenclawExtendedRepo",
      "isol8/openclaw-extended",
    );

    if (props.environment === "dev") {
      // OIDC role for the build-openclaw-image workflow. Scoped to this repo
      // so the existing isol8-dev-github-actions role (which only has
      // sts:AssumeRole on cdk-* roles) doesn't need broader ECR perms.
      // Created in dev only (account-scoped role, single source of truth).
      const oidcProviderArn = `arn:aws:iam::${this.account}:oidc-provider/token.actions.githubusercontent.com`;
      const oidcProvider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
        this,
        "GithubOidcProvider",
        oidcProviderArn,
      );
      const builderRole = new iam.Role(this, "OpenclawImageBuilderRole", {
        roleName: "isol8-openclaw-image-builder",
        assumedBy: new iam.OpenIdConnectPrincipal(oidcProvider, {
          StringLike: {
            "token.actions.githubusercontent.com:sub": "repo:Isol8AI/isol8:*",
          },
          StringEquals: {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          },
        }),
        description: "Used by .github/workflows/build-openclaw-image.yml to push to ECR",
        maxSessionDuration: cdk.Duration.minutes(60),
      });
      builderRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["ecr:GetAuthorizationToken"],
          resources: ["*"],
        }),
      );
      this.openclawExtendedRepo.grantPullPush(builderRole);
    }

    // Base OpenClaw task definition — the backend clones this per user,
    // replacing the EFS access point for data isolation.
    const env = props.environment;
    const openclawLogGroup = new cdk.aws_logs.LogGroup(this, "OpenClawLogGroup", {
      logGroupName: `/isol8/${env}/openclaw`,
      retention: cdk.aws_logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Single per-user task size for the flat-fee pivot. Per spec §3.2 / §10.
    // Old per-tier sizing deleted; resize add-ons are out of scope (§3.5).
    // Backend-side _TIER_TASK_RESOURCES collapse is in Task 13 of this plan.
    const PER_USER_TASK_RESOURCES = { cpu: 512, memoryMiB: 2048 } as const;

    this.openclawTaskDef = new ecs.FargateTaskDefinition(this, "OpenClawTaskDef", {
      family: `isol8-${env}-openclaw`,
      cpu: PER_USER_TASK_RESOURCES.cpu,
      memoryLimitMiB: PER_USER_TASK_RESOURCES.memoryMiB,
      taskRole: this.taskRole,
      executionRole: this.taskExecutionRole,
    });

    // SSM param + cross-stack export removed in #410. Backend now reads the
    // latest CDK base revision live via describe_task_definition(family) on
    // each provision call. The base family is uncontaminated by per-user
    // clones because EcsManager registers them in `<base>-user`, so a bare
    // family lookup deterministically returns the most recent CDK base —
    // no SSM coupling needed and the cache-at-startup bug class is gone.

    // Startup command — almost everything (apt packages, pip, npm globals,
    // gh, uv) is now baked into the extended OpenClaw image. We only:
    //   1. Install markdown-converter via clawhub (lives at clawhub.com,
    //      not bundled in the image — landing dir comes from
    //      CLAWHUB_WORKDIR=/home/node/.openclaw, set as a container env var).
    //   2. Launch the gateway.
    // Cold-start drops from ~30s to ~5s.
    const startupCommand = [
      "clawhub install markdown-converter --no-input 2>/dev/null",
      "exec node /app/openclaw.mjs gateway --port 18789 --bind lan",
    ].join("; ");

    // Image selection: use the extended ECR image once the per-env tag has been
    // promoted past the "bootstrap" placeholder. Until then, fall back to the
    // legacy upstream image so the env keeps deploying. This lets us flip dev
    // and prod independently via openclaw-version.json bumps without coupling.
    // Unknown envs (e.g. "local") default to legacy via the optional chain.
    const envCfg =
      props.environment === "dev" || props.environment === "prod"
        ? OPENCLAW_VERSION[props.environment]
        : undefined;
    const envTag = envCfg?.tag ?? "bootstrap";
    const containerImage =
      envTag === "bootstrap"
        ? ecs.ContainerImage.fromRegistry(OPENCLAW_VERSION.full)
        : ecs.ContainerImage.fromEcrRepository(this.openclawExtendedRepo, envTag);

    const openclawContainer = this.openclawTaskDef.addContainer("openclaw", {
      image: containerImage,
      essential: true,
      command: ["sh", "-c", startupCommand],
      user: "0:0",
      workingDirectory: "/home/node",
      environment: {
        HOME: "/home/node",
        CHOKIDAR_USEPOLLING: "true",
        // Redirect clawhub installs to the OpenClaw managed-skills directory
        // (~/.openclaw/skills) so they're scanned by every agent. Without
        // this, clawhub falls through to agents.defaults.workspace and lands
        // at /home/node/.openclaw/workspaces/skills — a path no scanner checks.
        CLAWHUB_WORKDIR: "/home/node/.openclaw",
        // Skip mDNS/bonjour service advertising. OpenClaw advertises itself
        // on the LAN via bonjour for desktop auto-discovery; in Fargate
        // there's no LAN multicast peer, so the announce gets stuck and
        // blocks channel/sidecar startup for ~13 minutes before the
        // bonjour watchdog finally gives up. Verified via runtime logs +
        // upstream `src/gateway/server-discovery-runtime.ts` — env var
        // present in image tag 2026.5.7.
        OPENCLAW_DISABLE_BONJOUR: "true",
        // Emit per-step startup trace lines so we can attribute the multi-minute
        // post-gateway-ready wedge (channels-and-sidecars phase) to a specific
        // step. Upstream wraps each phase in measureStartup; with this flag the
        // gateway logs `startup trace: <name> <ms>ms total=<ms>ms` per step
        // (src/gateway/server.impl.ts:136). Cheap to leave on permanently.
        OPENCLAW_GATEWAY_STARTUP_TRACE: "1",
      },
      portMappings: [{ containerPort: 18789, protocol: ecs.Protocol.TCP }],
      logging: ecs.LogDrivers.awsLogs({
        logGroup: openclawLogGroup,
        streamPrefix: "openclaw",
      }),
    });

    // Mount EFS workspace — OpenClaw reads openclaw.json and agent files from here
    openclawContainer.addMountPoints({
      containerPath: "/home/node/.openclaw",
      sourceVolume: "openclaw-workspace",
      readOnly: false,
    });

    // Add EFS volume — the backend replaces the access point per user
    this.openclawTaskDef.addVolume({
      name: "openclaw-workspace",
      efsVolumeConfiguration: {
        fileSystemId: this.efsFileSystem.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: { iam: "ENABLED" },
      },
    });

    // ---- ECS task lifecycle telemetry ----
    // ECS retains stopped-task records for only 1 hour, after which we lose
    // the `stoppedReason`/`stopCode` fields entirely. That window has bitten
    // us at least once already (2026-05-01 user wedge investigation): the
    // first task of the day was replaced and we couldn't reconstruct why
    // because the stopped-task record had aged out.
    //
    // EventBridge surfaces `ECS Task State Change` events as they happen.
    // We forward all transitions on the openclaw cluster to a dedicated log
    // group so the full event JSON (including stoppedReason, stopCode,
    // exit codes, attachments) is durable and queryable months later via
    // CloudWatch Logs Insights.
    //
    // Cost: events are ~1 KB each, free tier covers 5 GB/month log ingest.
    // At our scale this is well under $1/mo.
    const ecsTaskEventsLogGroup = new logs.LogGroup(
      this,
      "EcsTaskEventsLogGroup",
      {
        logGroupName: `/isol8/${env}/ecs-task-events`,
        // Three months — long enough to investigate weekly recurrences
        // without paying open-ended retention costs.
        retention: logs.RetentionDays.THREE_MONTHS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      },
    );

    new events.Rule(this, "EcsTaskStateChangeRule", {
      description:
        `Persist ECS Task State Change events for ${this.cluster.clusterName} to a CloudWatch log group, so we can reconstruct task stop reasons after the 1h ECS retention window expires.`,
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          // Only events from THIS cluster — avoids polluting the log
          // group with goosetown / paperclip / backend cluster noise.
          clusterArn: [this.cluster.clusterArn],
        },
      },
      targets: [new targets.CloudWatchLogGroup(ecsTaskEventsLogGroup)],
    });
  }
}
