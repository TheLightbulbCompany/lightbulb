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
  TOMBSTONES=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(agents/[^/]*\)/\.o6-deleted\$#\1#p" | sort -u)
  DBS=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(.*\)/[^/]*/[^/]*\.ltx\$#\1#p" | sed 's#/ltx\$##' | sort -u)
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
  # the prefix, and the marker sits at the prefix root, so "*.o6-deleted" and
  # ".o6-deleted" cover the two readings. Getting this wrong only matters on a
  # PARTIAL failure — a fully successful purge removes everything anyway — but
  # that is exactly the case this ordering exists for. Verified against real S3
  # in Phase 3.
  if aws s3 rm "s3://$BUCKET/$OWNER_ID/$_t/" --recursive \
       --exclude "*.o6-deleted" --exclude ".o6-deleted" >/dev/null 2>&1; then
    aws s3 rm "s3://$BUCKET/$OWNER_ID/$_t/.o6-deleted" >/dev/null 2>&1 \
      || log "warn: tombstone marker for $_t survived its own purge (harmless; next boot retries)"
  else
    log "warn: replica purge failed for $_t — keeping the tombstone so the next boot retries"
  fi
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
# EXISTENCE IS NOT ENOUGH. The nested mounts need their destination
# directories to exist inside the parent EFS mount, so after the cutover moved
# the originals into pre-o6-archive the runtime recreates them — EMPTY, on EFS.
# An existence test would therefore see "live EFS state" on every subsequent
# boot, re-arm the fill window forever, and try to re-archive onto an archive
# that already exists. Test for CONTENT.
o6_has_content() {
  [ -d "$1" ] && [ -n "$(ls -A "$1" 2>/dev/null)" ]
}
FILL_RAW=0
FILL_ARCHIVE=0
if o6_has_content "$EFS_RAW/state" || o6_has_content "$EFS_RAW/agents"; then
  FILL_RAW=1
fi
if [ -f "$FILL_MARKER" ]; then
  if o6_has_content "$ARCHIVE/state" || o6_has_content "$ARCHIVE/agents"; then
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

# Copy anything the destination does not already have, then VERIFY.
# `cp -an` cannot be trusted by exit status: GNU coreutils returns 0 when it
# skips an existing file, BSD returns 1 for the same skip. The image is Debian,
# but an exit code that means opposite things on two platforms is not something
# to build recovery on — so the copy's status is advisory and the postcondition
# (every source file now present at the destination) is what decides.
o6_copy_missing() {
  _s=$1; _d=$2
  [ -d "$_s" ] || return 0
  mkdir -p "$_d" || return 1
  cp -an "$_s/." "$_d/" 2>/dev/null || true
  # Existence is not enough: a cp that fails partway (ENOSPC, an EFS I/O
  # error, a short write) leaves a file that exists and is truncated. Compare
  # sizes. Files the destination ALREADY had are legitimately different — the
  # restored replica is newer than the fill source and must win — so only
  # files this copy was responsible for creating are checked.
  _bad=$( (cd "$_s" && find . -type f 2>/dev/null) | while IFS= read -r _f; do
    if [ ! -e "$_d/$_f" ]; then
      echo "MISSING $_f"
    elif [ "$(wc -c < "$_d/$_f" 2>/dev/null || echo -1)" -lt "$(wc -c < "$_s/$_f" 2>/dev/null || echo 0)" ]; then
      echo "TRUNCATED $_f"
    fi
  done )
  if [ -n "$_bad" ]; then
    log "FATAL: copy from $_s did not land intact at $_d:"
    printf '%s\n' "$_bad" | head -5 | sed 's/^/         /'
    return 1
  fi
  return 0
}

o6_fill_from() {
  _src=$1
  for _d in state agents; do
    o6_copy_missing "$_src/$_d" "$STATE_ROOT/$_d" || return 1
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

    # Convert to WAL here, while we still own these files exclusively.
    #
    # They were just copied off EFS, where OpenClaw force-sets
    # journal_mode=DELETE because WAL is unsafe on NFS — so they arrive as
    # rollback-journal databases with no -wal sidecar. litestream is exec'd
    # moments from now and immediately tries to establish one; against a
    # DELETE-mode database that fails ("ensure wal exists: disk I/O error"),
    # and litestream backs off EXPONENTIALLY. Observed on the first real
    # cutover: nine consecutive failures reaching a 4m16s backoff, replication
    # dead for the container's entire 5.5-minute life, with only the graceful
    # SIGTERM final-sync ever reaching S3. The gateway does convert these to
    # WAL when it opens them, but by then litestream is already deep in
    # backoff — the race is not winnable from litestream's side.
    #
    # A hard kill in that window would leave the cutover unreplicated. The
    # armed fill marker plus pre-o6-archive/ still recovers it, but that is a
    # safety net doing a job the design should not need.
    jm=$(sqlite3 "$db" "PRAGMA journal_mode=WAL;" 2>&1 | head -1)
    [ "$jm" = "wal" ] || { log "FATAL: could not put $db into WAL after cutover (got '$jm')"; exit 1; }
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
      o6_has_content "$EFS_RAW/$d" || continue
      if [ -e "$ARCHIVE/$d" ]; then
        # Anomalous: a canonical archive already exists AND live content came
        # back (an operator restore, or a rollback). DO NOT try to reconcile
        # them. Merging would have to choose a winner per colliding file, and
        # either choice destroys the other copy — with the live one being the
        # newer and the archive being the only recovery source if this task
        # dies before Litestream uploads. So: archive nothing this boot, delete
        # nothing, and say so loudly. The live databases are already copied
        # into the nested mounts and replicating; both copies survive on EFS
        # for an operator to reconcile deliberately.
        log "warn: $ARCHIVE/$d exists AND live $d has content — refusing to archive or merge."
        log "       Both copies are preserved on EFS; resolve manually. Boot continues on the live data."
        continue
      fi
      mv "$EFS_RAW/$d" "$ARCHIVE/$d" || exit 1
      # Recreate the mount destination so the next task can still mount over
      # it — the runtime would do this anyway, but leaving it implicit invites
      # exactly the confusion the content test above is guarding against.
      mkdir -p "$EFS_RAW/$d" || exit 1
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
if [ -f "$SENTINEL" ] && [ "$FILL_RAW" = "1" ]; then
  # A sentinel carried INTO the cutover boot is stale by construction, and
  # applying it would destroy a live credential.
  #
  # The backend writes the sentinel on every O6-env reconnect, but when the EFS
  # store is still visible it ALSO performs the hot DELETE against that same EFS
  # database — and returns a value no caller bounces on. So a container still
  # pinned to a pre-O6 task-def revision keeps running on the EFS layout,
  # rotating its refresh token for as long as it likes, while the sentinel sits
  # unread on the durable spine. This boot — its FIRST on the O6 revision —
  # would copy that well-rotated database in and immediately clear the row,
  # forcing a re-import of a staged oauth.json that is by now many rotations
  # spent. That is the June refresh_token_reused incident, reproduced exactly.
  #
  # Skipping is provably safe, not merely safer: the hot DELETE already applied
  # to the very database this boot just copied in, so the row is gone either
  # way. A sentinel written on any LATER boot is fresh by construction, because
  # every boot through the branch below consumes one.
  log "sentinel: dropping stale request carried into the cutover boot (hot delete already applied on EFS)"
  rm -f "$SENTINEL"
elif [ -f "$SENTINEL" ]; then
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
