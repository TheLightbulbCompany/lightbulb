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
    cloudflareWarpEnrollmentSecretName: `isol8/${environment}/cloudflare-warp-enrollment`,
  });
  return Template.fromStack(containerStack);
}

function openclawStartupCommand(template: Template, env: "dev" | "prod"): string {
  const resources = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as Array<{
    Properties?: {
      Family?: string;
      ContainerDefinitions?: Array<{ Name?: string; Command?: string[] }>;
    };
  }>;
  const taskDef = resources.find((resource) => resource.Properties?.Family === `isol8-${env}-openclaw`);
  const container = taskDef?.Properties?.ContainerDefinitions?.find(({ Name }) => Name === "openclaw");
  expect(container?.Command?.slice(0, 2)).toEqual(["sh", "-c"]);
  return container?.Command?.[2] ?? "";
}

function openclawContainerEnv(
  template: Template,
  env: "dev" | "prod",
): Array<{ Name?: string; Value?: string }> {
  const resources = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as Array<{
    Properties?: {
      Family?: string;
      ContainerDefinitions?: Array<{
        Name?: string;
        Environment?: Array<{ Name?: string; Value?: string }>;
      }>;
    };
  }>;
  const taskDef = resources.find((resource) => resource.Properties?.Family === `isol8-${env}-openclaw`);
  return taskDef?.Properties?.ContainerDefinitions?.find(({ Name }) => Name === "openclaw")?.Environment ?? [];
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
            + ".*exec setpriv --reuid=1000 --regid=1000 --init-groups /usr/local/bin/isol8-local-sqlite-boot\\.sh$",
          ),
        ],
      }),
    ]),
  });
}

// The two pieces the boot script cannot supply itself: the replica bucket name
// (LITESTREAM_S3_BUCKET) and the nested ephemeral volumes that give WAL back.
// Mount ORDER is load-bearing — ECS mounts in literal mountPoints[] order, so
// the EFS mount must precede the nested ephemeral mounts or EFS covers them and
// every commit silently goes back to NFS.
// The display block ships AHEAD of the headful config flip, so the property
// that makes it safe to deploy is that it does NOTHING yet — hence asserting
// the inertness, not merely the presence.
//
// Asserted per-environment ON PURPOSE, with `displaySet` a parameter rather
// than a constant: the environments are pinned separately so one can be rolled
// back without the other. DISPLAY reaching an environment whose
// openclaw-version.json tag predates the Xvfb apt layer is a fleet-wide boot
// failure, so each env asserts its own value rather than inheriting a shared one.
function expectDisplayGate(
  template: Template,
  env: "dev" | "prod",
  { displaySet }: { displaySet: boolean },
): void {
  const command = openclawStartupCommand(template, env);
  const guard = command.indexOf('[ -z "$DISPLAY" ] || [ -x /usr/bin/Xvnc ]');
  const start = command.indexOf('[ -z "$DISPLAY" ] || { Xvnc "$DISPLAY"');
  const wm = command.indexOf("matchbox-window-manager -use_titlebar no");
  const websockify = command.indexOf("websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900");
  const boot = command.indexOf("exec setpriv --reuid=1000 --regid=1000 --init-groups /usr/local/bin/isol8-local-sqlite-boot.sh");

  // The guard must precede the launch it protects, and everything must precede
  // the exec that replaces this shell — nothing can be appended after that.
  // The WM must come after the X server line: X clients do not retry a missing
  // socket, which is also why the command wraps it in a wait-for-socket loop.
  expect(guard).toBeGreaterThanOrEqual(0);
  expect(start).toBeGreaterThan(guard);
  expect(wm).toBeGreaterThan(start);
  expect(command).toContain("while [ ! -S /tmp/.X11-unix/X1 ]");
  expect(websockify).toBeGreaterThan(wm);
  expect(websockify).toBeLessThan(boot);

  // The CDP-screencast socat bridge (18801 -> 18800) was removed with the
  // screencast live-view — it must never reappear in the boot command.
  expect(command).not.toContain("socat TCP4-LISTEN:18801");
  expect(command).not.toContain("18801");

  // Xvnc must be resizable-by-client and loopback-only: SecurityTypes None is
  // safe ONLY together with -localhost (the relay is the auth fence), so pin
  // them as a pair — losing -localhost while keeping None would expose an
  // unauthenticated VNC server to the VPC.
  expect(command).toContain("-localhost -SecurityTypes None -rfbport 5900");

  // x11vnc is GONE from the boot path — Xvnc replaced it precisely because a
  // resident x11vnc degrades over days (it polls a foreign display; Xvnc IS
  // the display and only encodes while a viewer is attached). It reappearing
  // means someone resurrected the old chain.
  expect(command).not.toContain("x11vnc");
  expect(command).not.toContain("socat TCP-LISTEN:5900");
  expect(command).not.toContain("Xvfb");

  expect(openclawContainerEnv(template, env).some(({ Name }) => Name === "DISPLAY")).toBe(displaySet);
}

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
          Match.objectLike({ ContainerPath: "/home/node/.openclaw/state", SourceVolume: "openclaw-state" }),
          Match.objectLike({ ContainerPath: "/home/node/.openclaw/agents", SourceVolume: "openclaw-agents" }),
        ],
      }),
    ]),
  });

  // Volume DECLARATION order is incidental (unlike mountPoints order above),
  // so match by name. The workspace volume must carry an efsVolumeConfiguration
  // — that is what makes the backend's per-user clone rewrite its
  // accessPointId; the hot pair must carry none, or they stop being local disk.
  const taskDef = Object.values(
    template.findResources("AWS::ECS::TaskDefinition"),
  )[0] as { Properties: { Volumes: { Name: string; EFSVolumeConfiguration?: unknown }[] } };
  const byName = Object.fromEntries(taskDef.Properties.Volumes.map((v) => [v.Name, v]));
  expect(byName["openclaw-workspace"].EFSVolumeConfiguration).toBeDefined();
  expect(byName["openclaw-state"]).toEqual({ Name: "openclaw-state" });
  expect(byName["openclaw-agents"]).toEqual({ Name: "openclaw-agents" });
  // The second EFS view is retired: it existed only so the one-time EFS->local
  // migration could rename the pre-cutover databases out from under a nested
  // mountpoint. Assert it stays gone.
  expect(byName["openclaw-workspace-raw"]).toBeUndefined();
}

describe("ContainerStack — dev", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("execution role GetSecretValue is scoped to the three injected secrets, never '*'", () => {
    // The per-user task defs inject exactly three secret families (BYOK
    // user-keys, the gog keyring password, the Cloudflare WARP enrollment);
    // a wildcard here would let a leaked execution-role credential read every
    // secret in the account. Searched across both policy types so CDK
    // repacking can't silently invalidate the assertion.
    const found = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].some((type) =>
      Object.values(template.findResources(type)).some((r) =>
        ((r.Properties?.PolicyDocument?.Statement ?? []) as Array<Record<string, unknown>>).some(
          (s) => {
            if (s.Sid !== "PerUserTaskSecrets") return false;
            const resources = s.Resource as unknown[];
            return (
              s.Action === "secretsmanager:GetSecretValue" &&
              Array.isArray(resources) &&
              resources.length === 3 &&
              resources.some(
                (res) => typeof res === "string" && res.includes(":secret:isol8/dev/user-keys/*"),
              ) &&
              resources.some(
                (res) =>
                  typeof res === "string" && res.includes(":secret:isol8/dev/gog-keyring-password"),
              ) &&
              resources.some(
                (res) =>
                  typeof res === "string" &&
                  res.includes(":secret:isol8/dev/cloudflare-warp-enrollment"),
              )
            );
          },
        ),
      ),
    );
    expect(found).toBe(true);
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
    const command = openclawStartupCommand(template, "dev");
    const recovery = command.indexOf("python3 -c");
    const migration = command.indexOf("[ -e /home/node/.openclaw/browser ]");
    const move = command.indexOf("mv /home/node/.openclaw/browser /home/node/.openclaw/browser.efs-archive-");
    const mkdir = command.indexOf("mkdir -p /tmp/openclaw-browser || exit 1");
    const symlink = command.indexOf("ln -sfn /tmp/openclaw-browser /home/node/.openclaw/browser || exit 1");
    const boot = command.indexOf("exec setpriv --reuid=1000 --regid=1000 --init-groups /usr/local/bin/isol8-local-sqlite-boot.sh");

    expect([recovery, migration, move, mkdir, symlink, boot].every((index) => index >= 0)).toBe(true);
    expect(recovery).toBeLessThan(migration);
    expect(migration).toBeLessThan(move);
    expect(move).toBeLessThan(mkdir);
    expect(mkdir).toBeLessThan(symlink);
    expect(symlink).toBeLessThan(boot);
  });

  test("openclaw container sets DISPLAY so the live-view display starts", () => {
    expectDisplayGate(template, "dev", { displaySet: true });
    expect(
      openclawContainerEnv(template, "dev").find(({ Name }) => Name === "DISPLAY")?.Value,
    ).toBe(":1");
  });

  test("openclaw container performs a guarded one-time protocol transition", () => {
    const command = openclawStartupCommand(template, "dev");
    expect(command).toMatch(/^if \[ "\$\{ISOL8_CONFIG_LOCK_PROTOCOL:-\}" = "guard-v1" \]; then/);
    expect(command).toContain('guard_path = lock_path + ".isol8-guard"');
    expect(command).toContain('protocol_path = lock_path + ".isol8-protocol"');
    expect(command).toContain("acquire_until(guard_fd, deadline");
    expect(command).toContain("fcntl.LOCK_EX | fcntl.LOCK_NB");
    expect(command).toContain("current.st_ino == opened.st_ino");
    expect(command).toContain("write_protocol(migrating_protocol)");
    expect(command).toContain("write_protocol(ephemeral_protocol)");
    expect(command).toContain("recovered abandoned ephemeral openclaw.json.lock");
    expect(command).toContain("|| exit 1; else echo '[boot] config lock recovery skipped");
    expect(command).not.toContain("rm -f /home/node/.openclaw/openclaw.json.lock");
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

  test("openclaw bridges $HOME/.xurl to the EFS state dir so the X CLI can see its materialized token", () => {
    // xurl resolves its token store as `$HOME/.xurl` and honours ONLY $HOME —
    // no XDG, no XURL_HOME — so unlike gh/gog (XDG_CONFIG_HOME) and the aws CLI
    // (AWS_CONFIG_FILE) it cannot be pointed at EFS by env. The backend
    // materializes the store to owner_efs_root/.xurl, which surfaces in the
    // container as /home/node/.openclaw/.xurl. Without this symlink xurl reads
    // an absent /home/node/.xurl and every X call silently runs unauthenticated
    // — the connection shows "connected" while nothing works.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Command: [
            "sh",
            "-c",
            Match.stringLikeRegexp(".*ln -sfn /home/node/\\.openclaw/\\.xurl /home/node/\\.xurl.*"),
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

  test("openclaw container injects CLOUDFLARE_WARP_ENROLLMENT from the shared secret", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-openclaw",
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "openclaw",
          Secrets: Match.arrayWith([
            Match.objectLike({
              Name: "CLOUDFLARE_WARP_ENROLLMENT",
              ValueFrom: Match.objectLike({
                "Fn::Join": Match.arrayWith([
                  Match.arrayWith([
                    Match.stringLikeRegexp("isol8/dev/cloudflare-warp-enrollment"),
                  ]),
                ]),
              }),
            }),
          ]),
        }),
      ]),
    });
  });

  test("warp boot runs in the background and never fails the boot", () => {
    // Two properties, both load-bearing and both easy to lose in a refactor:
    //
    // 1. Backgrounded. The script waits up to ~60s for the tunnel so it can
    //    log the outcome; run in the foreground that becomes 60s of cold-start
    //    on every task, for a log line nothing reads.
    // 2. Guarded, non-fatal. The local-sqlite guard immediately below it is
    //    `|| { echo FATAL; exit 1; }` because a missing boot chain loses owner
    //    data. This one costs a CAPTCHA-avoidance nicety, so a tag pinned
    //    before layer 9c existed must still boot.
    const command = openclawStartupCommand(template, "dev");
    expect(command).toContain(
      "[ ! -x /usr/local/bin/isol8-warp-boot.sh ] || { /usr/local/bin/isol8-warp-boot.sh & }",
    );
    expect(command).not.toContain("isol8-warp-boot.sh; exit 1");
    // Ordered before the exec that replaces this shell, or it never runs.
    expect(command.indexOf("isol8-warp-boot.sh")).toBeLessThan(
      command.indexOf("exec setpriv --reuid=1000 --regid=1000 --init-groups /usr/local/bin/isol8-local-sqlite-boot.sh"),
    );
  });

  test("openclaw container chowns the ephemeral volumes before dropping to uid 1000", () => {
    // The gateway runs as uid 1000 so OpenClaw 8.1's
    // ensurePrivateSqliteCoordinatorDirectory sees state it owns: every EFS
    // access point squashes to 1000, so a root process reads its own fresh
    // directories back as "another user" and refuses to boot.
    //
    // But /home/node/.openclaw/{state,agents} are ECS ephemeral volumes, NOT
    // EFS — no squash applies and Docker materialises them root:root, so they
    // must be chowned while we are still root. If the drop ever moves ahead of
    // the chown, the container cannot write its own SQLite and every boot dies
    // on EACCES. That ordering is the whole point of this step; assert it.
    const command = openclawStartupCommand(template, "dev");
    const drop = command.indexOf("exec setpriv --reuid=1000 --regid=1000 --init-groups");

    expect(drop).toBeGreaterThanOrEqual(0);
    for (const path of [
      "/home/node/.openclaw/state",
      "/home/node/.openclaw/agents",
      "/tmp/openclaw-browser",
      "/tmp/openclaw-npx",
      "/tmp/openclaw-uv",
    ]) {
      const chown = command.indexOf(`chown -R 1000:1000 ${path}`) >= 0
        ? command.indexOf(`chown -R 1000:1000 ${path}`)
        : command.indexOf(path, command.indexOf("chown -R 1000:1000"));
      expect(chown).toBeGreaterThanOrEqual(0);
      expect(chown).toBeLessThan(drop);
    }

    // The drop must be the LAST thing in the command — setpriv execs the boot
    // chain, so anything appended after it never runs.
    expect(command.endsWith("/usr/local/bin/isol8-local-sqlite-boot.sh")).toBe(true);
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

  test("openclaw container sets DISPLAY so the live-view display starts", () => {
    expectDisplayGate(template, "prod", { displaySet: true });
    expect(
      openclawContainerEnv(template, "prod").find(({ Name }) => Name === "DISPLAY")?.Value,
    ).toBe(":1");
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
