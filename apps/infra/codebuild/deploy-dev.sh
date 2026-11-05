#!/usr/bin/env bash
# The dev deploy, run by the lightbulb-dev-deploy CodeBuild project on every
# push to main (apps/infra/lib/stacks/deploy-runner-stack.ts, README.md next
# to this file). It is the dev stage of the generated
# .github/workflows/deploy.yml (source: apps/infra/lib/app.ts), in order:
#
#   Build-Synth                  -> synth
#   Assets-* + dev-*-Deploy      -> cdk deploy --app cdk.out 'dev/*'
#   DeployVercelDev              -> frontend preview + the three dev aliases
#   DeployCompanyDev             -> company site preview
#
# No cdk.out S3 transport: synth and deploy share this filesystem, which was
# all the us-west-2 auth at the top of deploy.yml did. Credentials are the
# project's service role (role-policy.json); CDK assumes the cdk-* bootstrap
# roles from it exactly as the GitHub jobs did from lightbulb-github-actions.
set -euo pipefail

SECRET_ID=lightbulb/dev/deploy-runner
project="${CODEBUILD_BUILD_ID%%:*}"

# --- Serialise: one deploy at a time, oldest first; newer supersedes ---------
# CodeBuild drops builds past a project concurrentBuildLimit rather than
# queueing them, so the ordering lives here. A build waits for every older
# in-progress build of this project; if a newer one exists it stops, because
# that one will deploy a superset of this commit (dev converges on the newest
# push, and a burst of merges costs two deploys, not eight). Timestamps are
# the CLI's ISO 8601 strings, which order lexically.
mine=$(aws codebuild batch-get-builds --ids "$CODEBUILD_BUILD_ID" --query 'builds[0].startTime' --output text)
while :; do
  # shellcheck disable=SC2046  # ids are whitespace-separated, meant to split
  counts=$(aws codebuild batch-get-builds \
    --ids $(aws codebuild list-builds-for-project --project-name "$project" --query 'ids[:50]' --output text) \
    --output json |
    jq --arg me "$CODEBUILD_BUILD_ID" --arg t "$mine" \
      '[.builds[] | select(.buildStatus == "IN_PROGRESS" and .id != $me) | .startTime]
       | "\(map(select(. > $t)) | length) \(map(select(. <= $t)) | length)"' -r)
  read -r newer older <<<"$counts"
  if [ "$newer" -gt 0 ]; then
    echo "superseded by a newer build of $project; not deploying $CODEBUILD_RESOLVED_SOURCE_VERSION"
    exit 0
  fi
  [ "$older" -eq 0 ] && break
  echo "waiting for $older older build(s) of $project to finish"
  sleep 30
done

# --- Secrets -> env ---------------------------------------------------------
# Every key of the JSON secret becomes an exported variable, so the key list
# lives in the secret (README.md), not here. Missing keys are simply unset:
# synth reads each value with `?? ""`.
eval "$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text |
  jq -r 'to_entries[] | "export \(.key)=\(.value | tostring | @sh)"')"

# CodeBuild's shared egress IPs hit Docker Hub's anonymous pull limit; the
# backend image is FROM python:3.12-slim and golang. Optional keys.
if [ -n "${DOCKERHUB_TOKEN:-}" ]; then
  echo "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
fi

# --- 1. Synth (= Build-Synth) -----------------------------------------------
# deploy.yml's Build step verbatim. cdk.context.json is committed, so synth
# makes no lookups. Rewrites .github/workflows/deploy.yml in the checkout,
# which is discarded with it.
(
  cd apps/infra
  npm ci --no-audit --no-fund
  rm -rf cdk.out
  npx cdk synth --no-notices --quiet
)

# --- 2. Assets + the 11 dev stacks (= Assets-*, dev-*-Deploy) ---------------
# 'dev/*' is the nested assembly-dev and nothing of prod (`cdk --app cdk.out
# ls 'dev/*'`). cdk-assets publishes each stack's assets (42 files + the
# linux/amd64 backend image, built only when its hash is new to ECR) through
# the publishing roles, then CloudFormation runs as the deploy role with the
# cfn-exec role, NAMED_IAM + AUTO_EXPAND, stacks in dependency order.
(
  cd apps/infra
  npx cdk deploy --app cdk.out 'dev/*' --require-approval never --no-notices
)

# --- 3. Vercel (= DeployVercelDev, DeployCompanyDev) -------------------------
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
corepack enable # pnpm at the root package.json's packageManager version
npm install -g vercel@63.1.0

# Frontend: Root Directory (apps/frontend) is a Vercel project setting, so the
# CLI runs from the repo root. Register each dev host before aliasing, and
# bind it to a git branch so `vercel deploy --prod` leaves it alone (the why
# is above bindDevHostToPreview in app.ts). Hosts: dev.<DEV_ROOT_DOMAIN>,
# admin.dev.<DEV_ROOT_DOMAIN>, dev.<LAUNCH_DOMAIN> from lib/domains.ts.
vercel pull --yes --token="$VERCEL_TOKEN"
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY="$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV" \
NEXT_PUBLIC_API_URL="$NEXT_PUBLIC_API_URL_DEV" \
  vercel build --token="$VERCEL_TOKEN"
DEPLOY_URL=$(vercel deploy --prebuilt --archive=tgz --token="$VERCEL_TOKEN")
for host in dev.thelightbulb.company admin.dev.thelightbulb.company; do
  vercel domains add "$host" frontend --force --token="$VERCEL_TOKEN"
  curl -fsS -o /dev/null -X PATCH \
    "https://api.vercel.com/v9/projects/$VERCEL_PROJECT_ID/domains/$host?teamId=$VERCEL_ORG_ID" \
    -H "Authorization: Bearer $VERCEL_TOKEN" -H "Content-Type: application/json" \
    -d '{"gitBranch":"main"}'
done
vercel alias "$DEPLOY_URL" dev.thelightbulb.company --token="$VERCEL_TOKEN"
vercel alias "$DEPLOY_URL" admin.dev.thelightbulb.company --token="$VERCEL_TOKEN"
vercel alias "$DEPLOY_URL" dev.flickmeon.ai --token="$VERCEL_TOKEN"

# Company site: same token and org, its own project id, preview only in dev.
# `.vercel/` holds the frontend's link and output, so it goes first.
rm -rf .vercel
(
  export VERCEL_PROJECT_ID="$VERCEL_PROJECT_ID_COMPANY"
  vercel pull --yes --token="$VERCEL_TOKEN"
  vercel build --token="$VERCEL_TOKEN"
  vercel deploy --prebuilt --archive=tgz --token="$VERCEL_TOKEN"
)
