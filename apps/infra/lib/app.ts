#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { ShellStep } from "aws-cdk-lib/pipelines";
import {
  AwsCredentials,
  GitHubWorkflow,
  GitHubActionStep,
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

app.synth();
