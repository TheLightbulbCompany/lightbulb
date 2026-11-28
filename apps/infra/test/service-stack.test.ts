import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import { ContainerStack } from "../lib/stacks/container-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/** A real ServiceStack wired to real upstream stacks, as lightbulb-stage.ts wires it. */
function buildServiceStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");
  const network = new NetworkStack(app, `Network-${environment}`, { env, environment });
  const container = new ContainerStack(app, `Container-${environment}`, { env, environment, vpc: network.vpc });
  const service = new ServiceStack(app, `Service-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    targetGroup: network.targetGroup,
    albSecurityGroup: network.albSecurityGroup,
    secretNames: {
      clerkIssuer: `lightbulb/${environment}/clerk_issuer`,
      clerkSecretKey: `lightbulb/${environment}/clerk_secret_key`,
      clerkWebhookSecret: `lightbulb/${environment}/clerk_webhook_secret`,
      stripeSecretKey: `lightbulb/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `lightbulb/${environment}/stripe_webhook_secret`,
      posthogProjectApiKey: `lightbulb/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `lightbulb/${environment}/paperclip_service_token_key`,
      openaiApiKey: `lightbulb/${environment}/openai_api_key`,
    },
    kmsKeyArn: kmsKey.keyArn,
    cluster: container.cluster,
  });
  return Template.fromStack(service);
}

const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");
const SERVICE_TEMPLATE_PROD = buildServiceStack("prod");

function backendContainer(template: Template): any {
  return Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((td: any) => td.Properties.ContainerDefinitions)
    .find((c: any) => c.Name === "backend");
}

describe("ServiceStack — the Spark backend", () => {
  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, 1],
    ["prod", SERVICE_TEMPLATE_PROD, 2],
  ] as const)("%s runs one backend service (%s tasks) on a public IP behind the ALB", (_env, template, count) => {
    template.resourceCountIs("AWS::ECS::Service", 1);
    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: count,
      NetworkConfiguration: { AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "ENABLED" }) },
      LoadBalancers: [Match.objectLike({ ContainerPort: 8000 })],
    });
    template.resourceCountIs("AWS::Lambda::Function", 0);
    template.resourceCountIs("AWS::StepFunctions::StateMachine", 0);
  });

  test("the backend gets exactly the Spark secrets", () => {
    const names = backendContainer(SERVICE_TEMPLATE_DEV).Secrets.map((s: any) => s.Name).sort();
    expect(names).toEqual(
      [
        "CLERK_ISSUER",
        "CLERK_SECRET_KEY",
        "CLERK_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "PAPERCLIP_SERVICE_TOKEN_KEY",
        "POSTHOG_PROJECT_API_KEY",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
      ].sort(),
    );
  });

  test("the backend env carries the Spark settings", () => {
    const envNames = backendContainer(SERVICE_TEMPLATE_DEV).Environment.map((e: any) => e.Name);
    for (const name of [
      "PLATFORM_ADMIN_USER_IDS",
      "SPARK_INTERNAL_USER_IDS",
      "STRIPE_SPARK_PRICE_ID",
      "SPARK_TRANSCRIPTS_ENABLED",
      "SPARK_STORE_ENABLED",
      "SPARK_WORKLOG_SCOPE",
      "SPARK_MIN_VERSION_MAC",
      "SPARK_MIN_VERSION_WINDOWS",
      "WEBHOOK_DEDUP_TABLE",
      "PUBLIC_API_BASE",
    ]) {
      expect(envNames).toContain(name);
    }
  });

  // Clerk: "explicitly set authorizedParties", the origins allowed to mint session tokens. The Windows app signs in from
  // its own page, served at <scheme>://renderer, so that origin is one of ours (the Mac's native tokens carry no origin).
  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "spark-dev://renderer", "spark://renderer"],
    ["prod", SERVICE_TEMPLATE_PROD, "spark://renderer", "spark-dev://renderer"],
  ] as const)("%s accepts sign-in tokens minted by its own desktop app's origin, and not the other flavour's", (_env, template, ours, theirs) => {
    const env = backendContainer(template).Environment;
    const parties: string[] = env.find((e: any) => e.Name === "CLERK_AUTHORIZED_PARTIES").Value.split(",");
    expect(parties).toContain(ours);
    expect(parties).not.toContain(theirs);
  });

  // Production rejects a token minted by an origin off the list (Clerk's documented check); dev only logs, because
  // Vercel preview deployments sign in from per-PR origins that no fixed list can name.
  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "audit"],
    ["prod", SERVICE_TEMPLATE_PROD, "enforce"],
  ] as const)("%s checks the sign-in token's origin in %s mode", (_env, template, mode) => {
    const env = backendContainer(template).Environment;
    expect(env.find((e: any) => e.Name === "CLERK_AZP_MODE").Value).toBe(mode);
  });

  test("production accepts no localhost origin", () => {
    const env = backendContainer(SERVICE_TEMPLATE_PROD).Environment;
    expect(env.find((e: any) => e.Name === "CLERK_AUTHORIZED_PARTIES").Value).not.toMatch(/localhost|127\.0\.0\.1/);
  });

  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "1"],
    ["prod", SERVICE_TEMPLATE_PROD, "1"],
  ] as const)("%s backend sets SPARK_STORE_ENABLED=%s and no SPARK_STORE_DELETES", (_env, template, value) => {
    const env = backendContainer(template).Environment;
    expect(env).toContainEqual({ Name: "SPARK_STORE_ENABLED", Value: value });
    expect(env.map((e: any) => e.Name)).not.toContain("SPARK_STORE_DELETES");
  });

  // The autocomplete routes spend on the company key at every typing pause: on in dev, off (404) in production.
  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "1"],
    ["prod", SERVICE_TEMPLATE_PROD, "0"],
  ] as const)("%s backend sets SPARK_AUTOCOMPLETE_ENABLED=%s", (_env, template, value) => {
    expect(backendContainer(template).Environment).toContainEqual({ Name: "SPARK_AUTOCOMPLETE_ENABLED", Value: value });
  });

  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "512", "2048"],
    ["prod", SERVICE_TEMPLATE_PROD, "512", "1024"],
  ] as const)("%s task size is %s CPU / %s MiB", (_env, template, cpu, memory) => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", { Cpu: cpu, Memory: memory });
  });

  test("SparkStoreAccess: S3 put and delete on data/*, list limited to data/, nothing for Firehose, Athena or Glue", () => {
    const doc = Object.values(SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::ManagedPolicy")).find(
      (r: any) => r.Properties.ManagedPolicyName === "lightbulb-dev-backend-spark-store",
    ) as any;
    expect(doc).toBeDefined();
    expect(JSON.stringify(doc.Properties.Roles)).toContain("TaskRole");
    const stmts = doc.Properties.PolicyDocument.Statement as any[];
    expect(stmts).toEqual([
      expect.objectContaining({
        Action: ["s3:PutObject", "s3:DeleteObject"],
        Resource: "arn:aws:s3:::lightbulb-dev-spark-store/data/*",
      }),
      expect.objectContaining({
        Action: "s3:ListBucket",
        Resource: "arn:aws:s3:::lightbulb-dev-spark-store",
        Condition: { StringLike: { "s3:prefix": ["data/*"] } },
      }),
    ]);
  });

  test("the task role has no Firehose, Athena or Glue action anywhere", () => {
    const rendered = JSON.stringify(SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::Policy")) +
      JSON.stringify(SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::ManagedPolicy"));
    expect(rendered).not.toMatch(/"(firehose|athena|glue):/);
  });
});
