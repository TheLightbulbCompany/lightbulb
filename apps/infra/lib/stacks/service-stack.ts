import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";

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
    apiKeysTable: dynamodb.Table;
    usageCountersTable: dynamodb.Table;
    pendingUpdatesTable: dynamodb.Table;
    channelLinksTable: dynamodb.Table;
    adminActionsTable: dynamodb.Table;
    creditsTable: dynamodb.Table;
    creditTransactionsTable: dynamodb.Table;
    oauthTokensTable: dynamodb.Table;
    webhookDedupTable: dynamodb.Table;
    paperclipCompaniesTable: dynamodb.Table;
    marketplaceListingsTable: dynamodb.Table;
    marketplacePurchasesTable: dynamodb.Table;
    marketplacePayoutAccountsTable: dynamodb.Table;
    marketplaceTakedownsTable: dynamodb.Table;
    sellerUsernamesTable: dynamodb.Table;
    hookEventsTable: dynamodb.Table;
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
  };
  managementApiUrl: string;
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
    // ECR Repository
    // -------------------------------------------------------------------------
    const repository = new ecr.Repository(this, "BackendRepo", {
      repositoryName: `isol8-${env}-backend`,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: env !== "prod",
      lifecycleRules: [
        {
          maxImageCount: 10,
          description: "Keep last 10 images",
        },
      ],
    });

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

    // ECR pull
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcrAccess",
        actions: [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
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

    // IAM PassRole for ECS task roles (both execution role and task role)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IamPassRole",
        actions: ["iam:PassRole"],
        resources: [
          props.container.taskExecutionRole.roleArn,
          props.container.taskRole.roleArn,
        ],
      }),
    );

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
    // re-running fails with "user already exists"). Codex P1 on PR #504.
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

    // DynamoDB tables
    props.database.usersTable.grantReadWriteData(this.taskRole);
    props.database.containersTable.grantReadWriteData(this.taskRole);
    props.database.billingTable.grantReadWriteData(this.taskRole);
    props.database.apiKeysTable.grantReadWriteData(this.taskRole);
    props.database.usageCountersTable.grantReadWriteData(this.taskRole);
    props.database.pendingUpdatesTable.grantReadWriteData(this.taskRole);
    props.database.channelLinksTable.grantReadWriteData(this.taskRole);
    props.database.adminActionsTable.grantReadWriteData(this.taskRole);
    props.database.creditsTable.grantReadWriteData(this.taskRole);
    props.database.creditTransactionsTable.grantReadWriteData(this.taskRole);
    props.database.oauthTokensTable.grantReadWriteData(this.taskRole);
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
    // Seller-username reservations -- the backend writes via conditional
    // PutItem (claim flow) and reads to display handle state on the
    // settings + publish wizard pages. See ``core/services/seller_identity.py``.
    props.database.sellerUsernamesTable.grantReadWriteData(this.taskRole);

    // OpenClaw webhook ingress audit log (T4 of openclaw-hook-token-provisioning).
    // T5 repo writes one row per delivery; T6 proxy + admin views read.
    // KMS encrypt/decrypt is already covered by the broader KmsAccess
    // statement above (kmsKeyArn is the same CMK the table uses).
    props.database.hookEventsTable.grantReadWriteData(this.taskRole);

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
    // (apps/backend/core/services/cloudwatch_logs.py, Phase B). Scoped to
    // the backend's own log group ARN; never "*" — least privilege per
    // CEO review (#351, Phase A Task 2).
    //
    // Log group name matches service-stack.ts:519 + isol8-stage.ts:113 +
    // local-stage.ts:116 — `/ecs/isol8-${env}` (no /aws prefix, no
    // -backend suffix). The Phase B cloudwatch_logs service must use this
    // same name when constructing FilterLogEvents calls.
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
        WS_MANAGEMENT_API_URL: props.managementApiUrl,
        WS_CONNECTIONS_TABLE: props.connectionsTableName,
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
        // Public Paperclip ("Teams") hostname. Routed through Vercel
        // (frontend project has a host-conditional rewrite to this same
        // backend's /__paperclip_proxy__ path). The backend uses this
        // value for cookie-domain rewrites on Better Auth Set-Cookie
        // responses and for setting the proxy session cookie.
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
        // ECS_TASK_DEFINITION removed (#410). Backend now reads the latest
        // CDK base revision live via describe_task_definition(family) — the
        // base family is uncontaminated because per-user clones go to a
        // separate `<base>-user` family.
        ECS_SUBNETS: privateSubnetIds,
        ECS_SECURITY_GROUP_ID:
          props.container.containerSecurityGroup.securityGroupId,
        CLOUD_MAP_NAMESPACE_ID:
          props.container.cloudMapNamespace.namespaceId,
        CLOUD_MAP_SERVICE_ID: props.container.cloudMapService.serviceId,
        CLOUD_MAP_SERVICE_ARN: props.container.cloudMapService.serviceArn,
        DYNAMODB_TABLE_PREFIX: `isol8-${env}-`,
        CREDITS_TABLE: props.database.creditsTable.tableName,
        CREDIT_TRANSACTIONS_TABLE:
          props.database.creditTransactionsTable.tableName,
        OAUTH_TOKENS_TABLE: props.database.oauthTokensTable.tableName,
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
        SELLER_USERNAMES_TABLE:
          props.database.sellerUsernamesTable.tableName,
        MARKETPLACE_ARTIFACTS_BUCKET: marketplaceArtifactsBucket.bucketName,
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
