import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as kms from "aws-cdk-lib/aws-kms";
import * as rds from "aws-cdk-lib/aws-rds";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as fs from "node:fs";
import * as path from "node:path";
import { ROOT_DOMAIN } from "../lib/domains";
import { PaperclipStack } from "../lib/stacks/paperclip-stack";

// Read the version file the stack reads, and derive the expected task-def image
// the SAME way the stack does: per-env `tag === "bootstrap"` deploys the pinned
// upstream digest, otherwise the extended ECR image at that tag. Mirroring the
// selection (rather than hard-coding a value) keeps both states green — the
// current bootstrap default AND the one-line promotion PR that flips `tag` to a
// built ECR tag — so neither a digest re-pin nor a cutover silently desyncs the
// test from the stack.
const PAPERCLIP_VERSION: {
  upstream: string;
  extendedImage: string;
  dev?: { tag?: string };
  prod?: { tag?: string };
} = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "paperclip-version.json"),
    "utf8",
  ),
);

function expectedPaperclipImage(env: "dev" | "prod") {
  const tag = PAPERCLIP_VERSION[env]?.tag ?? "bootstrap";
  if (tag === "bootstrap") {
    // bootstrap → fromRegistry(upstream): the digest renders as a literal string.
    return PAPERCLIP_VERSION.upstream;
  }
  // promoted → fromEcrRepository: CDK renders the image as an Fn::Join over the
  // account-qualified ECR host (partition URLSuffix as a Ref) + "/<repo>:<tag>".
  // Match the "/<repo>:<tag>" suffix and stay agnostic to how CDK tokenizes the
  // host, so the assertion survives a partition/region change.
  const repoPath =
    "/" + PAPERCLIP_VERSION.extendedImage.split("/").slice(1).join("/");
  return Match.objectLike({
    "Fn::Join": Match.arrayWith([Match.arrayWith([`${repoPath}:${tag}`])]),
  });
}

/**
 * Builds a PaperclipStack against a synthetic Aurora cluster + Cloud Map
 * namespace in a sibling support stack. PaperclipStack uses
 * CfnSecurityGroupIngress (not addIngressRule) for the cross-stack DB SG
 * grant, which keeps the synth dependency graph one-directional and
 * avoids cycles.
 */
function buildStack(environment: "dev" | "prod"): Template {
  return Template.fromStack(buildStackInstance(environment).stack);
}

function buildStackInstance(environment: "dev" | "prod") {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };

  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const vpc = new ec2.Vpc(supportStack, "Vpc", { maxAzs: 2 });
  const kmsKey = new kms.Key(supportStack, "KmsKey");
  const cluster = new ecs.Cluster(supportStack, "Cluster", { vpc });

  const namespace = new servicediscovery.PrivateDnsNamespace(
    supportStack,
    "Namespace",
    { name: `lightbulb-${environment}.local`, vpc },
  );

  const dbSg = new ec2.SecurityGroup(supportStack, "PaperclipDbSg", {
    vpc,
    allowAllOutbound: false,
  });

  const dbCluster = new rds.DatabaseCluster(supportStack, "PaperclipDb", {
    engine: rds.DatabaseClusterEngine.auroraPostgres({
      version: rds.AuroraPostgresEngineVersion.VER_16_4,
    }),
    serverlessV2MinCapacity: 0,
    serverlessV2MaxCapacity: 4,
    writer: rds.ClusterInstance.serverlessV2("writer"),
    vpc,
    securityGroups: [dbSg],
    defaultDatabaseName: "paperclip",
    credentials: rds.Credentials.fromGeneratedSecret("paperclip_admin", {
      secretName: `isol8-${environment}-paperclip-db-credentials`,
    }),
    storageEncrypted: true,
    storageEncryptionKey: kmsKey,
  });

  const stack = new PaperclipStack(app, `Paperclip-${environment}`, {
    env,
    environment,
    vpc,
    cluster,
    cloudMapNamespace: namespace,
    paperclipDbCluster: dbCluster,
    paperclipDbSecurityGroup: dbSg,
    paperclipBetterAuthSecretName: `isol8/${environment}/paperclip_better_auth_secret`,
    paperclipEventsWebhookSecretName: `isol8/${environment}/paperclip_events_webhook_secret`,
    paperclipKmsKeyArn: kmsKey.keyArn,
  });

  return { stack, kmsKeyArn: kmsKey.keyArn };
}

describe("PaperclipStack — Fargate service shape", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates the Paperclip task definition with upstream GHCR image and 0.5 vCPU / 1 GB", () => {
    // Image MUST be the GHCR-prefixed pinned digest from the version file. A
    // bare `paperclipai/paperclip` resolves to docker.io and fails task launch
    // with CannotPullContainerError because Paperclip publishes to GHCR, not
    // Docker Hub; asserting the exact pinned value here keeps that bug from
    // sneaking back in AND keeps the default bootstrap path pinned (no floating
    // :latest).
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-paperclip-server",
      Cpu: "1024",
      Memory: "4096",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Image: expectedPaperclipImage("dev"),
        }),
      ]),
    });
  });

  test("migrate task definition uses the same upstream GHCR image", () => {
    // The migrate task pulls the same image as the main service so the
    // schema runner picks up code changes Drizzle expects. Same registry
    // pin applies — bare names break here too.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-paperclip-migrate",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Image: expectedPaperclipImage("dev"),
        }),
      ]),
    });
  });

  test("container env carries Paperclip's required runtime vars and dev-only sign-up flag", () => {
    // Pull the single TaskDef out of the template and assert directly
    // against its Environment array. arrayWith()-on-arrayWith() can't
    // express "all of these are present in any order" — it requires
    // monotonic ordering — so we extract once and use plain expect().
    const tasks = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "lightbulb-dev-paperclip-server" },
    });
    expect(Object.keys(tasks)).toHaveLength(1);
    const td = Object.values(tasks)[0] as { Properties: any };
    const env = td.Properties.ContainerDefinitions[0].Environment as Array<{
      Name: string;
      Value: string;
    }>;
    const envMap = Object.fromEntries(env.map((e) => [e.Name, e.Value]));
    // Dev keeps sign-up enabled so the first admin can bootstrap; prod
    // covered by a separate test below.
    expect(envMap.PAPERCLIP_AUTH_DISABLE_SIGN_UP).toBe("false");
    expect(envMap.PAPERCLIP_DEPLOYMENT_MODE).toBe("authenticated");
    expect(envMap.PAPERCLIP_DEPLOYMENT_EXPOSURE).toBe("public");
    // The sign-in limiter shares one bucket across every backend sign-in
    // (in-VPC, no X-Forwarded-For); a dropped or flipped line here silently
    // reintroduces cross-owner 429s, so pin it rather than trust the string.
    expect(envMap.PAPERCLIP_AUTH_RATE_LIMIT_ENABLED).toBe("false");
    // Telemetry is on unless this is exactly "1".
    expect(envMap.PAPERCLIP_TELEMETRY_DISABLED).toBe("1");
    // Without auto-apply, the server refuses to start on a fresh schema.
    expect(envMap.PAPERCLIP_MIGRATION_AUTO_APPLY).toBe("true");
    expect(envMap.HEARTBEAT_SCHEDULER_ENABLED).toBe("true");
    expect(envMap.NODE_ENV).toBe("production");
    expect(envMap.HOST).toBe("0.0.0.0");
    expect(envMap.SERVE_UI).toBe("true");
    expect(envMap.PORT).toBe("3100");
    expect(envMap.PGUSER).toBe("paperclip_admin");
    expect(envMap.PGDATABASE).toBe("paperclip");
    expect(envMap.PGPORT).toBe("5432");
    expect(envMap.PAPERCLIP_BIND).toBe("lan");
    // PAPERCLIP_PUBLIC_URL is Better Auth's https origin identity (not the agent's
    // API URL — that's the adapterConfig.paperclipApiUrl override). Dev shape is
    // `<env>.company.<root domain>`.
    expect(envMap.PAPERCLIP_PUBLIC_URL).toBe(`https://dev.company.${ROOT_DOMAIN}`);
  });

  test("container has DATABASE_URL-building shim with URL-encoded password and bypasses gosu", () => {
    // RDS-generated passwords routinely contain `/`, `+`, `=`, `@`, `:` —
    // the shim must URL-encode PGPASSWORD via Node's encodeURIComponent
    // before interpolating into DATABASE_URL, then exec node directly.
    //
    // The image's `docker-entrypoint.sh` is bypassed via `entryPoint`
    // override because Fargate's `NoNewPrivs=1` blocks the gosu setuid
    // call inside it (`error: failed switching to "node": operation not
    // permitted`). We run as user 1000 (the same UID the image chowns
    // /paperclip + /app to) to keep the privilege drop without gosu.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-paperclip-server",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          EntryPoint: ["/bin/sh", "-c"],
          User: "1000:1000",
          Command: [
            Match.stringLikeRegexp(
              'set -eu && : "\\$\\{PAPERCLIP_SECRETS_MASTER_KEY:\\?.*\\}" && : "\\$\\{PAPERCLIP_DECISION_SIGNING_SECRET:\\?.*\\}" && PGPASSWORD_ENC=\\$\\(node -e .*encodeURIComponent\\(process\\.env\\.PGPASSWORD\\).*\\) && export DATABASE_URL="postgres://\\$\\{PGUSER\\}:\\$\\{PGPASSWORD_ENC\\}@\\$\\{PGHOST\\}:\\$\\{PGPORT\\}/\\$\\{PGDATABASE\\}".*exec node ',
            ),
          ],
        }),
      ]),
    });
  });

  test("container health check hits /api/health", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          HealthCheck: Match.objectLike({
            Command: [
              "CMD-SHELL",
              "curl -fsS http://localhost:3100/api/health || exit 1",
            ],
          }),
        }),
      ]),
    });
  });

  test("Fargate service runs in private subnets with no public IP", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: "lightbulb-dev-paperclip-server",
      LaunchType: "FARGATE",
      DesiredCount: 1,
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: Match.objectLike({
          Enable: true,
          Rollback: true,
        }),
        // Old task stops before the new one starts (no overlap on schema/locks).
        MinimumHealthyPercent: 0,
        MaximumPercent: 100,
      }),
      NetworkConfiguration: Match.objectLike({
        AwsvpcConfiguration: Match.objectLike({
          AssignPublicIp: "DISABLED",
        }),
      }),
    });
  });

  test("server is pinned to one task (no autoscaling)", () => {
    // The fork's orphan reaper is not task-aware: a second task reaps a
    // sibling's in-flight runs as process_lost.
    template.resourceCountIs("AWS::ApplicationAutoScaling::ScalableTarget", 0);
    template.resourceCountIs("AWS::ApplicationAutoScaling::ScalingPolicy", 0);
  });

  test("run logs mirror to the attachments bucket under run-logs/", () => {
    const tasks = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "lightbulb-dev-paperclip-server" },
    });
    const td = Object.values(tasks)[0] as { Properties: any };
    const envArr = td.Properties.ContainerDefinitions[0].Environment as Array<{
      Name: string;
      Value: unknown;
    }>;
    const envMap = Object.fromEntries(envArr.map((e) => [e.Name, e.Value]));
    expect(envMap.RUN_LOG_S3_BUCKET).toBeDefined();
    expect(envMap.RUN_LOG_S3_BUCKET).toEqual(envMap.PAPERCLIP_STORAGE_S3_BUCKET);
    expect(envMap.RUN_LOG_S3_REGION).toEqual(envMap.PAPERCLIP_STORAGE_S3_REGION);
    expect(envMap.RUN_LOG_S3_PREFIX).toBe("run-logs");
  });

  test("registers `paperclip` in Cloud Map exactly once with A records (10s TTL)", () => {
    // The FargateService.cloudMapOptions block is the SINGLE canonical
    // registration of `paperclip` in the namespace. A standalone
    // servicediscovery.Service alongside it would collide on the
    // namespace Name and fail at first deploy — so we assert exactly one
    // such service exists.
    const services = template.findResources("AWS::ServiceDiscovery::Service", {
      Properties: {
        Name: "paperclip",
      },
    });
    expect(Object.keys(services)).toHaveLength(1);
    const svc = Object.values(services)[0] as { Properties: any };
    const records = svc.Properties.DnsConfig?.DnsRecords ?? [];
    expect(records).toEqual(
      expect.arrayContaining([expect.objectContaining({ Type: "A", TTL: 10 })]),
    );
  });

  test("Aurora SG gets ingress from Paperclip task SG on 5432", () => {
    // sg-on-sg ingress rule, the contract that lets Paperclip reach
    // Postgres. EC2 rejects non-ASCII in description fields, so plain
    // "to" stays in place of an arrow.
    template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
      FromPort: 5432,
      ToPort: 5432,
      IpProtocol: "tcp",
      Description: "Paperclip task to Aurora",
    });
  });

  test("exposes internal URL output for FastAPI proxy router (T14)", () => {
    template.hasOutput("PaperclipInternalUrl", {
      Value: "http://paperclip.lightbulb-dev.local:3100",
      Export: { Name: "lightbulb-dev-paperclip-internal-url" },
    });
  });

  test("log group lives at /lightbulb/{env}/paperclip with 14d retention", () => {
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/lightbulb/dev/paperclip",
      RetentionInDays: 14,
    });
  });

  test("migrate one-shot task def is 256/512 with the migrate command", () => {
    // A SECOND task definition (family `lightbulb-dev-paperclip-migrate`)
    // exists alongside the main service one. It runs CREATE EXTENSION
    // for pgvector, then `pnpm --filter @paperclipai/db migrate`.
    //
    // Migrate uses the same `entryPoint: ["/bin/sh", "-c"]` override as
    // the main service to bypass the image's gosu-based entrypoint
    // (Fargate `NoNewPrivs=1` blocks setuid). It runs as root because
    // apt-get-install postgresql-client requires it.
    const migrateDefs = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "lightbulb-dev-paperclip-migrate" },
    });
    expect(Object.keys(migrateDefs)).toHaveLength(1);
    const td = Object.values(migrateDefs)[0] as { Properties: any };
    expect(td.Properties.Cpu).toBe("256");
    expect(td.Properties.Memory).toBe("512");
    const containerDef = td.Properties.ContainerDefinitions[0];
    expect(containerDef.EntryPoint).toEqual(["/bin/sh", "-c"]);
    // No `User` field — migrate runs as root by design (apt-get).
    expect(containerDef.User).toBeUndefined();
    const cmd = containerDef.Command as string[];
    expect(cmd).toHaveLength(1);
    expect(cmd[0]).toMatch(/CREATE EXTENSION IF NOT EXISTS vector/);
    expect(cmd[0]).toMatch(/pnpm --filter @paperclipai\/db migrate/);
    // PGPASSWORD URL-encoding shim (T4 lesson) is also reused here.
    expect(cmd[0]).toMatch(/encodeURIComponent\(process\.env\.PGPASSWORD\)/);
  });

  test("migrate task def has its own log group at /lightbulb/{env}/paperclip-migrate", () => {
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/lightbulb/dev/paperclip-migrate",
      RetentionInDays: 14,
    });
  });

  test("server gets a stable, retained secrets master key (never an ephemeral key file)", () => {
    // Unset, Paperclip writes a fresh key file on each task's ephemeral disk and every
    // task replacement orphans every local_encrypted secret (2026-09-23 incident).
    template.hasResource("AWS::SecretsManager::Secret", {
      DeletionPolicy: "Retain",
      Properties: Match.objectLike({ Name: "lightbulb/dev/paperclip_secrets_master_key" }),
    });
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-paperclip-server",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Secrets: Match.arrayWith([Match.objectLike({ Name: "PAPERCLIP_SECRETS_MASTER_KEY" })]),
        }),
      ]),
    });
  });

  test("server gets a stable decision-signing secret and no local DB backups", () => {
    // Unset, Paperclip generates the decision-spec HMAC key on the ephemeral disk, so every
    // task replacement invalidates every pending decision's signature.
    template.hasResource("AWS::SecretsManager::Secret", {
      DeletionPolicy: "Retain",
      Properties: Match.objectLike({ Name: "lightbulb/dev/paperclip_decision_signing_secret" }),
    });
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "lightbulb-dev-paperclip-server",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Secrets: Match.arrayWith([Match.objectLike({ Name: "PAPERCLIP_DECISION_SIGNING_SECRET" })]),
          Environment: Match.arrayWith([{ Name: "PAPERCLIP_DB_BACKUP_ENABLED", Value: "false" }]),
        }),
      ]),
    });
  });

  test("both task execution roles have an explicit kms:Decrypt grant on the AuthStack CMK", () => {
    // CDK's `secret.grantRead()` only adds `secretsmanager:GetSecretValue`,
    // not the `kms:Decrypt` that customer-managed keys require for
    // GetSecretValue to actually return plaintext. Without this explicit
    // statement, ECS task launch fails:
    //   ResourceInitializationError: AccessDeniedException: Access to KMS
    //   is not allowed ... fetching .../paperclip_better_auth_secret
    // Same pattern as ApiStack T9 (commit 9188442) for the WS authorizer.
    //
    // The synthesized policy buckets statements by role; we look for
    // exactly one IAM::Policy attached to each execution role with a
    // statement whose Sid starts with KmsDecryptForPaperclipBetterAuth.
    const policies = template.findResources("AWS::IAM::Policy");
    const kmsStatements: any[] = [];
    for (const body of Object.values(policies) as Array<{ Properties: any }>) {
      const stmts = body.Properties?.PolicyDocument?.Statement ?? [];
      for (const s of stmts) {
        if (
          typeof s === "object" &&
          s.Sid === "KmsDecryptForPaperclipBetterAuthSecret"
        ) {
          kmsStatements.push(s);
        }
      }
    }
    // One statement per execution role (main + migrate).
    expect(kmsStatements).toHaveLength(2);
    for (const s of kmsStatements) {
      expect(s.Effect).toBe("Allow");
      expect(s.Action).toBe("kms:Decrypt");
      // Resource is a Ref/Fn::GetAtt to the support stack's KMS key (the
      // arn-string we passed in via props), so we check shape rather
      // than literal value.
      expect(s.Resource).toBeDefined();
    }
  });
});

describe("PaperclipStack — prod public URL", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("prod");
  });

  test("prod container env sets PAPERCLIP_PUBLIC_URL to the https auth origin (no env suffix)", () => {
    const tasks = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "lightbulb-prod-paperclip-server" },
    });
    expect(Object.keys(tasks)).toHaveLength(1);
    const td = Object.values(tasks)[0] as { Properties: any };
    const envArr = td.Properties.ContainerDefinitions[0].Environment as Array<{
      Name: string;
      Value: string;
    }>;
    const envMap = Object.fromEntries(envArr.map((e) => [e.Name, e.Value]));
    expect(envMap.PAPERCLIP_PUBLIC_URL).toBe(`https://company.${ROOT_DOMAIN}`);
  });

  test("prod keeps public sign-up enabled (no out-of-band admin path upstream)", () => {
    // Upstream Paperclip's first-admin bootstrap path is the same email/
    // password sign-up endpoint (POST /api/auth/sign-up/email). There is
    // no separate out-of-band provisioning path, so disabling sign-up
    // here would make the deployment un-bootstrappable. Practical
    // exposure is bounded: `company.isol8.co` has no DNS record, the
    // task is in private subnets, and the backend's paperclip_proxy
    // route-filters /api/auth/*. See the long-form rationale comment
    // in paperclip-stack.ts around the PAPERCLIP_AUTH_DISABLE_SIGN_UP
    // line.
    const tasks = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "lightbulb-prod-paperclip-server" },
    });
    const td = Object.values(tasks)[0] as { Properties: any };
    const envArr = td.Properties.ContainerDefinitions[0].Environment as Array<{
      Name: string;
      Value: string;
    }>;
    const envMap = Object.fromEntries(envArr.map((e) => [e.Name, e.Value]));
    expect(envMap.PAPERCLIP_AUTH_DISABLE_SIGN_UP).toBe("false");
    expect(envMap.PAPERCLIP_AUTH_RATE_LIMIT_ENABLED).toBe("false");
    expect(envMap.PAPERCLIP_TELEMETRY_DISABLED).toBe("1");
  });

  test("prod log group uses RETAIN removal policy", () => {
    const logGroups = template.findResources("AWS::Logs::LogGroup", {
      Properties: { LogGroupName: "/lightbulb/prod/paperclip" },
    });
    const ids = Object.keys(logGroups);
    expect(ids).toHaveLength(1);
    expect(logGroups[ids[0]].DeletionPolicy).toBe("Retain");
  });
});
