#!/usr/bin/env bash
# Disk budget for the self-hosted Mac runner (label macos-desktop). See desktop-build.yml.
#   mac-runner-disk.sh prune       end-of-job: bound the caches, drop rebuilt-anyway outputs
#   mac-runner-disk.sh preflight   start-of-job: prune if < 10 GB free, fail if still < 6 GB
# Touches ONLY ~/.cache/lightbulb-desktop, the job's $GITHUB_WORKSPACE and $RUNNER_TEMP.
set -uo pipefail

CACHE="$HOME/.cache/lightbulb-desktop"
BUDGET_GB_TARGET=6       # prod target dir, after incremental/bundle removal
BUDGET_GB_TARGET_TEST=4  # PR-test target dir
SOFT_GB=10
HARD_GB=6

free_gb() { df -k "$HOME" | awk 'NR==2{printf "%d", $4/1024/1024}'; }
size_gb() { if [ -d "$1" ]; then du -sk "$1" | awk '{printf "%d", $1/1024/1024}'; else echo 0; fi; }

prune_target() { # dir budget_gb
  local dir="$1" budget="$2"
  [ -d "$dir" ] || return 0
  # Incremental state is the bulk and is useless once the job is over; bundles are outputs.
  find "$dir" -maxdepth 3 \( -name incremental -o -name bundle \) -type d -prune -exec rm -rf {} + 2>/dev/null
  # Still over budget: a cold next build beats a full disk.
  if [ "$(size_gb "$dir")" -gt "$budget" ]; then
    echo "$dir is $(size_gb "$dir") GB (> $budget GB budget); wiping"
    rm -rf "$dir"
  fi
}

prune() {
  prune_target "$CACHE/target" "$BUDGET_GB_TARGET"
  prune_target "$CACHE/target-test" "$BUDGET_GB_TARGET_TEST"
  # Rebuilt by every run (pnpm install; src-tauri/bin is leftovers from the retired sidecars).
  if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -d "$GITHUB_WORKSPACE" ]; then
    rm -rf "$GITHUB_WORKSPACE/apps/desktop/src-tauri/bin" "$GITHUB_WORKSPACE/apps/desktop/src-tauri/.sidecar-tmp"
    find "$GITHUB_WORKSPACE" -name node_modules -type d -prune -exec rm -rf {} + 2>/dev/null
    find "$GITHUB_WORKSPACE" -maxdepth 4 \( -name .next -o -name .expo \) -type d -prune -exec rm -rf {} + 2>/dev/null
  fi
  if [ -n "${RUNNER_TEMP:-}" ] && [ -d "$RUNNER_TEMP" ]; then
    rm -rf "$RUNNER_TEMP"/*.dmg "$RUNNER_TEMP"/*.ipa "$RUNNER_TEMP"/lightbulb_dmgmnt "$RUNNER_TEMP"/setup-pnpm-* 2>/dev/null
  fi
  echo "disk free after prune: $(free_gb) GB"
  return 0
}

preflight() {
  echo "disk free: $(free_gb) GB (target $(size_gb "$CACHE/target") GB, target-test $(size_gb "$CACHE/target-test") GB)"
  if [ "$(free_gb)" -lt "$SOFT_GB" ]; then
    prune
    if [ "$(free_gb)" -lt "$SOFT_GB" ]; then
      echo "still under $SOFT_GB GB; wiping the Rust caches"
      rm -rf "$CACHE/target" "$CACHE/target-test"
    fi
  fi
  if [ "$(free_gb)" -lt "$HARD_GB" ]; then
    echo "::error::Only $(free_gb) GB free on the runner Mac after pruning (need $HARD_GB GB). Free disk space on the Mac (see ~/actions-runner/_work, ~/.cache/lightbulb-desktop) and re-run."
    exit 1
  fi
}

case "${1:-}" in prune) prune ;; preflight) preflight ;; *) echo "usage: $0 prune|preflight" >&2; exit 2 ;; esac
