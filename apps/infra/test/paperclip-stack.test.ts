import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as kms from "aws-cdk-lib/aws-kms";
import * as rds from "aws-cdk-lib/aws-rds";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Match, Template } from "aws-cdk-lib/assertions";
import { PaperclipStack } from "../lib/stacks/paperclip-stack";

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
    { name: `isol8-${environment}.local`, vpc },
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
    // Image MUST be the GHCR-prefixed name. A bare `paperclipai/paperclip`
    // resolves to docker.io and fails task launch with
    // CannotPullContainerError because Paperclip publishes to GHCR, not
    // Docker Hub. Asserting the literal string here keeps that bug from
    // sneaking back in.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-paperclip-server",
      Cpu: "512",
      Memory: "1024",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Image: "ghcr.io/paperclipai/paperclip:latest",
        }),
      ]),
    });
  });

  test("migrate task definition uses the same upstream GHCR image", () => {
    // The migrate task pulls the same image as the main service so the
    // schema runner picks up code changes Drizzle expects. Same registry
    // pin applies — bare names break here too.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-paperclip-migrate",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Image: "ghcr.io/paperclipai/paperclip:latest",
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
      Properties: { Family: "isol8-dev-paperclip-server" },
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
    // Dev / non-prod URL shape is `<env>.company.isol8.co` (matches the
    // `https://${env}.company.isol8.co` template in paperclip-stack.ts).
    expect(envMap.PAPERCLIP_PUBLIC_URL).toBe("https://dev.company.isol8.co");
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
      Family: "isol8-dev-paperclip-server",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          EntryPoint: ["/bin/sh", "-c"],
          User: "1000:1000",
          Command: [
            Match.stringLikeRegexp(
              'set -eu && PGPASSWORD_ENC=\\$\\(node -e .*encodeURIComponent\\(process\\.env\\.PGPASSWORD\\).*\\) && export DATABASE_URL="postgres://\\$\\{PGUSER\\}:\\$\\{PGPASSWORD_ENC\\}@\\$\\{PGHOST\\}:\\$\\{PGPORT\\}/\\$\\{PGDATABASE\\}".*exec node ',
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
      ServiceName: "isol8-dev-paperclip-server",
      LaunchType: "FARGATE",
      DesiredCount: 1,
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: Match.objectLike({ Enable: true, Rollback: true }),
      }),
      NetworkConfiguration: Match.objectLike({
        AwsvpcConfiguration: Match.objectLike({
          AssignPublicIp: "DISABLED",
        }),
      }),
    });
  });

  test("autoscaling target is 1–4 and tracks CPU at 70%", () => {
    template.hasResourceProperties("AWS::ApplicationAutoScaling::ScalableTarget", {
      MinCapacity: 1,
      MaxCapacity: 4,
      ServiceNamespace: "ecs",
    });
    template.hasResourceProperties(
      "AWS::ApplicationAutoScaling::ScalingPolicy",
      {
        PolicyType: "TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: 70,
          PredefinedMetricSpecification: Match.objectLike({
            PredefinedMetricType: "ECSServiceAverageCPUUtilization",
          }),
        }),
      },
    );
  });

  test("registers `paperclip` in Cloud Map exactly once with A records (10s TTL)", () => {
    // The FargateService.cloudMapOptions block is the SINGLE canonical
    // registration of `paperclip` in the namespace. A standalone
    // servicediscovery.Service alongside it would collide on the
    // namespace Name and fail at first deploy — so we assert exactly one
    // such service exists.
    const services = template.findResources(
      "AWS::ServiceDiscovery::Service",
      {
        Properties: {
          Name: "paperclip",
        },
      },
    );
    expect(Object.keys(services)).toHaveLength(1);
    const svc = Object.values(services)[0] as { Properties: any };
    const records = svc.Properties.DnsConfig?.DnsRecords ?? [];
    expect(records).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ Type: "A", TTL: 10 }),
      ]),
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
      Value: "http://paperclip.isol8-dev.local:3100",
      Export: { Name: "isol8-dev-paperclip-internal-url" },
    });
  });

  test("log group lives at /isol8/{env}/paperclip with 14d retention", () => {
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/isol8/dev/paperclip",
      RetentionInDays: 14,
    });
  });

  test("migrate one-shot task def is 256/512 with the migrate command", () => {
    // A SECOND task definition (family `isol8-dev-paperclip-migrate`)
    // exists alongside the main service one. It runs CREATE EXTENSION
    // for pgvector, then `pnpm --filter @paperclipai/db migrate`.
    //
    // Migrate uses the same `entryPoint: ["/bin/sh", "-c"]` override as
    // the main service to bypass the image's gosu-based entrypoint
    // (Fargate `NoNewPrivs=1` blocks setuid). It runs as root because
    // apt-get-install postgresql-client requires it.
    const migrateDefs = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "isol8-dev-paperclip-migrate" },
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

  test("migrate task def has its own log group at /isol8/{env}/paperclip-migrate", () => {
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/isol8/dev/paperclip-migrate",
      RetentionInDays: 14,
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

  test("prod container env points at https://company.isol8.co (no env suffix)", () => {
    const tasks = template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: "isol8-prod-paperclip-server" },
    });
    expect(Object.keys(tasks)).toHaveLength(1);
    const td = Object.values(tasks)[0] as { Properties: any };
    const envArr = td.Properties.ContainerDefinitions[0].Environment as Array<{
      Name: string;
      Value: string;
    }>;
    const envMap = Object.fromEntries(envArr.map((e) => [e.Name, e.Value]));
    expect(envMap.PAPERCLIP_PUBLIC_URL).toBe("https://company.isol8.co");
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
      Properties: { Family: "isol8-prod-paperclip-server" },
    });
    const td = Object.values(tasks)[0] as { Properties: any };
    const envArr = td.Properties.ContainerDefinitions[0].Environment as Array<{
      Name: string;
      Value: string;
    }>;
    const envMap = Object.fromEntries(envArr.map((e) => [e.Name, e.Value]));
    expect(envMap.PAPERCLIP_AUTH_DISABLE_SIGN_UP).toBe("false");
  });

  test("prod log group uses RETAIN removal policy", () => {
    const logGroups = template.findResources("AWS::Logs::LogGroup", {
      Properties: { LogGroupName: "/isol8/prod/paperclip" },
    });
    const ids = Object.keys(logGroups);
    expect(ids).toHaveLength(1);
    expect(logGroups[ids[0]].DeletionPolicy).toBe("Retain");
  });
});
