#!/usr/bin/env node
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
import { Isol8Stage } from "./isol8-stage";

const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };

// Vercel env vars used in all Vercel deploy steps
const vercelEnv = {
  VERCEL_TOKEN: "${{ secrets.VERCEL_TOKEN }}",
  VERCEL_ORG_ID: "${{ secrets.VERCEL_ORG_ID }}",
  VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID }}",
};

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
);

app.synth();
