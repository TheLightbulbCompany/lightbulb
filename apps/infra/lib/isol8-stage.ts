import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { ApiStack } from "./stacks/api-stack";
import { AuthStack } from "./stacks/auth-stack";
import { ContainerStack } from "./stacks/container-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { DnsStack } from "./stacks/dns-stack";
import { NetworkStack } from "./stacks/network-stack";
import { ObservabilityStack } from "./stacks/observability-stack";
import { PaperclipStack } from "./stacks/paperclip-stack";
import { ServiceStack } from "./stacks/service-stack";

export interface Isol8StageProps extends cdk.StageProps {
  environment: string;
}

export class Isol8Stage extends cdk.Stage {
  constructor(scope: Construct, id: string, props: Isol8StageProps) {
    super(scope, id, props);

    const env = props.environment;

    const auth = new AuthStack(this, `isol8-${env}-auth`, {
      stackName: `isol8-${env}-auth`,
      environment: env,
    });

    const dns = new DnsStack(this, `isol8-${env}-dns`, {
      stackName: `isol8-${env}-dns`,
      environment: env,
    });

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
    });

    // ApiStack deploys BEFORE ServiceStack (no circular dependency)
    const api = new ApiStack(this, `isol8-${env}-api`, {
      stackName: `isol8-${env}-api`,
      environment: env,
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
    });

    // ServiceStack replaces ComputeStack
    const service = new ServiceStack(this, `isol8-${env}-service`, {
      stackName: `isol8-${env}-service`,
      environment: env,
      vpc: network.vpc,
      targetGroup: network.targetGroup,
      albSecurityGroup: network.albSecurityGroup,
      database: {
        usersTable: database.usersTable,
        containersTable: database.containersTable,
        billingTable: database.billingTable,
        apiKeysTable: database.apiKeysTable,
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
      // Pass secret names as plain strings to avoid cross-stack refs to AuthStack.
      // These names match the secretName used in AuthStack's createSecret helper.
      secretNames: {
        clerkIssuer: `isol8/${env}/clerk_issuer`,
        clerkSecretKey: `isol8/${env}/clerk_secret_key`,
        stripeSecretKey: `isol8/${env}/stripe_secret_key`,
        stripeWebhookSecret: `isol8/${env}/stripe_webhook_secret`,
        encryptionKey: `isol8/${env}/encryption_key`,
        posthogProjectApiKey: `isol8/${env}/posthog_project_api_key`,
        paperclipServiceTokenKey: auth.paperclipServiceTokenKey.secretName,
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
      },
      managementApiUrl: api.managementApiUrl,
      webSocketUrl: api.webSocketUrl,
      connectionsTableName: api.connectionsTableName,
      wsApiId: api.wsApiId,
      wsStage: api.wsStage,
    });

    // PaperclipStack — runs upstream `paperclipai/paperclip:latest` as a
    // single Fargate service. Reachable from FastAPI via Cloud Map at
    // `http://paperclip.isol8-${env}.local:3100/`. T6 wires the public
    // host route on the existing ALB; T14 wires the FastAPI proxy router.
    const paperclip = new PaperclipStack(this, `isol8-${env}-paperclip`, {
      stackName: `isol8-${env}-paperclip`,
      environment: env,
      vpc: network.vpc,
      cluster: container.cluster,
      cloudMapNamespace: container.cloudMapNamespace,
      paperclipDbCluster: database.paperclipDbCluster,
      paperclipDbSecurityGroup: database.paperclipDbSecurityGroup,
      paperclipBetterAuthSecretName: auth.paperclipBetterAuthSecret.secretName,
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
    new ObservabilityStack(this, `isol8-${env}-observability`, {
      stackName: `isol8-${env}-observability`,
      envName: env,
      backendService: service.service,
      backendLogGroupName: `/ecs/isol8-${env}`,
      alb: network.alb,
      wsApiId: api.wsApiId,
      cluster: container.cluster,
      efsFileSystem: container.efsFileSystem,
      databaseTables: {
        usersTable: database.usersTable,
        containersTable: database.containersTable,
        billingTable: database.billingTable,
        apiKeysTable: database.apiKeysTable,
        usageCountersTable: database.usageCountersTable,
        pendingUpdatesTable: database.pendingUpdatesTable,
        channelLinksTable: database.channelLinksTable,
      },
      connectionsTableName: api.connectionsTableName,
      authorizerFunctionName: `isol8-${env}-ws-authorizer`,
    });

    // --- Tags ---
    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", env);
  }
}
