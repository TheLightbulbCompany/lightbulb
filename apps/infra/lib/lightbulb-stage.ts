import * as cdk from "aws-cdk-lib";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct, IConstruct } from "constructs";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "./domains";
import { ApiStack } from "./stacks/api-stack";
import { AuthStack } from "./stacks/auth-stack";
import { ContainerStack } from "./stacks/container-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { DnsStack } from "./stacks/dns-stack";
import { NetworkStack } from "./stacks/network-stack";
import { ObservabilityStack } from "./stacks/observability-stack";
import { ServiceStack } from "./stacks/service-stack";
import { SparkStoreStack } from "./stacks/spark-store-stack";
import { tableName } from "./table-names";

export interface LightbulbStageProps extends cdk.StageProps {
  environment: string;
  /**
   * Product root domain — every frontend and API hostname in the stage derives from it. Per-stage so
   * one environment can move independently of the other. Defaults to
   * ROOT_DOMAIN (see lib/domains.ts).
   */
  rootDomain?: string;
  /**
   * Domain the platform sender + agent mail live on. Defaults to `rootDomain`,
   * which is where mail lives today; it is a separate parameter so mail can be
   * moved to its own domain without touching the product hostnames.
   */
  platformMailDomain?: string;
}

/** Caps every log group's retention (unset = forever) at dev 14 / prod 30 days. Cost review
 *  2026-10-27: no alarm reads logs older than that. */
class CapLogRetention implements cdk.IAspect {
  constructor(private readonly maxDays: number) {}
  visit(node: IConstruct): void {
    if (node instanceof logs.CfnLogGroup && !((node.retentionInDays ?? Infinity) <= this.maxDays)) {
      node.retentionInDays = this.maxDays;
    }
  }
}

export class LightbulbStage extends cdk.Stage {
  constructor(scope: Construct, id: string, props: LightbulbStageProps) {
    super(scope, id, props);

    const env = props.environment;
    cdk.Aspects.of(this).add(new CapLogRetention(env === "prod" ? 30 : 14));
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const platformMailDomain =
      props.platformMailDomain ?? props.rootDomain ?? PLATFORM_MAIL_DOMAIN;

    const auth = new AuthStack(this, `lightbulb-${env}-auth`, {
      stackName: `lightbulb-${env}-auth`,
      environment: env,
    });

    const dns = new DnsStack(this, `lightbulb-${env}-dns`, {
      stackName: `lightbulb-${env}-dns`,
      environment: env,
      rootDomain,
    });

    // ServiceStack grants by name (sparkStoreNames), so it does not wait on this stack; the dev
    // frontend deploy and prod promotion do.
    new SparkStoreStack(this, `lightbulb-${env}-spark-store`, {
      stackName: `lightbulb-${env}-spark-store`,
      environment: env,
      kmsKey: auth.kmsKey,
    });

    const network = new NetworkStack(this, `lightbulb-${env}-network`, {
      stackName: `lightbulb-${env}-network`,
      environment: env,
    });

    const database = new DatabaseStack(this, `lightbulb-${env}-database`, {
      stackName: `lightbulb-${env}-database`,
      environment: env,
      kmsKey: auth.kmsKey,
    });

    const container = new ContainerStack(this, `lightbulb-${env}-container`, {
      stackName: `lightbulb-${env}-container`,
      environment: env,
      vpc: network.vpc,
    });

    const service = new ServiceStack(this, `lightbulb-${env}-service`, {
      stackName: `lightbulb-${env}-service`,
      environment: env,
      rootDomain,
      vpc: network.vpc,
      targetGroup: network.targetGroup,
      albSecurityGroup: network.albSecurityGroup,
      // Pass secret names as plain strings to avoid cross-stack refs to AuthStack.
      // These names match the secretName used in AuthStack's createSecret helper.
      secretNames: {
        clerkIssuer: `lightbulb/${env}/clerk_issuer`,
        clerkSecretKey: `lightbulb/${env}/clerk_secret_key`,
        clerkWebhookSecret: `lightbulb/${env}/clerk_webhook_secret`,
        stripeSecretKey: `lightbulb/${env}/stripe_secret_key`,
        stripeWebhookSecret: `lightbulb/${env}/stripe_webhook_secret`,
        posthogProjectApiKey: `lightbulb/${env}/posthog_project_api_key`,
        openaiApiKey: `lightbulb/${env}/openai_api_key`,
        paperclipServiceTokenKey: auth.paperclipServiceTokenKey.secretName,
      },
      kmsKeyArn: auth.kmsKey.keyArn,
      cluster: container.cluster,
    });

    // EXPLICIT, and load-bearing. The service stack used to take a `database`
    // prop, which gave CDK an implicit ordering. Decoupling the exports removed
    // the prop AND that ordering — but the backend still reads every table, now
    // BY NAME, so it must not start before the database stack has created them.
    // Without this the generated workflow drops database from service's `needs`.
    service.addDependency(database);

    // ObservabilityStack — alarms, dashboard, canaries, account hardening
    const observability = new ObservabilityStack(
      this,
      `lightbulb-${env}-observability`,
      {
        stackName: `lightbulb-${env}-observability`,
        envName: env,
        rootDomain,
        platformMailDomain,
        backendService: service.service,
        backendLogGroupName: `/ecs/lightbulb-${env}`,
        alb: network.alb,
        targetGroup: network.targetGroup,
        cluster: container.cluster,
        databaseTables: {
          usersTable: tableName(env, "users"),
          containersTable: tableName(env, "containers"),
          sparkTranscriptsTable: tableName(env, "spark-transcripts"),
        },
      },
    );
    observability.addDependency(database);

    new ApiStack(this, `lightbulb-${env}-api`, {
      stackName: `lightbulb-${env}-api`,
      environment: env,
      rootDomain,
      vpc: network.vpc,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
      albHttpListenerArn: network.albHttpListenerArn,
      albSecurityGroup: network.albSecurityGroup,
    });

    // --- Tags ---
    cdk.Tags.of(this).add("Project", "lightbulb");
    cdk.Tags.of(this).add("Environment", env);
  }


}
