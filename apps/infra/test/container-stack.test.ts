import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ContainerStack } from "../lib/stacks/container-stack";

function buildStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const vpc = new ec2.Vpc(supportStack, "Vpc");
  const kmsKey = new kms.Key(supportStack, "KmsKey");
  const containerStack = new ContainerStack(app, `Container-${environment}`, {
    env,
    environment,
    vpc,
    kmsKeyArn: kmsKey.keyArn,
    gogKeyringPasswordSecretName: `isol8/${environment}/gog-keyring-password`,
  });
  return Template.fromStack(containerStack);
}

// The local-sqlite layout is unconditional — no per-env `storage` gate — so
// both envs are asserted against the same shape. Extracted rather than
// duplicated so a future divergence has to be written deliberately.

// The boot guard is the ONLY thing between "tag built before the boot chain
// existed" and a container that fails on a bare `not found`. Synth cannot
// inspect image contents, so this diagnostic + `exit 1` is the whole safety
// net; it must stay ordered immediately before the exec that needs it.
// doctor + gateway now live inside the boot script (which runs them as a
// litestream-supervised child), not in this command.
function expectBootChainGuard(template: Template, env: "dev" | "prod"): void {
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    Family: `isol8-${env}-openclaw`,
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: "openclaw",
        Command: [
          "sh",
          "-c",
          Match.stringLikeRegexp(
            // `\\|` escapes the regex alternation so we match a literal `|`.
            "\\[ -x /usr/local/bin/isol8-local-sqlite-boot\\.sh \\] \\|\\| \\{ echo .*FATAL.*"
            + "exit 1; \\}; "
            + "exec /usr/local/bin/isol8-local-sqlite-boot\\.sh$",
          ),
        ],
      }),
    ]),
  });
}

// The three pieces the boot script cannot supply itself: the replica bucket
// name (LITESTREAM_S3_BUCKET), the second EFS view it archives through
// (/efs-raw), and the nested ephemeral volumes that give WAL back. Mount
// ORDER is load-bearing — ECS mounts in literal mountPoints[] order, so the
// EFS views must precede the nested ephemeral mounts or EFS covers them and
// every commit silently goes back to NFS.
function expectLocalSqliteLayout(template: Template, env: "dev" | "prod"): void {
  template.hasResourceProperties("AWS::S3::Bucket", {
    BucketName: `isol8-${env}-openclaw-replicas`,
  });
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    Family: `isol8-${env}-openclaw`,
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: "openclaw",
        Environment: Match.arrayWith([
          { Name: "LITESTREAM_S3_BUCKET", Value: `isol8-${env}-openclaw-replicas` },
        ]),
        MountPoints: [
          Match.objectLike({ ContainerPath: "/home/node/.openclaw", SourceVolume: "openclaw-workspace" }),
          Match.objectLike({ ContainerPath: "/efs-raw", SourceVolume: "openclaw-workspace-raw" }),
          Match.objectLike({ ContainerPath: "/home/node/.openclaw/state", SourceVolume: "openclaw-state" }),
          Match.objectLike({ ContainerPath: "/home/node/.openclaw/agents", SourceVolume: "openclaw-agents" }),
        ],
      }),
    ]),
  });

  // Volume DECLARATION order is incidental (unlike mountPoints order above),
  // so match by name. The raw view must carry an efsVolumeConfiguration —
  // that is what makes the backend's per-user clone rewrite its accessPointId
  // alongside the primary mount; the hot pair must carry none, or they stop
  // being local disk.
  const taskDef = Object.values(
    template.findResources("AWS::ECS::TaskDefinition"),
  )[0] as { Properties: { Volumes: { Name: string; EFSVolumeConfiguration?: unknown }[] } };
  const byName = Object.fromEntries(taskDef.Properties.Volumes.map((v) => [v.Name, v]));
  expect(byName["openclaw-workspace-raw"].EFSVolumeConfiguration).toBeDefined();
  expect(byName["openclaw-state"]).toEqual({ Name: "openclaw-state" });
  expect(byName["openclaw-agents"]).toEqual({ Name: "openclaw-agents" });
}

describe("ContainerStack — dev", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("does NOT create the extended OpenClaw ECR repo (referenced by name, account-scoped)", () => {
    // Both dev and prod now use `ecr.Repository.fromRepositoryName(...)` —
    // the repo is managed as existing AWS state outside any stack to avoid
    // the "RETAIN orphan blocks recreate" trap. See container-stack.ts
    // (around the OpenclawExtendedRepo declaration) for the rationale.
    // Lifecycle, immutability, and scan-on-push settings live on the repo
    // itself in AWS, not in CDK.
    template.resourceCountIs("AWS::ECR::Repository", 0);
  });

  test("creates the openclaw-image-builder OIDC role for CI image pushes", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "isol8-openclaw-image-builder",
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "sts:AssumeRoleWithWebIdentity",
            Condition: Match.objectLike({
              StringLike: {
                "token.actions.githubusercontent.com:sub":
                  "repo:Isol8AI/isol8:*",
              },
              StringEquals: {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
              },
            }),
          }),
        ]),
      }),
    });
  });

  test("builder role has ECR auth + push permissions on the openclaw-extended repo", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "ecr:GetAuthorizationToken",
            Effect: "Allow",
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("openclaw container has a /healthz healthcheck on port 18789", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          HealthCheck: {
            Command: [
              "CMD-SHELL",
              Match.stringLikeRegexp(
                "fetch\\('http://127\\.0\\.0\\.1:18789/healthz'\\)",
              ),
            ],
            Interval: 30,
            Timeout: 10,
            Retries: 6,
            // 300 is the ECS maximum for startPeriod (values >300 are rejected).
            StartPeriod: 300,
          },
        }),
      ]),
    });
  });

  test("openclaw container enables Fargate init process for zombie reaping", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          LinuxParameters: Match.objectLike({
            InitProcessEnabled: true,
          }),
        }),
      ]),
    });
  });

  test("openclaw container migrates legacy /home/node/.openclaw/browser and symlinks it to ephemeral storage", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          // ECS Command array for command=["sh","-c",<startupCommand>]
          // renders as exactly those three array elements in synthesized
          // CFN. Three ordered segments separated by `.*`:
          //   1) the archive-rename guard for a legacy REAL directory
          //   2) the actual mv to a timestamped archive path
          //   3) mkdir + symlink to ephemeral /tmp/openclaw-browser
          //      then `exec` into the baked local-sqlite boot chain, which
          //      owns doctor + gateway as a litestream-supervised child.
          Command: [
            "sh",
            "-c",
            Match.stringLikeRegexp(
              // Migration step + mandatory `|| exit 1` on each required
              // action so an NFS hiccup that fails `mv` doesn't let
              // Step B silently nest the symlink inside the legacy real
              // directory. `\\|` escapes the regex alternation operator
              // so we match a literal `|`.
              "^\\[ -e /home/node/\\.openclaw/browser \\] && \\[ ! -L /home/node/\\.openclaw/browser \\].*"
              + "mv /home/node/\\.openclaw/browser /home/node/\\.openclaw/browser\\.efs-archive-.*\\|\\| exit 1.*"
              + "mkdir -p /tmp/openclaw-browser \\|\\| exit 1.*"
              + "ln -sfn /tmp/openclaw-browser /home/node/\\.openclaw/browser \\|\\| exit 1.*"
              + "exec /usr/local/bin/isol8-local-sqlite-boot\\.sh$",
            ),
          ],
        }),
      ]),
    });
  });

  test("openclaw container guards the local-sqlite boot chain before exec-ing it", () => {
    expectBootChainGuard(template, "dev");
  });

  test("openclaw container gets the litestream replica bucket + nested local-sqlite mounts", () => {
    expectLocalSqliteLayout(template, "dev");
  });

  test("openclaw container startup never references the retired workspaces/ layout", () => {
    // Agent workspaces live at upstream's per-agent convention
    // (~/.openclaw/workspace-<agentId>). The old ~/.openclaw/workspaces/
    // layout is retired — upstream doctor deletes that exact dir as
    // removed-plugin state, so no boot step may create, move, or reference
    // it (there is deliberately no migration: pre-incident data there was
    // wiped or belongs to accounts that get reprovisioned).
    const tj = template.toJSON();
    let checked = 0;
    for (const r of Object.values(tj.Resources as Record<string, any>)) {
      if (r.Type !== "AWS::ECS::TaskDefinition") continue;
      for (const cd of r.Properties.ContainerDefinitions ?? []) {
        const cmd = (cd.Command ?? []).join(" ");
        expect(cmd).not.toContain(".openclaw/workspaces");
        checked += 1;
      }
    }
    expect(checked).toBeGreaterThan(0);
  });

  test("openclaw container sets XDG_CONFIG_HOME + XDG_DATA_HOME + GOG_KEYRING_BACKEND for the gog file keyring", () => {
    // XDG_DATA_HOME is the load-bearing one: gog stores its file keyring under
    // the data home (verified: setting XDG_DATA_HOME=/x puts the keyring at
    // /x/gogcli/keyring), NOT the config home. Both must point at the EFS mount
    // or the OAuth tokens land on the ephemeral overlay fs and are wiped on
    // every task restart.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Environment: Match.arrayWith([
            {
              Name: "XDG_CONFIG_HOME",
              Value: "/home/node/.openclaw/.config",
            },
            {
              Name: "XDG_DATA_HOME",
              Value: "/home/node/.openclaw/.local/share",
            },
            { Name: "GOG_KEYRING_BACKEND", Value: "file" },
          ]),
        }),
      ]),
    });
  });

  test("openclaw keeps the npm download cache on EFS but redirects the npx + uv extract storms to local /tmp", () => {
    // MCP servers spawn as npx/uvx children that inherit only HOME (not the
    // container env), so caches are relocated by symlinking the default
    // $HOME-relative paths. The per-run EXTRACT (npx `_npx`, uv cache) is a
    // metadata-heavy small-file unpack; on the EFS/NFS mount it stalls in
    // D-state and overruns the MCP connect timeout, so it must land on local
    // ephemeral /tmp. npm splits (keep `_cacache` on EFS so restarts don't
    // re-download, redirect only `_npx` → /tmp); uv cannot split (a cache
    // spread across filesystems hits a cross-device rename) so its whole
    // cache goes to /tmp.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Command: [
            "sh",
            "-c",
            Match.stringLikeRegexp(
              // npm cache root stays on EFS (persists the `_cacache` tarballs)…
              ".*ln -sfn /home/node/\\.openclaw/\\.cache/npm /home/node/\\.npm.*" +
                // …but the npx extract dir is redirected to local /tmp…
                "ln -sfn /tmp/openclaw-npx /home/node/\\.openclaw/\\.cache/npm/_npx.*" +
                // …and the whole uv cache goes to local /tmp.
                "ln -sfn /tmp/openclaw-uv /home/node/\\.cache/uv.*",
            ),
          ],
        }),
      ]),
    });
  });

  test("openclaw migrates a legacy real `_npx` dir aside with `mv`, never a boot-path `rm -rf`", () => {
    // A legacy `_npx` on EFS can be tens of thousands of files; an `rm -rf` of
    // it on the gateway-boot path is a synchronous NFS unlink storm that could
    // overrun the health-check start period. The migration must `mv` it aside
    // (an O(1) same-filesystem rename), exactly like the chromium/npm guards.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Command: Match.arrayWith([
            Match.stringLikeRegexp(
              "mv /home/node/\\.openclaw/\\.cache/npm/_npx /home/node/\\.openclaw/\\.cache/npm/_npx\\.efs-archive-",
            ),
          ]),
        }),
      ]),
    });
  });

  test("openclaw startup never `rm -rf`s the `_npx` extract dir on the boot path", () => {
    // Standalone guard so a future refactor can't swap the `mv`-aside
    // migration for a recursive delete and silently re-introduce the
    // boot-path NFS unlink storm.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Command: Match.not(
            Match.arrayWith([Match.stringLikeRegexp("rm -rf[^;]*_npx")]),
          ),
        }),
      ]),
    });
  });

  test("openclaw container injects GOG_KEYRING_PASSWORD from the shared secret", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Secrets: Match.arrayWith([
            Match.objectLike({
              Name: "GOG_KEYRING_PASSWORD",
              // fromSecretNameV2 renders the ValueFrom as a Fn::Join that
              // builds the secret ARN from the partition/region/account and
              // the secret name. Assert the secret name appears in that join.
              ValueFrom: Match.objectLike({
                "Fn::Join": Match.arrayWith([
                  Match.arrayWith([
                    Match.stringLikeRegexp("isol8/dev/gog-keyring-password"),
                  ]),
                ]),
              }),
            }),
          ]),
        }),
      ]),
    });
  });

  test("per-user execution role can kms:Decrypt the CMK to inject the gog secret", () => {
    // GOG_KEYRING_PASSWORD is encrypted with AuthStack's CMK; without an
    // explicit kms:Decrypt the ECS agent cannot inject it and every per-user
    // container launch fails at secret injection. GetSecretValue is not enough.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "KmsDecryptForSecrets",
            Action: "kms:Decrypt",
          }),
        ]),
      }),
    });
  });

  test("openclaw container startup no longer runs `clawhub install markdown-converter`", () => {
    // Standalone assertion (independent of the symlink ordering) so
    // future refactors of the command can't accidentally re-introduce
    // the markdown-converter install.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Command: Match.not(
            Match.arrayWith([Match.stringLikeRegexp("clawhub install markdown-converter")]),
          ),
        }),
      ]),
    });
  });

  test("shared task role no longer carries the broad bedrock:* on Resource '*'", () => {
    // The per-owner role supersedes it; the base task-def must have NO Bedrock
    // access so every bedrock container is forced onto a per-owner role.
    const policies = template.findResources("AWS::IAM::Policy");
    const offending = Object.values(policies).filter((p: any) =>
      (p.Properties.PolicyDocument.Statement as any[]).some(
        (s) =>
          s.Resource === "*" &&
          JSON.stringify(s.Action).includes("bedrock:InvokeModel"),
      ),
    );
    expect(offending).toHaveLength(0);
  });

  test("creates the OwnerBedrockBasePolicy managed policy (EFS/logs/ssm)", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-owner-bedrock-base",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["elasticfilesystem:ClientMount"]),
          }),
        ]),
      }),
    });
  });

  test("creates the OwnerBedrockBoundary permissions-boundary policy", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-owner-bedrock-boundary",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:InvokeModel"]),
          }),
        ]),
      }),
    });
  });

  test("boundary invoke statement carries all 9 FM ARNs + the AIP wildcard", () => {
    // Guards against FM_IDS/FAN_OUT_REGIONS drift between this stack and the
    // Python mint (core/billing/bedrock_aip.py). A region/model added on one
    // side but not the other would silently make the boundary too narrow
    // (AccessDenied on invoke) or too wide. 3 regions x 3 models = 9 ARNs.
    const FAN_OUT_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];
    const FM_IDS = [
      "anthropic.claude-opus-4-6-v1",
      "anthropic.claude-sonnet-4-6",
      "anthropic.claude-haiku-4-5-20251001-v1:0",
    ];
    const expectedFmArns = FAN_OUT_REGIONS.flatMap((region) =>
      FM_IDS.map((model) => `arn:aws:bedrock:${region}::foundation-model/${model}`),
    );
    expect(expectedFmArns).toHaveLength(9);

    const policies = template.findResources("AWS::IAM::ManagedPolicy");
    const boundary = Object.values(policies).find(
      (p: any) => p.Properties.ManagedPolicyName === "isol8-dev-owner-bedrock-boundary",
    ) as any;
    expect(boundary).toBeDefined();

    const invokeStatement = (boundary.Properties.PolicyDocument.Statement as any[]).find(
      (s) => JSON.stringify(s.Action).includes("bedrock:InvokeModel"),
    );
    expect(invokeStatement).toBeDefined();

    const resources: string[] = invokeStatement.Resource;
    for (const arn of expectedFmArns) {
      expect(resources).toContain(arn);
    }
    // The AIP wildcard must also be present (the profile ARNs are runtime-minted).
    expect(
      resources.some((r) => r.includes("application-inference-profile/*")),
    ).toBe(true);
  });

  test("boundary denies assume-into-our-account unconditionally and grants no sts:AssumeRole", () => {
    // The per-owner container role has no AWS-rail job anymore: the backend
    // vends connector credentials itself. The Deny must be unconditional
    // (no founder carve-out) and the broad AssumeRole ceiling must be gone.
    const policies = template.findResources("AWS::IAM::ManagedPolicy");
    const boundary = Object.values(policies).find(
      (p: any) => p.Properties.ManagedPolicyName === "isol8-dev-owner-bedrock-boundary",
    ) as any;
    expect(boundary).toBeDefined();

    const stmts = boundary.Properties.PolicyDocument.Statement as any[];
    const deny = stmts.find((s) => s.Effect === "Deny");
    expect(deny).toBeDefined();
    expect(JSON.stringify(deny.Action)).toContain("sts:AssumeRole");
    expect(deny.Condition).toBeUndefined();

    const allowAssume = stmts.find(
      (s) => s.Effect === "Allow" && JSON.stringify(s.Action).includes("sts:AssumeRole"),
    );
    expect(allowAssume).toBeUndefined();
  });
});

describe("ContainerStack — prod", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("prod");
  });

  test("does NOT create the ECR repo (referenced by name from dev)", () => {
    template.resourceCountIs("AWS::ECR::Repository", 0);
  });

  // Prod is the whole point of removing the `storage` gate: it used to synth
  // the EFS layout, and these two now pin that it gets the identical
  // local-sqlite shape dev does.
  test("openclaw container guards the local-sqlite boot chain before exec-ing it", () => {
    expectBootChainGuard(template, "prod");
  });

  test("openclaw container gets the litestream replica bucket + nested local-sqlite mounts", () => {
    expectLocalSqliteLayout(template, "prod");
  });

  test("replica bucket is RETAINed in prod (never auto-deleted)", () => {
    template.hasResource("AWS::S3::Bucket", {
      Properties: Match.objectLike({ BucketName: "isol8-prod-openclaw-replicas" }),
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
    });
  });

  test("does NOT create the image-builder role (single account-wide role)", () => {
    // No role with name isol8-openclaw-image-builder — verify by checking
    // no IAM::Role with that exact RoleName exists.
    const roles = template.findResources("AWS::IAM::Role");
    const builderRoles = Object.values(roles).filter(
      (r: unknown) =>
        (r as { Properties?: { RoleName?: string } }).Properties?.RoleName ===
        "isol8-openclaw-image-builder",
    );
    expect(builderRoles).toHaveLength(0);
  });
});
