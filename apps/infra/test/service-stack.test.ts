import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { ContainerStack } from "../lib/stacks/container-stack";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/**
 * Builds a real ServiceStack wired to real upstream stacks (Network, Database,
 * Container). Mirrors the wiring in isol8-stage.ts (and marketplace-resources.test.ts)
 * so the synthesized template matches what gets deployed.
 */
function buildServiceStack(environment: "dev" | "prod"): Template {
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
    vpc: network.vpc,
  });

  const container = new ContainerStack(app, `Container-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    kmsKeyArn: kmsKey.keyArn,
    gogKeyringPasswordSecretName: `isol8/${environment}/gog-keyring-password`,
  });

  const service = new ServiceStack(app, `Service-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    targetGroup: network.targetGroup,
    albSecurityGroup: network.albSecurityGroup,
    database: {
      usersTable: database.usersTable,
      containersTable: database.containersTable,
      billingTable: database.billingTable,
      usageCountersTable: database.usageCountersTable,
      pendingUpdatesTable: database.pendingUpdatesTable,
      channelLinksTable: database.channelLinksTable,
      adminActionsTable: database.adminActionsTable,
      creditsTable: database.creditsTable,
      creditTransactionsTable: database.creditTransactionsTable,
      oauthTokensTable: database.oauthTokensTable,
      webhookDedupTable: database.webhookDedupTable,
      paperclipCompaniesTable: database.paperclipCompaniesTable,
      marketplaceListingsTable: database.marketplaceListingsTable,
      marketplacePurchasesTable: database.marketplacePurchasesTable,
      marketplacePayoutAccountsTable: database.marketplacePayoutAccountsTable,
      marketplaceTakedownsTable: database.marketplaceTakedownsTable,
      marketplaceSellersTable: database.marketplaceSellersTable,
      hookEventsTable: database.hookEventsTable,
    },
    secretNames: {
      clerkIssuer: `isol8/${environment}/clerk_issuer`,
      clerkSecretKey: `isol8/${environment}/clerk_secret_key`,
      stripeSecretKey: `isol8/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `isol8/${environment}/stripe_webhook_secret`,
      encryptionKey: `isol8/${environment}/encryption_key`,
      posthogProjectApiKey: `isol8/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `isol8/${environment}/paperclip_service_token_key`,
      gogKeyringPassword: `isol8/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
    },
    kmsKeyArn: kmsKey.keyArn,
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
    managementApiUrl: "https://example.execute-api.us-east-1.amazonaws.com",
    webSocketUrl: "wss://example.execute-api.us-east-1.amazonaws.com",
    connectionsTableName: `isol8-${environment}-ws-connections`,
    wsApiId: "test-ws-api-id",
    wsStage: "prod",
  });

  return Template.fromStack(service);
}

// Hoist a single ServiceStack synthesis and reuse across tests.
const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");

describe("ServiceStack — clean handoff / rolling deploy", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("backend service uses rolling deploy that starts new before stopping old", () => {
    // At desiredCount=1, MinimumHealthyPercent=100 forces ECS to keep the old
    // task healthy until the new task is healthy (MaximumPercent=200 gives it
    // the headroom to run both at once), so a deploy is a clean handoff with no
    // outage window. Circuit breaker stays on so a bad rollout auto-rolls back.
    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: {
        MinimumHealthyPercent: 100,
        MaximumPercent: 200,
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
      },
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

  test("backend role can pass per-owner bedrock roles to ECS", () => {
    // The owner-bedrock prefix ARN is built from ${this.account}; in this
    // concrete-account synth CDK renders it as a literal string (it becomes an
    // Fn::Join only when the account is an unresolved token). The PassRole
    // statement is also PassedToService-conditioned to ecs-tasks.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "iam:PassedToService": "ecs-tasks.amazonaws.com",
              }),
            }),
            Resource: Match.arrayWith([
              "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-*",
            ]),
          }),
        ]),
      }),
    });
  });

  // The per-owner role-management + Bedrock-control-plane grants live in a
  // dedicated ManagedPolicy (isol8-dev-backend-owner-bedrock-manage), NOT the
  // inline default policy, so the inline policy stays under CloudFormation's
  // 10 KB limit and does not spill other statements into an overflow policy.
  test("backend role can create boundary-bounded per-owner roles", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
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
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:CreateInferenceProfile"]),
            Resource: "*",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "aws:RequestTag/ManagedBy": "isol8-backend",
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend role can act on its own Bedrock profiles, scoped by the ManagedBy resource tag", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:DeleteInferenceProfile"]),
            Resource: "*",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "aws:ResourceTag/ManagedBy": "isol8-backend",
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend container has the BEDROCK_OWNER_ROLE_PREFIX env var", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "BEDROCK_OWNER_ROLE_PREFIX",
              Value: "isol8-dev-owner-bedrock",
            }),
          ]),
        }),
      ]),
    });
  });
});
