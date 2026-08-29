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
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";
import { openclawConfigLockRecoveryCommand } from "../openclaw-config-lock-recovery";
import { logGroupPath } from "../table-names";

// Single source of truth for the pinned OpenClaw container image.
// Bump openclaw-version.json at the repo root to upgrade.
type OpenClawEnvConfig = {
  tag: string;
};
type OpenClawVersionConfig = {
  upstream: string;
  image: string;
  tag: string;
  full: string;
  extendedImage: string;
  dev: OpenClawEnvConfig;
  prod: OpenClawEnvConfig;
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
  /**
   * Name (NOT ISecret) of the shared gog file-keyring password secret created
   * in AuthStack. Imported locally via fromSecretNameV2 and injected into the
   * per-user OpenClaw task def as GOG_KEYRING_PASSWORD — same secret-name
   * plumbing the ServiceStack uses to avoid cross-stack KMS auto-grant cycles.
   */
  gogKeyringPasswordSecretName: string;
  /**
   * Name (NOT ISecret) of the Cloudflare Zero Trust enrollment secret created
   * in AuthStack, injected as CLOUDFLARE_WARP_ENROLLMENT. Same secret-name
   * plumbing as gogKeyringPasswordSecretName above, and for the same reason.
   */
  cloudflareWarpEnrollmentSecretName: string;
}

/**
 * Loopback port the container's WARP local proxy listens on, and the port the
 * backend points Chrome's PAC at. ONE constant so the two halves cannot drift:
 * service-stack imports it for the backend's BROWSER_EGRESS_PROXY_PORT env,
 * and the task definition below passes it to isol8-warp-boot.sh, which writes
 * it into mdm.xml as `proxy_port`. A plain module import, not a CDK
 * cross-stack ref, so it creates no CFN export.
 */
export const BROWSER_EGRESS_PROXY_PORT = 40000;

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
  public readonly ownerBedrockBasePolicy: iam.ManagedPolicy;
  public readonly ownerBedrockBoundary: iam.ManagedPolicy;
  public readonly openclawExtendedRepo: ecr.IRepository;
  // Exposed so service-stack can pin the backend's ECS_TASK_DEFINITION env to
  // a specific revision ARN — never the family name. The backend cloner
  // reads this ARN to build per-user task defs; pinning prevents per-user
  // clones (which register into the same family) from poisoning family-latest.
  public readonly openclawTaskDef: ecs.FargateTaskDefinition;
  // SSM parameter carrying the current task-def ARN. Consumed by service-stack
  // via ecs.Secret.fromSsmParameter so the value travels through SSM rather
  // than a cross-stack Fn::ImportValue — the Fn::ImportValue path made every
  // task-def bump hit CFN's "export is in use" lock.

  constructor(scope: Construct, id: string, props: ContainerStackProps) {
    super(scope, id, props);

    const config = ENV_CONFIG[props.environment] ?? ENV_CONFIG.dev;

    // Per-env OpenClaw pin, resolved ONCE up front — it selects the image
    // below. Unknown envs (e.g. "local") fall back to the public bootstrap
    // image, which carries no local-sqlite boot chain; the boot guard in the
    // startup command is what catches that, loudly, at runtime.
    const envCfg =
      props.environment === "dev" || props.environment === "prod"
        ? OPENCLAW_VERSION[props.environment]
        : undefined;
    const envTag = envCfg?.tag ?? "bootstrap";
    const litestreamBucketName = `isol8-${props.environment}-openclaw-replicas`;

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
      // Associates FARGATE + FARGATE_SPOT capacity providers so the backend
      // can launch per-user containers on Spot (~70% cheaper). The backend
      // falls back to on-demand when Spot capacity is unavailable.
      enableFargateCapacityProviders: true,
    });

    // Cloud Map private DNS namespace.
    //
    // The construct ids stay "NamespaceV2"/"ContainerServiceV2" even though the
    // TypeScript members are the plain names. Renaming a construct id changes
    // the LOGICAL id, which CloudFormation reads as delete-and-recreate — it
    // would destroy the live namespace every service is registered in, purely
    // to make this file read more nicely. The V2 suffix is the scar from the
    // rename; leave it.
    //
    // The retired `isol8-${env}.local` pair that used to live here is gone as of
    // this change. It could not be renamed in place: `name` is create-only, so a
    // rename is a replacement, and the replacement changed this stack's exported
    // service ARN while {env}-service still imported it —
    //
    //   "Cannot update export ...NamespaceContainerService34...Arn
    //    as it is in use by isol8-{env}-service"
    //
    // Stacks deploy container -> service, so no single deploy could move the
    // consumer off first. It took two: add alongside and move every consumer,
    // then delete. Removing the old pair also drops the four exportValue pins
    // that kept its exports published through the middle deploy.
    this.cloudMapNamespace = new servicediscovery.PrivateDnsNamespace(
      this,
      "NamespaceV2",
      {
        name: `lightbulb-${props.environment}.local`,
        vpc: props.vpc,
        description: `Lightbulb ${props.environment} service discovery namespace`,
      },
    );

    this.cloudMapService = this.cloudMapNamespace.createService(
      "ContainerServiceV2",
      {
        dnsRecordType: servicediscovery.DnsRecordType.A,
        dnsTtl: cdk.Duration.seconds(10),
        description: `Lightbulb ${props.environment} container service discovery`,
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

    // Scoped to the only secrets per-user task defs inject: per-user BYOK
    // LLM keys (ecs_manager._build_byo_secrets_for_user) and the shared gog
    // keyring password (referenced by name below, so CDK cannot auto-grant
    // it). Secret ARNs carry a random 6-char suffix, hence the trailing
    // wildcards. Unscoped GetSecretValue would let a leaked execution-role
    // credential read every secret in the account.
    this.taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PerUserTaskSecrets",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${props.environment}/user-keys/*`,
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${props.gogKeyringPasswordSecretName}*`,
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${props.cloudflareWarpEnrollmentSecretName}*`,
        ],
      }),
    );

    // The gog file-keyring password secret is encrypted with the AuthStack CMK
    // (unlike per-user BYOK secrets, which use the AWS-managed key that
    // GetSecretValue covers implicitly). Injecting a CMK-encrypted secret needs
    // an explicit kms:Decrypt on that key. Manual grant (not CDK auto-grant) to
    // avoid a cross-stack KMS cycle, mirroring the backend service.
    this.taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForSecrets",
        actions: ["kms:Decrypt"],
        resources: [props.kmsKeyArn],
      }),
    );

    // ECS task role (used by the container itself)
    this.taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Isol8 ${props.environment} ECS task role`,
    });

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

    // The 3 fan-out regions x 3 foundation models = 9 FM ARNs. Mirrors
    // core/billing/bedrock_aip.py FM_IDS/FAN_OUT_REGIONS — keep in lockstep.
    const FAN_OUT_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];
    const FM_IDS = [
      "anthropic.claude-opus-4-6-v1",
      "anthropic.claude-sonnet-4-6",
      "anthropic.claude-haiku-4-5-20251001-v1:0",
    ];
    const foundationModelArns = FAN_OUT_REGIONS.flatMap((region) =>
      FM_IDS.map((model) => `arn:aws:bedrock:${region}::foundation-model/${model}`),
    );

    // EFS/logs/ssm grants the backend attaches to every per-owner role. Keeps
    // the non-Bedrock grants in IaC (auditable); only the per-owner-varying
    // scoped-Bedrock inline policy lives in backend code.
    this.ownerBedrockBasePolicy = new iam.ManagedPolicy(this, "OwnerBedrockBasePolicy", {
      managedPolicyName: `isol8-${props.environment}-owner-bedrock-base`,
      statements: [
        new iam.PolicyStatement({
          sid: "EfsClientAccess",
          actions: [
            "elasticfilesystem:ClientMount",
            "elasticfilesystem:ClientWrite",
            "elasticfilesystem:ClientRootAccess",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "CloudWatchLogs",
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "SsmExec",
          actions: [
            "ssmmessages:CreateControlChannel",
            "ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel",
            "ssmmessages:OpenDataChannel",
          ],
          resources: ["*"],
        }),
      ],
    });

    // Permissions boundary capping every backend-minted per-owner role to the
    // UNION of what it may ever hold: scoped Bedrock invoke (on inference
    // profiles + the 9 FM ARNs) + EFS + logs + ssm. A backend that can
    // CreateRole+PutRolePolicy cannot escalate a per-owner role past this.
    this.ownerBedrockBoundary = new iam.ManagedPolicy(this, "OwnerBedrockBoundary", {
      managedPolicyName: `isol8-${props.environment}-owner-bedrock-boundary`,
      statements: [
        new iam.PolicyStatement({
          sid: "BoundaryBedrockInvoke",
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [
            `arn:aws:bedrock:*:${this.account}:application-inference-profile/*`,
            ...foundationModelArns,
          ],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryEfs",
          actions: [
            "elasticfilesystem:ClientMount",
            "elasticfilesystem:ClientWrite",
            "elasticfilesystem:ClientRootAccess",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryLogs",
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "BoundarySsm",
          actions: [
            "ssmmessages:CreateControlChannel",
            "ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel",
            "ssmmessages:OpenDataChannel",
          ],
          resources: ["*"],
        }),
        // local-sqlite layout: the boundary must CAP (not grant) S3 access
        // to the Litestream replica bucket, or the per-owner inline
        // "owner-litestream-s3" policy the backend mints would be silently
        // nullified (effective perms = inline ∩ boundary). Bucket-wide here —
        // the boundary is shared across owners; per-owner prefix isolation
        // lives in the inline policy. DeleteObject is required: Litestream
        // retention/compaction prunes old LTX files.
        new iam.PolicyStatement({
          sid: "BoundaryLitestreamS3",
          actions: [
            "s3:GetBucketLocation",
            "s3:ListBucket",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
          ],
          resources: [
            `arn:aws:s3:::${litestreamBucketName}`,
            `arn:aws:s3:::${litestreamBucketName}/*`,
          ],
        }),
        // Defense in depth: never let a per-owner role assume into OUR account,
        // even if a misconfigured inline policy tried to. An explicit Deny
        // always wins. Unconditional: the AWS integration's credentials are
        // vended by the backend, so no per-owner role ever needs an assume.
        new iam.PolicyStatement({
          sid: "BoundaryDenyAssumeIntoOurAccount",
          effect: iam.Effect.DENY,
          actions: ["sts:AssumeRole"],
          resources: [`arn:aws:iam::${this.account}:role/*`],
        }),
      ],
    });

    // ROLLBACK CONSTRAINT: there is no EFS-layout flag to flip back to — the
    // local-sqlite layout is the only layout. Reverting means reverting the
    // code, and that alone does NOT restore an env: RemovalPolicy.RETAIN
    // (prod) leaves this literal-named bucket behind, so a later re-promotion
    // fails with "bucket already exists" until it is imported or deleted, and
    // containers that already migrated have their pre-cutover state in EFS
    // pre-migration-archive/, which must be restored too. Rollback is a manual
    // ops sequence -- restore that archive into .openclaw/{state,agents}, then
    // revert -- never a bare revert.
    //
    // The per-owner S3 GRANT is NOT here: this boundary statement is only a
    // cap. The actual prefix-scoped inline policy ("owner-litestream-s3") is
    // minted per owner by the backend's ensure_owner_bedrock_role, and
    // existing owners are backfilled by
    // apps/backend/scripts/ensure_litestream_s3_policies.py — which MUST run
    // before any fleet restart wave in an env whose owners predate this
    // bucket, or litestream (the boot supervisor) AccessDenies and tasks
    // crash-loop.
    //
    // Litestream replica bucket — durability layer for hot SQLite on
    // container-local disk. Literal name (consumers reference by name, never
    // cross-stack refs — KMS-cycle posture); SSE-S3 like every other bucket in
    // this repo (the CMK posture deliberately does not extend to S3 — flagged
    // trade-off: replicas carry auth_profile_store rows, fenced by BLOCK_ALL +
    // per-owner-prefix IAM). Request metrics enabled so the idle-PUT pathology
    // (litestream#1171, still OPEN: ~480 PUT/hr per idle DB) is alarmable.
    // Litestream's docs say an incremental sync with no new WAL pages writes
    // no file, but we have NOT observed our own idle rate yet — treat this
    // alarm, not that claim, as the source of truth, and gate the prod
    // promotion on a measured idle window. No lifecycle expiration — Litestream retention owns pruning
    // (deleting LTX files under it would break restores).
    new s3.Bucket(this, "OpenclawReplicasBucket", {
      bucketName: litestreamBucketName,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: config.removalPolicy,
      autoDeleteObjects: props.environment !== "prod",
      metrics: [{ id: "EntireBucket" }],
    });

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

      // The build-paperclip-image workflow uses this same OIDC role to push the
      // extended Paperclip image (apps/infra/paperclip-extended/). Grant it
      // pull+push on that account-scoped repo too.
      const paperclipExtendedRepo = ecr.Repository.fromRepositoryName(
        this,
        "PaperclipExtendedRepoForBuilder",
        "isol8/paperclip-extended",
      );
      paperclipExtendedRepo.grantPullPush(builderRole);
    }

    // Base OpenClaw task definition — the backend clones this per user,
    // replacing the EFS access point for data isolation.
    const env = props.environment;
    const openclawLogGroup = new cdk.aws_logs.LogGroup(this, "OpenClawLogGroup", {
      logGroupName: logGroupPath(env, "openclaw"),
      retention: cdk.aws_logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Single per-user task size for the flat-fee pivot. Per spec §3.2 / §10.
    // Old per-tier sizing deleted; resize add-ons are out of scope (§3.5).
    // 1 vCPU / 4 GB: 2 GB was upstream OpenClaw's documented floor and OOMed
    // under two seeded agents + memory sidecar + in-container Chromium; the
    // full vCPU stops boot/tool-exec/Chromium contending for half a core.
    // Affordable because per-user services launch on Fargate Spot.
    // Keep in lockstep with PER_USER_CPU / PER_USER_MEMORY_MIB in
    // apps/backend/core/containers/ecs_manager.py (clones stamp those values).
    const PER_USER_TASK_RESOURCES = { cpu: 1024, memoryMiB: 4096 } as const;

    this.openclawTaskDef = new ecs.FargateTaskDefinition(this, "OpenClawTaskDef", {
      family: `isol8-${env}-openclaw`,
      cpu: PER_USER_TASK_RESOURCES.cpu,
      memoryLimitMiB: PER_USER_TASK_RESOURCES.memoryMiB,
      taskRole: this.taskRole,
      executionRole: this.taskExecutionRole,
    });

    // Backend reads the latest CDK base revision live via
    // describe_task_definition(family) on each provision call rather than
    // via an SSM param or cross-stack export. The base family is
    // uncontaminated by per-user clones because EcsManager registers them
    // in `<base>-user`, so a bare family lookup deterministically returns
    // the most recent CDK base — no SSM coupling needed and the
    // cache-at-startup bug class is gone.

    // Startup command — almost everything (apt packages, pip, npm globals,
    // gh, uv) is baked into the extended OpenClaw image. The boot sequence:
    //
    // Step 0 (lock recovery): perform the one-time transition away from the
    // backend's retired permanent-fcntl sidecar. The helper takes the backend's
    // persistent recovery guard, publishes a resumable protocol marker, then
    // fcntl-locks and identity-checks the legacy sidecar before unlinking it.
    // Thus a backend hot patch cannot open/wait on an inode that recovery
    // detaches from the pathname. Per-user service updates use
    // SINGLE_WRITER_DEPLOYMENT_CONFIG (min=0/max=100), so the old OpenClaw task
    // is fully stopped before this command runs. Later boots use the completed
    // marker to recover a sidecar abandoned by either O_EXCL writer while the
    // same guard still excludes every prepared backend writer. Unexpected
    // filesystem objects and a still-held legacy fcntl lock fail closed.
    //
    // Step A (migration): rename a legacy real `~/.openclaw/browser`
    // directory aside. This handles the first boot per per-user EFS
    // access point after this change deploys — without it, Step B's
    // `ln -sfn` would silently nest a symlink inside the existing
    // real directory (empirically verified: `ln -sfn target real-dir`
    // exits 0 with no error and creates `real-dir/target -> ...`
    // rather than replacing `real-dir`). After the first boot the
    // path is a symlink and `[ ! -L ]` is false, so the mv is skipped
    // on every subsequent start (idempotent). The archive name keeps
    // the prior chromium profile on EFS for forensic recovery; ops
    // can delete it manually later. `mv` on NFS is one rename
    // syscall, cheap; `rm -rf` of a deep chromium tree on NFS would
    // itself risk the very wedge class this migration prevents.
    //
    // Step B (symlink): point `~/.openclaw/browser` at Fargate
    // disk-backed ephemeral task storage (`/tmp`, NOT kernel tmpfs —
    // true tmpfs requires EC2 launch type, unavailable on Fargate).
    // Chromium's `--user-data-dir` (LevelDB + SQLite + cache files)
    // now lives on local disk instead of EFS NFS. Upstream
    // chrome.ts:resolveOpenClawUserDataDir constructs
    // `path.join(CONFIG_DIR, "browser", profileName, "user-data")`
    // and Node's fs.* follow the symlink transparently. Motivated by
    // a wedge investigation: chromium fsync + lock traffic against
    // NFSv4.1 EFS is the most likely trigger of the NFS RPC stalls
    // that wedge Node's event loop (rpc_wait_bit_killable). Tradeoff:
    // chromium cookies / localStorage are ephemeral per container
    // restart. Container restart frequency is bounded by the /healthz
    // task healthcheck, so this is rare in steady state.
    //
    // Step C: launch the gateway. Agents that need markdown-converter
    // can `clawhub install` it at runtime.
    //
    // Failure semantics: each step that MUST succeed is suffixed with
    // `|| exit 1` so the container fails fast and ECS replaces it
    // rather than silently degrading. Naïve `set -e` or
    // `&&`-everywhere would break the happy path because Step A's
    // `[ -e ] && [ ! -L ]` legitimately returns non-zero on the
    // steady-state case (path is already a symlink → no migration
    // needed). The conditional migration uses `{ ... ; }` (command
    // group in the same shell, NOT a subshell — `exit 1` inside a
    // subshell would only exit the subshell and the parent would
    // continue, defeating the purpose). Without these guards, an NFS
    // hiccup that fails `mv` would let Step B's `ln -sfn` silently
    // nest the symlink inside the legacy real directory (empirically
    // verified: `ln -sfn target real-dir` exits 0 and creates
    // `real-dir/target -> ...` rather than replacing `real-dir`),
    // shipping a no-op that LOOKS deployed.
    const startupCommand = [
      // Step 0 — recover only while holding the same guard every backend
      // config writer takes. Python + fcntl are present in the pinned image.
      openclawConfigLockRecoveryCommand(),
      // Step A — migration. POSIX-sh: `[ ... ]`, `&&`, `$(...)`,
      // and `{ ... ; }` are all portable to `sh -c`. The trailing
      // `; ` inside `{ ... ; }` is required by POSIX shell syntax.
      "[ -e /home/node/.openclaw/browser ] && [ ! -L /home/node/.openclaw/browser ] && { mv /home/node/.openclaw/browser /home/node/.openclaw/browser.efs-archive-$(date +%s) || exit 1; }",
      // Step B — symlink chromium user-data to ephemeral task storage.
      // `install -d -o 1000 -g 1000` rather than `mkdir -p`: this path is on
      // the container's own writable layer, NOT the EFS mount, so no access
      // point squashes it and a root `mkdir` would leave it root-owned and
      // unwritable by the uid-1000 gateway (Step D). Create it correctly
      // instead of creating it wrong and repairing it later.
      "install -d -o 1000 -g 1000 /tmp/openclaw-browser || exit 1",
      "ln -sfn /tmp/openclaw-browser /home/node/.openclaw/browser || exit 1",
      // Step C — make MCP package caches fast AND restart-safe by splitting
      // each package manager's PERSISTENT download cache from its per-run
      // EXTRACT directory. MCP servers launch per chat session as
      // `npx -y <pkg>` / `uvx <pkg>` children that inherit only the MCP SDK's
      // getDefaultEnvironment() (HOME, PATH, USER, SHELL, …) merged with the
      // per-server `mcp.servers[].env` — NOT the container env (upstream
      // src/agents/mcp-stdio-transport.ts) — so a container-level
      // NPM_CONFIG_CACHE / UV_CACHE_DIR cannot relocate their cache; only the
      // HOME-relative default paths ($HOME/.npm, $HOME/.cache/uv) are
      // reachable, via symlinks.
      //
      // The EXTRACT step is the problem. `npx` unpacks a package's full
      // node_modules tree (thousands of small files) into `_npx/<hash>/`, and
      // `uv` unpacks wheels into its cache. On the EFS-backed .openclaw mount
      // every one of those file creates is a synchronous NFS round trip; the
      // single-threaded unpack serializes thousands of them and, under
      // concurrent in-container I/O, wedges in uninterruptible D-state —
      // overrunning the gateway's per-server MCP connect timeout so the tool
      // never comes up, and getting killed before the cache warms (a
      // self-perpetuating stall). The hot extract therefore must live on local
      // ephemeral task storage (/tmp), not EFS. Same migration guard as the
      // chromium symlink: `ln -sfn` nests inside an existing real directory
      // instead of replacing it, so move a legacy real dir aside first. A
      // legacy `_npx` can be tens of thousands of files, so use `mv` (an O(1)
      // same-filesystem rename) — never `rm -rf`, whose synchronous NFS
      // unlink storm on the boot path could overrun the health-check start
      // period.
      //
      // npm splits cleanly: keep the compressed download cache (`_cacache`,
      // immutable tarballs — cheap on NFS, read-mostly) on EFS so a restart
      // never re-downloads, but redirect ONLY the `_npx` extract tree to /tmp.
      // Split by filesystem: `.openclaw/.cache/npm` is on EFS, where the access
      // point squashes every create to 1000:1000 already. The other three are
      // on the container's writable layer and must be created as 1000 outright
      // — see the note on /tmp/openclaw-browser above.
      "mkdir -p /home/node/.openclaw/.cache/npm || exit 1",
      "install -d -o 1000 -g 1000 /home/node/.cache /tmp/openclaw-npx /tmp/openclaw-uv || exit 1",
      "[ -e /home/node/.npm ] && [ ! -L /home/node/.npm ] && { mv /home/node/.npm /home/node/.npm.efs-archive-$(date +%s) || exit 1; }",
      "ln -sfn /home/node/.openclaw/.cache/npm /home/node/.npm || exit 1",
      "[ -e /home/node/.openclaw/.cache/npm/_npx ] && [ ! -L /home/node/.openclaw/.cache/npm/_npx ] && { mv /home/node/.openclaw/.cache/npm/_npx /home/node/.openclaw/.cache/npm/_npx.efs-archive-$(date +%s) || exit 1; }",
      "ln -sfn /tmp/openclaw-npx /home/node/.openclaw/.cache/npm/_npx || exit 1",
      // uv CANNOT split. It stages each unpack in a temp dir on the cache root
      // and then atomically renames it into `archive-v0`, so a cache spread
      // across two filesystems hits a cross-device rename (EXDEV) and fails
      // outright (verified live; UV_LINK_MODE=copy does not help — the failure
      // is the rename, not the link). Put the WHOLE uv cache on /tmp. uv then
      // re-downloads on restart, but uv installs take seconds and happen
      // lazily at first tool use, so there is no boot-time re-download storm
      // like npm's.
      "[ -e /home/node/.cache/uv ] && [ ! -L /home/node/.cache/uv ] && { mv /home/node/.cache/uv /home/node/.cache/uv.efs-archive-$(date +%s) || exit 1; }",
      "ln -sfn /tmp/openclaw-uv /home/node/.cache/uv || exit 1",
      // Step C2 — mcporter (the MCP-to-CLI bridge lobster workflows use to reach
      // hosted-OAuth MCP servers) reads its config + OAuth vault from ~/.mcporter.
      // The backend materializes that vault onto the EFS state dir, so symlink
      // ~/.mcporter there — same relocation pattern as the npm cache above — so the
      // materialized creds are what mcporter sees and they survive restarts. The
      // whole dir lives on EFS (config + vault are small, read-mostly, no extract
      // storm), so it points straight at .openclaw, not /tmp.
      "mkdir -p /home/node/.openclaw/mcporter || exit 1",
      "[ -e /home/node/.mcporter ] && [ ! -L /home/node/.mcporter ] && { mv /home/node/.mcporter /home/node/.mcporter.efs-archive-$(date +%s) || exit 1; }",
      "ln -sfn /home/node/.openclaw/mcporter /home/node/.mcporter || exit 1",
      // Step C2b — xurl (the X CLI) reads its token store from `$HOME/.xurl` and
      // honours ONLY $HOME: store.resolveHomeDir() reads $HOME then falls back to
      // os.UserHomeDir(), with no XDG and no XURL_HOME override. So unlike gh/gog
      // (XDG_CONFIG_HOME) and the aws CLI (AWS_CONFIG_FILE), xurl CANNOT be pointed
      // at the EFS state dir by env — a symlink is the only bridge. The backend
      // materializes the store to owner_efs_root/.xurl (xurl_cli materializer),
      // which surfaces here as /home/node/.openclaw/.xurl; without this link xurl
      // reads an absent /home/node/.xurl and every X call runs unauthenticated.
      // Unlike the relocations above, the target is a FILE, and a dangling link is
      // fine: xurl's saveToFile does os.WriteFile, which follows the link and
      // creates the EFS target on first write.
      // CEILING — this assumes xurl's single-FILE store, which is what the pinned
      // 1.2.3 uses. Upstream past 1.2.3 moved to a DIRECTORY (`~/.xurl/auth.yml`,
      // migrating a legacy file on first use), so a version bump must re-point
      // this link at the directory; leaving it as-is degrades to a real ephemeral
      // dir holding a symlink, which works but re-migrates on every task start.
      "[ -e /home/node/.xurl ] && [ ! -L /home/node/.xurl ] && { mv /home/node/.xurl /home/node/.xurl.efs-archive-$(date +%s) || exit 1; }",
      "ln -sfn /home/node/.openclaw/.xurl /home/node/.xurl || exit 1",
      // Step C4 — virtual display backing the browser live view.
      //
      // Gated entirely on DISPLAY so image and boot stay independently
      // deployable: an image carrying these binaries is inert until the task
      // definition sets DISPLAY, and either can roll back alone.
      //
      // Xvnc (TigerVNC) is the X server AND the VNC server in one resident
      // process — it replaced the Xvfb + socat-spawned-x11vnc chain because
      // panel-driven resize needs the VNC server to honor client
      // SetDesktopSize, which x11vnc cannot. Resident is safe where resident
      // x11vnc was not: x11vnc degrades over days because it POLLS a foreign
      // display for damage (docker-selenium #1027); Xvnc IS the display and
      // only encodes updates while a viewer is attached — no viewer, no work.
      //
      // matchbox is the kiosk window manager: without one, Chrome floats at
      // its default window size over an unpainted (black) X root — the
      // "watching a movie of a browser" effect. matchbox keeps every window
      // borderless-fullscreen and re-fits it when the display resizes, which
      // is what makes noVNC's resizeSession (panel size -> display size)
      // end-to-end useful. It must connect AFTER the X socket exists — X
      // clients do not retry — hence the bounded wait-for-socket subshell
      // (the socket name X1 is pinned to DISPLAY=:1, same as -geometry's
      // assumptions elsewhere in this block).
      //
      // -SecurityTypes None + -localhost: the backend relay is the auth fence
      // (Clerk-validated upgrade, owner-scoped) and 5900 never leaves
      // loopback; a VNC password here would have to be minted at boot and
      // independently derived by the backend for zero added exclusion.
      //
      // Eager display + lazy encoding is not an oversight, and the asymmetry
      // cannot be removed: OpenClaw decides headless-vs-headful ONCE, at
      // Chrome launch, from whether DISPLAY resolves —
      // resolveManagedBrowserHeadlessMode evaluates its linux-no-DISPLAY
      // branch BEFORE the configured browser.headless value. A display
      // started after Chrome is useless to the Chrome already running.
      //
      // Ports: 6080 (websockify, container IP — reachable only from the
      // backend SG + the :8081 ALB path rule) → 5900 (Xvnc, loopback only).
      //
      // Fail loudly rather than at first browser use: DISPLAY set on an image
      // without Xvnc launches a headful Chrome against no X server, and
      // surfaces as upstream's "No DISPLAY/X server was detected" at the
      // first browser tool call — arbitrarily long after boot and nowhere
      // near the cause.
      '[ -z "$DISPLAY" ] || [ -x /usr/bin/Xvnc ] || { echo "[boot] FATAL: DISPLAY is set but Xvnc is missing. Pin a tag built from a commit whose apps/infra/openclaw/Dockerfile installs tigervnc-standalone-server."; exit 1; }',
      '[ -z "$DISPLAY" ] || { Xvnc "$DISPLAY" -geometry 1280x800 -depth 24 -localhost -SecurityTypes None -rfbport 5900 & }',
      // On socket timeout: loud warning + skip the WM, never a boot failure.
      // The live view is auxiliary — a dead Xvnc costs the Browser panel and
      // headful Chrome, but chat/channels/everything else still works, and
      // killing the whole agent container over it would invert priorities.
      // (Skipping the exec also beats running matchbox against a dead
      // display, where it would just exit after an X error.)
      '[ -z "$DISPLAY" ] || { ( i=0; while [ ! -S /tmp/.X11-unix/X1 ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i+1)); done; if [ -S /tmp/.X11-unix/X1 ]; then exec matchbox-window-manager -use_titlebar no; else echo "[boot] WARNING: Xvnc socket never appeared; live view and headful browser will be broken this task"; fi ) & }',
      '[ -z "$DISPLAY" ] || { websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 & }',
      // Step C4b — Cloudflare WARP browser egress (Dockerfile layer 9c).
      //
      // Backgrounded, and that is the whole point of the `&`: the script
      // waits up to ~60s for the tunnel to report Connected, and boot must
      // not pay that. The wait exists only to turn the outcome into ONE
      // CloudWatch line ("[warp] connected after Ns" / "[warp] not Connected
      // after Ns"); nothing downstream reads it. Chrome launches lazily at
      // the first browser tool call, minutes later at the earliest, so the
      // tunnel is up long before anything looks for it — and if it never
      // comes up, the PAC the backend emits ends in "; DIRECT", so Chrome
      // falls back to the NAT IP (measured: 0.6s to first byte) rather than
      // failing to load.
      //
      // The `[ ! -x ]` guard is deliberately the opposite shape from the
      // local-sqlite boot guard below, which is FATAL. That one is fatal
      // because a container without it silently loses owner data; this one
      // costs a CAPTCHA-avoidance nicety, so an older pinned tag without the
      // script must keep booting normally.
      "[ ! -x /usr/local/bin/isol8-warp-boot.sh ] || { /usr/local/bin/isol8-warp-boot.sh & }",
      // The CDP-screencast bridge (socat 18801 -> loopback DevTools 18800) was
      // removed with the screencast live-view; the Browser panel is VNC-only.
      // Step C3/D — legacy-state migration (doctor --fix) then gateway
      // launch. BOTH are owned by the baked boot script: it restores hot
      // SQLite state from S3 (or migrates it off EFS on the first cutover
      // boot), then runs doctor + gateway as a litestream-supervised child so
      // every committed write replicates and a SIGTERM final-syncs to S3
      // before exit. The doctor invocation's load-bearing flags (`--fix
      // --yes --non-interactive < /dev/null`, bounded by `timeout
      // --kill-after` inside the startPeriod-300 health-check budget, made
      // non-fatal by `|| echo`) live in
      // apps/infra/openclaw/local-sqlite/isol8-local-sqlite-app.sh — 2026.7.x
      // fail-closes agent runs on un-migrated legacy per-workspace setup
      // state, and doctor's confirm prompts HANG on a closed stdin.
      //
      // SAFETY — why doctor is allowed on this boot path at all (2026-07-21
      // incident follow-up): upstream doctor's removed-workspaces-state
      // repair recursively DELETES `<stateDir>/workspaces` as retired
      // Workspaces-PLUGIN state. Agent workspaces now live at upstream's
      // own per-agent convention (~/.openclaw/workspace-<agentId>/ —
      // agent-scope-config.ts resolveAgentWorkspaceDir), so that target
      // either doesn't exist (fresh provisions) or holds only post-incident
      // junk that we WANT cleaned. Nothing legitimate may ever be placed at
      // ~/.openclaw/workspaces again (regression-tested in
      // container-stack.test.ts). The only containers where doctor remains
      // destructive are pre-migration old-layout ones — those stay frozen
      // until reprovisioned and must never be rolled onto this task def.
      //
      // The boot guard is the ONLY thing between a tag/config mismatch and a
      // silently-wrong container: synth cannot inspect image contents and
      // accepts any tag, so a tag built before the local-sqlite chain existed
      // would otherwise die on a bare "not found". Diagnose it instead.
      "[ -x /usr/local/bin/isol8-local-sqlite-boot.sh ] || { echo '[boot] FATAL: this image has no local-sqlite boot chain. Pin a tag built from a commit containing apps/infra/openclaw/local-sqlite/.'; exit 1; }",
      // Step D — hand the gateway over to uid 1000 (`node`, the image's own
      // USER) instead of running it as root.
      //
      // Everything else in this stack already agrees on 1000: the extended
      // image ends `USER node`, every per-user EFS access point squashes
      // ownership to 1000:1000 (PosixUser + CreationInfo in ecs_manager.py),
      // the boot script chowns restored agent dirs to 1000, and the backend
      // chowns config to 1000 after every write. The running process was the
      // one component that disagreed, and only by accident: `user: "0:0"`
      // arrived in 2026-03 alongside a startup command that ran `apt-get
      // install` and `pip install`. Those installs moved into the extended
      // image Dockerfile long ago; the override outlived its reason.
      //
      // OpenClaw 2026.8.1 made that disagreement fatal. Its new
      // ensurePrivateSqliteCoordinatorDirectory (src/infra/sqlite-coordinator.ts)
      // creates a directory and then rejects it unless `stat().uid ===
      // getuid()`. Under uid-squash the directory root itself just created
      // stats back as 1000 while getuid() is 0, so the check can never pass —
      // fresh state or old, no EFS cleanup fixes it, and there is no env or
      // config escape hatch. Observed as a 4-task crash-loop on 2026-08-25:
      // "device identity coordinator directory belongs to another user".
      //
      // Why not simply set `user: "1000:1000"` on the container: the two
      // ephemeral volumes below are ECS-managed mount points the image never
      // creates, so Docker materialises them **root:root** (verified against a
      // live prod task: .openclaw is 1000:1000 while .openclaw/state and
      // .openclaw/agents are 0:0). A task starting as 1000 could not write its
      // own SQLite. Root must therefore create and chown them first — which is
      // exactly what a privilege drop is for, and why the drop happens HERE,
      // after Xvnc/warp/websockify have taken the ports they need as root,
      // rather than at the container level.
      //
      // Exactly ONE class of path needs repairing here, because everything this
      // command creates itself is now created as 1000 outright (Steps B/C use
      // `install -d -o 1000`) and everything on EFS is squashed to 1000 by the
      // access point: the two ECS-managed ephemeral volumes. We do not create
      // those — the container runtime does, root:root, because the image has no
      // mountpoint at those paths for it to copy ownership from. They are empty
      // at task start, so this is two inodes, not a walk.
      //
      // There is deliberately NO second class here any more — no sweep of
      // /home/node's dotfiles. That sweep assumed the image baked them
      // root-owned; it does not. Verified entry by entry against the ECR
      // layers of 2026.8.1-isol8.1-1c67107: the base creates .config and
      // .openclaw with `install -d -o node`, the extended Dockerfile chowns
      // the one thing a COPY lands root-owned (.sage), and every build step
      // runs as root with HOME=/root — which is why npm's cache is /root/.npm
      // and not ~node/.npm. Ownership is now the IMAGE's job and is kept there
      // by a maxdepth-1 `chown node:node` at the end of
      // apps/infra/openclaw/Dockerfile, so a base bump cannot quietly
      // reintroduce one.
      //
      // The root-owned entries a live container shows at maxdepth 1
      // (.npm, .mcporter, .xurl) are the symlinks Step C plants moments
      // earlier, as root. Their ownership is inert: symlink resolution ignores
      // it (the target's mode is what counts, and every target is either EFS
      // squashed to 1000 or a /tmp dir created with `install -d -o 1000`), and
      // replacing one needs write on /home/node — owned by node, not sticky —
      // not on the link.
      //
      // Note this runs BEFORE the boot script, so litestream's restore writes
      // into state/ and agents/ as 1000 and needs no chown of its own.
      "chown 1000:1000 /home/node/.openclaw/state /home/node/.openclaw/agents || exit 1",
      'echo "[boot] init complete as root (warp/Xvnc/websockify started); exec gateway chain as uid=1000 via setpriv"',
      "exec setpriv --reuid=1000 --regid=1000 --init-groups /usr/local/bin/isol8-local-sqlite-boot.sh",
    ].join("; ");

    // Image selection: use the extended ECR image once the per-env tag has been
    // promoted past the "bootstrap" placeholder. Until then, fall back to the
    // legacy upstream image so the env keeps deploying. This lets us flip dev
    // and prod independently via openclaw-version.json bumps without coupling.
    // Unknown envs (e.g. "local") default to legacy via the optional chain.
    const containerImage =
      envTag === "bootstrap"
        ? ecs.ContainerImage.fromRegistry(OPENCLAW_VERSION.full)
        : ecs.ContainerImage.fromEcrRepository(this.openclawExtendedRepo, envTag);

    const openclawContainer = this.openclawTaskDef.addContainer("openclaw", {
      image: containerImage,
      essential: true,
      command: ["sh", "-c", startupCommand],
      // Root is the BOOT user, not the run user. The startup command needs it
      // to chown the ECS-managed ephemeral volumes (which Docker materialises
      // root:root) and to start Xvnc/warp/websockify, then hands the gateway
      // to uid 1000 via setpriv — see Step D at the end of startupCommand.
      // Do not "simplify" this to user: "1000:1000": the volume chown has to
      // happen before the drop or the container cannot write its own SQLite.
      user: "0:0",
      workingDirectory: "/home/node",
      environment: {
        HOME: "/home/node",
        // Turns on the Step C4 display block, and with it the headful browser
        // that backs the live view. This is the switch that makes every
        // container launch a headful Chrome on the Xvfb display instead of a
        // headless one.
        //
        // NOT env-gated, and that is only safe because DISPLAY and the image
        // tag live in the SAME task definition: one CDK deploy registers both,
        // so no revision can exist where DISPLAY is set on an image without
        // Xvfb — the combination the Step C4 guard kills the boot over. If you
        // ever set this for an environment, promote that environment's tag in
        // openclaw-version.json in the same commit.
        DISPLAY: ":1",
        CHOKIDAR_USEPOLLING: "true",
        // Redirect clawhub installs to the OpenClaw managed-skills directory
        // (~/.openclaw/skills) so they're scanned by every agent. Without
        // this, clawhub falls through to agents.defaults.workspace and lands
        // at /home/node/.openclaw/workspaces/skills — a path no scanner checks.
        CLAWHUB_WORKDIR: "/home/node/.openclaw",
        // Point the gog CLI at the EFS-backed .openclaw mount so the owner's
        // Google OAuth keyring survives task restarts. gog follows the XDG
        // base-dir spec and splits config from data: it reads its config from
        // XDG_CONFIG_HOME but stores the file keyring under XDG_DATA_HOME. BOTH
        // must be on EFS — XDG_DATA_HOME especially, because the keyring (the
        // actual OAuth tokens) lives there; its default (~/.local/share) is the
        // Fargate ephemeral overlay filesystem, which is wiped on every task
        // replacement. Setting XDG_CONFIG_HOME alone persists only gog's config
        // and leaves the tokens on ephemeral storage.
        // GOG_KEYRING_BACKEND=file selects the password-encrypted file keyring
        // (the password comes in via the GOG_KEYRING_PASSWORD secret below).
        XDG_CONFIG_HOME: "/home/node/.openclaw/.config",
        XDG_DATA_HOME: "/home/node/.openclaw/.local/share",
        GOG_KEYRING_BACKEND: "file",
        // Point the AWS CLI v2 (baked into the extended image) at the EFS-backed
        // config the connector-integration materializer writes. /home/node/.openclaw
        // is the per-user EFS mount, so this resolves to the same file written at
        // owner_efs_root(owner)/.aws/config (a named [profile isol8-connector] with
        // credential_process — named, not [default], so it never enters the AWS SDK
        // default chain the in-process Bedrock provider uses; the aws-exec-profile
        // plugin selects it for the agent's shell via AWS_PROFILE in the exec env
        // only).
        AWS_CONFIG_FILE: "/home/node/.openclaw/.aws/config",
        // Skip mDNS/bonjour service advertising. OpenClaw advertises itself
        // on the LAN via bonjour for desktop auto-discovery; in Fargate
        // there's no LAN multicast peer, so the announce gets stuck and
        // blocks channel/sidecar startup for ~13 minutes before the
        // bonjour watchdog finally gives up. Verified via runtime logs +
        // upstream `src/gateway/server-discovery-runtime.ts`.
        OPENCLAW_DISABLE_BONJOUR: "true",
        // Emit per-step startup trace lines so we can attribute the multi-minute
        // post-gateway-ready wedge (channels-and-sidecars phase) to a specific
        // step. Upstream wraps each phase in measureStartup; with this flag the
        // gateway logs `startup trace: <name> <ms>ms total=<ms>ms` per step
        // (src/gateway/server.impl.ts:136). Cheap to leave on permanently.
        OPENCLAW_GATEWAY_STARTUP_TRACE: "1",
        // local-sqlite layout: the state root moves to container-local
        // disk (WAL returns — the journal-mode probe classifies the lexical
        // path against mountinfo, so it must live OUTSIDE the EFS
        // mountpoint's subtree), while openclaw.json stays pinned to the EFS
        // durable spine so the backend's config channel (write_user_configs,
        // config_patcher, chokidar) is unchanged. The boot script
        // reverse-symlinks the durable stateDir subdirs (credentials/,
        // identity/, devices/, extensions/, skills/, npm/, git/, tools/)
        // back onto EFS. LITESTREAM_S3_BUCKET + per-user ISOL8_OWNER_ID (from
        // the backend) parameterize replication: s3://bucket/{owner}/<db>.
        // local-sqlite layout: the state dir does NOT move. Two config-less
        // ECS volumes are mounted NESTED inside the EFS mount at
        // .openclaw/state and .openclaw/agents (real ext4 on the task NVMe —
        // measured 1.38ms/commit vs ~95ms on the EFS mount), so WAL comes back
        // while every other path, and every absolute path already recorded
        // inside the databases, stays literally unchanged. Only the bucket
        // needs to reach the container.
        LITESTREAM_S3_BUCKET: litestreamBucketName,
        // Loopback port the WARP local proxy listens on (Step C4b). The SAME
        // constant reaches the backend as BROWSER_EGRESS_PROXY_PORT, which is
        // what decides whether Chrome gets a PAC pointing here at all — so the
        // container side is safe to set unconditionally: an unenrolled warp
        // just never listens, and a backend with the knob off never points
        // Chrome at it.
        BROWSER_EGRESS_PROXY_PORT: String(BROWSER_EGRESS_PROXY_PORT),
      },
      secrets: {
        // Password for the gog CLI's file keyring (see XDG_CONFIG_HOME +
        // GOG_KEYRING_BACKEND above). Imported by name — NOT as a cross-stack
        // ISecret — so CDK doesn't auto-grant KMS decrypt on AuthStack's key,
        // which would create a circular dependency between the stacks. Because
        // this secret is CMK-encrypted, the execution role's explicit
        // kms:Decrypt grant (KmsDecryptForSecrets above) is what lets ECS
        // inject it — GetSecretValue alone is not enough for a CMK secret.
        GOG_KEYRING_PASSWORD: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportGogKeyringPassword",
            props.gogKeyringPasswordSecretName,
          ),
        ),
        // Cloudflare Zero Trust service token for the WARP browser egress.
        // Same import-by-name / no-KMS-auto-grant reasoning as the gog secret
        // above. Until a human sets the real JSON, this carries CDK's random
        // placeholder and isol8-warp-boot.sh skips — so it is safe to inject
        // in every environment from the first deploy.
        CLOUDFLARE_WARP_ENROLLMENT: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportCloudflareWarpEnrollment",
            props.cloudflareWarpEnrollmentSecretName,
          ),
        ),
      },
      portMappings: [{ containerPort: 18789, protocol: ecs.Protocol.TCP }],
      logging: ecs.LogDrivers.awsLogs({
        logGroup: openclawLogGroup,
        streamPrefix: "openclaw",
      }),
      // Liveness probe against the upstream gateway's /healthz endpoint
      // (exposed by openclaw.mjs gateway — see upstream Dockerfile and
      // src/gateway/server-http.ts:152-157, where /healthz is auth-
      // bypassed and runs on the Node event loop). When Node's event
      // loop wedges on EFS NFS RPC, the /healthz async handler can't
      // run; ECS marks the task UNHEALTHY after retries * interval and
      // replaces it. TCP-only probes (e.g. nc -z) are insufficient here
      // because TCP accept stays in the kernel while the JS event loop
      // is frozen.
      healthCheck: {
        command: [
          "CMD-SHELL",
          "node -e \"fetch('http://127.0.0.1:18789/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"",
        ],
        interval: cdk.Duration.seconds(30),
        // 10s (was 5s). A cold chat connects every configured MCP server
        // SERIALLY (npx spawn + handshake, several seconds each); on the
        // 0.5-vCPU box those spawns contend for the half-core and starve the
        // gateway's event loop, so /healthz — which runs on that loop — can
        // momentarily take seconds to answer even though the gateway is alive.
        // A 5s probe timeout turns that transient busyness into a kill; 10s
        // rides it out. A genuinely dead gateway still fails every probe.
        timeout: cdk.Duration.seconds(10),
        // 6 retries x 30s = ~180s of tolerated unhealthiness. The earlier
        // premise ("installs run in a child process, not the event loop, so
        // /healthz stays responsive") was wrong: the serial connect storm
        // CPU-starves the gateway process itself, so probes do slow during it.
        // This margin lets a cold connect storm finish (baking the paperclip
        // MCP + warm /tmp caches keep it well under this) without ECS killing
        // the task mid-chat — which previously wiped /tmp and re-triggered a
        // cold storm, a crash-loop. A truly dead gateway is still replaced in
        // ~3 min.
        retries: 6,
        // Start period covers EFS first-touch latency, the boot-time
        // `openclaw doctor --fix` legacy-state migration (startupCommand Step C3,
        // bounded at 300s), gateway boot, and the first cold MCP connect on a
        // brand-new container. Set to 300s — the ECS MAXIMUM for startPeriod
        // (values >300 are rejected: "Health check startPeriod must be less than
        // or equal to the maximum allowed value 300") — up from 180s to give the
        // added doctor pass as much grace as ECS allows. Combined with the
        // retries*interval window (6*30s=180s) after this, a task tolerates ~480s
        // of gateway-not-ready before replacement. The doctor step is bounded at
        // 300s to stay inside that, so a normal first boot never trips a spurious
        // replacement; a migration heavier than the window self-heals (the
        // migration persists, ECS replaces the task once, the replacement comes up
        // fast). A healthy gateway reports ready as soon as it binds, so
        // steady-state boots are unaffected.
        startPeriod: cdk.Duration.seconds(300),
      },
      // Inject Fargate's init process (tini-equivalent) as PID 1. The
      // openclaw container runs `sh -c "... ; exec node ..."` so Node
      // would otherwise be PID 1, and Node doesn't reap orphan children
      // by default. When chromium subprocesses' immediate parent dies
      // and they get re-parented to PID 1 (e.g. crashpad zombies with
      // ppid=1), init reaps them unconditionally regardless of whether
      // Node's event loop is responsive. Does NOT prevent the NFS wedge
      // itself; pairs with the chromium-tmpfs symlink in the startup
      // command for that.
      linuxParameters: new ecs.LinuxParameters(this, "OpenclawLinuxParams", {
        initProcessEnabled: true,
      }),
    });

    // Mount EFS workspace — OpenClaw reads openclaw.json and agent files from here
    openclawContainer.addMountPoints({
      containerPath: "/home/node/.openclaw",
      sourceVolume: "openclaw-workspace",
      readOnly: false,
    });

    // local-sqlite layout. Mount ORDER is load-bearing: ECS mounts in the
    // literal mountPoints[] order rather than by depth, so the ephemeral
    // volumes must be registered AFTER the primary EFS mount above. If that
    // order ever inverts, the EFS mount covers the ephemeral ones and every
    // commit silently goes back to NFS; the boot script asserts the filesystem
    // type and refuses to start rather than degrade.
    //
    // There is no second EFS view any more. /efs-raw existed solely so the
    // one-time EFS->local migration could reach — and rename aside — the
    // pre-cutover databases that these nested mounts shadow. That migration ran
    // once, on 2026-08-04, for all five prod owners; the archives it left sit on
    // the EFS spine at .openclaw/pre-migration-archive/ and are reachable
    // through the primary mount.
    //
    // The hot SQLite: real ext4 on the task's NVMe device. Draws from the
    // same 20 GiB pool Fargate already includes, so it adds no cost.
    for (const [name, path] of [
      ["openclaw-state", "/home/node/.openclaw/state"],
      ["openclaw-agents", "/home/node/.openclaw/agents"],
    ] as const) {
      this.openclawTaskDef.addVolume({ name });
      openclawContainer.addMountPoints({
        containerPath: path,
        sourceVolume: name,
        readOnly: false,
      });
    }

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
    // us already — the first task of the day was replaced and we couldn't
    // reconstruct why because the stopped-task record had aged out.
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
        logGroupName: logGroupPath(env, "ecs-task-events"),
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
