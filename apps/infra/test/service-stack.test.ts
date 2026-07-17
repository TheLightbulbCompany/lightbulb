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
      oauthTokensTable: database.oauthTokensTable,
      webhookDedupTable: database.webhookDedupTable,
      paperclipCompaniesTable: database.paperclipCompaniesTable,
      marketplaceListingsTable: database.marketplaceListingsTable,
      marketplacePurchasesTable: database.marketplacePurchasesTable,
      marketplacePayoutAccountsTable: database.marketplacePayoutAccountsTable,
      marketplaceTakedownsTable: database.marketplaceTakedownsTable,
      marketplaceSellersTable: database.marketplaceSellersTable,
      hookEventsTable: database.hookEventsTable,
      opsCacheTable: database.opsCacheTable,
    },
    secretNames: {
      clerkIssuer: `isol8/${environment}/clerk_issuer`,
      clerkSecretKey: `isol8/${environment}/clerk_secret_key`,
      stripeSecretKey: `isol8/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `isol8/${environment}/stripe_webhook_secret`,
      encryptionKey: `isol8/${environment}/encryption_key`,
      posthogProjectApiKey: `isol8/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `isol8/${environment}/paperclip_service_token_key`,
      platformOpsTokenKey: `isol8/${environment}/platform_ops_token_key`,
      gogKeyringPassword: `isol8/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
      resendApiKey: `isol8/${environment}/resend_api_key`,
      wsInternalSecret: `isol8/${environment}/ws_internal_secret`,
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

  // Account-wipe tool (Task 5): the backend needs to delete the per-owner
  // Bedrock role once its policies are detached. Scoped to the same
  // owner-bedrock-* role-name pattern as the rest of OwnerBedrockRoleManage —
  // never a bare "*", and never reaching the shared base managed policy.
  test("backend role can delete per-owner bedrock roles, scoped to the owner-bedrock-* role pattern", () => {
    // OwnerBedrockRoleManage has a single-element `resources` array, so CDK
    // synthesizes it as a bare Resource string (not a list) — unlike the
    // multi-resource IamPassRole statement asserted with arrayWith() above.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "OwnerBedrockRoleManage",
            Action: Match.arrayWith(["iam:DeleteRole"]),
            Resource: "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-*",
          }),
        ]),
      }),
    });
  });

  // Account-wipe (Task 7 fix): the in-backend teardown deletes the owner's
  // Paperclip Better Auth "user" row via the RDS Data API, which needs three
  // grants the task role otherwise lacks. These live in the inline default
  // policy (AWS::IAM::Policy) via addToPolicy — unlike the bedrock-role-delete
  // grant, which lives in the dedicated ManagedPolicy. Assertions are
  // non-vacuous: each asserts the action IS present AND the resource is the
  // expected scoped ARN (not a bare "*"), except ListSecrets which IS "*".
  test("backend role can run the Paperclip Aurora-user delete (rds-data:ExecuteStatement scoped to the paperclip cluster)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipePaperclipAuroraExec",
            Action: "rds-data:ExecuteStatement",
            Resource:
              "arn:aws:rds:us-east-1:877352799272:cluster:isol8-dev-paperclip-db",
          }),
        ]),
      }),
    });
  });

  test("backend role can read the dash-named paperclip-db-credentials secret (GetSecretValue scoped, not the slash-named wildcard)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipePaperclipDbSecretRead",
            Action: "secretsmanager:GetSecretValue",
            Resource:
              "arn:aws:secretsmanager:us-east-1:877352799272:secret:isol8-dev-paperclip-db-credentials-*",
          }),
        ]),
      }),
    });
  });

  test("backend role can list secrets (metadata-only, resource '*' — the paginated ARN discovery)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipeListSecrets",
            Action: "secretsmanager:ListSecrets",
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("backend role can read the bedrock-gate URL SSM parameter (single-parameter GetParameter, not a wildcard)", () => {
    // The gate stack publishes each env's Function URL to
    // /isol8/{env}/bedrock-gate-url; the backend discovers it at runtime and
    // the parameter doubles as the break-glass lever ("direct" = bypass gate).
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "BedrockGateUrlParamRead",
            Action: "ssm:GetParameter",
            Resource:
              "arn:aws:ssm:us-east-1:877352799272:parameter/isol8/dev/bedrock-gate-url",
          }),
        ]),
      }),
    });
  });

});

describe("ServiceStack — Metronome lifecycle env split", () => {
  test("dev runs the lifecycle live against the sandbox rate card", () => {
    // Dev's runbook is done (sandbox token + rate card + webhook secret), so
    // the lifecycle switch is on and the contract references the sandbox
    // rate card created 2026-06-10.
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "METRONOME_LIFECYCLE_ENABLED",
              Value: "true",
            }),
            Match.objectLike({
              Name: "METRONOME_RATE_CARD_ID",
              Value: "99576f5a-37d2-4983-ba64-fea9bad96e08",
            }),
          ]),
        }),
      ]),
    });
  });

  test("prod runs the lifecycle live against the prod rate card", () => {
    // Prod's runbook completed 2026-06-12: prod token in Secrets Manager,
    // rate card + credits product created via API (rates verified 12/12
    // against sandbox), Stripe live product wired, webhook secret set.
    const prodTemplate = buildServiceStack("prod");
    prodTemplate.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "METRONOME_LIFECYCLE_ENABLED",
              Value: "true",
            }),
            Match.objectLike({
              Name: "METRONOME_RATE_CARD_ID",
              Value: "fdac2edd-f207-4f11-b65b-01fad6fbbdbc",
            }),
            Match.objectLike({
              Name: "METRONOME_CREDITS_PRODUCT_ID",
              Value: "876e1694-9d76-4c37-9814-c957dbd9370f",
            }),
          ]),
        }),
      ]),
    });
  });
});
