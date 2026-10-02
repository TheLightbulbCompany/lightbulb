import * as cdk from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";
import { logGroupPath } from "../table-names";

/**
 * What the name no longer means: nothing per-user runs on ECS. Every workspace
 * is a Bedrock AgentCore runtime running the Hermes bridge image, so this stack
 * keeps only
 *
 *  - the PLATFORM ECS cluster (the backend Fargate service runs on it; the
 *    provisioner is a Lambda now),
 *  - the Cloud Map private DNS namespace PaperclipStack registers
 *    `paperclip.<namespace>` into,
 *  - the per-owner Bedrock base policy + permissions boundary, which double as
 *    every workspace's AgentCore Runtime EXECUTION role,
 *  - the per-owner state bucket (`lightbulb-{env}-openclaw-replicas`) every
 *    Hermes profile lives under, at `<owner>/hermes/` — the name is a
 *    litestream-era scar and renaming it strands every profile,
 *  - the CI image-builder OIDC role,
 *  - the EventBridge trail that persists ECS task-state changes for that
 *    cluster (the backend's own stop reasons outlive ECS's 1h window there),
 *  - the workspace-shared AgentCore Memory (one per env; tenants are isolated
 *    by the per-owner `owner-memory` inline policy, not by resource),
 *  - `ContainerSecurityGroup`, which despite its name is LIVE: it is the VPC
 *    security group every workspace's AgentCore runtime ENI carries, threaded
 *    to the backend as ECS_SECURITY_GROUP_ID and put into
 *    `CreateAgentRuntime`'s networkConfiguration by hermes_provisioning.
 */
export interface ContainerStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
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
  public readonly containerSecurityGroup: ec2.SecurityGroup;
  public readonly ownerBedrockBasePolicy: iam.ManagedPolicy;
  public readonly ownerBedrockBoundary: iam.ManagedPolicy;
  public readonly workspaceMemory: bedrockagentcore.CfnMemory;

  constructor(scope: Construct, id: string, props: ContainerStackProps) {
    super(scope, id, props);

    const config = ENV_CONFIG[props.environment] ?? ENV_CONFIG.dev;

    const litestreamBucketName = `lightbulb-${props.environment}-openclaw-replicas`;

    // THE PLATFORM ECS cluster — the backend Fargate service (service-stack)
    // runs on it. Nothing per-user does
    // any more. Container Insights stays off: it is ~15 metrics per service, and
    // AWS/ECS standard metrics (CPUUtilization, MemoryUtilization) plus custom
    // EMF cover the backend's alarms.
    this.cluster = new ecs.Cluster(this, "Cluster", {
      // Explicit so the physical name stops embedding the old stack name; changing
      // it replaces the cluster, which takes the backend down with it.
      clusterName: `lightbulb-${props.environment}-container`,
      vpc: props.vpc,
      containerInsights: false,
      // FARGATE + FARGATE_SPOT capacity providers. Spot was the per-user
      // container's cost lever and nothing requests it now; the association is
      // inert, and removing it is a live-cluster property change for no gain.
      enableFargateCapacityProviders: true,
    });

    // Cloud Map private DNS namespace.
    //
    // The construct id stays "NamespaceV2" even though the TypeScript member
    // is the plain name. Renaming a construct id changes
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

    this.containerSecurityGroup = new ec2.SecurityGroup(
      this,
      "ContainerSecurityGroup",
      {
        vpc: props.vpc,
        description: `Isol8 ${props.environment} Fargate container security group`,
        allowAllOutbound: true,
      },
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
    // These shared policies attach to every owner role. IAM substitutes the
    // role's owner_id tag, so one runtime cannot read another owner's boot
    // bearer tokens. The boundary mirrors the same ceiling.
    const ownerBootSecretArn =
      `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${props.environment}/hermes/\${aws:PrincipalTag/owner_id}/boot-*`;

    // The shared grants the backend attaches to every per-owner role. Keeps
    // them in IaC (auditable); per-owner-varying grants are inline, in backend code.
    this.ownerBedrockBasePolicy = new iam.ManagedPolicy(this, "OwnerBedrockBasePolicy", {
      managedPolicyName: `lightbulb-${props.environment}-owner-bedrock-base`,
      statements: [
        // No logs grant here: the runtime log group's name carries a hash of the owner id, which
        // no policy variable can compute, so it is the per-owner inline `owner-logs`
        // (bedrock_aip.build_owner_logs_policy). The agent's shell can read this role's
        // credentials over the microVM metadata service, so every grant here is the shell's too.
        // (The ECS-exec `ssmmessages` grant is gone with ECS.)
        // Async turn contract: the container publishes its run frames straight to
        // AppSync Events. Namespace-wide grant; per-owner tenancy is enforced by the
        // ws namespace handler (api-stack), which only IAM callers reach.
        new iam.PolicyStatement({
          sid: "AppSyncWsPublish",
          actions: ["appsync:EventPublish"],
          resources: [`arn:aws:appsync:${this.region}:${this.account}:apis/*/channelNamespace/ws`],
        }),
        // AgentCore Runtime: the per-owner role doubles as the runtime's EXECUTION role, so it
        // has to pull the gateway-only arm64 image itself. Repo-scoped; GetAuthorizationToken
        // is account-wide by API design.
        new iam.PolicyStatement({
          sid: "AgentCoreEcrPull",
          actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
          resources: [
            // The Hermes bridge image. The boundary must grant it too — a boundary that omits
            // it caps the role out of pulling its own image, and the runtime fails to start
            // with no useful error.
            `arn:aws:ecr:${this.region}:${this.account}:repository/lightbulb/hermes-agent-arm64`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "AgentCoreEcrToken",
          actions: ["ecr:GetAuthorizationToken"],
          resources: ["*"],
        }),
        // Runtime-managed workload identity. Required by the platform at session start; the
        // container never calls it directly.
        new iam.PolicyStatement({
          sid: "AgentCoreWorkloadToken",
          actions: [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
          ],
        }),
        // The Hermes runtime env carries secret NAMES only; the bridge resolves them at
        // boot. Scoped to the owner's own hermes/ prefix, not the whole env — narrower
        // than the general per-owner secrets a future lane may add.
        new iam.PolicyStatement({
          sid: "HermesSecretsRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [ownerBootSecretArn],
        }),
        new iam.PolicyStatement({
          sid: "AgentCoreTelemetry",
          actions: [
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
            "xray:GetSamplingRules",
            "xray:GetSamplingTargets",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "AgentCoreMetrics",
          actions: ["cloudwatch:PutMetricData"],
          resources: ["*"],
          conditions: { StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" } },
        }),
      ],
    });

    // Workspace memory: ONE resource per env, shared by every tenant. Isolation is IAM, proven in
    // the 2026-09-22 dev spike: the per-owner `owner-memory` inline policy
    // (bedrock_aip.ensure_owner_memory_policy) scopes events by `actorId` = owner and records by
    // the exact `namespace` /owners/<owner>/ that this strategy extracts into. Semantic only.
    // Encryption: AWS-owned key, DELIBERATELY (Track G, 2026-09-22). A CMK needs a key-policy grant
    // to the service plus kms grants on every owner role. EncryptionKeyArn (like Name) is
    // create-only: changing it later REPLACES this resource, and with a fixed Name the prod
    // replacement fails on the name clash -- a CMK later means a new name and a data migration.
    this.workspaceMemory = new bedrockagentcore.CfnMemory(this, "WorkspaceMemory", {
      name: `lightbulb_${props.environment}_workspace_memory`,
      description: "Long-term memory shared by the agents of each workspace (namespace per owner).",
      // The shortest AWS allows: events are only extraction input (records are the memory), and the
      // wipe deletes them in a best-effort background tail, so expiry bounds any residue.
      eventExpiryDuration: 7,
      memoryStrategies: [
        {
          semanticMemoryStrategy: {
            name: "workspace_facts",
            namespaces: ["/owners/{actorId}/"],
          },
        },
      ],
    });
    this.workspaceMemory.applyRemovalPolicy(config.removalPolicy);

    // Permissions boundary capping every backend-minted per-owner role to the
    // UNION of what it may ever hold: scoped Bedrock invoke (on inference
    // profiles + the 9 FM ARNs) + runtime logs + AgentCore. A backend that can
    // CreateRole+PutRolePolicy cannot escalate a per-owner role past this.
    this.ownerBedrockBoundary = new iam.ManagedPolicy(this, "OwnerBedrockBoundary", {
      managedPolicyName: `lightbulb-${props.environment}-owner-bedrock-boundary`,
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
        // Ceiling for the per-owner inline `owner-logs`, which names the owner's OWN runtime
        // group (`<runtime name>-*`). Covers the group and its streams.
        new iam.PolicyStatement({
          sid: "BoundaryLogs",
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
          ],
        }),
        // Async turn contract: the container publishes its run frames straight to
        // AppSync Events. Namespace-wide grant; per-owner tenancy is enforced by the
        // ws namespace handler (api-stack), which only IAM callers reach.
        new iam.PolicyStatement({
          sid: "BoundaryAppSyncWsPublish",
          actions: ["appsync:EventPublish"],
          resources: [`arn:aws:appsync:${this.region}:${this.account}:apis/*/channelNamespace/ws`],
        }),
        // Boundary twins of the base policy's AgentCore statements (#2688 added the base grants
        // without these, which made them no-ops -- a grant the boundary does not also allow is
        // inert; effective permissions are inline/base ∩ boundary). The ECR pull twin is
        // BoundaryHermesEcrPull below — the Hermes bridge image is the only image a runtime pulls.
        new iam.PolicyStatement({
          sid: "BoundaryAgentCoreWorkloadToken",
          actions: [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
          ],
        }),
        // Ceiling only. The GRANT is the per-owner inline policy `owner-browser`
        // (bedrock_aip.ensure_owner_browser_policy), which names that workspace's own profile ARN --
        // a boundary applies to inline policies too, so without this twin the inline grant is inert
        // (#2705). Deliberately NOT mirrored into ownerBedrockBasePolicy, and deliberately WITHOUT
        // ListBrowserSessions: that omission is what stops a rogue runtime learning another tenant's
        // session id (design §1, §2c).
        new iam.PolicyStatement({
          sid: "BoundaryAgentCoreBrowserUse",
          actions: [
            "bedrock-agentcore:StartBrowserSession",
            "bedrock-agentcore:GetBrowserSession",
            "bedrock-agentcore:StopBrowserSession",
            "bedrock-agentcore:ConnectBrowserAutomationStream",
            "bedrock-agentcore:InvokeBrowser",
            "bedrock-agentcore:SaveBrowserSessionProfile",
          ],
          // BOTH resources carry StartBrowserSession: a start with profileConfiguration authorizes against
          // the PROFILE arn too (dev spike 2026-09-19 -- the 403 named browser-profile/<id>, not the browser).
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:aws:browser/aws.browser.v1`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser-profile/*`,
          ],
        }),
        // Ceiling only; the grant is the per-owner inline `owner-memory` policy
        // (bedrock_aip.ensure_owner_memory_policy), which adds the actorId/namespace conditions.
        // Record get/delete and every Batch* write are absent on purpose: they carry no condition
        // keys, so allowing them would let one owner read or delete another's records by id.
        new iam.PolicyStatement({
          sid: "BoundaryAgentCoreMemoryUse",
          actions: [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:GetEvent",
            "bedrock-agentcore:ListSessions",
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:ListMemoryRecords",
          ],
          resources: [this.workspaceMemory.attrMemoryArn],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryAgentCoreTelemetry",
          actions: [
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
            "xray:GetSamplingRules",
            "xray:GetSamplingTargets",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryAgentCoreMetrics",
          actions: ["cloudwatch:PutMetricData"],
          resources: ["*"],
          conditions: { StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" } },
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
        // AgentCore Runtime for Hermes: the boundary previously had NO ecr:* at all, which
        // silently caps the base policy's (pre-existing, still-live) openclaw-gateway-arm64
        // pull down to nothing — that dead path is not fixed here. This grant is scoped to
        // ONLY the Hermes repo, so the boundary ∩ base intersection actually admits the
        // Hermes pull the base policy grants above.
        new iam.PolicyStatement({
          sid: "BoundaryHermesEcrPull",
          actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
          resources: [`arn:aws:ecr:${this.region}:${this.account}:repository/lightbulb/hermes-agent-arm64`],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryHermesEcrToken",
          actions: ["ecr:GetAuthorizationToken"],
          resources: ["*"],
        }),
        // Mirrors HermesSecretsRead on the base policy — the boundary must admit it too, or
        // the intersection silently nullifies the base grant.
        new iam.PolicyStatement({
          sid: "BoundaryHermesSecretsRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [ownerBootSecretArn],
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
    // (deleting LTX files under it would break restores). The one rule reclaims the parts of a
    // multipart upload a stopped session never completed (a SIGTERM save of a large state.db is
    // cut off at os._exit), which are billed storage and never otherwise removed.
    new s3.Bucket(this, "OpenclawReplicasBucket", {
      bucketName: litestreamBucketName,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: config.removalPolicy,
      autoDeleteObjects: props.environment !== "prod",
      metrics: [{ id: "EntireBucket" }],
      lifecycleRules: [{ abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) }],
    });

    // The account-scoped image repositories are IMPORTED, never created by CDK — the
    // repos and their image history outlive any stack teardown. Only the builder role's
    // grants live here.
    if (props.environment === "dev") {
      // OIDC role for the image-build workflows. Scoped to this repo so the
      // existing isol8-dev-github-actions role (which only has sts:AssumeRole
      // on cdk-* roles) doesn't need broader ECR perms. Created in dev only
      // (account-scoped role, single source of truth). The `openclaw` in its
      // name is a scar — renaming replaces the role, and build-hermes-image.yml
      // and build-paperclip-image.yml hardcode this ARN.
      const oidcProviderArn = `arn:aws:iam::${this.account}:oidc-provider/token.actions.githubusercontent.com`;
      const oidcProvider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
        this,
        "GithubOidcProvider",
        oidcProviderArn,
      );
      const builderRole = new iam.Role(this, "OpenclawImageBuilderRole", {
        roleName: "lightbulb-openclaw-image-builder",
        assumedBy: new iam.OpenIdConnectPrincipal(oidcProvider, {
          StringEquals: {
            // After the 2026-08-30 org/repo rename GitHub mints id-qualified
            // subs (owner@id/repo@id); the ids are immutable, so this form is
            // rename-proof and the plain-name form never matches. Main is the
            // publisher boundary: a feature-branch workflow must not bypass
            // branch review and gain write access to either image repository.
            "token.actions.githubusercontent.com:sub":
              "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:workflow": [
              "build-paperclip-image",
              "build-hermes-image",
            ],
          },
        }),
        description: "Used by .github/workflows/build-{paperclip,hermes}-image.yml to push to ECR",
        maxSessionDuration: cdk.Duration.minutes(60),
      });
      builderRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["ecr:GetAuthorizationToken"],
          resources: ["*"],
        }),
      );

      // The build-paperclip-image workflow uses this same OIDC role to push the
      // extended Paperclip image (apps/infra/paperclip-extended/). Grant it
      // pull+push on that account-scoped repo too.
      const paperclipExtendedRepo = ecr.Repository.fromRepositoryName(
        this,
        "PaperclipExtendedRepoForBuilder",
        "lightbulb/paperclip-extended",
      );
      paperclipExtendedRepo.grantPullPush(builderRole);

      // The build-hermes-image workflow (Task 20) uses this same OIDC role to push the
      // Hermes bridge image. The repo itself already exists (the spike pushed to it
      // manually) — referenced by name here only for this grant, same as the two above.
      const hermesAgentRepoForBuilder = ecr.Repository.fromRepositoryName(
        this,
        "HermesAgentRepoForBuilder",
        "lightbulb/hermes-agent-arm64",
      );
      hermesAgentRepoForBuilder.grantPullPush(builderRole);
    }

    // ECS task-state-change trail for the PLATFORM cluster. ECS keeps a stopped
    // task's record for ~1 hour, then drops `stoppedReason`/`stopCode` entirely —
    // and this repo has already lost RCA time to exactly that window (the backend
    // freeze and the crash-loop investigations both needed a stop reason that had
    // aged out). EventBridge forwards every transition on this cluster to a log
    // group so the full event JSON stays queryable in Logs Insights for months.
    //
    // KEPT through the runtime move on purpose: the rule is scoped to the CLUSTER,
    // not to the retired per-user services, and the cluster still carries the
    // backend service and the Paperclip service. ~1 KB an event, well under $1/mo.
    const ecsTaskEventsLogGroup = new logs.LogGroup(
      this,
      "EcsTaskEventsLogGroup",
      {
        logGroupName: logGroupPath(props.environment, "ecs-task-events"),
        // Three months — long enough to investigate weekly recurrences
        // without paying open-ended retention costs.
        retention: logs.RetentionDays.THREE_MONTHS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      },
    );

    new events.Rule(this, "EcsTaskStateChangeRule", {
      ruleName: `lightbulb-${props.environment}-ecs-task-state-change`,
      description:
        `Persist ECS Task State Change events for ${this.cluster.clusterName} to a CloudWatch log group, so we can reconstruct task stop reasons after the 1h ECS retention window expires.`,
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          // Only events from THIS cluster — avoids polluting the log group with
          // other clusters' noise.
          clusterArn: [this.cluster.clusterArn],
        },
      },
      // Raw target instead of targets.CloudWatchLogGroup: the L2 target
      // ships an AwsCustomResource singleton Lambda (auto-named after the
      // retired isol8 stack name) just to write the log-group resource
      // policy. The native AWS::Logs::ResourcePolicy below does the same
      // thing with no helper function.
      targets: [{ bind: () => ({ id: "", arn: ecsTaskEventsLogGroup.logGroupArn }) }],
    });

    new logs.ResourcePolicy(this, "EcsTaskEventsLogPolicy", {
      resourcePolicyName: `lightbulb-${props.environment}-ecs-task-events`,
      policyStatements: [
        new iam.PolicyStatement({
          actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
          principals: [
            new iam.ServicePrincipal("events.amazonaws.com"),
            new iam.ServicePrincipal("delivery.logs.amazonaws.com"),
          ],
          resources: [ecsTaskEventsLogGroup.logGroupArn],
        }),
      ],
    });
  }
}
