#!/usr/bin/env node
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { ShellStep } from "aws-cdk-lib/pipelines";
import {
  AwsCredentials,
  GitHubWorkflow,
  GitHubActionStep,
  JsonPatch,
  Runner,
  StackCapabilities,
} from "cdk-pipelines-github";
import { PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN, DEV_ROOT_DOMAIN } from "./domains";
import { Isol8Stage } from "./isol8-stage";

const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };

// Vercel env vars used in all Vercel deploy steps
const vercelEnv = {
  VERCEL_TOKEN: "${{ secrets.VERCEL_TOKEN }}",
  VERCEL_ORG_ID: "${{ secrets.VERCEL_ORG_ID }}",
  VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID }}",
};

// The holding-company site (apps/company, thelightbulb.company) is a second
// Vercel project: same token and org, its own project id. Unlike the frontend,
// whose root directory is a project setting on Vercel, these steps run IN
// apps/company, so the CLI builds that directory with no project-setting
// dependency (pnpm still resolves the workspace from the repo root).
const companyVercelEnv = { ...vercelEnv, VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID_COMPANY }}" };
const COMPANY_DOMAIN = "thelightbulb.company";
const COMPANY_DIR = "apps/company";
const companyVercelSteps = (prod: boolean) => [
  { name: "Checkout", uses: "actions/checkout@v4" },
  { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
  { name: "Setup Node.js", uses: "actions/setup-node@v4", with: { "node-version": "20", cache: "pnpm" } },
  { name: "Install Vercel CLI", run: "npm install -g vercel" },
  { name: "Pull Vercel Settings", run: `cd ${COMPANY_DIR} && vercel pull --yes${prod ? " --environment=production" : ""} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
  { name: "Build Company Site", run: `cd ${COMPANY_DIR} && vercel build${prod ? " --prod" : ""} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
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
const CDKOUT_BUCKET = "cdk-hnb659fds-assets-877352799272-us-east-1";
const CDKOUT_S3_URI = `s3://${CDKOUT_BUCKET}/gha-cdk-out/\${{ github.run_id }}-\${{ github.run_attempt }}`;
const CDKOUT_OIDC_STEP = {
  name: "Authenticate Via OIDC Role",
  uses: "aws-actions/configure-aws-credentials@v4",
  with: {
    "aws-region": "us-west-2",
    "role-duration-seconds": 1800,
    "role-skip-session-tagging": true,
    "role-to-assume": "arn:aws:iam::877352799272:role/isol8-dev-github-actions",
  },
};

const pipeline = new GitHubWorkflow(app, "isol8-pipeline", {
  // postBuildSteps is injected immediately before the cdk.out upload
  // (cdk-pipelines-github pipeline.js:458), so the credentials the S3 upload
  // needs are added through the supported API rather than a positional patch.
  // Build-Synth otherwise has id-token: write but never assumes a role.
  postBuildSteps: [CDKOUT_OIDC_STEP],
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
      // Stripe per-seat Teams Price IDs — same synth-time bake + per-env
      // live/test split as the flat price above. Empty string is tolerated
      // (the backend surfaces "STRIPE_TEAM_SEAT_PRICE_ID not configured" at
      // the team-checkout call site) but org admins cannot subscribe until
      // both secrets are populated.
      STRIPE_TEAM_SEAT_PRICE_ID_DEV: "${{ secrets.STRIPE_TEAM_SEAT_PRICE_ID_DEV }}",
      STRIPE_TEAM_SEAT_PRICE_ID_PROD: "${{ secrets.STRIPE_TEAM_SEAT_PRICE_ID_PROD }}",
      // Stripe Teams BASE Price IDs — the $50/mo flat component charged
      // alongside the per-seat item. Same synth-time bake + per-env live/test
      // split as the seat price above.
      STRIPE_TEAM_BASE_PRICE_ID_DEV: "${{ secrets.STRIPE_TEAM_BASE_PRICE_ID_DEV }}",
      STRIPE_TEAM_BASE_PRICE_ID_PROD: "${{ secrets.STRIPE_TEAM_BASE_PRICE_ID_PROD }}",
      // Stripe Ultra Pro Price IDs — same synth-time bake + per-env live/test
      // split as the flat/team prices above. Empty string is tolerated (the
      // backend surfaces "STRIPE_ULTRA_PRICE_ID not configured" at the
      // ultra-checkout call site) but the Ultra tier cannot be purchased until
      // both secrets are populated.
      STRIPE_ULTRA_PRICE_ID_DEV: "${{ secrets.STRIPE_ULTRA_PRICE_ID_DEV }}",
      STRIPE_ULTRA_PRICE_ID_PROD: "${{ secrets.STRIPE_ULTRA_PRICE_ID_PROD }}",
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
    group: "isol8-deploy",
    cancelInProgress: false,
  },
});

// ---------------------------------------------------------------------------
// Dev stage — auto-deploy infra + frontend on merge to main
// ---------------------------------------------------------------------------
const devStage = new Isol8Stage(app, "dev", {
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
          name: `Alias to dev.${DEV_ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} dev.${DEV_ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
        {
          name: `Alias to admin.dev.${DEV_ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} admin.dev.${DEV_ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
          env: vercelEnv,
        },
      ],
    }),
    // The holding-company site. Preview only in dev: `dev.thelightbulb.company`
    // is the dev agent-mail suffix (SES), so no dev alias is registered on it.
    new GitHubActionStep("DeployCompanyDev", {
      jobSteps: [
        ...companyVercelSteps(false),
        { name: "Deploy to Vercel (Preview)", run: `cd ${COMPANY_DIR} && vercel deploy --prebuilt --token=$VERCEL_TOKEN`, env: companyVercelEnv },
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
//   PUT /repos/Isol8AI/isol8/environments/prod {"reviewers":[…]}
//     422 Failed to create the environment protection rule. Please ensure the
//         billing plan supports the required reviewers protection rule.
//   PUT /repos/Isol8AI/isol8/environments/prod {"wait_timer":1}
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

const prodStage = new Isol8Stage(app, "prod", {
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
          name: `Alias to admin.${ROOT_DOMAIN}`,
          run: `vercel alias \${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} admin.${ROOT_DOMAIN} --token=$VERCEL_TOKEN`,
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
          run: `cd ${COMPANY_DIR} && DEPLOY_URL=$(vercel deploy --prebuilt --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT`,
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
        {
          name: `Register www.${COMPANY_DOMAIN} project domain`,
          run: `vercel domains add www.${COMPANY_DOMAIN} company --force --token="$VERCEL_TOKEN"`,
          env: companyVercelEnv,
        },
        {
          name: `Alias to www.${COMPANY_DOMAIN}`,
          run: `vercel alias \${{ steps.company-deploy-prod.outputs.DEPLOY_URL }} www.${COMPANY_DOMAIN} --token=$VERCEL_TOKEN`,
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

  // Expire the `cdk.out` handoff artifact after one day. It is consumed by the
  // asset/deploy jobs inside the same run, so nothing reads it after the run
  // ends — at the repo default of 90 days each ~33 MB upload just accrues
  // against the org's 2 GB shared Actions/Packages storage until uploads start
  // failing with "Artifact storage quota has been hit", blocking every deploy.
  //
  // cdk-pipelines-github has no retention option (`stepsToUploadAssembly` in
  // pipeline.js hardcodes the `with:` block), and this file is regenerated on
  // every synth, so a hand-edit to deploy.yml would be silently reverted. A
  // synth-time patch is the only form of this setting that survives.
  //
  // The step index is positional because RFC 6902 has no find-by-predicate. If
  // a construct upgrade reorders the steps this throws at synth ("Cannot set
  // properties of undefined") for any step lacking a `with:` block — but it
  // would land silently on the wrong step if that step happens to have one, so
  // `deploy-workflow-retention.test.ts` asserts the setting is on the cdk.out
  // upload specifically.
  JsonPatch.add("/jobs/Build-Synth/steps/2/with/retention-days", 1),

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
    run: `aws s3 cp apps/infra/cdk.out ${CDKOUT_S3_URI}/ --recursive --only-show-errors`,
  }),
];
for (const job of assetJobNames()) {
  // Asset jobs are [Download cdk.out, Install, Authenticate, Publish]. The
  // download runs BEFORE credentials exist, so it is removed from the front
  // and re-inserted after the auth step rather than swapped in place.
  cdkoutPatches.push(JsonPatch.remove(`/jobs/${job}/steps/0`));
  cdkoutPatches.push(
    JsonPatch.add(`/jobs/${job}/steps/2`, {
      name: "Download cdk.out from S3",
      run: `aws s3 cp ${CDKOUT_S3_URI}/ cdk.out/ --recursive --only-show-errors`,
    }),
  );
}
pipeline.workflowFile.patch(...cdkoutPatches);

app.synth();
