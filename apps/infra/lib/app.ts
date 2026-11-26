#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { DEV_ROOT_DOMAIN, PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "./domains";
import { LightbulbStage } from "./lightbulb-stage";

// Deployed by .github/workflows/deploy.yml: `cdk deploy 'dev/*'` on merge to main,
// `cdk deploy 'prod/*'` only on a workflow_dispatch.
const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };

new LightbulbStage(app, "dev", {
  env: awsEnv,
  environment: "dev",
  rootDomain: DEV_ROOT_DOMAIN,
  platformMailDomain: PLATFORM_MAIL_DOMAIN,
});

new LightbulbStage(app, "prod", {
  env: awsEnv,
  environment: "prod",
  rootDomain: ROOT_DOMAIN,
  platformMailDomain: PLATFORM_MAIL_DOMAIN,
});

app.synth();
