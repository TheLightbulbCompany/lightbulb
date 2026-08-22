#!/bin/sh
# isol8-local-sqlite-boot.sh — prepare container-local SQLite state, then hand
# off to litestream.
#
# Invoked unconditionally by the CDK startupCommand in every environment. The
# per-env `storage` gate this used to sit behind was removed in #1601 — there is
# no EFS layout left to fall back to, so there is nothing to gate on.
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
# there is no EFS->local migration: every pre-existing container was migrated
# once by hand on 2026-08-04, and a new container has never had SQLite on EFS.
#
# Sequence:
#   0. resolve owner id + preflight the binaries this script needs
#   1. assert the nested mounts really are local (a wrong mount order would
#      silently put SQLite back on NFS)
#   2. enumerate S3: agent tombstones + replicated databases
#   3. restore every replicated database, integrity-checked
#   4. cold-start log (nothing to migrate — see the note there)
#   5. apply the backend's clear-auth sentinel
#   6. exec litestream replicate -exec <doctor+gateway>
#
# Fail-loud: any enumeration or restore error exits non-zero so ECS
# replaces the task. Proceeding past a missed restore would let OpenClaw create
# a fresh empty database that litestream then snapshots FORWARD over the S3
# replica — silent data destruction, the one outcome this script must prevent.
set -eu

STATE_ROOT="${ISOL8_STATE_ROOT:-/home/node/.openclaw}"
BUCKET="${LITESTREAM_S3_BUCKET:?LITESTREAM_S3_BUCKET is required}"
LITESTREAM_CONFIG="${ISOL8_LITESTREAM_CONFIG:-/etc/isol8-litestream.yml}"
APP_CMD="${ISOL8_APP_CMD:-/usr/local/bin/isol8-local-sqlite-app.sh}"

log() { echo "[storage-boot] $*"; }

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
# The baked litestream config references $ISOL8_STATE_ROOT so it always replicates
# the SAME databases this script restored into.
export ISOL8_STATE_ROOT="$STATE_ROOT"
log "owner=$OWNER_ID bucket=$BUCKET state_root=$STATE_ROOT"

mkdir -p "$STATE_ROOT/state" "$STATE_ROOT/agents"

# --- 1. The whole design rests on these two paths being local. ECS mounts in
# literal mountPoints[] order rather than by depth, so a reordering would leave
# the EFS mount covering them and every commit would go back to NFS at ~95ms —
# a green deploy that silently fixes nothing. Fail instead of degrading.
# ALLOW-LIST, not a deny-list. The previous spelling denied nfs/smb/cifs and
# fell back to "unknown" when stat failed — and "unknown" matched nothing, so
# ANY failure to classify the filesystem PASSED the fence. A guard whose
# failure mode is "assume it's fine" is not a guard.
#
# The fence used to also protect the one-time EFS->local migration, which would
# copy and then `mv` an owner's live databases aside on the assumption these
# paths were local disk. That machinery is gone, so the stakes are lower now:
# continuing past a failed check means running SQLite on NFS — the ~95ms commit
# latency and lock contention this whole layout exists to escape — not data
# loss. Still worth failing on, because the alternative is a green deploy that
# silently fixes nothing.
for _p in "$STATE_ROOT/state" "$STATE_ROOT/agents"; do
  _fs=$(stat -f -c %T "$_p" 2>/dev/null || echo "stat-failed")
  case "$_fs" in
    ext2/ext3|ext4|xfs|btrfs|overlayfs|tmpfs) ;;
    *)
      log "FATAL: $_p reports filesystem '$_fs'; expected local disk (ext4/xfs/...)."
      log "       Either the nested ephemeral mount is missing, or the EFS mount is"
      log "       shadowing it, or the filesystem could not be identified at all."
      log "       Continuing would run SQLite on the network filesystem — ~95ms per"
      log "       commit and the lock contention this layout exists to escape — so"
      log "       the deploy would look green while fixing nothing. Refusing to boot."
      log "       Check mountPoints order in container-stack.ts and that the"
      log "       openclaw-state / openclaw-agents volumes actually attached."
      exit 1
      ;;
  esac
done

# --- 2. Enumerate the owner's S3 prefix.
KEYS=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$OWNER_ID/" \
        --query 'Contents[].Key' --output text 2>/tmp/isol8-s3-err) || {
  log "FATAL: S3 enumeration failed: $(cat /tmp/isol8-s3-err 2>/dev/null)"; exit 1; }
[ "$KEYS" = "None" ] && KEYS=""

# Agent tombstones are parsed BEFORE the database keys: a freshly deleted
# agent's prefix can legitimately contain ONLY the marker and no LTX object
# yet, and the "recognized nothing" guard below would otherwise crash-loop the
# container on it forever.
#
# Replica key layout DIFFERS BY REPLICA TYPE — verified against a real dev
# bucket, after a local file-replica test led us to the wrong answer:
#   s3   : <owner>/<db-rel-path>/<%04d-level>/<minTXID>-<maxTXID>.ltx
#   file : <path>/<db-rel-path>/ltx/<level>/<minTXID>-<maxTXID>.ltx
# Accept both: strip the trailing <level>/<file>.ltx, then strip a trailing
# "/ltx" if that form was used. Hard-coding either shape makes DBS empty, and
# an empty DBS with a live replica is the worst failure this script has.
TOMBSTONES=""
DBS=""
if [ -n "$KEYS" ]; then
  TOMBSTONES=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(agents/[^/]*\)/\.replica-deleted\$#\1#p" | sort -u)
  DBS=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(.*\)/[^/]*/[^/]*\.ltx\$#\1#p" | sed 's#/ltx$##' | sort -u)
  if [ -z "$DBS" ] && [ -z "$TOMBSTONES" ]; then
    log "FATAL: S3 prefix $OWNER_ID/ has objects but none match <db>/ltx/<level>/<file>; refusing to continue"
    exit 1
  fi
fi

# Purge tombstoned prefixes — at boot nothing is replicating them, which is the
# one moment the delete is actually safe — and drop them from the restore set.
for _t in $TOMBSTONES; do
  log "tombstone: purging deleted agent prefix $_t"
  # ORDER MATTERS: purge the replica objects FIRST and only drop the tombstone
  # once they are gone. `aws s3 rm --recursive` deletes in arbitrary order, so
  # a partial failure that happened to remove the marker first would leave
  # surviving LTX objects with nothing marking them deleted — and the next boot
  # would restore the agent we just deleted. Keeping the tombstone means the
  # next boot simply retries.
  # Both exclude spellings: the CLI matches these against the key relative to
  # the prefix, and the marker sits at the prefix root, so "*.replica-deleted" and
  # ".replica-deleted" cover the two readings. Getting this wrong only matters on a
  # PARTIAL failure — a fully successful purge removes everything anyway — but
  # that is exactly the case this ordering exists for. Verified against real S3
  # in Phase 3.
  if aws s3 rm "s3://$BUCKET/$OWNER_ID/$_t/" --recursive \
       --exclude "*.replica-deleted" --exclude ".replica-deleted" >/dev/null 2>&1; then
    aws s3 rm "s3://$BUCKET/$OWNER_ID/$_t/.replica-deleted" >/dev/null 2>&1 \
      || log "warn: tombstone marker for $_t survived its own purge (harmless; next boot retries)"
  else
    log "warn: replica purge failed for $_t — keeping the tombstone so the next boot retries"
  fi
  DBS=$(printf '%s\n' $DBS | grep -v "^$_t/" || true)
done

# --- 3. Restore every replicated database. Explicit CLI restores (rather than
# replicate's restore-if-db-not-exists, which covers only path-form entries and
# runs no integrity check) so EVERY database — including the dynamic per-agent
# set — comes back through one integrity-checked path.
#
# Restores run CONCURRENTLY, bounded. Each one is process start + a LIST + a GET
# chain — S3-I/O-bound, not CPU-bound, so a 1 vCPU task is not the limiter. The
# targets are distinct by construction ($DBS is `sort -u`'d), so no two restores
# can write the same path. Serial cost grows with the owner's agent count: ~3-5s
# at 3 databases, ~10-20s at 11 — paid on EVERY scale-from-zero wake, in front
# of doctor's own 300s cap inside a 300s health-check start period.
#
# Failure handling is the reason for the explicit PID list rather than a bare
# `wait`: `wait` with no argument discards every child's status, and a missed
# restore is the one outcome this script must never continue past.
_restore_pids=""
_restore_running=0
for rel in $DBS; do
  log "restore $rel"
  mkdir -p "$STATE_ROOT/${rel%/*}" || exit 1
  litestream restore -if-db-not-exists -if-replica-exists \
    -integrity-check quick \
    -o "$STATE_ROOT/$rel" "s3://$BUCKET/$OWNER_ID/$rel" &
  _restore_pids="$_restore_pids $!"
  _restore_running=$((_restore_running + 1))
  if [ "$_restore_running" -ge 6 ]; then
    for _p in $_restore_pids; do
      wait "$_p" || { log "FATAL: a restore failed (pid $_p)"; exit 1; }
    done
    _restore_pids=""; _restore_running=0
  fi
done
for _p in $_restore_pids; do
  wait "$_p" || { log "FATAL: a restore failed (pid $_p)"; exit 1; }
done

# --- 4. Cold start.
#
# There is deliberately no EFS->local copy here. Every pre-existing container
# was migrated once, by hand, on 2026-08-04 (all five prod owners, via the
# admin reprovision path); their pre-cutover databases remain on the EFS spine
# under pre-migration-archive/ if anyone ever needs them. A container created
# from now on has never had SQLite on EFS, so there is nothing to carry over:
# it either restores from its S3 replica above, or it is genuinely new.
#
# The copy/archive/fill-window machinery that used to live here was ~180 lines
# guarding a one-time event for five containers. Deleting it also retires the
# second EFS view (/efs-raw), which existed only so the archive could be
# renamed out from under a nested mountpoint.
if [ -z "$DBS" ]; then
  log "cold start: no replica — fresh container"
fi

# --- 4b. Imported history hand-off (written by the backend's backup import).
# The backend cannot reach the local agents/ volume, so it stages a sanitized
# per-agent tree under the EFS spine; we move it into place AFTER the restores
# so the import wins over whatever the briefly-running new agent replicated.
# litestream opens a new generation for a replaced database.
IMPORT_ROOT="$STATE_ROOT/import-state/agents"
if [ -d "$IMPORT_ROOT" ]; then
  for _src in "$IMPORT_ROOT"/*; do
    [ -d "$_src" ] || continue
    _id=$(basename "$_src")
    case "$_id" in *[!A-Za-z0-9._-]*|"") log "import-state: ignoring bad agent dir name '$_id'"; continue ;; esac
    _dst="$STATE_ROOT/agents/$_id"
    mkdir -p "$_dst"
    if [ -f "$_src/agent/openclaw-agent.sqlite" ]; then
      if sqlite3 "$_src/agent/openclaw-agent.sqlite" "PRAGMA integrity_check;" 2>/dev/null | grep -qx ok; then
        mkdir -p "$_dst/agent"
        rm -f "$_dst/agent/openclaw-agent.sqlite" "$_dst/agent/openclaw-agent.sqlite-wal" "$_dst/agent/openclaw-agent.sqlite-shm"
        mv "$_src/agent/openclaw-agent.sqlite" "$_dst/agent/openclaw-agent.sqlite"
        log "import-state: installed history database for agent $_id"
      else
        log "import-state: database for agent $_id failed integrity_check — skipped"
      fi
    fi
    if [ -d "$_src/sessions" ]; then
      mkdir -p "$_dst/sessions"
      cp -R "$_src/sessions/." "$_dst/sessions/"
      log "import-state: installed legacy session files for agent $_id"
    fi
    chown -R 1000:1000 "$_dst" 2>/dev/null || true
    rm -rf "$_src"
  done
  rmdir "$IMPORT_ROOT" 2>/dev/null || true
  rmdir "$STATE_ROOT/import-state" 2>/dev/null || true
fi

# --- 5. Clear-auth sentinel (written by the backend when it cannot reach the
# container-local store). Applied BEFORE gateway start so a freshly staged
# credentials/oauth.json re-imports — the import fires only when the persisted
# store row is absent.
SENTINEL="$STATE_ROOT/clear-auth-profiles.json"
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
