#!/bin/bash
# Build the microVM's ~/.openclaw layout, then hand off to the contract server.
#
# THE SHAPE, and why it is this way round:
#   ~/.openclaw            LOCAL directory on the microVM's overlay disk
#   ~/.openclaw/state/     LOCAL, real dir  — SQLite lives here
#   ~/.openclaw/agents/    LOCAL, real dir  — per-agent SQLite lives here
#   ~/.openclaw/<spine>    SYMLINK -> /mnt/oc/<spine>  (config, workspaces, credentials, …)
#
# SQLite must never sit on the EFS mount. OpenClaw classifies a database by matching its
# LEXICAL path against /proc/self/mountinfo, and measured 2026-09-16 in a real microVM:
# /home/node/.openclaw/workspace-x/probe.db LOOKS local but resolves to /mnt/oc/... on nfs4,
# and SQLite there happily reported journal_mode=wal AND committed. SQLite does not defend
# itself. So the fence below classifies the RESOLVED path (readlink -f), not the lexical one.
set -uo pipefail
OC="${OPENCLAW_HOME:-/home/node/.openclaw}"
MNT="${AGENTCORE_EFS_MOUNT:-/mnt/oc}"
log() { echo "[layout] $*"; }

mkdir -p "$OC/state" "$OC/agents" || { log "FATAL: cannot create local state dirs"; exit 1; }

if [ -d "$MNT" ]; then
  log "EFS present at $MNT — symlinking spine entries"
  for entry in "$MNT"/*; do
    [ -e "$entry" ] || continue
    name=$(basename "$entry")
    # state/ and agents/ must stay LOCAL even if they somehow exist on the mount.
    case "$name" in state|agents) log "SKIP $name — must remain local"; continue;; esac
    ln -sfn "$entry" "$OC/$name"
  done
else
  log "no EFS at $MNT — running with local-only state (bring-up mode)"
fi

# ---- the fence: classify the RESOLVED path, never the lexical one ----
fence_failed=0
for d in "$OC/state" "$OC/agents"; do
  real=$(readlink -f "$d")
  fs=$(df -T "$real" 2>/dev/null | tail -1 | awk '{print $2}')
  case "$fs" in
    nfs|nfs4|9p|virtiofs|fuse*)
      log "FATAL: $d resolves to $real on '$fs' — SQLite must not live on a network filesystem."
      log "       SQLite will NOT refuse WAL here; it reports success and corrupts silently."
      fence_failed=1
      ;;
    "") log "WARN: could not determine filesystem for $real";;
    *)  log "ok: $d -> $real ($fs)";;
  esac
done
[ "$fence_failed" -eq 0 ] || { log "FATAL: refusing to start with SQLite on a network filesystem"; exit 1; }

log "layout ready; starting contract server"
exec node /opt/agentcore/contract-server.mjs
