#!/usr/bin/env bash
# Flip CI / deploy workflows between GitHub-hosted and the self-hosted WSL
# runner by setting two repo Actions variables. Takes effect on the NEXT
# workflow run — no PR, no re-synth, no redeploy.
#
#   CI_RUNNER     -> the hand-authored workflows (Backend CI, Frontend CI,
#                    Infra CI, Claude reviews, e2e, image builds)
#   DEPLOY_RUNNER -> the CDK deploy pipeline (.github/workflows/deploy.yml)
#
# Both reference the variable in `runs-on:`. `isol8-wsl` targets the
# self-hosted WSL runner (its Linux minutes are free on this private repo, and
# are therefore not subject to the org Actions budget); `ubuntu-latest` targets
# GitHub-hosted runners.
#
# Some jobs deliberately do not follow CI_RUNNER: `runner-maintenance.yml` is
# hardcoded to [self-hosted, isol8-wsl] (it maintains that box), and the
# desktop/mobile builds need a macOS runner. `hosted` therefore means
# "everything that can run either way", not "everything".
#
# 2026-10-08 deploy split: CI and deploy no longer share a pool. Base
# runner-1..5 serve CI_RUNNER=isol8-wsl, runner-deploy-1..2 serve
# DEPLOY_RUNNER=isol8-deploy (same box, disjoint labels so slow backend suites
# can't starve prod dispatches). `self-hosted` therefore resolves per-scope.
# Usage:
#   scripts/switch-ci-runner.sh hosted             # both -> ubuntu-latest
#   scripts/switch-ci-runner.sh self-hosted        # ci -> isol8-wsl, deploy -> isol8-deploy
#   scripts/switch-ci-runner.sh hosted ci          # only CI_RUNNER
#   scripts/switch-ci-runner.sh self-hosted deploy # only DEPLOY_RUNNER (-> isol8-deploy)
#   scripts/switch-ci-runner.sh status
set -euo pipefail
REPO="${ISOL8_REPO:-TheLightbulbCompany/lightbulb}"

label_for() {
  case "$1/$2" in
    hosted/*) echo "ubuntu-latest" ;;
    self-hosted/ci) echo "isol8-wsl" ;;
    self-hosted/deploy) echo "isol8-deploy" ;;
    self-hosted/both) echo "SPLIT" ;;
    *) echo "error: target must be 'hosted' or 'self-hosted'" >&2; exit 1 ;;
  esac
}

case "${1:-}" in
  status)
    gh variable list --repo "$REPO" | grep -E '^(CI_RUNNER|DEPLOY_RUNNER)[[:space:]]' \
      || echo "no runner variables set on $REPO"
    ;;
  hosted|self-hosted)
    scope="${2:-both}"
    label="$(label_for "$1/$scope")"
    case "$scope" in
      ci)     gh variable set CI_RUNNER     --repo "$REPO" --body "$label" ;;
      deploy) gh variable set DEPLOY_RUNNER --repo "$REPO" --body "$label" ;;
      both)
        if [ "$label" = "SPLIT" ]; then
          gh variable set CI_RUNNER     --repo "$REPO" --body "isol8-wsl"
          gh variable set DEPLOY_RUNNER --repo "$REPO" --body "isol8-deploy"
          label="isol8-wsl (ci) + isol8-deploy (deploy)"
        else
          gh variable set CI_RUNNER     --repo "$REPO" --body "$label"
          gh variable set DEPLOY_RUNNER --repo "$REPO" --body "$label"
        fi ;;
      *) echo "error: scope must be 'ci', 'deploy', or omitted (both)" >&2; exit 1 ;;
    esac
    echo "set ${scope} runner(s) -> ${label} (${1}) on ${REPO}"
    echo "note: applies on the next workflow run; in-flight runs are unaffected"
    ;;
  *)
    echo "usage: $0 {hosted|self-hosted} [ci|deploy] | status" >&2
    exit 1 ;;
esac
