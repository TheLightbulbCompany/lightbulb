import * as path from "path";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";

// openclaw-version.json is the single source of truth for the pinned OpenClaw
// version + per-env extended-image tags. The backend image's Docker build
// context is apps/backend/, so the file (at the repo root) can't be COPY-d into
// the image; instead we read it here at synth time and inject the values as
// container env vars, which core/openclaw_version.py reads at runtime. Mirrors
// the read in container-stack.ts — bump openclaw-version.json only.
interface OpenClawVersionConfig {
  tag: string;
  dev: { tag: string };
  prod: { tag: string };
  [key: string]: unknown;
}

const OPENCLAW_VERSION: OpenClawVersionConfig = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "..", "openclaw-version.json"),
    "utf8",
  ),
);

/**
 * Secret name strings (NOT ISecret objects) to avoid cross-stack dependency
 * cycles. ServiceStack imports them locally via fromSecretNameV2, which
 * prevents CDK from auto-granting KMS access on AuthStack's key.
 */
export interface SecretNames {
  clerkIssuer: string;
  clerkSecretKey: string;
  stripeSecretKey: string;
  stripeWebhookSecret: string;
  encryptionKey: string;
  /** PostHog personal API key for admin Activity tab. Empty string stubs the endpoint gracefully. */
  posthogProjectApiKey: string;
  /**
   * Symmetric secret used by the FastAPI Paperclip proxy router to sign
   * service-token JWTs forwarded to the Paperclip server (Better Auth
   * verifies them). Must match the secret name AuthStack creates in the
   * same env. Encrypted under the same CMK as the other auth secrets, so
   * the existing `KmsDecryptForSecrets` policy already covers it.
   */
  paperclipServiceTokenKey: string;
  /**
   * Dedicated symmetric secret for signing/verifying platform_ops tokens.
   * Encrypted under the same CMK as the other auth secrets, so the existing
   * KmsDecryptForSecrets policy covers it.
   */
  platformOpsTokenKey: string;
  /**
   * Shared password for the gog CLI file-keyring backend. The backend
   * materializes the owner's Google OAuth keyring on EFS by running gog with
   * this password; the same secret is injected into every per-user OpenClaw
   * container. Must match the secret name AuthStack creates in the same env
   * and is encrypted under the same CMK, so `KmsDecryptForSecrets` covers it.
   */
  gogKeyringPassword: string;
  /** Metronome API bearer token (sandbox/prod by token). Read by the lifecycle/gating modules. */
  metronomeApiKey: string;
  /** HMAC key verifying inbound Metronome webhook signatures. */
  metronomeWebhookSecret: string;
  /**
   * Resend API key secret name. Same isol8/{env}/* CMK + wildcard read as the
   * other auth secrets, so no extra IAM. The placeholder is a no-op until a
   * real re_… key is set (email_service gates on the key prefix).
   */
  resendApiKey: string;
  /**
   * Shared secret the WebSocket Lambdas attach to authenticate themselves to
   * the backend /api/v1/ws/* endpoints. Same secret AuthStack creates and the
   * WS Lambdas read; same CMK as the other auth secrets, so `KmsDecryptForSecrets`
   * already covers it. Enforcement is gated by WS_ENFORCE_INTERNAL_SECRET (a
   * plain env, default "false") so the secret can be wired to both sides before
   * the backend starts rejecting callers that lack the header.
   */
  wsInternalSecret: string;
}

export interface ServiceStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
  targetGroup: elbv2.IApplicationTargetGroup;
  albSecurityGroup: ec2.ISecurityGroup;
  database: {
    usersTable: dynamodb.Table;
    containersTable: dynamodb.Table;
    billingTable: dynamodb.Table;
    usageCountersTable: dynamodb.Table;
    pendingUpdatesTable: dynamodb.Table;
    channelLinksTable: dynamodb.Table;
    adminActionsTable: dynamodb.Table;
    oauthTokensTable: dynamodb.Table;
    webhookDedupTable: dynamodb.Table;
    paperclipCompaniesTable: dynamodb.Table;
    marketplaceListingsTable: dynamodb.Table;
    marketplacePurchasesTable: dynamodb.Table;
    marketplacePayoutAccountsTable: dynamodb.Table;
    marketplaceTakedownsTable: dynamodb.Table;
    marketplaceSellersTable: dynamodb.Table;
    hookEventsTable: dynamodb.Table;
    opsCacheTable: dynamodb.Table;
  };
  /** Pass secret names (strings) to avoid cross-stack KMS auto-grant cycles. */
  secretNames: SecretNames;
  /** Pass as string ARN to avoid cross-stack dependency cycle. */
  kmsKeyArn: string;
  container: {
    cluster: ecs.ICluster;
    cloudMapNamespace: servicediscovery.IPrivateDnsNamespace;
    cloudMapService: servicediscovery.IService;
    efsFileSystem: efs.IFileSystem;
    efsSecurityGroup: ec2.ISecurityGroup;
    containerSecurityGroup: ec2.ISecurityGroup;
    taskExecutionRole: iam.IRole;
    taskRole: iam.IRole;
    openclawTaskDef: ecs.ITaskDefinition;
    // Per-owner Bedrock enforcement (Plan 2): the backend reads these ARNs to
    // condition iam:CreateRole on the boundary and to attach the base policy to
    // every minted per-owner role. Live construct refs (same CDK stage), NOT
    // CfnOutput/ImportValue — matching how taskRole is already passed through.
    ownerBedrockBasePolicy: iam.IManagedPolicy;
    ownerBedrockBoundary: iam.IManagedPolicy;
  };
  managementApiUrl: string;
  /** Public wss:// endpoint returned by /api/v1/desktop/pair so desktop clients
   * know where to connect. Sourced from ApiStack.webSocketUrl. */
  webSocketUrl: string;
  connectionsTableName: string;
  wsApiId: string;
  wsStage: string;
  /** Optional: page topic ARN from ObservabilityStack (set via env var import). */
  alertPageTopicArn?: string;
}

export class ServiceStack extends cdk.Stack {
  public readonly service: ecs.FargateService;
  public readonly taskRole: iam.Role;

  constructor(scope: Construct, id: string, props: ServiceStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // -------------------------------------------------------------------------
    // Docker Image Asset (built & pushed by CDK Pipeline)
    // -------------------------------------------------------------------------
    const backendImage = new DockerImageAsset(this, "BackendImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_AMD64,
    });

    // -------------------------------------------------------------------------
    // Service Security Group
    // -------------------------------------------------------------------------
    const serviceSg = new ec2.SecurityGroup(this, "ServiceSecurityGroup", {
      vpc: props.vpc,
      description: `Isol8 ${env} Fargate service security group`,
      allowAllOutbound: true,
    });

    // Allow traffic from ALB on port 8000
    serviceSg.addIngressRule(
      props.albSecurityGroup,
      ec2.Port.tcp(8000),
      "HTTP from ALB",
    );

    // Cross-stack security group ingress rules.
    // We use CfnSecurityGroupIngress to avoid circular dependencies between stacks.

    // Allow service to mount EFS (port 2049)
    new ec2.CfnSecurityGroupIngress(this, "EfsFromServiceIngress", {
      groupId: props.container.efsSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 2049,
      toPort: 2049,
      sourceSecurityGroupId: serviceSg.securityGroupId,
      description: "Allow NFS from Fargate service",
    });

    // Allow service to manage Fargate containers (all TCP)
    new ec2.CfnSecurityGroupIngress(this, "ContainerFromServiceIngress", {
      groupId: props.container.containerSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 0,
      toPort: 65535,
      sourceSecurityGroupId: serviceSg.securityGroupId,
      description: "Allow all TCP from Fargate service for container management",
    });

    // -------------------------------------------------------------------------
    // IAM Task Role
    // -------------------------------------------------------------------------
    this.taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Isol8 ${env} Fargate task role`,
    });

    // ECR pull + DescribeImages (admin dropdown of available openclaw-extended tags)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcrAccess",
        actions: [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
        ],
        resources: ["*"],
      }),
    );

    // ECS management (per-user Fargate tasks)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcsManagement",
        actions: [
          "ecs:CreateService",
          "ecs:UpdateService",
          "ecs:DeleteService",
          "ecs:DescribeServices",
          "ecs:DescribeTasks",
          "ecs:ListServices",
          "ecs:ListTasks",
          "ecs:ExecuteCommand",
        ],
        resources: ["*"],
        conditions: {
          ArnEquals: {
            "ecs:cluster": props.container.cluster.clusterArn,
          },
        },
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcsTaskDefinition",
        actions: [
          "ecs:RegisterTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
        ],
        resources: ["*"],
      }),
    );

    // IAM PassRole for ECS task roles (shared execution + task roles AND the
    // per-owner Bedrock roles the backend mints at provision time). Without
    // the owner-bedrock-* prefix, RegisterTaskDefinition/CreateService with a
    // per-owner taskRoleArn fails AccessDenied on PassRole.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IamPassRole",
        actions: ["iam:PassRole"],
        resources: [
          props.container.taskExecutionRole.roleArn,
          props.container.taskRole.roleArn,
          `arn:aws:iam::${this.account}:role/isol8-${env}-owner-bedrock-*`,
        ],
        conditions: {
          StringEquals: {
            "iam:PassedToService": "ecs-tasks.amazonaws.com",
          },
        },
      }),
    );

    // Self-account trust-pin gate (aws-sts scope tiers, Task 5): the backend
    // reads the trust policy of a pasted our-account connector role before
    // trusting it. Scoped to the single fixed role name CFN mints for this
    // path — not a prefix wildcard like the owner-bedrock-* roles above.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadConnectorRoleForTrustCheck",
        actions: ["iam:GetRole"],
        // Canonical connector role name = bedrock_aip.CONNECTOR_ROLE_NAME. Must
        // match the CFN template RoleName and the backend constant; renaming
        // any one silently breaks the self-account trust-pin check (GetRole
        // scoped to the old name → AccessDenied → fail-closed 400).
        resources: [
          `arn:aws:iam::${this.account}:role/isol8-connector`,
        ],
      }),
    );

    // Backend mints + scopes + kills per-owner Bedrock roles (Plan 2). These
    // three statements live in a dedicated ManagedPolicy attached to the task
    // role, NOT in the inline default policy: the backend role's inline policy
    // is already near CloudFormation's 10 KB inline-policy limit, and adding
    // these inline would tip CDK into spilling statements (e.g. the by-name
    // DynamoDbIntegrations grant) into an overflow ManagedPolicy — silently
    // relocating them out of the AWS::IAM::Policy resource other features rely
    // on. A standalone, explicitly named ManagedPolicy keeps the inline policy
    // stable and is more auditable. CreateRole is boundary-conditioned so a
    // backend that can CreateRole+PutRolePolicy cannot escalate beyond the
    // OwnerBedrockBoundary (Bedrock+EFS+logs+ssm).
    new iam.ManagedPolicy(this, "BackendOwnerBedrockManage", {
      managedPolicyName: `isol8-${env}-backend-owner-bedrock-manage`,
      roles: [this.taskRole],
      statements: [
        new iam.PolicyStatement({
          sid: "OwnerBedrockRoleManage",
          actions: [
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
            "iam:GetRolePolicy",
            "iam:AttachRolePolicy",
            "iam:DetachRolePolicy",
            "iam:TagRole",
            "iam:GetRole",
            // Lets ensure_owner_bedrock_role converge a previously-minted role's
            // trust policy onto the SourceAccount/SourceArn-conditioned form.
            "iam:UpdateAssumeRolePolicy",
            // Account-wipe tool (Task 5): lets bedrock_aip.delete_owner_bedrock_role
            // remove the per-owner role once its inline policy is deleted and the
            // base managed policy is detached (IAM refuses DeleteRole otherwise).
            // Same owner-bedrock-* resource scope as the rest of this statement —
            // never touches the shared base managed policy itself.
            "iam:DeleteRole",
          ],
          resources: [
            `arn:aws:iam::${this.account}:role/isol8-${env}-owner-bedrock-*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "OwnerBedrockRoleCreate",
          actions: ["iam:CreateRole"],
          resources: [
            `arn:aws:iam::${this.account}:role/isol8-${env}-owner-bedrock-*`,
          ],
          conditions: {
            StringEquals: {
              "iam:PermissionsBoundary":
                props.container.ownerBedrockBoundary.managedPolicyArn,
            },
          },
        }),
        // Bedrock control plane. AIP ARNs are created at runtime so cannot be
        // pre-scoped by ARN; we constrain by tag instead. The mint stamps every
        // AIP with ManagedBy=isol8-backend (see bedrock_aip.ensure_owner_aips),
        // so:
        //   - create + tag-on-create are gated on aws:RequestTag/ManagedBy, so
        //     the backend can only mint profiles it marks as its own;
        //   - delete/get/untag/list-tags are gated on aws:ResourceTag/ManagedBy,
        //     so the backend can only act on profiles it minted;
        //   - ListInferenceProfiles is a collection-level read with no resource
        //     to tag, so it stays unconditioned (read-only; leaks no access).
        // The actions are split across three statements because not every
        // action supports the same condition key (RequestTag applies only to
        // create/tag; ResourceTag applies only to actions that name a resource;
        // List supports neither).
        new iam.PolicyStatement({
          sid: "OwnerBedrockProfileCreate",
          actions: [
            "bedrock:CreateInferenceProfile",
            "bedrock:TagResource",
          ],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "aws:RequestTag/ManagedBy": "isol8-backend",
            },
          },
        }),
        new iam.PolicyStatement({
          sid: "OwnerBedrockProfileMutate",
          actions: [
            "bedrock:DeleteInferenceProfile",
            "bedrock:GetInferenceProfile",
            "bedrock:UntagResource",
            "bedrock:ListTagsForResource",
          ],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "aws:ResourceTag/ManagedBy": "isol8-backend",
            },
          },
        }),
        new iam.PolicyStatement({
          sid: "OwnerBedrockProfileList",
          actions: ["bedrock:ListInferenceProfiles"],
          resources: ["*"],
        }),
      ],
    });

    // Secrets Manager
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SecretsAccess",
        actions: [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${env}/*`,
        ],
      }),
    );

    // Bootstrap admin credentials write (Paperclip service account):
    // apps/backend/scripts/bootstrap_paperclip_admin.py is invoked via
    // `aws ecs execute-command` against the backend task. It signs up
    // the bootstrap admin email (see ADMIN_EMAIL constant in the
    // script) in Paperclip, then calls PutSecretValue on
    // isol8/{env}/paperclip_admin_credentials. Without this scoped
    // grant the put fails with AccessDeniedException after the
    // Paperclip user is already created — leaves an inconsistent
    // bootstrap state (user exists but credentials not persisted, so
    // re-running fails with "user already exists").
    // Read of this secret is already covered by the wildcard
    // SecretsAccess + KmsDecryptForSecrets policies.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipAdminCredentialsWrite",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:PutSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${env}/paperclip_admin_credentials-*`,
        ],
      }),
    );

    // Account-wipe (Task 7 fix): the in-backend teardown deletes the owner's
    // Paperclip Better Auth "user" row directly via the RDS Data API
    // (core/services/paperclip_user_delete.py) because Paperclip exposes no
    // user-delete API. Three grants the task role otherwise lacks:
    //   - rds-data:ExecuteStatement on the paperclip Aurora cluster;
    //   - secretsmanager:GetSecretValue on the DASH-named DB-credentials secret
    //     (the wildcard SecretsAccess above only covers SLASH-named isol8/{env}/*
    //     secrets, NOT isol8-{env}-paperclip-db-credentials);
    //   - secretsmanager:ListSecrets (metadata only; the helper paginates to find
    //     that secret's full ARN — ListSecrets has no resource-level scoping, so
    //     it must be "*").
    // Without these the Aurora-user step 403s on every real account and orphans
    // the Better Auth identity (Teams 401 on re-signup with the same email).
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AccountWipePaperclipAuroraExec",
        effect: iam.Effect.ALLOW,
        actions: ["rds-data:ExecuteStatement"],
        resources: [
          `arn:aws:rds:${this.region}:${this.account}:cluster:isol8-${env}-paperclip-db`,
        ],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AccountWipePaperclipDbSecretRead",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8-${env}-paperclip-db-credentials-*`,
        ],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AccountWipeListSecrets",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:ListSecrets"],
        resources: ["*"],
      }),
    );

    // Per-user LLM-key secrets (Plan 2 Task 10): backend creates/updates/reads
    // a Secrets Manager secret per user under isol8/{env}/user-keys/*. Scoped
    // tightly to that prefix so the broad SecretsAccess policy above is not
    // implicitly widened to write access on all isol8/{env}/* secrets.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "UserKeysSecretsRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${env}/user-keys/*`,
        ],
      }),
    );

    // Per-owner integration credential secrets (credential-layer redesign):
    // paste-key tokens and backend-custodian OAuth credentials live under
    // isol8/{env}/integrations/*. The secret-free integrations DDB index points
    // here. Same tight prefix scoping as the user-keys statement above.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IntegrationSecretsRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${env}/integrations/*`,
        ],
      }),
    );

    // Per-agent Paperclip API keys (credential-layer redesign): minted by the
    // backend and stored under isol8/{env}/paperclip-agent-keys/* so Friday's
    // openclaw.json MCP block can authenticate to Paperclip. Replaces the old
    // encrypted-in-DynamoDB api-keys storage. Same tight prefix scoping.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipAgentKeysRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${env}/paperclip-agent-keys/*`,
        ],
      }),
    );

    // DynamoDB tables
    props.database.usersTable.grantReadWriteData(this.taskRole);
    props.database.containersTable.grantReadWriteData(this.taskRole);
    props.database.billingTable.grantReadWriteData(this.taskRole);
    props.database.usageCountersTable.grantReadWriteData(this.taskRole);
    props.database.pendingUpdatesTable.grantReadWriteData(this.taskRole);
    props.database.channelLinksTable.grantReadWriteData(this.taskRole);
    props.database.adminActionsTable.grantReadWriteData(this.taskRole);
    props.database.oauthTokensTable.grantReadWriteData(this.taskRole);
    // The credential-layer integration tables are granted by NAME, not via
    // grantReadWriteData. grantReadWriteData would export each table's ARN from
    // the database stack and import it here, creating a cross-stack export. An
    // export that another stack imports cannot be deleted, which permanently
    // blocks renaming or replacing the table. Granting by the table name avoids
    // the export entirely. The action set matches what integrations_repo and
    // oauth_state_repo actually call.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbIntegrations",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-integrations`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-integrations/index/*`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-oauth-state`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-oauth-state/index/*`,
          // Per-owner AIP reverse index (Plan 2). Folded into this by-name DDB
          // statement (identical action set + condition-less shape) rather than
          // added as a separate fourth statement: the backend role's inline
          // policy is near CloudFormation's 10 KB limit, so a separate statement
          // risks tipping CDK into an overflow ManagedPolicy that would relocate
          // this very statement out of the AWS::IAM::Policy resource other
          // features assert on. Same cross-stack-export-avoidance posture; the
          // grant (GetItem/PutItem/UpdateItem/DeleteItem/Query) is identical.
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-bedrock-aip-owners`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-bedrock-aip-owners/index/*`,
          // Metronome local fast-gate flag (Phase 2). Folded into this by-name
          // statement for the SAME reason as bedrock-aip-owners above: a separate
          // grantReadWriteData would both bloat the near-10 KB inline policy
          // (tipping CDK into an overflow ManagedPolicy that relocates this very
          // statement) AND export the table ARN cross-stack. The action set
          // (GetItem/PutItem/UpdateItem/Query) matches MetronomeFastGate +
          // metronome_provisioning + metronome_funding.
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-metronome-fast-gate`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-metronome-fast-gate/index/*`,
        ],
      }),
    );
    // Webhook dedup helper does conditional PutItem with attribute_not_exists;
    // never reads. Write-only grant is sufficient.
    props.database.webhookDedupTable.grantWriteData(this.taskRole);
    // Paperclip integration: the proxy router (paperclip_proxy.py:388,
    // routers/webhooks.py:146) and the daily purge worker
    // (services/update_service.py:198) all read/write this table to look up
    // each user's Paperclip company row and decrypt the per-user Better Auth
    // password. Without this grant the proxy 500s on every Teams click with
    // an AccessDeniedException, even when Clerk auth succeeds.
    props.database.paperclipCompaniesTable.grantReadWriteData(this.taskRole);

    // Marketplace tables — backend needs read/write on listings, purchases,
    // payout-accounts, takedowns. Browse + search are served by the in-process
    // search service (60s-TTL DDB scan), so the same listings grant covers it.
    props.database.marketplaceListingsTable.grantReadWriteData(this.taskRole);
    props.database.marketplacePurchasesTable.grantReadWriteData(this.taskRole);
    props.database.marketplacePayoutAccountsTable.grantReadWriteData(this.taskRole);
    props.database.marketplaceTakedownsTable.grantReadWriteData(this.taskRole);
    props.database.marketplaceSellersTable.grantReadWriteData(this.taskRole);

    // OpenClaw webhook ingress audit log (T4 of openclaw-hook-token-provisioning).
    // T5 repo writes one row per delivery; T6 proxy + admin views read.
    // KMS encrypt/decrypt is already covered by the broader KmsAccess
    // statement above (kmsKeyArn is the same CMK the table uses).
    props.database.hookEventsTable.grantReadWriteData(this.taskRole);

    // Platform-ops fleet cache. The merged ops layer reads AND writes this
    // table: ops_cache_repo.get()/put() are called from ecs_ops.fleet_grid()
    // with no try/except, so without this grant the first get() raises
    // AccessDeniedException and 500s the ops fleet endpoint. Its name is wired
    // into OPS_CACHE_TABLE just below.
    props.database.opsCacheTable.grantReadWriteData(this.taskRole);

    // Bedrock
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockInvoke",
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
          "arn:aws:bedrock:*:*:inference-profile/*",
        ],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockList",
        actions: [
          "bedrock:ListFoundationModels",
          "bedrock:ListInferenceProfiles",
        ],
        resources: ["*"],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Marketplace",
        actions: [
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Subscribe",
          "aws-marketplace:Unsubscribe",
        ],
        resources: ["*"],
      }),
    );

    // CloudWatch Logs
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogs",
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "cloudwatch:PutMetricData",
        ],
        resources: ["*"],
      }),
    );

    // CloudWatch Logs read — for the admin dashboard inline log viewer
    // (apps/backend/core/services/cloudwatch_logs.py). Scoped to the
    // backend's own log group ARN; never "*" — least privilege.
    //
    // Log group name matches service-stack.ts + isol8-stage.ts +
    // local-stage.ts — `/ecs/isol8-${env}` (no /aws prefix, no
    // -backend suffix). The cloudwatch_logs service must use this same
    // name when constructing FilterLogEvents calls.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogsReadForAdmin",
        actions: [
          "logs:FilterLogEvents",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
          "logs:GetLogEvents",
          "logs:DescribeLogStreams",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/ecs/isol8-${env}:*`,
        ],
      }),
    );

    // CloudWatch alarms read for the admin operator surface. DescribeAlarms
    // is not resource-scoped, so it requires "*".
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchAlarmsReadForAdmin",
        actions: ["cloudwatch:DescribeAlarms"],
        resources: ["*"],
      }),
    );

    // Cloud Map (service discovery) — constrained to namespace + service
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudMapAccess",
        actions: [
          "servicediscovery:RegisterInstance",
          "servicediscovery:DeregisterInstance",
          "servicediscovery:GetNamespace",
          "servicediscovery:GetService",
          "servicediscovery:ListInstances",
        ],
        resources: [
          props.container.cloudMapNamespace.namespaceArn,
          props.container.cloudMapService.serviceArn,
        ],
      }),
    );
    // DiscoverInstances is not resource-scoped — it requires "*"
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudMapDiscover",
        actions: ["servicediscovery:DiscoverInstances"],
        resources: ["*"],
      }),
    );

    // KMS (use string ARN to avoid cross-stack dependency)
    //
    // kms:Encrypt is required so the provision flow can seal per-container
    // secrets (operator device Ed25519 seeds, gateway tokens) before writing
    // them to the containers DynamoDB table. kms:Decrypt is the pre-existing
    // permission for reading them back at handshake time.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsAccess",
        actions: ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
        resources: [props.kmsKeyArn],
      }),
    );

    // EC2 self-discovery + CloudFormation read
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Ec2SelfDiscovery",
        actions: [
          "ec2:DescribeInstances",
          "ec2:DescribeTags",
          "cloudformation:DescribeStacks",
          "cloudformation:DescribeStackEvents",
          // Required to call DescribeStacks WITHOUT a StackName (enumerate all
          // stacks): CloudFormation authorizes that form against ListStacks,
          // not DescribeStacks. The deploy-status view lists every stack, so
          // dropping this returns AccessDenied on the enumerate call.
          "cloudformation:ListStacks",
        ],
        resources: ["*"],
      }),
    );

    // STS
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "StsAccess",
        actions: ["sts:GetCallerIdentity", "sts:AssumeRole"],
        resources: ["*"],
      }),
    );

    // EFS access points management
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EfsAccessPoints",
        actions: [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:CreateAccessPoint",
          "elasticfilesystem:DescribeAccessPoints",
          "elasticfilesystem:TagResource",
        ],
        resources: [props.container.efsFileSystem.fileSystemArn],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EfsDeleteAccessPoints",
        actions: [
          "elasticfilesystem:DeleteAccessPoint",
          "elasticfilesystem:TagResource",
        ],
        resources: [
          `arn:aws:elasticfilesystem:*:${this.account}:access-point/*`,
        ],
      }),
    );

    // S3 (OpenClaw config bucket)
    // Per-user path scoping via IAM policy variables (${aws:userid}) is not
    // feasible: aws:userid resolves to the IAM role unique ID, not the app-
    // level Clerk user ID that keys the S3 paths. The bucket is already
    // constrained to isol8-${env}-openclaw-configs so the blast radius is
    // limited to this bucket. Application-level authorization enforces per-
    // user access.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "S3Access",
        actions: [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ],
        resources: [
          `arn:aws:s3:::isol8-${env}-openclaw-configs`,
          `arn:aws:s3:::isol8-${env}-openclaw-configs/*`,
        ],
      }),
    );

    // WebSocket push (execute-api:ManageConnections)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WebSocketManageConnections",
        actions: ["execute-api:ManageConnections"],
        resources: [
          `arn:aws:execute-api:${this.region}:${this.account}:${props.wsApiId}/${props.wsStage}/*`,
        ],
      }),
    );

    // SNS (page topic — for backend-initiated alerts like fleet patch audits)
    if (props.alertPageTopicArn) {
      this.taskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "SnsPublishPageTopic",
          actions: ["sns:Publish"],
          resources: [props.alertPageTopicArn],
        }),
      );
    }

    // DynamoDB CRUD on connections table
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbConnections",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.connectionsTableName}`,
          // GSI ARN — required to Query the by-user-id index added in
          // api-stack.ts. Without this, broker fanout queries 400 with
          // AccessDeniedException at runtime.
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.connectionsTableName}/index/*`,
        ],
      }),
    );

    // -------------------------------------------------------------------------
    // Marketplace Artifacts S3 Bucket
    //
    // Stores artifacts uploaded for marketplace listings (agent bundles,
    // screenshots, manifests, signed receipts). Versioned so a bad publish
    // can be rolled back without losing buyer history. Blocked from public
    // access; bytes are served via signed URLs from the marketplace API.
    // -------------------------------------------------------------------------
    const marketplaceArtifactsBucket = new s3.Bucket(this, "MarketplaceArtifactsBucket", {
      bucketName: `isol8-${env}-marketplace-artifacts`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
    });

    marketplaceArtifactsBucket.grantReadWrite(this.taskRole);

    // -------------------------------------------------------------------------
    // CFN Templates S3 Bucket (public-read of the connector template only)
    //
    // Hosts the AWS connector CloudFormation template so the
    // "Create role in AWS" quick-create button works: CloudFormation requires
    // the TemplateURL to be an S3 URL it can fetch ANONYMOUSLY (not a
    // CloudFront/Vercel URL). The template (apps/infra/aws/aws-connector.yaml)
    // is generic + fully parameterized — no secrets — so public READ of that
    // single object is safe.
    //
    // Public-access posture: block public ACLs (we never use ACLs) but ALLOW a
    // public bucket POLICY, and the policy below grants ONLY s3:GetObject on the
    // one template object to everyone. No public write, no listing, no other
    // objects (the deployment prunes the bucket to exactly this template). SSE-S3
    // (not KMS) so anonymous CloudFormation GETs aren't blocked on decrypt.
    // -------------------------------------------------------------------------
    const CFN_TEMPLATE_KEY = "aws-connector.yaml";
    const cfnTemplatesBucket = new s3.Bucket(this, "CfnTemplatesBucket", {
      bucketName: `isol8-${env}-cfn-templates`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: new s3.BlockPublicAccess({
        blockPublicAcls: true,
        ignorePublicAcls: true,
        blockPublicPolicy: false,
        restrictPublicBuckets: false,
      }),
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
    });

    // Public READ of the single connector template object — nothing else.
    cfnTemplatesBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "PublicReadConnectorTemplate",
        effect: iam.Effect.ALLOW,
        principals: [new iam.AnyPrincipal()],
        actions: ["s3:GetObject"],
        resources: [cfnTemplatesBucket.arnForObjects(CFN_TEMPLATE_KEY)],
      }),
    );

    // Upload the connector template. Exclude the dir's README so the bucket
    // holds ONLY the template; `prune` (default true) keeps it to that object.
    new s3deploy.BucketDeployment(this, "CfnTemplateDeployment", {
      destinationBucket: cfnTemplatesBucket,
      sources: [
        s3deploy.Source.asset(path.join(__dirname, "..", "..", "aws"), {
          exclude: ["*.md"],
        }),
      ],
    });

    // -------------------------------------------------------------------------
    // Marketplace browse / search is served by the backend's in-process
    // search service (apps/backend/core/services/marketplace_search.py) —
    // a 60s-TTL DDB scan over published listings. At v0 scale the listing
    // count is small enough that we don't need an external SaaS index;
    // there is no separate sync Lambda or Stream subscriber.
    // -------------------------------------------------------------------------

    // -------------------------------------------------------------------------
    // Task Execution Role
    // -------------------------------------------------------------------------
    const taskExecutionRole = new iam.Role(this, "TaskExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Isol8 ${env} Fargate task execution role`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy",
        ),
      ],
    });

    // Allow execution role to pull secrets for container injection
    taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SecretsManagerForEcs",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:isol8/${env}/*`,
        ],
      }),
    );

    // Allow execution role to decrypt secrets encrypted with KMS
    // (manual grant instead of CDK auto-grant to avoid cross-stack cycle)
    taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForSecrets",
        actions: ["kms:Decrypt"],
        resources: [props.kmsKeyArn],
      }),
    );

    // -------------------------------------------------------------------------
    // Log Group
    // -------------------------------------------------------------------------
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/isol8-${env}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // -------------------------------------------------------------------------
    // Fargate Task Definition
    // -------------------------------------------------------------------------
    const privateSubnetIds = props.vpc
      .selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS })
      .subnetIds.join(",");

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      family: `isol8-${env}-backend`,
      cpu: env === "prod" ? 2048 : 1024,
      memoryLimitMiB: env === "prod" ? 4096 : 2048,
      taskRole: this.taskRole,
      executionRole: taskExecutionRole,
    });

    // EFS Volume
    taskDef.addVolume({
      name: "efs-users",
      efsVolumeConfiguration: {
        fileSystemId: props.container.efsFileSystem.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: { iam: "ENABLED" },
      },
    });

    // Container definition
    const container = taskDef.addContainer("backend", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      // Run as root — backend writes to EFS user directories
      user: "0:0",
      portMappings: [{ containerPort: 8000 }],
      environment: {
        ENVIRONMENT: env,
        AWS_REGION: "us-east-1",
        // OpenClaw image versions — injected from openclaw-version.json (the
        // single source of truth) so the backend never hand-mirrors them.
        // Read at runtime by core/openclaw_version.py.
        OPENCLAW_UPSTREAM_VERSION: OPENCLAW_VERSION.tag,
        OPENCLAW_DEV_TAG: OPENCLAW_VERSION.dev.tag,
        OPENCLAW_PROD_TAG: OPENCLAW_VERSION.prod.tag,
        WS_MANAGEMENT_API_URL: props.managementApiUrl,
        WS_GATEWAY_URL: props.webSocketUrl,
        WS_CONNECTIONS_TABLE: props.connectionsTableName,
        // WS caller-attestation gate, enforced in every env. The
        // x-isol8-ws-internal header is verified flowing from the WS Lambdas on
        // both dev and prod (zero secret-fetch failures across real connects),
        // so the backend rejects /ws/* callers that lack it — closing the
        // unauthenticated WebSocket-impersonation hole on the public HTTP
        // API Gateway $default path.
        WS_ENFORCE_INTERNAL_SECRET: "true",
        WEBHOOK_DEDUP_TABLE: props.database.webhookDedupTable.tableName,
        CORS_ORIGINS:
          env === "local"
            ? "http://localhost:3000"
            : env === "prod"
              ? "https://isol8.co"
              : "https://dev.isol8.co",
        // Per-container secrets CMK — used by core/crypto/kms_secrets.py to
        // encrypt operator device seeds + gateway tokens at rest. Reuses the
        // same CMK as the BYOK Fernet layer since the blast radius is the same
        // (any principal with kms:Decrypt on this key can read any of them).
        CONTAINER_SECRETS_KMS_KEY_ID: props.kmsKeyArn,
        // Stripe flat-fee Price ID is environment-specific: prod is created in
        // Stripe live mode, dev in test mode, and they MUST NOT cross-pollinate
        // (a test-mode Price would silently create test subscriptions in prod).
        // Read per-env from the runner's secrets at synth time.
        STRIPE_FLAT_PRICE_ID:
          env === "prod"
            ? (process.env.STRIPE_FLAT_PRICE_ID_PROD ?? "")
            : (process.env.STRIPE_FLAT_PRICE_ID_DEV ?? ""),
        // Stripe per-seat Teams Price ID — same env-specific live/test split
        // as STRIPE_FLAT_PRICE_ID above (a test-mode Price would silently
        // create test subscriptions in prod). Read per-env from the runner's
        // secrets at synth time. Consumed by billing_service.create_team_seat_checkout.
        STRIPE_TEAM_SEAT_PRICE_ID:
          env === "prod"
            ? (process.env.STRIPE_TEAM_SEAT_PRICE_ID_PROD ?? "")
            : (process.env.STRIPE_TEAM_SEAT_PRICE_ID_DEV ?? ""),
        FRONTEND_URL:
          env === "local"
            ? "http://localhost:3000"
            : env === "prod"
              ? "https://isol8.co"
              : "https://dev.isol8.co",
        // Public-facing base URL of THIS API. Used by /hooks/credentials
        // (and any future endpoint that surfaces an absolute URL back to
        // the browser) to build the webhook URL the Settings panel
        // displays to the user. Per-env API hostname, matches the DNS
        // records provisioned by dns-stack.ts.
        PUBLIC_API_BASE:
          env === "local"
            ? "http://localhost:8000"
            : env === "prod"
              ? "https://api.isol8.co"
              : `https://api-${env}.isol8.co`,
        // Paperclip wiring. Without these the backend's HostDispatcherMiddleware
        // (apps/backend/main.py) builds an empty dispatch host set, so requests
        // to `company-{env}.isol8.co` fall through to the default Isol8 API
        // instead of being rewritten to the paperclip_proxy router. Mirrors
        // the env-derivation in paperclip-stack.ts so the two stay in lockstep
        // (keeps ServiceStack independent of PaperclipStack synth order).
        // The `Origin` header the Teams BFF sends on Paperclip /api/auth/* +
        // board-mutation calls. MUST match PaperclipStack's PAPERCLIP_PUBLIC_URL
        // (Better Auth's trustedOrigins / origin guard), so keep the two in lockstep
        // at the same https origin. NOT a live link and NOT the agent's API URL —
        // see the PAPERCLIP_PUBLIC_URL comment in paperclip-stack.ts. Switching this
        // to the http in-VPC host (PR #1073) broke Better Auth sign-in (403).
        PAPERCLIP_PUBLIC_URL:
          env === "prod"
            ? "https://company.isol8.co"
            : `https://${env}.company.isol8.co`,
        // Cloud Map A record published by PaperclipStack at:
        //   `paperclip.<cloudMapNamespace.namespaceName>:3100`
        // Namespace name is `isol8-${env}.local` (ContainerStack). Reading
        // it off the namespace handle keeps this string in lockstep with
        // ContainerStack rather than hard-coding the env suffix here.
        PAPERCLIP_INTERNAL_URL: `http://paperclip.${props.container.cloudMapNamespace.namespaceName}:3100`,
        // Clerk publishable key for the paperclip-proxy bootstrap HTML.
        // The bootstrap loads the Clerk SDK in the browser, fetches a JWT,
        // and POSTs it to /__handshake__ to get a host-scoped session
        // cookie on company.isol8.co. Same value the frontend uses as
        // NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY — publishable by design (Clerk
        // serves it in HTML), so plain env var is appropriate (not a
        // Secrets Manager secret). Same per-env runner-secret pattern as
        // STRIPE_FLAT_PRICE_ID. Dev fallback hardcoded so local synths
        // and developer cdk-synth-from-laptop work out of the box.
        CLERK_PUBLISHABLE_KEY:
          env === "prod"
            ? (process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD ?? "")
            : (process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV ??
                "pk_test_dXAtbW90aC01NS5jbGVyay5hY2NvdW50cy5kZXYk"),
        DEBUG: env === "local" ? "true" : "false",
        // LocalStack needs this to redirect boto3 calls inside the ECS container
        ...(env === "local" ? { AWS_ENDPOINT_URL: "http://localhost.localstack.cloud:4566" } : {}),
        BEDROCK_ENABLED: env === "local" ? "false" : "true",
        EFS_MOUNT_PATH: "/mnt/efs/users",
        EFS_FILE_SYSTEM_ID: props.container.efsFileSystem.fileSystemId,
        CONTAINER_EXECUTION_ROLE_ARN:
          props.container.taskExecutionRole.roleArn,
        ECS_CLUSTER_ARN: props.container.cluster.clusterArn,
        // ECS_TASK_DEFINITION is intentionally not set. Backend reads the
        // latest CDK base revision live via describe_task_definition(family)
        // — the base family is uncontaminated because per-user clones go to
        // a separate `<base>-user` family.
        ECS_SUBNETS: privateSubnetIds,
        ECS_SECURITY_GROUP_ID:
          props.container.containerSecurityGroup.securityGroupId,
        CLOUD_MAP_NAMESPACE_ID:
          props.container.cloudMapNamespace.namespaceId,
        CLOUD_MAP_SERVICE_ID: props.container.cloudMapService.serviceId,
        CLOUD_MAP_SERVICE_ARN: props.container.cloudMapService.serviceArn,
        CLOUD_MAP_NAMESPACE_NAME: props.container.cloudMapNamespace.namespaceName,
        CLOUD_MAP_SERVICE_NAME: props.container.cloudMapService.serviceName,
        DYNAMODB_TABLE_PREFIX: `isol8-${env}-`,
        OAUTH_TOKENS_TABLE: props.database.oauthTokensTable.tableName,
        // Metronome lifecycle + gating settings. Metronome is the ONLY
        // billing ledger: the backend provisions Metronome customers, funds
        // payment-gated top-up commits, handles the low-balance webhook,
        // lazily refreshes the gate, and displays balances — it never
        // ingests usage (the meter Lambda does). Base URL is the same for
        // sandbox + prod (token decides env). Fast-gate table backs the
        // synchronous chat gate's balance projection.
        METRONOME_BASE_URL: "https://api.metronome.com/v1",
        // Literal name (not a cross-stack tableName Ref) — same posture as
        // BEDROCK_AIP_OWNERS_TABLE above: the table is granted BY NAME in the
        // DynamoDbIntegrations statement, so no cross-stack export is created.
        METRONOME_FAST_GATE_TABLE: `isol8-${env}-metronome-fast-gate`,
        // Master switch for the Metronome customer lifecycle (provisioning,
        // funding, balance display). Dev went live 2026-06-10 (sandbox
        // runbook); prod went live 2026-06-12 (prod token in Secrets
        // Manager, prod rate card + credits product created via API,
        // Stripe live product wired into the stripe_product_id custom
        // field, webhook secret set). Any future env starts "false" until
        // its console runbook completes — flipping earlier would spam
        // doomed calls against placeholder secrets on every status poll.
        METRONOME_LIFECYCLE_ENABLED:
          env === "dev" || env === "prod" ? "true" : "false",
        // Per-env rate card the per-customer contract references at
        // provision. Dev = sandbox rate card (2026-06-10); prod =
        // isol8-bedrock-prod (2026-06-12). Both price AWS us. CRIS x 1.03
        // in cents per token (margin decision 2026-06-11).
        METRONOME_RATE_CARD_ID:
          env === "dev"
            ? "99576f5a-37d2-4983-ba64-fea9bad96e08"
            : env === "prod"
              ? "fdac2edd-f207-4f11-b65b-01fad6fbbdbc"
              : "",
        // The FIXED-type product top-up commits land on. Payment-gated
        // invoicing additionally requires a stripe_product_id custom field
        // on it (set: dev -> test-mode product, prod -> live-mode
        // prod_UgkNkatoEaWFnb); without it, top-ups 400 loudly rather
        // than silently no-op.
        METRONOME_CREDITS_PRODUCT_ID:
          env === "dev"
            ? "6dab1634-020d-4288-b5e6-99568a30404c"
            : env === "prod"
              ? "876e1694-9d76-4c37-9814-c957dbd9370f"
              : "",
        // Per-owner Bedrock AIP + scoped-role enforcement (Plan 2). The reverse
        // table is granted BY NAME above, so the env var is the literal name
        // (not a cross-stack tableName ref). The boundary + base-policy ARNs
        // come from the container stack's live members.
        BEDROCK_AIP_OWNERS_TABLE: `isol8-${env}-bedrock-aip-owners`,
        BEDROCK_OWNER_ROLE_PREFIX: `isol8-${env}-owner-bedrock`,
        BEDROCK_OWNER_BOUNDARY_ARN:
          props.container.ownerBedrockBoundary.managedPolicyArn,
        BEDROCK_OWNER_BASE_POLICY_ARN:
          props.container.ownerBedrockBasePolicy.managedPolicyArn,
        // ACP coding-harness runtime (acpx). Master switch read by
        // core.config.settings.ACP_ENABLED — when true, write_openclaw_config
        // emits the acp block + plugins.entries.acpx and provision wires the
        // per-owner Bedrock harness env, letting a native agent (Jarvis)
        // sessions_spawn Claude Code / Codex. The harness set is DERIVED
        // per-owner from provider_choice (bedrock/byo-anthropic -> claude;
        // chatgpt-oauth/byo-openai -> codex), so no owner is handed a harness
        // it cannot authenticate. Enabled fleet-wide now that the acpx-baked
        // extended image (2026.6.10-isol8.2, Dockerfile Layer 13) is promoted
        // in openclaw-version.json for both dev and prod; existing containers
        // pick up the acp block on their next reprovision / config patch.
        ACP_ENABLED: "true",
        // Marketplace DDB tables + S3 artifacts bucket. Backend reads these
        // env vars via core.config.settings; empty defaults keep the app
        // booting in non-marketplace contexts (unit tests, smoke tests).
        MARKETPLACE_LISTINGS_TABLE:
          props.database.marketplaceListingsTable.tableName,
        MARKETPLACE_PURCHASES_TABLE:
          props.database.marketplacePurchasesTable.tableName,
        MARKETPLACE_PAYOUT_ACCOUNTS_TABLE:
          props.database.marketplacePayoutAccountsTable.tableName,
        MARKETPLACE_TAKEDOWNS_TABLE:
          props.database.marketplaceTakedownsTable.tableName,
        MARKETPLACE_SELLERS_TABLE:
          props.database.marketplaceSellersTable.tableName,
        OPS_CACHE_TABLE: props.database.opsCacheTable.tableName,
        // Per-env owner the platform_ops agent token binds to (the founder's
        // own owner_id). Read from the runner's secrets at synth time like the
        // Stripe/Clerk per-env values above; the dev and prod Clerk instances
        // mint different owner_ids for the same admin email. Empty ⇒ ops-token
        // surface stays off in that env (fail-closed).
        PLATFORM_OPS_OWNER_ID:
          env === "prod"
            ? (process.env.PLATFORM_OPS_OWNER_ID_PROD ?? "")
            : (process.env.PLATFORM_OPS_OWNER_ID_DEV ?? ""),
        MARKETPLACE_ARTIFACTS_BUCKET: marketplaceArtifactsBucket.bucketName,
        // Deterministic public S3 URL of the hosted AWS connector
        // CloudFormation template. The connections aws-setup endpoint feeds this
        // as the quick-create TemplateURL (CloudFormation must fetch it
        // anonymously). Bucket name + key are fixed, so this is a literal string
        // — no cross-stack output ref needed (the bucket lives in this stack).
        CFN_TEMPLATE_URL: `https://isol8-${env}-cfn-templates.s3.us-east-1.amazonaws.com/${CFN_TEMPLATE_KEY}`,
        // Stripe Connect onboarding redirects — environment-specific so
        // dev/prod hit the correct marketplace host. Stripe-hosted onboarding
        // bounces sellers back here on refresh + completion.
        STRIPE_CONNECT_REFRESH_URL:
          env === "prod"
            ? "https://isol8.co/marketplace/payouts/refresh"
            : "https://dev.isol8.co/marketplace/payouts/refresh",
        STRIPE_CONNECT_RETURN_URL:
          env === "prod"
            ? "https://isol8.co/marketplace/payouts/return"
            : "https://dev.isol8.co/marketplace/payouts/return",
        // PostHog server-side reads for the admin Activity tab.
        // us.posthog.com is the API host for US Cloud (our project lives
        // there). Project ID is public — visible in the PostHog dashboard
        // URL for the Default project. Personal API key comes in via the
        // secrets: block below.
        POSTHOG_HOST: "https://us.posthog.com",
        POSTHOG_PROJECT_ID: "380894",
        // PostHog project (phc_) INGESTION key for the server-side billing
        // bridge (core/observability/product_analytics.py -> trial_started /
        // trial_converted / subscription_cancelled, keyed on the Clerk user).
        // Write-only / public — the same key the frontend ships as
        // NEXT_PUBLIC_POSTHOG_KEY (PostHog: "Safe to use in public apps"), so
        // it lives in the plain env block, NOT secrets. Distinct from the
        // POSTHOG_PROJECT_API_KEY personal read key in the secrets: block.
        POSTHOG_PROJECT_WRITE_KEY: "phc_nzqnGGPwHfrKycptH3TViSAo3uaxT8CHAXovvHnBGEma",
        // Observability: page topic ARN for backend-initiated SNS alerts.
        // Populated after first deploy via Fn.importValue from ObservabilityStack.
        ...(props.alertPageTopicArn
          ? { ALERT_PAGE_TOPIC_ARN: props.alertPageTopicArn }
          : {}),
      },
      secrets: {
        // Import secrets by name (NOT cross-stack ISecret) to avoid CDK
        // auto-granting KMS decrypt on AuthStack's key, which causes a
        // circular dependency: auth -> service -> container -> auth.
        //
        CLERK_ISSUER: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportClerkIssuer", props.secretNames.clerkIssuer),
        ),
        CLERK_SECRET_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportClerkSecretKey", props.secretNames.clerkSecretKey),
        ),
        STRIPE_SECRET_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportStripeSecretKey", props.secretNames.stripeSecretKey),
        ),
        STRIPE_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportStripeWebhookSecret", props.secretNames.stripeWebhookSecret),
        ),
        // Marketplace's Stripe Connect webhook signing secret (separate
        // from the regular Stripe webhook above — Connect events come
        // through a different webhook endpoint with its own signing key).
        // marketplace_purchases.stripe_webhook reads this via
        // settings.STRIPE_CONNECT_WEBHOOK_SECRET to verify event signatures;
        // an empty value means stripe.Webhook.construct_event will reject
        // every event and paid checkouts silently never produce purchase
        // rows. Operator provisions the secret via
        // `aws secretsmanager update-secret` with the value from the
        // Stripe dashboard's Connect webhook (per the Plan 1 runbook).
        STRIPE_CONNECT_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportStripeConnectWebhookSecret",
            `isol8/${env}/stripe_connect_webhook_secret`,
          ),
        ),
        ENCRYPTION_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportEncryptionKey", props.secretNames.encryptionKey),
        ),
        // PostHog personal API key for the admin Activity tab. Empty value
        // → posthog_admin.py returns {stubbed: true}; populated value →
        // real Persons API calls. Rotate via `aws secretsmanager update-secret`.
        POSTHOG_PROJECT_API_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportPosthogProjectApiKey",
            props.secretNames.posthogProjectApiKey,
          ),
        ),
        // Symmetric secret used by the paperclip_proxy router to sign
        // service-token JWTs Paperclip's Better Auth verifies. KMS decrypt
        // for this secret is covered by the existing `KmsDecryptForSecrets`
        // policy below — same CMK as the other auth secrets.
        PAPERCLIP_SERVICE_TOKEN_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportPaperclipServiceTokenKey",
            props.secretNames.paperclipServiceTokenKey,
          ),
        ),
        // Dedicated symmetric secret used to sign/verify platform_ops tokens.
        // KMS decrypt for this secret is covered by the existing
        // `KmsDecryptForSecrets` policy below — same CMK as the other auth
        // secrets.
        PLATFORM_OPS_TOKEN_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportPlatformOpsTokenKey",
            props.secretNames.platformOpsTokenKey,
          ),
        ),
        // Shared password for the gog CLI file keyring. The backend's gog_cli
        // materializer runs gog against the owner's EFS workspace to write the
        // Google OAuth keyring, which the file-keyring backend encrypts with
        // this password — the identical value every per-user OpenClaw container
        // also receives. KMS decrypt is covered by `KmsDecryptForSecrets` below
        // (same CMK as the other auth secrets).
        GOG_KEYRING_PASSWORD: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportGogKeyringPassword",
            props.secretNames.gogKeyringPassword,
          ),
        ),
        // Read via settings.METRONOME_API_KEY by the Metronome lifecycle
        // modules (customer provisioning, top-up funding, balance display)
        // unconditionally — Metronome shadows even while the DDB ledger
        // still bills, so the key is live in every environment.
        // KMS decrypt covered by the wildcard SecretsAccess + same CMK.
        METRONOME_API_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportMetronomeApiKey", props.secretNames.metronomeApiKey),
        ),
        METRONOME_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportMetronomeWebhookSecret",
            props.secretNames.metronomeWebhookSecret,
          ),
        ),
        RESEND_API_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportResendApiKey", props.secretNames.resendApiKey),
        ),
        // Shared secret the WS Lambdas attach as x-isol8-ws-internal. The
        // backend compares it (require_ws_internal_caller) only when
        // WS_ENFORCE_INTERNAL_SECRET is "true". Same CMK as the other auth
        // secrets, so KmsDecryptForSecrets below already covers decrypt.
        WS_INTERNAL_SECRET: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportWsInternalSecret",
            props.secretNames.wsInternalSecret,
          ),
        ),
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "curl -f http://localhost:8000/health || exit 1",
        ],
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "backend",
        logGroup,
      }),
      // SIGTERM->SIGKILL window for graceful shutdown. Sits at the top of the
      // timing ladder: app asyncio budget (20s) < uvicorn --timeout-graceful-shutdown
      // (25s) < this stopTimeout (30s), so the app + uvicorn drain in-flight work
      // before ECS hard-kills the task.
      stopTimeout: cdk.Duration.seconds(30),
    });

    // EFS mount point
    container.addMountPoints({
      containerPath: "/mnt/efs",
      sourceVolume: "efs-users",
      readOnly: false,
    });

    // -------------------------------------------------------------------------
    // Fargate Service
    // -------------------------------------------------------------------------
    this.service = new ecs.FargateService(this, "Service", {
      cluster: props.container.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      // Rolling deploy as a clean handoff: at desiredCount=1, keeping 100%
      // healthy forces ECS to bring the new task to healthy BEFORE stopping
      // the old one, and 200% gives the headroom to run both at once. So a
      // deploy has no outage window. Circuit breaker still rolls back a bad
      // rollout.
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      circuitBreaker: { rollback: true },
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      assignPublicIp: false,
      enableExecuteCommand: true,
    });

    // Register with ALB target group
    this.service.attachToApplicationTargetGroup(props.targetGroup);

    // -------------------------------------------------------------------------
    // Tags
    // -------------------------------------------------------------------------
    cdk.Tags.of(this.service).add("Name", `isol8-${env}-backend`);
    cdk.Tags.of(this.service).add("Project", "isol8");
    cdk.Tags.of(this.service).add("Environment", env);
  }
}
