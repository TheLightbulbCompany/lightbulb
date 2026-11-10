#!/usr/bin/env node
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { ShellStep } from "aws-cdk-lib/pipelines";
import {
  AwsCredentials,
  AwsCredentialsProvider,
  GitHubWorkflow,
  GitHubActionStep,
  JobPermission,
  JobStep,
  JsonPatch,
  Runner,
  StackCapabilities,
} from "cdk-pipelines-github";
import { DEV_ROOT_DOMAIN, LAUNCH_DOMAIN, PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "./domains";
import { LightbulbStage } from "./lightbulb-stage";

const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };

// Vercel env vars used in all Vercel deploy steps
const vercelEnv = {
  VERCEL_TOKEN: "${{ secrets.VERCEL_TOKEN }}",
  VERCEL_ORG_ID: "${{ secrets.VERCEL_ORG_ID }}",
  VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID }}",
};

// A Vercel project domain with no git branch is a PRODUCTION domain, and every
// `vercel deploy --prod` re-points all of those at the new prod build. The dev
// hosts were registered that way in the 2026-10 domain move, so after each
// promotion dev.<root> and admin.dev.<root> served the PROD bundle (prod Clerk,
// prod API) until the next dev deploy re-aliased them. `vercel domains add`
// cannot set the branch, so this binds it over the REST API on every dev
// deploy: a branch-bound domain is a preview domain, which prod deploys leave
// alone. dev.flickmeon.ai was bound this way by hand and never had the problem.
const bindDevHostToPreview = (host: string) => ({
  name: `Keep ${host} off production deploys`,
  run: `curl -fsS -o /dev/null -X PATCH "https://api.vercel.com/v9/projects/$VERCEL_PROJECT_ID/domains/${host}?teamId=$VERCEL_ORG_ID" -H "Authorization: Bearer $VERCEL_TOKEN" -H "Content-Type: application/json" -d '{"gitBranch":"main"}'`,
  env: vercelEnv,
});

// Every job on the self-hosted box runs as the same user with the same $HOME,
// and pnpm/action-setup's default `dest` is ~/setup-pnpm, which it deletes with
// a recursive rm at the start of every run (install-pnpm/run.ts). Two jobs
// setting pnpm up at the same moment race that removal and one dies with
// "ENOTEMPTY: directory not empty, rmdir '/home/prasiddha/setup-pnpm/...'".
// Structural rather than a rare interleaving: DeployVercelProd and
// DeployCompanyProd share a `needs` set, so they start in the same second --
// both at 07:12:17Z in run 33239214668, where VercelProd was the one that died.
// `runner.temp` is per-runner-instance, so it keeps them apart. Same fix that
// frontend-ci.yml and mobile-ci.yml already carry.
const setupPnpmStep = () => ({
  name: "Setup pnpm",
  uses: "pnpm/action-setup@v6",
  with: { dest: "${{ runner.temp }}/setup-pnpm" },
});

// The holding-company site (apps/company, thelightbulb.company) is a second
// Vercel project: same token and org, its own project id. Like the frontend,
// its Root Directory (apps/company) is a PROJECT SETTING on Vercel and the CLI
// runs from the repo root — building inside apps/company instead makes the
// output trace pnpm's store at a path relative to the app, and
// `vercel deploy --prebuilt` then fails with "File does not exist:
// node_modules/.pnpm/…" (reproduced 2026-08-29).
const companyVercelEnv = { ...vercelEnv, VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID_COMPANY }}" };
// The product took the apex (2026-10); the holding-company page lives on `about.`.
const COMPANY_DOMAIN = "about.thelightbulb.company";
const companyVercelSteps = (prod: boolean) => [
  // Vercel needs no git credentials; do not leave the token in the checkout.
  { name: "Checkout", uses: "actions/checkout@v7", with: { "persist-credentials": false } },
  setupPnpmStep(),
  { name: "Setup Node.js", uses: "actions/setup-node@v7", with: { "node-version": "20", cache: "pnpm" } },
  { name: "Install Vercel CLI", run: "npm install -g vercel@63.1.0" },
  { name: "Pull Vercel Settings", run: `vercel pull --yes${prod ? " --environment=production" : ""} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
  { name: "Build Company Site", run: `vercel build${prod ? " --prod" : ""} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
];

// cdk-pipelines-github's `Runner` has a private constructor and its only
// self-hosted factory force-prepends the `self-hosted` label, so there is no
// supported way to express `runs-on: <expression>`. The pipeline reads only
// `.runsOn`, so a minimal stand-in is behaviourally identical to a real Runner.
// Isolated here so the one unavoidable assertion stays out of the pipeline
// wiring, and so the deploy runner can be driven by a GitHub Actions variable
// the same way the hand-authored workflows are.
function expressionRunner(runsOn: string): Runner {
  return { runsOn } as unknown as Runner;
}

// ---------------------------------------------------------------------------
// CDK Pipeline (generates .github/workflows/deploy.yml)
// ---------------------------------------------------------------------------
const GITHUB_ACTIONS_ROLE_ARN = "arn:aws:iam::877352799272:role/lightbulb-github-actions";

// CDK bootstrap v32 rewrote the trust policy on all four `cdk-hnb659fds-*`
// roles to add `"Condition": {"Null": {"sts:ExternalId": "true"}}` — assume is
// permitted ONLY when no ExternalId is supplied. cdk-pipelines-github hardcodes
// `roleExternalId: 'Pipeline'` on its "Assume CDK Deploy Role" step
// (lib/aws-credentials.js, unchanged since the package's first release), so the
// moment this account was re-bootstrapped every deploy AND asset job in BOTH
// environments failed at that step with AccessDenied.
//
// `AwsCredentialsProvider` is the package's own public extension point: an
// exported abstract class whose two methods are the only thing the pipeline
// ever calls on the provider (pipeline.js:294,401,444,563 — no instanceof, no
// property reads). So the external id is dropped by delegating to the stock
// OIDC provider and stripping the one `with:` key it sets.
//
// Deliberately NOT a set of `JsonPatch.remove` operations: there is one such
// step per deploy/asset job (49 on this commit) and the RFC 6902 paths would be
// positional step indexes inside job names that churn whenever a stack or asset
// is added — the same convergence and "job no longer exists" fragility the
// cdk.out patches below have to work around.
class NoExternalIdCredentials extends AwsCredentialsProvider {
  private readonly inner = AwsCredentials.fromOpenIdConnect({
    gitHubActionRoleArn: GITHUB_ACTIONS_ROLE_ARN,
  });

  public jobPermission(): JobPermission {
    return this.inner.jobPermission();
  }

  public credentialSteps(region: string, assumeRoleArn?: string): JobStep[] {
    return this.inner.credentialSteps(region, assumeRoleArn).map((step) => {
      if (!step.with || !("role-external-id" in step.with)) return step;
      const { "role-external-id": _dropped, ...rest } = step.with;
      return { ...step, with: rest };
    });
  }
}

const CDKOUT_BUCKET = "cdk-hnb659fds-assets-877352799272-us-east-1";
const CDKOUT_S3_URI = `s3://${CDKOUT_BUCKET}/gha-cdk-out/\${{ github.run_id }}-\${{ github.run_attempt }}`;
// Per-asset slices, not the whole tarball and never 6,376 loose objects. See the
// "cdk.out transport" block below. Each asset job GETs two objects: base.tgz
// (~0.5 MB: manifests, templates, publish scripts) and <assetId>.tgz (its own
// asset only). With DEPLOY_RUNNER on GitHub-hosted runners (since 2026-11-05)
// there is no host cache, and every job pulling the full ~40 MB tarball was
// ~1.6 GB of S3 internet egress per run, ~75-98 GB/day, ~$260/mo
// (docs/ops/2026-11-10-cost-cuts.md). Slices cut that to ~60 MB per run.
const CDKOUT_SLICES = `${CDKOUT_S3_URI}/slices`;
// Host-local copy of the same tarball, shared by every runner on the box.
// All DEPLOY_RUNNER instances run on one self-hosted machine outside AWS, so
// each S3 GET of the tarball is billed as internet egress: 43 asset jobs x
// ~34 MB = ~1.5 GB per run, ~817 GB/mo, ~$70/mo (docs/research/
// 2026-10-27-aws-cost-review.md). Synth writes the tarball here, so asset jobs
// on the same host read it from disk; S3 stays the source of truth and the
// fallback for any runner that does not share this filesystem. Keyed by run
// id + attempt, written via an atomic rename, so a job never reads another
// run's assembly or a half-written file.
const CDKOUT_HOST_ROOT = "$HOME/.cache/lightbulb-cdkout";
const CDKOUT_HOST_TARBALL = `${CDKOUT_HOST_ROOT}/\${{ github.run_id }}-\${{ github.run_attempt }}/cdk.out.tgz`;
// Best effort: a failed cache write never fails the job.
const CDKOUT_HOST_PUT = `{ mkdir -p "$(dirname "${CDKOUT_HOST_TARBALL}")" && cp cdk.out.tgz "${CDKOUT_HOST_TARBALL}.$$" && mv "${CDKOUT_HOST_TARBALL}.$$" "${CDKOUT_HOST_TARBALL}"; } || true`;
const CDKOUT_OIDC_STEP = {
  name: "Authenticate Via OIDC Role",
  uses: "aws-actions/configure-aws-credentials@v6",
  with: {
    "aws-region": "us-west-2",
    "role-duration-seconds": 1800,
    "role-skip-session-tagging": true,
    "role-to-assume": GITHUB_ACTIONS_ROLE_ARN,
  },
};

const pipeline = new GitHubWorkflow(app, "isol8-pipeline", {
  // postBuildSteps is injected immediately before the cdk.out upload
  // (cdk-pipelines-github pipeline.js:458), so the credentials the S3 upload
  // needs are added through the supported API rather than a positional patch.
  // Build-Synth otherwise has id-token: write but never assumes a role.
  postBuildSteps: [CDKOUT_OIDC_STEP],
  dockerAssetJobSettings: {
    setupSteps: [{ name: "Enable ARM64 image builds", uses: "docker/setup-qemu-action@v3", with: { platforms: "arm64" } }],
  },
  synth: new ShellStep("Synth", {
    commands: [
      "cd apps/infra",
      "npm ci",
      // The synth runner is self-hosted with a persistent workspace, and CDK
      // reuses a cached bundle whenever the cache key (source + docker command)
      // is unchanged -- which replayed a broken authorizer bundle across two
      // deploys after the local bundler was fixed. Start every synth clean.
      "rm -rf cdk.out",
      "npx cdk synth",
    ],
    // Stripe flat-fee Price IDs are baked into the per-env ECS task env at
    // synth time by service-stack.ts. Empty string is tolerated (the backend
    // surfaces "STRIPE_WORKSPACE_PRICE_ID not configured" at the billing
    // checkout endpoint) but onboarding will fail until both secrets are populated.
    env: {
      STRIPE_WORKSPACE_PRICE_ID_DEV: "${{ secrets.STRIPE_WORKSPACE_PRICE_ID_DEV }}",
      STRIPE_WORKSPACE_PRICE_ID_PROD: "${{ secrets.STRIPE_WORKSPACE_PRICE_ID_PROD }}",
      STRIPE_BASE_PRICE_ID_DEV: "${{ secrets.STRIPE_BASE_PRICE_ID_DEV }}",
      STRIPE_BASE_PRICE_ID_PROD: "${{ secrets.STRIPE_BASE_PRICE_ID_PROD }}",
      // Stripe Ultra Price IDs — same synth-time bake + per-env live/test
      // split as the Workspace price above. Empty string is tolerated (the
      // backend surfaces "STRIPE_ULTRA_PRICE_ID not configured" at the billing
      // checkout endpoint) but the Ultra tier cannot be purchased until
      // both secrets are populated.
      STRIPE_ULTRA_PRICE_ID_DEV: "${{ secrets.STRIPE_ULTRA_PRICE_ID_DEV }}",
      STRIPE_ULTRA_PRICE_ID_PROD: "${{ secrets.STRIPE_ULTRA_PRICE_ID_PROD }}",
      // $20/month per member from the 6th (T04). Empty = seat billing fails closed.
      STRIPE_SEAT_PRICE_ID_DEV: "${{ secrets.STRIPE_SEAT_PRICE_ID_DEV }}",
      STRIPE_SEAT_PRICE_ID_PROD: "${{ secrets.STRIPE_SEAT_PRICE_ID_PROD }}",
      // $100/month Front desk add-on. Empty = the add-on cannot be bought.
      STRIPE_FRONT_DESK_PRICE_ID_DEV: "${{ secrets.STRIPE_FRONT_DESK_PRICE_ID_DEV }}",
      STRIPE_FRONT_DESK_PRICE_ID_PROD: "${{ secrets.STRIPE_FRONT_DESK_PRICE_ID_PROD }}",
      OPENROUTER_WORKSPACE_ID_DEV: "${{ vars.OPENROUTER_WORKSPACE_ID_DEV }}",
      OPENROUTER_WORKSPACE_ID_PROD: "${{ vars.OPENROUTER_WORKSPACE_ID_PROD }}",
      OPENROUTER_MANAGED_MODEL_ID_DEV: "${{ vars.OPENROUTER_MANAGED_MODEL_ID_DEV }}",
      OPENROUTER_MANAGED_MODEL_ID_PROD: "${{ vars.OPENROUTER_MANAGED_MODEL_ID_PROD }}",
      OPENROUTER_MODEL_POLICY_JSON_DEV: "${{ vars.OPENROUTER_MODEL_POLICY_JSON_DEV }}",
      OPENROUTER_MODEL_POLICY_JSON_PROD: "${{ vars.OPENROUTER_MODEL_POLICY_JSON_PROD }}",
      STRIPE_PUBLISHABLE_KEY_DEV: "${{ vars.STRIPE_PUBLISHABLE_KEY_DEV }}",
      STRIPE_PUBLISHABLE_KEY_PROD: "${{ vars.STRIPE_PUBLISHABLE_KEY_PROD }}",
      // Same secrets the Vercel build job already consumes (lines below).
      // Re-exported here so service-stack.ts can bake the publishable key
      // into the backend container env at synth time — the paperclip-proxy
      // bootstrap HTML needs it to load the Clerk SDK.
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV: "${{ secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV }}",
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD: "${{ secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD }}",
      // Empty GitHub variables keep desktop submissions closed in both envs.
      DESKTOP_EXECUTION_OWNER_DEV: "${{ vars.DESKTOP_EXECUTION_OWNER_DEV }}",
      DESKTOP_EXECUTION_OWNER_PROD: "${{ vars.DESKTOP_EXECUTION_OWNER_PROD }}",
      // OpenAI plugin portal's domain token, served at /.well-known/openai-apps-challenge. Empty = 404.
      OPENAI_APPS_CHALLENGE_TOKEN_DEV: "${{ vars.OPENAI_APPS_CHALLENGE_TOKEN_DEV }}",
      OPENAI_APPS_CHALLENGE_TOKEN_PROD: "${{ vars.OPENAI_APPS_CHALLENGE_TOKEN_PROD }}",
    },
    primaryOutputDirectory: "apps/infra/cdk.out",
  }),
  awsCreds: new NoExternalIdCredentials(),
  workflowPath: "../../.github/workflows/deploy.yml",
  // Pipeline runner is driven by the `DEPLOY_RUNNER` repo Actions variable so
  // hosted <-> self-hosted flips from the GitHub UI (or
  // scripts/switch-ci-runner.sh) with no code change or re-synth — set it to
  // `isol8-wsl` (self-hosted WSL; free Linux minutes) or `ubuntu-latest`
  // (GitHub-hosted). See expressionRunner() for why this can't be a plain
  // Runner. Desktop DMG builds are NOT part of this pipeline — they live in
  // the standalone .github/workflows/desktop-build.yml on a self-hosted macOS
  // runner.
  runner: expressionRunner("${{ vars.DEPLOY_RUNNER }}"),
  // Serialize deploys: only one pipeline run may touch the CloudFormation
  // stacks at a time. cancelInProgress:false queues an overlapping deploy
  // rather than killing an in-flight CFN update (two concurrent deploys
  // racing on the same stack produce "UPDATE_IN_PROGRESS ... can not be
  // updated" failures).
  concurrency: {
    group: "lightbulb-deploy",
    cancelInProgress: false,
  },
});

// ---------------------------------------------------------------------------
// Dev stage — auto-deploy infra + frontend on merge to main
// ---------------------------------------------------------------------------
const devStage = new LightbulbStage(app, "dev", {
  env: awsEnv,
  environment: "dev",
  rootDomain: DEV_ROOT_DOMAIN,
  platformMailDomain: PLATFORM_MAIL_DOMAIN,
});

pipeline.addStageWithGitHubOptions(devStage, {
  stackCapabilities: [
    StackCapabilities.NAMED_IAM,
    StackCapabilities.AUTO_EXPAND,
  ],
  post: [
    // Deploy frontend to Vercel (preview) and alias to dev.{rootDomain}
    new GitHubActionStep("DeployVercelDev", {
      jobSteps: [
        { name: "Checkout", uses: "actions/checkout@v7" },
        setupPnpmStep(),
        { name: "Setup Node.js", uses: "actions/setup-node@v7", with: { "node-version": "20", cache: "pnpm" } },
        { name: "Install Vercel CLI", run: "npm install -g vercel@63.1.0" },
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
          run: 'DEPLOY_URL=$(vercel deploy --prebuilt --archive=tgz --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
          env: vercelEnv,
        },
        {
          // REGISTER before aliasing — same SSO-wall trap as the prod apex below:
          // a host Vercel does not know as a project domain sits behind its login.
          name: `Register dev.${DEV_ROOT_DOMAIN} project domain`,
          run: `vercel domains add dev.${DEV_ROOT_DOMAIN} frontend --force --token="$VERCEL_TOKEN"`,
          env: vercelEnv,
        },
        bindDevHostToPreview(`dev.${DEV_ROOT_DOMAIN}`),
        {
          name: `Register admin.dev.${DEV_ROOT_DOMAIN} project domain`,
          run: `vercel domains add admin.dev.${DEV_ROOT_DOMAIN} frontend --force --token="$VERCEL_TOKEN"`,
          env: vercelEnv,
        },
        bindDevHostToPreview(`admin.dev.${DEV_ROOT_DOMAIN}`),
        {
          name: `Alias to dev.${DEV_ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} dev.${DEV_ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        {
          name: `Alias to admin.dev.${DEV_ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} admin.dev.${DEV_ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        {
          name: `Alias to dev.${LAUNCH_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} dev.${LAUNCH_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
      ],
    }),
    // The holding-company site. Preview only in dev.
    new GitHubActionStep("DeployCompanyDev", {
      jobSteps: [
        ...companyVercelSteps(false),
        { name: "Deploy to Vercel (Preview)", run: "vercel deploy --prebuilt --archive=tgz --token=$VERCEL_TOKEN", env: companyVercelEnv },
      ],
    }),
  ],
});

// ---------------------------------------------------------------------------
// Prod stage — deploy ONLY on a human `workflow_dispatch`
//
// Merging to main used to ship straight to production: dev deployed, then prod
// deployed off the same run with nothing in between, so there was no
// dev-validated state to sit on and every merge WAS a release.
//
// The package's documented manual-approval mechanism is `gitHubEnvironment` +
// GitHub Environment protection rules, and that is unavailable on this plan.
// Required reviewers and wait timers are public-repo-only on Free/Pro/Team;
// this org is Team with a private repo, and the API says so:
//
//   PUT /repos/TheLightbulbCompany/lightbulb/environments/prod {"reviewers":[…]}
//     422 Failed to create the environment protection rule. Please ensure the
//         billing plan supports the required reviewers protection rule.
//   PUT /repos/TheLightbulbCompany/lightbulb/environments/prod {"wait_timer":1}
//     422 … supports the wait timer protection rule.
//
// (The `dev` and `prod` environments do exist, with `protection_rules: []` —
// pointing jobs at them gates nothing.)
//
// So the gate is the workflow trigger instead: every prod stack Deploy job
// carries `if: github.event_name == 'workflow_dispatch'`, via the stage-level
// `jobSettings` the package exposes for exactly this. On `push` those jobs
// skip; a human running the workflow from the Actions tab deploys them.
// Nothing else changes — the job DAG, stack set, and ordering are identical,
// and dev still deploys on every merge.
const PROD_PROMOTE_IF = "github.event_name == 'workflow_dispatch'";

const prodStage = new LightbulbStage(app, "prod", {
  env: awsEnv,
  environment: "prod",
  rootDomain: ROOT_DOMAIN,
  platformMailDomain: PLATFORM_MAIL_DOMAIN,
});

pipeline.addStageWithGitHubOptions(prodStage, {
  stackCapabilities: [
    StackCapabilities.NAMED_IAM,
    StackCapabilities.AUTO_EXPAND,
  ],
  // Reaches the 13 prod stack Deploy jobs only — `jobForGitHubActionStep`
  // (pipeline.js:550) renders pipeline-level job settings, never the stage's,
  // so DeployVercelProd is gated by an explicit patch further down.
  jobSettings: { if: PROD_PROMOTE_IF },
  post: [
    // Deploy frontend to Vercel (production) and alias to {rootDomain}
    new GitHubActionStep("DeployVercelProd", {
      jobSteps: [
        { name: "Checkout", uses: "actions/checkout@v7" },
        setupPnpmStep(),
        { name: "Setup Node.js", uses: "actions/setup-node@v7", with: { "node-version": "20", cache: "pnpm" } },
        { name: "Install Vercel CLI", run: "npm install -g vercel@63.1.0" },
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
          run: 'DEPLOY_URL=$(vercel deploy --prebuilt --archive=tgz --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
          env: vercelEnv,
        },
        {
          // REGISTER before aliasing. The project's SSO protection is set to
          // `all_except_custom_domains`, so a host Vercel does not know as a
          // project domain is treated as a deployment URL and sits behind the
          // Vercel login wall. The apex was aliased but never registered, which
          // is why flickmeon.ai answered 302 -> vercel.com/sso-api while
          // www.flickmeon.ai (registered just below) served fine.
          name: `Register ${ROOT_DOMAIN} project domain`,
          run: `vercel domains add ${ROOT_DOMAIN} frontend --force --token="$VERCEL_TOKEN"`,
          env: vercelEnv,
        },
        {
          name: `Alias to ${ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} ${ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        {
          name: `Register www.${ROOT_DOMAIN} project domain`,
          run: `vercel domains add www.${ROOT_DOMAIN} frontend --force --token="$VERCEL_TOKEN"`,
          env: vercelEnv,
        },
        {
          name: `Alias to www.${ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} www.${ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        {
          name: `Register admin.${ROOT_DOMAIN} project domain`,
          run: `vercel domains add admin.${ROOT_DOMAIN} frontend --force --token="$VERCEL_TOKEN"`,
          env: vercelEnv,
        },
        {
          name: `Alias to admin.${ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} admin.${ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        // The retired product domain stays on this project: middleware answers it
        // (decideLaunchHostRouting). Without a fresh alias it would keep serving
        // the last build made before the move.
        {
          name: `Alias to ${LAUNCH_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} ${LAUNCH_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        {
          name: `Alias to www.${LAUNCH_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} www.${LAUNCH_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
      ],
    }),
    // Gated by the same JsonPatch mechanism as DeployVercelProd (below).
    new GitHubActionStep("DeployCompanyProd", {
      jobSteps: [
        ...companyVercelSteps(true),
        {
          name: "Deploy to Vercel (Production)",
          id: "company-deploy-prod",
          run: 'DEPLOY_URL=$(vercel deploy --prebuilt --archive=tgz --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
          env: companyVercelEnv,
        },
        // Register before aliasing — the same SSO-wall trap as the product apex.
        {
          name: `Register ${COMPANY_DOMAIN} project domain`,
          run: `vercel domains add ${COMPANY_DOMAIN} company --force --token="$VERCEL_TOKEN"`,
          env: companyVercelEnv,
        },
        {
          name: `Alias to ${COMPANY_DOMAIN}`,
          run: `vercel alias \${{ steps.company-deploy-prod.outputs.DEPLOY_URL }} ${COMPANY_DOMAIN} --token=$VERCEL_TOKEN`,
          env: companyVercelEnv,
        },
      ],
    }),
  ],
});

pipeline.workflowFile.patch(
  // Grant write-scope to GITHUB_TOKEN at the WORKFLOW level. The org
  // enforces default_workflow_permissions=read (gh api returns 409 on
  // attempts to flip the repo default), and `JobSettings` in
  // cdk-pipelines-github only exposes `if`, not `permissions`. Setting
  // permissions at the workflow level here covers every job (synth,
  // asset publish, deploy, Vercel deploys) in one
  // place — equivalent to writing `permissions:` at the top of a
  // hand-authored workflow YAML. `id-token: write` is preserved for
  // the OIDC role assumption used by AwsCredentials.fromOpenIdConnect.
  JsonPatch.add("/permissions", {
    "id-token": "write",
    contents: "write",
  }),

  // Gate the prod frontend release on the same `workflow_dispatch` condition
  // the prod stack Deploy jobs get from `jobSettings` above.
  //
  // It would skip anyway — it `needs:` all 13 prod Deploy jobs, and a job whose
  // dependencies all skip is itself skipped — but this job is the half users
  // actually see (the root domain, www, admin), so the fence is stated on the job
  // rather than inferred from the shape of the DAG. `GitHubActionStep` has no
  // `if` prop and stage `jobSettings` do not reach it, so a patch is the only
  // route. Unlike the positional step patch above, the path is a job NAME, so
  // it cannot land on the wrong job: rename the step and RFC 6902 fails to
  // resolve the path and synth throws, rather than gating something else.
  JsonPatch.add("/jobs/prod-DeployVercelProd/if", PROD_PROMOTE_IF),
  JsonPatch.add("/jobs/prod-DeployCompanyProd/if", PROD_PROMOTE_IF),

  // Queue pending runs instead of evicting them.
  //
  // `cancel-in-progress: false` above protects a run that is already
  // `in_progress`. It does NOT protect a run that is still PENDING: by default
  // a concurrency group holds at most ONE pending run, and "any existing
  // pending job or workflow in the same concurrency group will be canceled and
  // the new queued job or workflow will take its place".
  //
  // Prod ships only on `workflow_dispatch`, and this pipeline is runner-bound
  // (~2 concurrent job slots for 67 jobs, 10-30 min per run), so a promotion
  // routinely sits PENDING for minutes waiting on the group. Every merge to
  // main inside that window fires a push run that silently evicts it. On the
  // night of 2026-09-07 that killed three consecutive promotions of a live
  // prod defect fix -- runs 34177495429, 34177614173 and 34173158922, each
  // cancelled having run ZERO jobs, which is the signature of a pending
  // eviction. The person merging never feels the cost; it lands on whoever is
  // promoting, which is why it recurs.
  //
  // `queue: max` raises the pending limit to 100 and drains FIFO, so nothing is
  // evicted and a dispatched promotion always eventually runs. The GROUP IS
  // UNCHANGED -- one run at a time still touches CloudFormation -- and that is
  // exactly why this is preferred over giving `workflow_dispatch` a group of
  // its own: separate groups would let a push run and a promotion (which
  // deploys the dev stage first, by design) deploy the same dev stacks at once.
  //
  // Tradeoff, deliberately accepted: eviction was also acting as coalescing, so
  // a burst of merges used to collapse into one or two dev deploys and now runs
  // one per merge. Nothing is lost -- dev still converges on HEAD -- it costs
  // self-hosted runner time and queue depth. `cancel-in-progress: true` would
  // restore the coalescing but is a validation ERROR alongside `queue: max`,
  // and would kill in-flight CloudFormation updates besides.
  //
  // Not expressible through the construct: `ConcurrencyOptions` carries exactly
  // `group` and `cancelInProgress` and renders exactly those two keys
  // (cdk-pipelines-github pipeline.js:205-208). Same patch mechanism, and the
  // same stable non-positional path, as the two `if` patches above.
  JsonPatch.add("/concurrency/queue", "max"),
);



// ---------------------------------------------------------------------------
// cdk.out transport: S3 instead of GitHub Actions artifacts.
//
// cdk-pipelines-github hardcodes actions/upload-artifact + download-artifact
// (node_modules/cdk-pipelines-github/lib/pipeline.js:21,502,511,607,628) and
// exposes NO option to change it -- `artifact` appears nowhere in its public
// pipeline.d.ts. So the transport is swapped here, by patching the generated
// workflow, the same mechanism the cdk.out retention cap uses.
//
// WHY: Actions artifact storage is a shared account-wide quota (Actions +
// Packages). Every deploy uploaded a fresh ~33 MB cdk.out across 6218 files;
// once the quota is hit, `Upload cdk.out` fails and ALL 29 downstream jobs
// skip -- backend, infra, AND both Vercel jobs, since Vercel deploys through
// this pipeline (prod-DeployVercelProd), not through Vercel's Git
// integration. Deleting artifacts frees the space immediately but the quota
// figure lags 6-12h (reported up to 24-48h), so a reclaim does not unblock.
// Moving the transport to S3 removes this pipeline from the quota entirely.
//
// WHERE: the CDK bootstrap assets bucket, which the OIDC role the asset jobs
// already assume can write. Deliberately NOT a new bucket: creating one needs
// a CDK deploy, and CDK deploys are what this unblocks.
//
// ONE TARBALL, NOT A RECURSIVE COPY. The first version of this shipped cdk.out
// as ~6,376 loose objects and every one of the 39 Assets-* jobs pulled the whole
// tree back down. The assets bucket is the account's only `aws:kms` bucket and
// bootstrap leaves `BucketKeyEnabled=false`, so each object GET is also one KMS
// Decrypt: measured 2026-08-08..09-07, 11,709 downloads x 6,373 objects = 74.6M
// Decrypt calls = $223.81/mo, plus $29.85 GET + $21.86 PUT + $60.33 egress --
// more than all customer Bedrock inference combined. A tar.gz makes it 1 object
// (89 MB -> 33 MB), which is 1 Decrypt and 1 GET per job and a third of the
// egress: ~$336/mo -> ~$27/mo, without touching the bootstrap stack.
//
// Job names are read from the committed workflow rather than hardcoded: the
// Assets-FileAssetN set changes whenever assets are added or removed, and a
// stale hardcoded list would silently skip a job's download. CI's workflow
// drift check fails loudly if this enumeration goes stale.



const assetJobNames = (): string[] => {
  const workflowPath = path.join(__dirname, "..", "..", "..", ".github", "workflows", "deploy.yml");
  if (!fs.existsSync(workflowPath)) return [];
  return [
    ...new Set(
      fs
        .readFileSync(workflowPath, "utf8")
        .split("\n")
        .map((line) => /^ {2}(Assets-[A-Za-z0-9]+):\s*$/.exec(line)?.[1])
        .filter((name): name is string => Boolean(name)),
    ),
  ];
};

const cdkoutPatches: JsonPatch[] = [
  // Build-Synth steps after postBuildSteps injection:
  // [Checkout, Build, Authenticate, Upload cdk.out]. Only the upload needs
  // patching -- the construct offers no hook for the artifact step itself
  // (pipeline.js:502,511 hardcode actions/upload-artifact).
  JsonPatch.replace("/jobs/Build-Synth/steps/3", {
    name: "Upload cdk.out to S3",
    // Uploads per-asset slices (see CDKOUT_SLICES), seeds the host cache with
    // the whole tarball, and prunes host-cache entries older than a day.
    run: [
      "rm -rf cdkout-slices",
      `tar -czf cdk.out.tgz -C apps/infra cdk.out && node apps/infra/scripts/slice-cdkout.mjs apps/infra/cdk.out cdkout-slices && aws s3 cp cdkout-slices ${CDKOUT_SLICES} --recursive --only-show-errors`,
      CDKOUT_HOST_PUT,
      `find "${CDKOUT_HOST_ROOT}" -mindepth 1 -maxdepth 1 -mmin +1440 -exec rm -rf {} + 2>/dev/null || true`,
    ].join("\n"),
  }),
];
for (const job of assetJobNames()) {
  // Asset jobs are [Download cdk.out, Install, Authenticate, Publish]. The
  // download runs BEFORE credentials exist, so it is removed from the front
  // and re-inserted after the auth step rather than swapped in place.
  // `rm -rf cdk.out` before the extract: asset jobs never check out, so on the
  // self-hosted runner the workspace still holds whatever the last job left
  // there, and tar merges into an existing tree rather than replacing it.
  cdkoutPatches.push(JsonPatch.remove(`/jobs/${job}/steps/0`));
  cdkoutPatches.push(
    JsonPatch.add(`/jobs/${job}/steps/2`, {
      name: "Download cdk.out from S3",
      run: [
        `if [ -s "${CDKOUT_HOST_TARBALL}" ]; then`,
        `  echo "cdk.out: host cache hit" && cp "${CDKOUT_HOST_TARBALL}" cdk.out.tgz`,
        "  rm -rf cdk.out && tar -xzf cdk.out.tgz",
        "else",
        `  aws s3 cp ${CDKOUT_SLICES}/base.tgz cdk.out.tgz --only-show-errors || exit 1`,
        "  rm -rf cdk.out && tar -xzf cdk.out.tgz",
        `  id=$(sed -n 's/.*publish "\\([0-9a-f]*\\):.*/\\1/p' cdk.out/assembly-*/publish-${job}-step.sh)`,
        `  [ -n "$id" ] && aws s3 cp "${CDKOUT_SLICES}/$id.tgz" asset.tgz --only-show-errors && tar -xzf asset.tgz || exit 1`,
        "fi",
      ].join("\n"),
    }),
  );
  // The `Install` step is `npm install --no-save cdk-assets` and the publish
  // script runs through npx. The self-hosted pool's BURST instances carry no
  // node/npm, so every asset job that lands on one dies with exit 127 and the
  // whole deploy fails (three dev deploys on 2026-09-17 alone; whole-run
  // re-runs were the only recovery). setup-node fetches a toolcache node on
  // any runner that lacks one, and is a no-op on the standing runners.
  cdkoutPatches.push(
    JsonPatch.add(`/jobs/${job}/steps/0`, {
      name: "Setup Node",
      uses: "actions/setup-node@v7",
      with: { "node-version": "20" },
    }),
  );
}
pipeline.workflowFile.patch(...cdkoutPatches);

app.synth();
