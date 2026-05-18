#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { ShellStep } from "aws-cdk-lib/pipelines";
import {
  AwsCredentials,
  GitHubWorkflow,
  GitHubActionStep,
  JsonPatch,
  StackCapabilities,
} from "cdk-pipelines-github";
import { Isol8Stage } from "./isol8-stage";

// Tauri auto-updater compares versions semver-style. Use a monotonic
// ${MAJOR}.${MINOR}.${GITHUB_RUN_NUMBER} so every CDK pipeline run
// publishes a strictly greater version than the previous release. Bump
// the prefix manually only on a real feature/breaking change. The
// existing published baseline is desktop-dev-v0.3.0, so we stay on 0.3.x.
const DESKTOP_VERSION_PREFIX = "0.3";
const DESKTOP_VERSION = `${DESKTOP_VERSION_PREFIX}.\${{ github.run_number }}`;

// Shared jobSteps for both dev + prod desktop builds. The only per-env
// pieces (tag prefix, frontend URL, updater URL, release name, prerelease
// flag) are threaded in via the buildDesktopJobSteps() helper below.
//
// Steps are copy-faithful relocations from the now-deleted
// .github/workflows/desktop-build.yml — see git history on that file for
// the rationale comments on each step (Apple notary signing, keychain
// setup, pre-signing embedded Mach-Os, etc.). Don't re-engineer them here.
function buildDesktopJobSteps(args: {
  targetEnv: "dev" | "prod";
  frontendUrl: string;
  updateUrl: string;
  callbackUrl: string;
  releaseName: string;
  tagName: string;
  prerelease: boolean;
}): any[] {
  return [
    { name: "Checkout", uses: "actions/checkout@v4" },
    { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
    {
      name: "Setup Node.js",
      uses: "actions/setup-node@v4",
      with: { "node-version": 22, cache: "pnpm" },
    },
    {
      name: "Install Rust toolchain",
      uses: "dtolnay/rust-toolchain@stable",
      with: { targets: "aarch64-apple-darwin" },
    },
    // Same key + shared-key across dev+prod so the prod build inherits
    // the cargo registry/target dir that the earlier dev build just
    // populated on this same run.
    {
      name: "Cache Rust target dir",
      uses: "Swatinem/rust-cache@v2",
      with: {
        workspaces: "apps/desktop/src-tauri -> target",
        key: "aarch64-apple-darwin",
        "shared-key": "desktop",
      },
    },
    { name: "Install dependencies", run: "pnpm install" },
    // Patch BOTH the env-specific URLs AND the top-level .version so
    // Tauri's auto-updater compares against the monotonic CI version
    // rather than the static 0.1.0 baked into tauri.conf.json.
    {
      name: `Patch tauri.conf.json for ${args.targetEnv}`,
      run: [
        "TAURI_CFG=apps/desktop/src-tauri/tauri.conf.json",
        `jq --arg url "${args.frontendUrl}" \\`,
        `   --arg updateUrl "${args.updateUrl}" \\`,
        `   --arg version "${DESKTOP_VERSION}" '`,
        "  .version = $version |",
        "  .build.devUrl = $url |",
        "  .build.frontendDist = $url |",
        "  .app.windows[0].url = $url |",
        "  .plugins.updater.endpoints = [$updateUrl]",
        '\' "$TAURI_CFG" > "$TAURI_CFG.new"',
        'mv "$TAURI_CFG.new" "$TAURI_CFG"',
        'echo "Patched tauri.conf.json:"',
        'jq \'{version, devUrl: .build.devUrl, frontendDist: .build.frontendDist, windowUrl: .app.windows[0].url, signingIdentity: .bundle.macOS.signingIdentity, updaterEndpoints: .plugins.updater.endpoints}\' "$TAURI_CFG"',
      ].join("\n"),
    },
    // Vendor Node.js + pinned `openclaw` npm package for aarch64.
    {
      name: "Vendor browser sidecar (aarch64-apple-darwin)",
      run: "bash apps/desktop/src-tauri/scripts/vendor-sidecars.sh aarch64-apple-darwin",
    },
    // Import the Developer ID cert into a temp keychain BEFORE
    // tauri-action runs so we can pre-sign the vendored native modules.
    {
      name: "Set up signing keychain",
      run: [
        "set -euo pipefail",
        'KEYCHAIN_PATH="$RUNNER_TEMP/isol8-build.keychain-db"',
        'KEYCHAIN_PASSWORD="$(uuidgen)"',
        'CERT_PATH="$RUNNER_TEMP/cert.p12"',
        "",
        'echo "$APPLE_CERTIFICATE" | base64 --decode > "$CERT_PATH"',
        'security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"',
        'security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"',
        'security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"',
        'security import "$CERT_PATH" -P "$APPLE_CERTIFICATE_PASSWORD" \\',
        '  -A -t cert -f pkcs12 -k "$KEYCHAIN_PATH"',
        'security list-keychain -d user -s "$KEYCHAIN_PATH" \\',
        "  $(security list-keychain -d user | tr -d '\" \\t')",
        "security set-key-partition-list -S apple-tool:,apple:,codesign: \\",
        '  -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" >/dev/null',
        'rm -f "$CERT_PATH"',
        "",
        'echo "KEYCHAIN_PATH=$KEYCHAIN_PATH" >> "$GITHUB_ENV"',
        'echo "Identity in keychain:"',
        'security find-identity -v -p codesigning "$KEYCHAIN_PATH"',
      ].join("\n"),
      env: {
        APPLE_CERTIFICATE: "${{ secrets.APPLE_CERTIFICATE }}",
        APPLE_CERTIFICATE_PASSWORD:
          "${{ secrets.APPLE_CERTIFICATE_PASSWORD }}",
      },
    },
    // Pre-sign every Mach-O under bin/ (vendored Node + openclaw npm
    // native modules). Without this, Apple notary rejects the bundle.
    // See deleted desktop-build.yml history for full rationale.
    {
      name: "Pre-sign embedded native modules",
      run: [
        "set -euo pipefail",
        'export SIGN_ID="Developer ID Application: Prasiddha Parthsarthy (WZX4U3C22Y)"',
        'export NODE_ENTITLEMENTS="$PWD/apps/desktop/src-tauri/entitlements-node.plist"',
        'BIN_ROOT="apps/desktop/src-tauri/bin"',
        "",
        "CANDIDATES=$(mktemp)",
        "MACHO_LIST=$(mktemp)",
        'find "$BIN_ROOT" \\( \\',
        "  -name '*.node' -o \\",
        "  -name '*.dylib' -o \\",
        "  -name '*.so' -o \\",
        "  -name 'spawn-helper' -o \\",
        "  -name 'node-*-apple-darwin' \\",
        '\\) -type f -print0 > "$CANDIDATES"',
        "while IFS= read -r -d '' f; do",
        '  if file -b "$f" | grep -q "Mach-O"; then',
        "    printf '%s\\0' \"$f\" >> \"$MACHO_LIST\"",
        "  fi",
        'done < "$CANDIDATES"',
        'rm -f "$CANDIDATES"',
        "MACHO_COUNT=$(tr -dc '\\0' < \"$MACHO_LIST\" | wc -c | tr -d ' ')",
        'echo "Found $MACHO_COUNT Mach-O binaries to sign under $BIN_ROOT."',
        "",
        "cat > /tmp/sign-one.sh <<'WORKER'",
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'f="$1"',
        "ENT_ARGS=()",
        'case "$(basename "$f")" in',
        "  node-*-apple-darwin)",
        '    ENT_ARGS=(--entitlements "$NODE_ENTITLEMENTS")',
        "    ;;",
        "esac",
        "codesign --force --options runtime --timestamp \\",
        '  ${ENT_ARGS[@]+"${ENT_ARGS[@]}"} \\',
        '  --sign "$SIGN_ID" --keychain "$KEYCHAIN_PATH" "$f"',
        "WORKER",
        "chmod +x /tmp/sign-one.sh",
        "",
        'xargs -0 -P 8 -n 1 /tmp/sign-one.sh < "$MACHO_LIST"',
        'rm -f "$MACHO_LIST"',
      ].join("\n"),
    },
    {
      name: "Build desktop app (aarch64-apple-darwin)",
      uses: "tauri-apps/tauri-action@v0",
      with: {
        projectPath: "apps/desktop",
        tagName: args.tagName,
        releaseName: args.releaseName,
        // Create as DRAFT first. Drafts were proven to work yesterday
        // with the org's read-only default_workflow_permissions. The
        // separate "Publish draft release" step below flips the draft
        // bit via a second API call — splitting create-and-publish
        // isolates whatever scope problem auto-publish hit.
        releaseDraft: true,
        prerelease: args.prerelease,
        args: "--target aarch64-apple-darwin",
      },
      env: {
        GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}",
        APPLE_ID: "${{ secrets.APPLE_ID }}",
        APPLE_PASSWORD: "${{ secrets.APPLE_PASSWORD }}",
        APPLE_TEAM_ID: "${{ secrets.APPLE_TEAM_ID }}",
        TAURI_SIGNING_PRIVATE_KEY: "${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}",
        TAURI_SIGNING_PRIVATE_KEY_PASSWORD:
          "${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}",
        ISOL8_CALLBACK_URL: args.callbackUrl,
      },
    },
    // Flip the draft bit so the release becomes visible. Done as a
    // separate `gh release edit` API call rather than relying on
    // tauri-action's auto-publish, which 403s under the org's
    // enforced default_workflow_permissions=read posture.
    {
      name: "Publish draft release",
      run: `gh release edit "${args.tagName}" --repo Isol8AI/isol8 --draft=false`,
      env: {
        GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}",
      },
    },
  ];
}

const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };

// Vercel env vars used in all Vercel deploy steps
const vercelEnv = {
  VERCEL_TOKEN: "${{ secrets.VERCEL_TOKEN }}",
  VERCEL_ORG_ID: "${{ secrets.VERCEL_ORG_ID }}",
  VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID }}",
};

// ---------------------------------------------------------------------------
// CDK Pipeline (generates .github/workflows/deploy.yml)
// ---------------------------------------------------------------------------
const pipeline = new GitHubWorkflow(app, "isol8-pipeline", {
  synth: new ShellStep("Synth", {
    commands: [
      "cd apps/infra",
      "npm ci",
      "npx cdk synth",
    ],
    // Stripe flat-fee Price IDs are baked into the per-env ECS task env at
    // synth time by service-stack.ts. Empty string is tolerated (the backend
    // surfaces "STRIPE_FLAT_PRICE_ID not configured" at the trial-checkout
    // call site) but onboarding will fail until both secrets are populated.
    env: {
      STRIPE_FLAT_PRICE_ID_DEV: "${{ secrets.STRIPE_FLAT_PRICE_ID_DEV }}",
      STRIPE_FLAT_PRICE_ID_PROD: "${{ secrets.STRIPE_FLAT_PRICE_ID_PROD }}",
      // Same secrets the Vercel build job already consumes (lines below).
      // Re-exported here so service-stack.ts can bake the publishable key
      // into the backend container env at synth time — the paperclip-proxy
      // bootstrap HTML needs it to load the Clerk SDK.
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV: "${{ secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV }}",
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD: "${{ secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD }}",
    },
    primaryOutputDirectory: "apps/infra/cdk.out",
  }),
  awsCreds: AwsCredentials.fromOpenIdConnect({
    gitHubActionRoleArn: "arn:aws:iam::877352799272:role/isol8-dev-github-actions",
  }),
  workflowPath: "../../.github/workflows/deploy.yml",
});

// Desktop build steps for dev + prod. Held as variables so the post-synth
// JsonPatch logic below can look up the tauri-action step by NAME instead
// of by hardcoded index — see findStepIndexInPatchedJob() / TAURI_BUILD_STEP_NAME.
const devDesktopSteps = buildDesktopJobSteps({
  targetEnv: "dev",
  frontendUrl: "https://dev.isol8.co/chat",
  // dev.isol8.co is gated by Vercel deployment protection. Append
  // Vercel's documented bypass query params so Tauri's updater (a
  // plain HTTPS client that can't do the interactive auth dance) walks
  // past protection. Token leakage from `strings` on the DMG is
  // acceptable here — the endpoint only returns public GitHub Release
  // manifest data.
  updateUrl:
    "https://dev.isol8.co/api/desktop/update/{{target}}/{{current_version}}?x-vercel-protection-bypass=${{ secrets.VERCEL_AUTOMATION_BYPASS_SECRET }}&x-vercel-set-bypass-cookie=true",
  callbackUrl: "https://dev.isol8.co/auth/desktop-callback",
  releaseName: `Isol8 Desktop [DEV] v${DESKTOP_VERSION}`,
  tagName: `desktop-dev-v${DESKTOP_VERSION}`,
  prerelease: true,
});
const prodDesktopSteps = buildDesktopJobSteps({
  targetEnv: "prod",
  frontendUrl: "https://isol8.co/chat",
  // isol8.co has no Vercel deployment protection — no bypass needed.
  updateUrl:
    "https://isol8.co/api/desktop/update/{{target}}/{{current_version}}",
  callbackUrl: "https://isol8.co/auth/desktop-callback",
  releaseName: `Isol8 Desktop v${DESKTOP_VERSION}`,
  tagName: `desktop-v${DESKTOP_VERSION}`,
  prerelease: false,
});

// ---------------------------------------------------------------------------
// Dev stage — auto-deploy infra + frontend on merge to main
// ---------------------------------------------------------------------------
const devStage = new Isol8Stage(app, "dev", {
  env: awsEnv,
  environment: "dev",
});

pipeline.addStageWithGitHubOptions(devStage, {
  stackCapabilities: [
    StackCapabilities.NAMED_IAM,
    StackCapabilities.AUTO_EXPAND,
  ],
  post: [
    // Deploy frontend to Vercel (preview) and alias to dev.isol8.co
    new GitHubActionStep("DeployVercelDev", {
      jobSteps: [
        { name: "Checkout", uses: "actions/checkout@v4" },
        { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
        { name: "Setup Node.js", uses: "actions/setup-node@v4", with: { "node-version": "20", cache: "pnpm" } },
        { name: "Install Vercel CLI", run: "npm install -g vercel" },
        { name: "Pull Vercel Settings", run: "vercel pull --yes --token=$VERCEL_TOKEN", env: vercelEnv },
        {
          name: "Build Frontend",
          run: "vercel build --token=$VERCEL_TOKEN",
          env: {
            ...vercelEnv,
            NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "${{ secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV }}",
            NEXT_PUBLIC_API_URL: "${{ secrets.NEXT_PUBLIC_API_URL_DEV }}",
          },
        },
        {
          name: "Deploy to Vercel (Preview)",
          id: "vercel-deploy-dev",
          run: 'DEPLOY_URL=$(vercel deploy --prebuilt --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
          env: vercelEnv,
        },
        {
          name: "Alias to dev.isol8.co",
          run: "vercel alias ${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} dev.isol8.co --token=$VERCEL_TOKEN",
          env: vercelEnv,
        },
        {
          name: "Alias to admin.dev.isol8.co",
          run: "vercel alias ${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} admin.dev.isol8.co --token=$VERCEL_TOKEN",
          env: vercelEnv,
        },
      ],
    }),
    // Build, sign, notarize, and publish the dev desktop DMG. Runs on
    // every merge to main so installed dev clients get an auto-update
    // prompt at next launch. Published (not draft) and prerelease: true
    // so it's visible but flagged non-prod. macos-15 runner is enforced
    // via the workflowFile.patch() call below — GitHubActionStep itself
    // has no per-step runner prop in cdk-pipelines-github. Default
    // permissions for GitHubActionStep are { contents: write }, which is
    // exactly what tauri-action needs to create + upload the release.
    new GitHubActionStep("BuildDesktopDev", {
      jobSteps: devDesktopSteps,
    }),
  ],
});

// ---------------------------------------------------------------------------
// Prod stage — deploy after manual approval
// ---------------------------------------------------------------------------
const prodStage = new Isol8Stage(app, "prod", {
  env: awsEnv,
  environment: "prod",
});

pipeline.addStageWithGitHubOptions(prodStage, {
  stackCapabilities: [
    StackCapabilities.NAMED_IAM,
    StackCapabilities.AUTO_EXPAND,
  ],
  post: [
    // Deploy frontend to Vercel (production) and alias to isol8.co
    new GitHubActionStep("DeployVercelProd", {
      jobSteps: [
        { name: "Checkout", uses: "actions/checkout@v4" },
        { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
        { name: "Setup Node.js", uses: "actions/setup-node@v4", with: { "node-version": "20", cache: "pnpm" } },
        { name: "Install Vercel CLI", run: "npm install -g vercel" },
        { name: "Pull Vercel Settings", run: "vercel pull --yes --environment=production --token=$VERCEL_TOKEN", env: vercelEnv },
        {
          name: "Build Frontend (Production)",
          run: "vercel build --prod --token=$VERCEL_TOKEN",
          env: {
            ...vercelEnv,
            NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "${{ secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD }}",
            NEXT_PUBLIC_API_URL: "${{ secrets.NEXT_PUBLIC_API_URL_PROD }}",
          },
        },
        {
          name: "Deploy to Vercel (Production)",
          id: "vercel-deploy-prod",
          run: 'DEPLOY_URL=$(vercel deploy --prebuilt --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
          env: vercelEnv,
        },
        {
          name: "Alias to isol8.co",
          run: "vercel alias ${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} isol8.co --token=$VERCEL_TOKEN",
          env: vercelEnv,
        },
        {
          name: "Alias to admin.isol8.co",
          run: "vercel alias ${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} admin.isol8.co --token=$VERCEL_TOKEN",
          env: vercelEnv,
        },
      ],
    }),
    // Prod desktop build — same step list as dev with prod URLs/tag.
    // Runs after BuildDesktopDev (sequential via stage ordering), so it
    // inherits the populated cargo cache from the same run (same key +
    // shared-key).
    new GitHubActionStep("BuildDesktopProd", {
      jobSteps: prodDesktopSteps,
    }),
  ],
});

// Post-synth patches for the desktop build jobs. Two corrections:
//
// 1. runs-on must be macos-15. GitHubActionStep has no per-step runner
//    prop in cdk-pipelines-github (the pipeline-level `runner` would force
//    macOS on every job including synth + 30 asset publishers). macos-15
//    (not macos-latest) per actions/runner-images#12482: macos-14's
//    hdiutil bundler hangs ~10min then fails since the 2025-06-19
//    Provisioner bump.
//
// 2. cdk-pipelines-github decamelizes every `with:` key (turning
//    `tagName` → `tag-name`, etc.). That breaks tauri-action because its
//    inputs call `core.getInput('tagName')` which only finds the env var
//    that GitHub Actions sets from a camelCase `with:` key. Re-replace
//    the `with:` blocks here back to the camelCase shape the action
//    expects. The step is located by NAME via `tauriBuildStepIdx(...)`
//    below, not by a fragile hardcoded index — if anyone inserts a step
//    into buildDesktopJobSteps() the lookup tracks the move automatically.
function tauriActionWithBlock(args: {
  tagName: string;
  releaseName: string;
  releaseDraft: boolean;
  prerelease: boolean;
}): Record<string, unknown> {
  return {
    projectPath: "apps/desktop",
    tagName: args.tagName,
    releaseName: args.releaseName,
    releaseDraft: args.releaseDraft,
    prerelease: args.prerelease,
    args: "--target aarch64-apple-darwin",
  };
}

// The paths-filter step is inserted by gateDesktopJobOnPaths() at this
// fixed index (right after Checkout). Every subsequent step shifts down
// by one in the post-patch YAML — `findStepIndexInPatchedJob()` accounts
// for that shift.
const PATHS_FILTER_STEP_IDX = 1;
const TAURI_BUILD_STEP_NAME = "Build desktop app (aarch64-apple-darwin)";

/**
 * Find the post-patch index of a step in the synthesized YAML, given the
 * pre-patch jobSteps array. The +1 accounts for the paths-filter step
 * inserted at PATHS_FILTER_STEP_IDX, which shifts every later step down
 * by one. Fails synth loudly if the named step isn't found — better than
 * silently patching the wrong step.
 */
function findStepIndexInPatchedJob(steps: { name?: string }[], stepName: string): number {
  const idx = steps.findIndex((s) => s.name === stepName);
  if (idx < 0) {
    throw new Error(
      `Desktop build pipeline: step "${stepName}" not found in jobSteps — ` +
        `did you rename it in buildDesktopJobSteps()? Patch paths need updating.`,
    );
  }
  return idx >= PATHS_FILTER_STEP_IDX ? idx + 1 : idx;
}

// Backend-only / infra-only / frontend-only pushes to main shouldn't
// burn ~10min of macos-15 runner time rebuilding the unchanged desktop
// app. cdk-pipelines-github doesn't expose per-job `paths:` filtering or
// per-step `if:` conditions natively, so we patch the generated YAML
// directly: insert dorny/paths-filter as step 1, then gate every
// subsequent step on its `desktop` output. When nothing under
// apps/desktop/**, pnpm-lock.yaml, or this pipeline file changed, all
// real work skips and the job finishes in ~30s of runner spin-up + the
// paths check rather than a full Tauri build.
function gateDesktopJobOnPaths(jobId: string, steps: { name?: string }[]): JsonPatch[] {
  // Steps after the paths-filter (all of them, since the filter goes at
  // index 1 and every later step needs the gate) get the
  // `if: steps.changes.outputs.desktop == 'true'` condition. Range is
  // `[PATHS_FILTER_STEP_IDX + 1, steps.length]` (post-insertion the
  // last index = original length).
  const firstGated = PATHS_FILTER_STEP_IDX + 1;
  const lastGated = steps.length;
  const patches: JsonPatch[] = [
    JsonPatch.add(`/jobs/${jobId}/steps/${PATHS_FILTER_STEP_IDX}`, {
      name: "Check for desktop changes",
      id: "changes",
      uses: "dorny/paths-filter@v3",
      with: {
        // Including this pipeline file in the filter means changes to
        // the desktop build steps themselves still re-validate; and
        // pnpm-lock.yaml because dep shifts can affect the Tauri JS
        // portion of the build.
        filters:
          "desktop:\n  - 'apps/desktop/**'\n  - 'pnpm-lock.yaml'\n  - 'apps/infra/lib/app.ts'\n",
      },
    }),
  ];
  for (let i = firstGated; i <= lastGated; i++) {
    patches.push(
      JsonPatch.add(
        `/jobs/${jobId}/steps/${i}/if`,
        "steps.changes.outputs.desktop == 'true'",
      ),
    );
  }
  return patches;
}

pipeline.workflowFile.patch(
  // Grant write-scope to GITHUB_TOKEN at the WORKFLOW level. The org
  // enforces default_workflow_permissions=read (gh api returns 409 on
  // attempts to flip the repo default), and `JobSettings` in
  // cdk-pipelines-github only exposes `if`, not `permissions`. Setting
  // permissions at the workflow level here covers every job (synth,
  // asset publish, deploy, desktop builds, Vercel deploys) in one
  // place — equivalent to writing `permissions:` at the top of a
  // hand-authored workflow YAML. `id-token: write` is preserved for
  // the OIDC role assumption used by AwsCredentials.fromOpenIdConnect.
  JsonPatch.add("/permissions", {
    "id-token": "write",
    contents: "write",
  }),
  JsonPatch.replace("/jobs/dev-BuildDesktopDev/runs-on", "macos-15"),
  JsonPatch.replace("/jobs/prod-BuildDesktopProd/runs-on", "macos-15"),
  ...gateDesktopJobOnPaths("dev-BuildDesktopDev", devDesktopSteps),
  ...gateDesktopJobOnPaths("prod-BuildDesktopProd", prodDesktopSteps),
  JsonPatch.replace(
    `/jobs/dev-BuildDesktopDev/steps/${findStepIndexInPatchedJob(devDesktopSteps, TAURI_BUILD_STEP_NAME)}/with`,
    tauriActionWithBlock({
      tagName: `desktop-dev-v${DESKTOP_VERSION}`,
      releaseName: `Isol8 Desktop [DEV] v${DESKTOP_VERSION}`,
      releaseDraft: true,
      prerelease: true,
    }),
  ),
  JsonPatch.replace(
    `/jobs/prod-BuildDesktopProd/steps/${findStepIndexInPatchedJob(prodDesktopSteps, TAURI_BUILD_STEP_NAME)}/with`,
    tauriActionWithBlock({
      tagName: `desktop-v${DESKTOP_VERSION}`,
      releaseName: `Isol8 Desktop v${DESKTOP_VERSION}`,
      releaseDraft: true,
      prerelease: false,
    }),
  ),
);

app.synth();
