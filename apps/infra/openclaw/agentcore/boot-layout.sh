#!/bin/bash
# Build the microVM's ~/.openclaw layout, then exec the gateway chain given as arguments.
#
# RUN AT FIRST INVOCATION, NOT AT CONTAINER START. AgentCore attaches the session's EFS mount
# when a session is assigned to an instance, not when a pooled instance boots: measured
# 2026-09-16, a warm-pool instance saw an EMPTY /mnt/oc at start and a populated one once a
# session landed. A layout built at start therefore symlinks nothing, and the gateway chain
# later finds no openclaw.json — doctor invents a 1.3 KB stub and the gateway refuses it
# (exit 78, "missing gateway.mode"). The contract server invokes this script from
# spawnGateway(), i.e. on the first /invocations, when the mount is guaranteed present.
#
# THE SHAPE, and why it is this way round:
#   ~/.openclaw            LOCAL directory on the microVM's overlay disk
#   ~/.openclaw/state/     LOCAL, real dir  — SQLite lives here
#   ~/.openclaw/agents/    LOCAL, real dir  — per-agent SQLite lives here
#   ~/.openclaw/<spine>    SYMLINK -> /mnt/oc/<spine>  (config, workspaces, credentials, …)
#
# KNOWN GAP (2026-09-16, unresolved): OpenClaw's state store keys every workspace-<agent>/ by
# its RESOLVED path. On ECS that was /home/node/.openclaw/workspace-<agent> (EFS mounted at
# ~/.openclaw); here it resolves to /mnt/oc/workspace-<agent>, so a state DB restored from an
# ECS-era replica refuses the turn ("workspace path alias points to a different current
# target"). Mounting EFS at ~/.openclaw is not allowed (AgentCore: mountPath must match
# /mnt/[a-zA-Z0-9._-]+/?). A fresh state DB works; a migrated one needs an identity remap.
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
  # The workspace's config is the proof the session's mount is attached and populated. Never
  # let the gateway start without it: doctor would write a stub config on the overlay and the
  # gateway would refuse to start on it anyway — a slower, confusing failure.
  [ -f "$MNT/openclaw.json" ] || { log "FATAL: $MNT is mounted but has no openclaw.json — session storage not attached or not this workspace's"; exit 1; }
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

[ "$#" -gt 0 ] || { log "FATAL: no gateway command given"; exit 1; }
log "layout ready; exec: $*"
exec "$@"
