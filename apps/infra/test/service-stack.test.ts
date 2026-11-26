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
      "WEBHOOK_DEDUP_TABLE",
      "PUBLIC_API_BASE",
    ]) {
      expect(envNames).toContain(name);
    }
  });

  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "1"],
    ["prod", SERVICE_TEMPLATE_PROD, "1"],
  ] as const)("%s backend sets SPARK_STORE_ENABLED=%s and no SPARK_STORE_DELETES", (_env, template, value) => {
    const env = backendContainer(template).Environment;
    expect(env).toContainEqual({ Name: "SPARK_STORE_ENABLED", Value: value });
    expect(env.map((e: any) => e.Name)).not.toContain("SPARK_STORE_DELETES");
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
