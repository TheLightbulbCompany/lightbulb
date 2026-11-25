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
    agentEmailDomain: environment === "prod" ? "thelightbulb.company" : "dev.thelightbulb.company",
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

  test("the backend env carries the Spark and support-mail settings", () => {
    const envNames = backendContainer(SERVICE_TEMPLATE_DEV).Environment.map((e: any) => e.Name);
    for (const name of [
      "PLATFORM_ADMIN_USER_IDS",
      "STRIPE_SPARK_PRICE_ID",
      "SPARK_TRANSCRIPTS_ENABLED",
      "SPARK_STORE_ENABLED",
      "SPARK_STORE_DELETES",
      "SPARK_WORKLOG_SCOPE",
      "WEBHOOK_DEDUP_TABLE",
      "AGENT_EMAIL_DOMAIN",
      "EMAIL_FROM_TICKETS",
      "PUBLIC_API_BASE",
    ]) {
      expect(envNames).toContain(name);
    }
  });

  test.each([
    ["dev", SERVICE_TEMPLATE_DEV, "1"],
    ["prod", SERVICE_TEMPLATE_PROD, "0"],
  ] as const)("%s backend sets SPARK_STORE_DELETES=%s", (_env, template, value) => {
    expect(backendContainer(template).Environment).toContainEqual({ Name: "SPARK_STORE_DELETES", Value: value });
  });

  test("SES send is scoped to the mail domain identity", () => {
    SERVICE_TEMPLATE_PROD.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-prod-backend-ses-send",
      PolicyDocument: {
        Statement: [
          Match.objectLike({
            Action: ["ses:SendEmail", "ses:SendRawEmail"],
            Resource: "arn:aws:ses:us-east-1:877352799272:identity/thelightbulb.company",
          }),
        ],
      },
    });
  });

  test("SparkStoreAccess: Firehose put only on the three named streams, Athena only on the app workgroup", () => {
    const arn = (p: string) => `arn:aws:${p}`;
    const doc = Object.values(SERVICE_TEMPLATE_DEV.findResources("AWS::IAM::ManagedPolicy")).find(
      (r: any) => r.Properties.ManagedPolicyName === "lightbulb-dev-backend-spark-store",
    ) as any;
    expect(doc).toBeDefined();
    const byAction = (action: string) =>
      doc.Properties.PolicyDocument.Statement.filter((st: any) => (Array.isArray(st.Action) ? st.Action : [st.Action]).includes(action));
    const flat = (st: any[]) => st.flatMap((x) => (Array.isArray(x.Resource) ? x.Resource : [x.Resource]));
    expect(flat(byAction("firehose:PutRecordBatch"))).toEqual(
      ["turns", "steps", "events"].map((s) =>
        arn(`firehose:us-east-1:877352799272:deliverystream/lightbulb-dev-spark-${s}`),
      ),
    );
    expect(flat(byAction("athena:StartQueryExecution"))).toEqual([
      arn("athena:us-east-1:877352799272:workgroup/lightbulb-dev-spark"),
      arn("athena:us-east-1:877352799272:workgroup/lightbulb-dev-spark-jobs"),
    ]);
    expect(JSON.stringify(doc.Properties.Roles)).toContain("TaskRole");
    const stmts = doc.Properties.PolicyDocument.Statement as any[];
    const actions = (st: any) => (Array.isArray(st.Action) ? st.Action : [st.Action]) as string[];
    for (const a of stmts.filter((x) => actions(x).some((y) => y.startsWith("glue:"))).flatMap(actions)) {
      expect(a).not.toMatch(/^glue:(Create|Delete)/);
    }
    const s3Resources = stmts.filter((x) => actions(x).some((y) => y.startsWith("s3:"))).flatMap((x) =>
      Array.isArray(x.Resource) ? x.Resource : [x.Resource],
    );
    expect(s3Resources.sort()).toEqual(
      [
        "arn:aws:s3:::lightbulb-dev-spark-store",
        ...["warehouse/*", "athena-results/app/*", "athena-results/jobs/*", "errors/*", "jobs/*"].map((p) => `arn:aws:s3:::lightbulb-dev-spark-store/${p}`),
      ].sort(),
    );
  });
});
