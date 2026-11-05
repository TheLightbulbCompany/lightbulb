#!/usr/bin/env bash
# One-time IAM setup for .circleci/deploy-dev.yml: the CircleCI OIDC provider
# and the lightbulb-circleci-deploy role. Run from this directory with an admin
# profile, after reading oidc-provider.md:
#
#   AWS_PROFILE=isol8-admin ./apply.sh
#
# Later edits: `aws iam update-assume-role-policy --role-name $ROLE
# --policy-document file://trust-policy.json` and the same put-role-policy as
# below (both replace in place).
set -euo pipefail
cd "$(dirname "$0")"

ORG=Jt5rbVSvEdDxeXLX3bvu6H
ROLE=lightbulb-circleci-deploy

# Thumbprint omitted on purpose: IAM fetches the CA thumbprint itself
# (aws iam create-open-id-connect-provider help, --thumbprint-list).
aws iam create-open-id-connect-provider \
  --url "https://oidc.circleci.com/org/$ORG" \
  --client-id-list "$ORG"

# 2h max session: the aws-cli orb exports static keys for the whole job and
# CircleCI's OIDC token cannot be re-used after 1h (deploy-dev.yml header).
aws iam create-role \
  --role-name "$ROLE" \
  --description "CircleCI dev deploy (.circleci/deploy-dev.yml): assumes the cdk-hnb659fds-* bootstrap roles" \
  --max-session-duration 7200 \
  --assume-role-policy-document file://trust-policy.json

aws iam put-role-policy \
  --role-name "$ROLE" \
  --policy-name "$ROLE" \
  --policy-document file://permissions-policy.json

aws iam get-role --role-name "$ROLE" --query 'Role.{Arn:Arn,MaxSession:MaxSessionDuration}'
