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
            StartPeriod: 480,
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
          //      then directly `exec node` to launch the gateway
          //      (no pre-step `clawhub install` anymore — markdown-
          //      converter wasn't needed and was bloating cold-start).
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
              + "exec node /app/openclaw\\.mjs gateway --port 18789 --bind lan$",
            ),
          ],
        }),
      ]),
    });
  });

  test("openclaw container runs bounded, non-blocking `doctor --fix` legacy-state migration before the gateway", () => {
    // Load-bearing: 2026.7.x fail-closes agent runs on un-migrated legacy
    // per-workspace state, so this boot step is what unbricks agents on a base
    // roll. Assert it survives future startup-command refactors, and that its
    // three safety properties stay intact — bounded (`timeout --kill-after`),
    // headless-safe (`< /dev/null`), non-blocking (`|| echo`, never `&& exec`) —
    // AND that it runs BEFORE `exec node ... gateway` so no gateway holds the
    // SQLite lock during the migration. The trailing `.*exec node ... gateway$`
    // anchors ordering: doctor must appear earlier in the single joined command.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Command: [
            "sh",
            "-c",
            Match.stringLikeRegexp(
              // `\\|` escapes the regex alternation so we match a literal `|`.
              "timeout --kill-after=30 420 node /app/openclaw\\.mjs doctor --fix < /dev/null "
              + "\\|\\| echo .*"
              + "exec node /app/openclaw\\.mjs gateway --port 18789 --bind lan$",
            ),
          ],
        }),
      ]),
    });
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
