#!/usr/bin/env bash
# =============================================================================
# Fast Deploy to Dev
# =============================================================================
# Triggers a fast deploy that skips tests and uses SSM restart instead of
# ASG instance refresh. Takes ~7 min instead of ~25 min.
#
# Usage:
#   ./deploy_dev_fast.sh              # deploy current main
#   ./deploy_dev_fast.sh --push       # commit, push, then deploy
# =============================================================================

set -euo pipefail

REPO="TheLightbulbCompany/backend"
BRANCH="main"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; exit 1; }

# Check gh CLI
command -v gh >/dev/null 2>&1 || error "gh CLI not installed. Run: brew install gh"

# Check auth
gh auth status >/dev/null 2>&1 || error "Not logged into gh CLI. Run: gh auth login"

# Optional: push first
if [[ "${1:-}" == "--push" ]]; then
    info "Pushing to $BRANCH..."
    git push origin "$BRANCH"
    info "Pushed. Waiting 3s for GitHub to register..."
    sleep 3
fi

# Check we're on main and up to date
CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
    warn "Not on $BRANCH (on $CURRENT_BRANCH). Deploy will use remote $BRANCH HEAD."
fi

# Trigger fast deploy
info "Triggering fast deploy workflow..."
gh workflow run "Backend CI/CD" \
    --repo "$REPO" \
    --ref "$BRANCH" \
    -f environment=dev \
    -f fast_deploy=true

sleep 3

# Get the run ID
RUN_ID=$(gh run list \
    --repo "$REPO" \
    --workflow "Backend CI/CD" \
    --branch "$BRANCH" \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId')

if [[ -z "$RUN_ID" ]]; then
    error "Could not find workflow run"
fi

info "Workflow run: https://github.com/$REPO/actions/runs/$RUN_ID"
info "Watching progress (Ctrl+C to detach, deploy continues)..."
echo ""

gh run watch "$RUN_ID" --repo "$REPO" --exit-status
