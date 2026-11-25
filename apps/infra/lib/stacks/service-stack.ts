import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { allTableArns, resourcePrefix, sparkStoreNames, tableName, tablePrefix } from "../table-names";
import { Construct } from "constructs";
import { DEV_SPOT } from "./container-stack";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../domains";

/**
 * Secret name strings (NOT ISecret objects) to avoid cross-stack dependency
 * cycles. ServiceStack imports them locally via fromSecretNameV2, which
 * prevents CDK from auto-granting KMS access on AuthStack's key.
 */
export interface SecretNames {
  clerkIssuer: string;
  clerkSecretKey: string;
  /** Svix signing secret for the Clerk webhook endpoint: the only auth on /webhooks/clerk. */
  clerkWebhookSecret: string;
  stripeSecretKey: string;
  stripeWebhookSecret: string;
  /** PostHog personal API key for the admin Spark metrics. Empty string stubs them gracefully. */
  posthogProjectApiKey: string;
  /** Platform key for Spark's GPT-Live voice sessions. */
  openaiApiKey: string;
  /** HMAC key for Spark's signed checkout/portal links (core/services/service_token.py). */
  paperclipServiceTokenKey: string;
}

export interface ServiceStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  /** Domain the support-ticket mail is sent from: the SES domain identity. Defaults to PLATFORM_MAIL_DOMAIN. */
  platformMailDomain?: string;
  vpc: ec2.IVpc;
  targetGroup: elbv2.IApplicationTargetGroup;
  albSecurityGroup: ec2.ISecurityGroup;
  // NOTE: this stack deliberately takes NO database table constructs. Every
  // table is reached by NAME (tableName() + the DynamoDbOwnedTables grant): a
  // construct would create a cross-stack Export, and CloudFormation cannot
  // change an exported value while anything imports it.
  /** Pass secret names (strings) to avoid cross-stack KMS auto-grant cycles. */
  secretNames: SecretNames;
  /** Pass as string ARN to avoid cross-stack dependency cycle. */
  kmsKeyArn: string;
  cluster: ecs.ICluster;
  /** Verified SES domain support tickets are sent under; empty sends nothing. */
  agentEmailDomain?: string;
}

export class ServiceStack extends cdk.Stack {
  public readonly service: ecs.FargateService;
  public readonly taskRole: iam.Role;

  constructor(scope: Construct, id: string, props: ServiceStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const platformMailDomain = props.platformMailDomain ?? PLATFORM_MAIL_DOMAIN;

    // -------------------------------------------------------------------------
    // Docker Image Asset (built & pushed by CDK Pipeline)
    // -------------------------------------------------------------------------
    const backendRuntimePlatform = {
      cpuArchitecture: ecs.CpuArchitecture.ARM64,
      operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
    };
    const backendImage = new DockerImageAsset(this, "BackendImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_ARM64,
    });

    // -------------------------------------------------------------------------
    // Service Security Group
    // -------------------------------------------------------------------------
    const serviceSg = new ec2.SecurityGroup(this, "ServiceSecurityGroup", {
      vpc: props.vpc,
      description: `Isol8 ${env} Fargate service security group`,
      allowAllOutbound: true,
    });
    serviceSg.addIngressRule(props.albSecurityGroup, ec2.Port.tcp(8000), "HTTP from ALB");

    // -------------------------------------------------------------------------
    // IAM Task Role
    // -------------------------------------------------------------------------
    this.taskRole = new iam.Role(this, "TaskRole", {
      roleName: `lightbulb-${env}-backend-task`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Fargate task role`,
    });

    // Tables by NAME, one wildcard pair over this env's prefix (no cross-stack export).
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbOwnedTables",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:ConditionCheckItem",
          "dynamodb:TransactWriteItems",
          "dynamodb:DescribeTable",
        ],
        resources: allTableArns(this.region, this.account, env),
      }),
    );

    // The stage CMK encrypts the DynamoDB tables, the secrets and the Spark store.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsAccess",
        actions: ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
        resources: [props.kmsKeyArn],
      }),
    );

    // Spark data store. Everything is named from sparkStoreNames, not imported from
    // SparkStoreStack, so this stack never waits on it (the streams do not exist yet; IAM
    // accepts ARNs of absent resources). A ManagedPolicy for the same packing reason as
    // the other task-role policies. KMS is in KmsAccess above.
    const spark = sparkStoreNames(env);
    const glueArn = (path: string) => `arn:aws:glue:${this.region}:${this.account}:${path}`;
    new iam.ManagedPolicy(this, "SparkStoreAccess", {
      managedPolicyName: `${resourcePrefix(env)}backend-spark-store`,
      statements: [
        new iam.PolicyStatement({
          sid: "SparkStorePut",
          actions: ["firehose:PutRecordBatch"],
          resources: spark.streams.map((s) => `arn:aws:firehose:${this.region}:${this.account}:deliverystream/${s}`),
        }),
        // The app workgroup only, never adhoc.
        new iam.PolicyStatement({
          sid: "SparkStoreAthena",
          actions: [
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:StopQueryExecution",
          ],
          resources: [`arn:aws:athena:${this.region}:${this.account}:workgroup/${spark.workgroup}`],
        }),
        new iam.PolicyStatement({
          sid: "SparkStoreGlue",
          actions: ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions", "glue:UpdateTable"],
          resources: [
            glueArn("catalog"),
            glueArn(`database/${spark.database}`),
            glueArn(`table/${spark.database}/*`),
          ],
        }),
        // Objects only under the prefixes the backend uses: never athena-results/adhoc/ (the team's results).
        new iam.PolicyStatement({
          sid: "SparkStoreS3Objects",
          actions: [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
          ],
          resources: ["warehouse/*", "athena-results/app/*", "errors/*", "jobs/*"].map(
            (p) => `arn:aws:s3:::${spark.bucket}/${p}`,
          ),
        }),
        new iam.PolicyStatement({
          sid: "SparkStoreS3Bucket",
          actions: ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
          resources: [`arn:aws:s3:::${spark.bucket}`],
        }),
      ],
      roles: [this.taskRole],
    });

    // Support tickets go out through SES (core/services/ses_email.py) under the mail domain's
    // identity, which only exists where the prod SES stack created it. Sends are gated on
    // AGENT_EMAIL_DOMAIN being set.
    new iam.ManagedPolicy(this, "BackendSesSend", {
      managedPolicyName: `lightbulb-${env}-backend-ses-send`,
      roles: [this.taskRole],
      statements: [
        new iam.PolicyStatement({
          sid: "AgentMailSesSend",
          actions: ["ses:SendEmail", "ses:SendRawEmail"],
          resources: [`arn:aws:ses:${this.region}:${this.account}:identity/${platformMailDomain}`],
        }),
      ],
    });

    // -------------------------------------------------------------------------
    // Task Execution Role
    // -------------------------------------------------------------------------
    const taskExecutionRole = new iam.Role(this, "TaskExecutionRole", {
      roleName: `lightbulb-${env}-backend-task-execution`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Fargate task execution role`,
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
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/*`,
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
      logGroupName: `/ecs/lightbulb-${env}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // -------------------------------------------------------------------------
    // Fargate Task Definition
    // -------------------------------------------------------------------------
    const backendContainerEnvironment: Record<string, string> = {
      ENVIRONMENT: env,
      AWS_REGION: "us-east-1",
      // Platform admin (/admin, /ops) is this reviewed list of Clerk USER IDS per env, never an
      // email domain. Empty => nobody is admin. Comma-separated. Ids are per Clerk instance.
      PLATFORM_ADMIN_USER_IDS:
        env === "prod"
          ? [
              "user_3IROHYIZPPS1G7ISXhjsnnKBEMX",
              "user_3HmnXQdFUk2pP2Zjw4HEGkefcNM",
              "user_3J8t0Yp76auS131Lk9ov9YCc5We",
              "user_3I8s15Ex1b9xin0keAJTMcoipm2",
              "user_3H3vhcZzM4vaNCe17zs0XXIrTMW",
            ].join(",")
          : [
              "user_3I4REo0WJ0GoGqQFN6gmKAN4Hqs",
              "user_3JISO0QYSpiaupJII105uDWMApg",
              "user_3IEMUT2a1z8za7m4lnFzpdZrccc",
            ].join(","),
      WEBHOOK_DEDUP_TABLE: tableName(env, "webhook-event-dedup"),
      CORS_ORIGINS:
        env === "prod"
          ? `https://${rootDomain},https://www.${rootDomain}`
          : `https://dev.${rootDomain}`,
      // SEC-8 token-origin fence: Clerk session tokens carry no `aud`, so the backend validates
      // the `azp` claim against this allowlist. Broader than CORS_ORIGINS because azp reflects the
      // MINT origin. Native tokens carry no azp and always pass. Ships in audit mode.
      CLERK_AUTHORIZED_PARTIES:
        env === "prod"
          ? `https://${rootDomain},https://www.${rootDomain},https://admin.${rootDomain}`
          : `https://dev.${rootDomain},https://admin.dev.${rootDomain},http://localhost:3000`,
      CLERK_AZP_MODE: "audit",
      FRONTEND_URL:
        env === "prod" ? `https://${rootDomain}` : `https://dev.${rootDomain}`,
      // Public-facing base URL of THIS API (Spark's billing links).
      PUBLIC_API_BASE:
        env === "prod" ? `https://api.${rootDomain}` : `https://api-${env}.${rootDomain}`,
      DYNAMODB_TABLE_PREFIX: tablePrefix(env),
      // Spark's $20/month subscription (the Mac app's person-keyed plan).
      STRIPE_SPARK_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_SPARK_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_SPARK_PRICE_ID_DEV ?? ""),
      // Save Spark voice transcripts (text only, 90 days) in every env; "0" turns it off.
      SPARK_TRANSCRIPTS_ENABLED: "1",
      // Spark data store (S3/Iceberg). "0" in prod until the store is verified there.
      SPARK_STORE_ENABLED: env === "prod" ? "0" : "1",
      // Work-log upload scope: "problem" keeps text only for failed or cancelled tasks.
      SPARK_WORKLOG_SCOPE: "problem",
      // PostHog server-side reads for the admin Spark metrics. Project ID is public.
      POSTHOG_HOST: "https://us.posthog.com",
      POSTHOG_PROJECT_ID: "380894",
      // PostHog project (phc_) INGESTION key for server-side capture. Write-only and public (the
      // same key the frontend ships), so it lives in the plain env block, NOT secrets.
      POSTHOG_PROJECT_WRITE_KEY: "phc_nzqnGGPwHfrKycptH3TViSAo3uaxT8CHAXovvHnBGEma",
      // The verified SES domain support tickets are sent under; empty sends nothing.
      AGENT_EMAIL_DOMAIN: props.agentEmailDomain ?? "",
      EMAIL_SUPPORT_ADDRESS: `support@${platformMailDomain}`,
      EMAIL_FROM_TICKETS: `Lightbulb Support <tickets@${platformMailDomain}>`,
    };

    const secret = (envName: string, id: string, name: string) =>
      [envName, ecs.Secret.fromSecretsManager(secretsmanager.Secret.fromSecretNameV2(this, id, name))] as const;
    const backendContainerSecrets: Record<string, ecs.Secret> = Object.fromEntries([
      // Imported by name (NOT cross-stack ISecret) to avoid CDK auto-granting KMS decrypt on
      // AuthStack's key, which causes a circular dependency.
      secret("CLERK_ISSUER", "ImportClerkIssuer", props.secretNames.clerkIssuer),
      secret("CLERK_SECRET_KEY", "ImportClerkSecretKey", props.secretNames.clerkSecretKey),
      secret("CLERK_WEBHOOK_SECRET", "ImportClerkWebhookSecret", props.secretNames.clerkWebhookSecret),
      secret("STRIPE_SECRET_KEY", "ImportStripeSecretKey", props.secretNames.stripeSecretKey),
      secret("STRIPE_WEBHOOK_SECRET", "ImportStripeWebhookSecret", props.secretNames.stripeWebhookSecret),
      secret("POSTHOG_PROJECT_API_KEY", "ImportPosthogProjectApiKey", props.secretNames.posthogProjectApiKey),
      secret("PAPERCLIP_SERVICE_TOKEN_KEY", "ImportPaperclipServiceTokenKey", props.secretNames.paperclipServiceTokenKey),
      secret("OPENAI_API_KEY", "ImportOpenaiApiKey", props.secretNames.openaiApiKey),
    ]);

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      family: `lightbulb-${env}-backend`,
      runtimePlatform: backendRuntimePlatform,
      cpu: env === "prod" ? 1024 : 512,
      memoryLimitMiB: 2048,
      taskRole: this.taskRole,
      executionRole: taskExecutionRole,
    });

    taskDef.addContainer("backend", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      portMappings: [{ containerPort: 8000 }],
      environment: backendContainerEnvironment,
      secrets: backendContainerSecrets,
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
      // SIGTERM->SIGKILL window for graceful shutdown: app asyncio budget (20s) <
      // uvicorn --timeout-graceful-shutdown (25s) < this stopTimeout (30s).
      stopTimeout: cdk.Duration.seconds(30),
    });

    // -------------------------------------------------------------------------
    // Fargate Service
    // -------------------------------------------------------------------------
    // No NAT gateway on this path: the tasks reach the internet on their own public IP;
    // serviceSg admits only the ALB.
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `lightbulb-${env}-backend`,
      cluster: props.cluster,
      taskDefinition: taskDef,
      // Dev runs one task on Spot. With one task, min 100 / max 200 still starts the new task
      // before stopping the old one.
      desiredCount: env === "prod" ? 2 : 1,
      ...(env === "prod" ? {} : { capacityProviderStrategies: DEV_SPOT }),
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      circuitBreaker: { rollback: true },
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      assignPublicIp: true,
      securityGroups: [serviceSg],
      enableExecuteCommand: true,
    });

    // Register with ALB target group
    this.service.attachToApplicationTargetGroup(props.targetGroup);

    this.service
      .autoScaleTaskCount({ minCapacity: env === "prod" ? 2 : 1, maxCapacity: 4 })
      .scaleOnCpuUtilization("Cpu", { targetUtilizationPercent: 60 });

    // -------------------------------------------------------------------------
    // Tags
    // -------------------------------------------------------------------------
    cdk.Tags.of(this.service).add("Name", `lightbulb-${env}-backend`);
    cdk.Tags.of(this.service).add("Project", "lightbulb");
    cdk.Tags.of(this.service).add("Environment", env);
  }
}
