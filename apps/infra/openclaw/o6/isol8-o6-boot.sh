#!/bin/sh
# isol8-o6-boot.sh — O6 local-SQLite boot chain.
#
# Invoked by the CDK startupCommand ONLY on envs whose openclaw-version.json
# storage field is "local-sqlite"; the image is backward compatible — nothing
# runs this unless the task-def command names it.
#
# DESIGN: the OpenClaw state dir does NOT move. Two config-less ECS volumes are
# mounted NESTED INSIDE the EFS mount, at <state-root>/state and
# <state-root>/agents — real ext4 on the task's NVMe device, which is what
# brings WAL back (OpenClaw forces rollback journal on NFS). Every other path
# under the state root is untouched EFS, so openclaw.json, credentials/,
# devices/, skills/, plugin installs, workspaces, media and logs all stay
# exactly where they are — and every absolute path already recorded inside the
# databases stays literally correct. No symlinks, no state-dir env overrides,
# and no rewriting of session markers or the agent registry.
#
# The nested mounts SHADOW whatever the EFS mount holds at those two paths, so
# the pre-cutover state is reached through a SECOND view of the same EFS
# ($EFS_RAW) — which is also where it gets archived to, since a mountpoint
# cannot be renamed but the un-shadowed view of it can.
#
# Sequence:
#   0. resolve owner id + preflight the binaries this script needs
#   1. assert the nested mounts really are local (a wrong mount order would
#      silently put SQLite back on NFS)
#   2. enumerate S3: agent tombstones + replicated databases
#   3. restore every replicated database, integrity-checked
#   4. fill anything still missing from the un-shadowed EFS view (or a prior
#      boot's archive), integrity-gate, then archive the originals
#   5. apply the backend's clear-auth sentinel
#   6. exec litestream replicate -exec <doctor+gateway>
#
# Fail-loud: any enumeration/restore/migration error exits non-zero so ECS
# replaces the task. Proceeding past a missed restore would let OpenClaw create
# a fresh empty database that litestream then snapshots FORWARD over the S3
# replica — silent data destruction, the one outcome this script must prevent.
set -eu

STATE_ROOT="${O6_STATE_ROOT:-/home/node/.openclaw}"
EFS_RAW="${O6_EFS_RAW:-/efs-raw}"
BUCKET="${LITESTREAM_S3_BUCKET:?LITESTREAM_S3_BUCKET is required}"
LITESTREAM_CONFIG="${O6_LITESTREAM_CONFIG:-/etc/isol8-litestream.yml}"
APP_CMD="${O6_APP_CMD:-/usr/local/bin/isol8-o6-app.sh}"
ARCHIVE="$EFS_RAW/pre-o6-archive"
FILL_MARKER="$ARCHIVE/.o6-fill-pending"

log() { echo "[o6-boot] $*"; }

# Dependency preflight. All of these are baked into the extended image, but a
# missing binary would otherwise degrade silently rather than loudly: no jq
# means the clear-auth sentinel is consumed WITHOUT being applied (a stale auth
# row survives and a freshly staged oauth.json never re-imports).
for _bin in sqlite3 jq aws litestream; do
  command -v "$_bin" >/dev/null 2>&1 || { log "FATAL: required binary '$_bin' is missing from this image"; exit 1; }
done

# --- 0. Owner id: per-user task env, else the facts file on the EFS spine.
OWNER_ID="${ISOL8_OWNER_ID:-}"
if [ -z "$OWNER_ID" ] && [ -f "$STATE_ROOT/isol8-facts.json" ]; then
  OWNER_ID=$(jq -r '.owner_id // empty' "$STATE_ROOT/isol8-facts.json" 2>/dev/null || true)
fi
[ -n "$OWNER_ID" ] || { log "FATAL: owner id unresolved (no ISOL8_OWNER_ID env, no isol8-facts.json)"; exit 1; }
export OWNER_ID
# The baked litestream config references $O6_STATE_ROOT so it always replicates
# the SAME databases this script restored into.
export O6_STATE_ROOT="$STATE_ROOT"
log "owner=$OWNER_ID bucket=$BUCKET state_root=$STATE_ROOT"

mkdir -p "$STATE_ROOT/state" "$STATE_ROOT/agents"

# --- 1. The whole design rests on these two paths being local. ECS mounts in
# literal mountPoints[] order rather than by depth, so a reordering would leave
# the EFS mount covering them and every commit would go back to NFS at ~95ms —
# a green deploy that silently fixes nothing. Fail instead of degrading.
for _p in "$STATE_ROOT/state" "$STATE_ROOT/agents"; do
  _fs=$(stat -f -c %T "$_p" 2>/dev/null || echo unknown)
  case "$_fs" in
    nfs*|smb*|cifs*)
      log "FATAL: $_p is on '$_fs' — the nested ephemeral mount is missing or shadowed by the EFS mount."
      log "       SQLite would run on the network filesystem and O6 would be a silent no-op. Check mountPoints order."
      exit 1
      ;;
  esac
done

# --- 2. Enumerate the owner's S3 prefix.
KEYS=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$OWNER_ID/" \
        --query 'Contents[].Key' --output text 2>/tmp/o6-s3-err) || {
  log "FATAL: S3 enumeration failed: $(cat /tmp/o6-s3-err 2>/dev/null)"; exit 1; }
[ "$KEYS" = "None" ] && KEYS=""

# Agent tombstones are parsed BEFORE the database keys: a freshly deleted
# agent's prefix can legitimately contain ONLY the marker and no LTX object
# yet, and the "recognized nothing" guard below would otherwise crash-loop the
# container on it forever.
#
# Replica keys are <owner>/<db-rel-path>/ltx/<level>/<minTXID>-<maxTXID>.ltx —
# VERIFIED by running litestream v0.5.15 against this exact config, not
# inferred: there IS a literal "ltx/" segment and levels are plain decimal.
TOMBSTONES=""
DBS=""
if [ -n "$KEYS" ]; then
  TOMBSTONES=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(agents/[^/]*\)/\.o6-deleted\$#\1#p" | sort -u)
  DBS=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(.*\)/ltx/[^/]*/[^/]*\$#\1#p" | sort -u)
  if [ -z "$DBS" ] && [ -z "$TOMBSTONES" ]; then
    log "FATAL: S3 prefix $OWNER_ID/ has objects but none match <db>/ltx/<level>/<file>; refusing to continue"
    exit 1
  fi
fi

# Purge tombstoned prefixes — at boot nothing is replicating them, which is the
# one moment the delete is actually safe — and drop them from the restore set.
for _t in $TOMBSTONES; do
  log "tombstone: purging deleted agent prefix $_t"
  aws s3 rm "s3://$BUCKET/$OWNER_ID/$_t/" --recursive >/dev/null 2>&1 \
    || log "warn: tombstone purge failed for $_t (will retry next boot)"
  DBS=$(printf '%s\n' $DBS | grep -v "^$_t/" || true)
done

# Re-applied after every fill: both migration sources predate the deletion and
# would copy the agent straight back, after which litestream republishes it and
# the delete is silently undone.
o6_drop_tombstoned() {
  for _t in $TOMBSTONES; do
    rm -rf "${STATE_ROOT:?}/$_t" 2>/dev/null || true
  done
}
o6_drop_tombstoned

# --- 3. Restore every replicated database. Explicit CLI restores (rather than
# replicate's restore-if-db-not-exists, which covers only path-form entries and
# runs no integrity check) so EVERY database — including the dynamic per-agent
# set — comes back through one integrity-checked path.
for rel in $DBS; do
  log "restore $rel"
  mkdir -p "$STATE_ROOT/$(dirname "$rel")" || exit 1
  litestream restore -if-db-not-exists -if-replica-exists \
    -integrity-check quick \
    -o "$STATE_ROOT/$rel" "s3://$BUCKET/$OWNER_ID/$rel" || {
      log "FATAL: restore failed for $rel"; exit 1; }
done

# --- 4. Fill anything the restore did not produce.
#
# Sources are ADDITIVE, not alternatives: a task killed between the two archive
# moves leaves one subtree live and the other already archived, so picking a
# single source would silently drop whichever half is in the other place.
# cp -an makes applying both idempotent — a restored replica always wins.
#
# The archive arm is BOUNDED by a fill-pending marker written before the moves.
# Unbounded, the frozen archive would be consulted on every boot forever, so an
# agent deleted after cutover would be resurrected from it. The marker clears
# as soon as the replica provably covers everything the archive holds.
FILL_RAW=0
FILL_ARCHIVE=0
if [ -d "$EFS_RAW/state" ] || [ -d "$EFS_RAW/agents" ]; then
  FILL_RAW=1
fi
if [ -f "$FILL_MARKER" ]; then
  if [ -d "$ARCHIVE/state" ] || [ -d "$ARCHIVE/agents" ]; then
    FILL_ARCHIVE=1
  fi
fi

# Close the fill window once the replica covers every archived database: from
# then on the archive is redundant and post-cutover deletions stay deleted.
if [ -f "$FILL_MARKER" ]; then
  _missing=0
  for _adb in "$ARCHIVE"/state/openclaw.sqlite "$ARCHIVE"/state/openclaw-quarantine.sqlite \
              "$ARCHIVE"/agents/*/agent/openclaw-agent.sqlite; do
    [ -f "$_adb" ] || continue
    _rel=${_adb#"$ARCHIVE"/}
    # A deliberately deleted agent must not hold the fill window open forever.
    _skip=0
    for _t in $TOMBSTONES; do
      case "$_rel" in "$_t"/*) _skip=1 ;; esac
    done
    [ "$_skip" = "1" ] && continue
    case "$DBS" in
      *"$_rel"*) ;;
      *) _missing=1 ;;
    esac
  done
  if [ "$_missing" = "0" ]; then
    rm -f "$FILL_MARKER"
    FILL_ARCHIVE=0
    log "replica covers the full archive — fill window closed"
  fi
fi

o6_fill_from() {
  _src=$1
  for _d in state agents; do
    if [ -d "$_src/$_d" ]; then
      cp -an "$_src/$_d/." "$STATE_ROOT/$_d/" || return 1
    fi
  done
  return 0
}

if [ "$FILL_RAW" = "1" ] || [ "$FILL_ARCHIVE" = "1" ]; then
  log "cutover fill (efs_raw=$FILL_RAW archive=$FILL_ARCHIVE)"
  [ "$FILL_RAW" = "1" ] && { o6_fill_from "$EFS_RAW" || exit 1; }
  [ "$FILL_ARCHIVE" = "1" ] && { o6_fill_from "$ARCHIVE" || exit 1; }
  o6_drop_tombstoned

  # Integrity gate before anything is archived. NOTE there is no path rewriting
  # here and none is needed: the databases land at the SAME absolute paths they
  # were copied from, so every session marker and agent-registry row inside
  # them stays correct by construction. That is the whole point of this design.
  for db in "$STATE_ROOT/state/openclaw.sqlite" "$STATE_ROOT/state/openclaw-quarantine.sqlite" \
            "$STATE_ROOT"/agents/*/agent/openclaw-agent.sqlite; do
    [ -f "$db" ] || continue
    ok=$(sqlite3 "$db" "PRAGMA integrity_check;" | head -1)
    [ "$ok" = "ok" ] || { log "FATAL: integrity_check failed for $db: $ok"; exit 1; }
  done

  # A stale legacy workspace-state.json would re-trip the migration gate the
  # native-seed heal used to clear; the state DB is present by construction
  # here, which is exactly that heal's own guard condition.
  rm -f "$EFS_RAW"/workspace-*/.openclaw/workspace-state.json 2>/dev/null || true

  # Archive the originals through the UN-SHADOWED view: $STATE_ROOT/state is a
  # mountpoint and cannot be renamed, but $EFS_RAW/state is an ordinary
  # directory on the same filesystem. Once moved, the backend's own "does the
  # EFS database exist" probes correctly report absent — which is what routes
  # agent-auth clearing through the boot sentinel below instead of a hot delete
  # against a database the container is no longer using.
  # The marker is armed BEFORE the first move so an interruption between the
  # two is still recoverable.
  if [ "$FILL_RAW" = "1" ]; then
    mkdir -p "$ARCHIVE" || exit 1
    : > "$FILL_MARKER" || exit 1
    for d in state agents; do
      if [ -d "$EFS_RAW/$d" ]; then mv "$EFS_RAW/$d" "$ARCHIVE/$d" || exit 1; fi
    done
    log "cutover complete; EFS originals archived at pre-o6-archive/ (fill window armed)"
  else
    log "cutover complete; filled in from archive (archive left in place)"
  fi
elif [ -z "$DBS" ]; then
  log "cold start: no replica, no EFS state — fresh container"
fi

# --- 5. Clear-auth sentinel (written by the backend when it cannot reach the
# container-local store). Applied BEFORE gateway start so a freshly staged
# credentials/oauth.json re-imports — the import fires only when the persisted
# store row is absent.
SENTINEL="$STATE_ROOT/o6-clear-auth-profiles.json"
if [ -f "$SENTINEL" ]; then
  for agent in $(jq -r '.agent_ids[]? // empty' "$SENTINEL" 2>/dev/null); do
    # ONLY the default agent. Every other agent merges its credentials from
    # main's store, so clearing a non-default row makes that agent import the
    # shared one-time refresh token independently -> refresh_token_reused
    # fan-out. The backend only ever writes ["main"]; this is the fence.
    if [ "$agent" != "main" ]; then
      log "sentinel: ignoring non-default agent '$agent' (only main may be cleared)"
      continue
    fi
    db="$STATE_ROOT/agents/$agent/agent/openclaw-agent.sqlite"
    if [ -f "$db" ]; then
      if _serr=$(sqlite3 "$db" "DELETE FROM auth_profile_store WHERE store_key = 'primary';" 2>&1); then
        log "sentinel: cleared auth row for agent $agent"
      else
        case "$_serr" in
          *"no such table"*) log "sentinel: no auth table for agent $agent (benign)" ;;
          *)
            # Consuming the sentinel here would leave the stale primary row
            # shadowing the freshly staged oauth.json with no retry request on
            # disk, so the owner's reconnect would silently keep using the
            # spent token.
            log "sentinel: FAILED for agent $agent ($_serr) — keeping sentinel for the next boot"
            SENTINEL_RETRY=1
            ;;
        esac
      fi
    else
      log "sentinel: no DB for agent $agent yet (benign)"
    fi
  done
  [ "${SENTINEL_RETRY:-0}" = "1" ] || rm -f "$SENTINEL"
fi

# --- 6. Hand off. litestream supervises the app: SIGTERM is forwarded to the
# child (the gateway, via exec in the app script), then every database
# final-syncs to S3 — but only when the child exits cleanly, so the gateway
# must exit 0 on SIGTERM.
log "starting litestream replicate (supervising doctor+gateway)"
exec litestream replicate -config "$LITESTREAM_CONFIG" -exec "$APP_CMD"
