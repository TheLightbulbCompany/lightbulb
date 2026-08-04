#!/bin/sh
# isol8-o6-boot.sh — O6 local-SQLite boot chain.
#
# Invoked by the CDK startupCommand ONLY on envs whose openclaw-version.json
# storage field is "local-sqlite"; the image is backward compatible — nothing
# runs this unless the task-def command names it.
#
# Sequence:
#   0. resolve owner id (per-user task env, else the EFS facts file)
#   1. create the local state root + reverse-symlink the durable spine to EFS
#   2. restore every replicated SQLite DB from S3 (explicit, integrity-checked)
#   3. fill any database the restore did not produce from EFS (or a prior
#      boot's archive): copy, rewrite the two relocated path prefixes inside
#      the DBs, integrity-check, then archive the live EFS originals
#   4. apply the backend's clear-auth sentinel (if present)
#   5. exec litestream replicate -exec <doctor+gateway>
#
# Fail-loud: any enumeration/restore/migration error exits non-zero so ECS
# replaces the task. Proceeding past a missed restore would let OpenClaw
# create a fresh empty DB that litestream then snapshots FORWARD over the S3
# replica — silent data destruction, the one outcome this script must never
# allow.
set -eu

EFS="${O6_EFS_ROOT:-/home/node/.openclaw}"
LOCAL="${O6_LOCAL_ROOT:-/openclaw-local}"
BUCKET="${LITESTREAM_S3_BUCKET:?LITESTREAM_S3_BUCKET is required}"
LITESTREAM_CONFIG="${O6_LITESTREAM_CONFIG:-/etc/isol8-litestream.yml}"
APP_CMD="${O6_APP_CMD:-/usr/local/bin/isol8-o6-app.sh}"

log() { echo "[o6-boot] $*"; }

# Dependency preflight. Every one of these is baked into the extended image,
# but a missing binary would otherwise degrade silently rather than loudly:
# no jq means the clear-auth sentinel is consumed without being applied (a
# stale auth row survives and a freshly staged oauth.json never re-imports),
# and no sqlite3 means the migration's path rewrite is skipped.
for _bin in sqlite3 jq aws litestream; do
  command -v "$_bin" >/dev/null 2>&1 || { log "FATAL: required binary '$_bin' is missing from this image"; exit 1; }
done

# --- 0. Owner id: per-user task env, else the facts file on the EFS spine.
OWNER_ID="${ISOL8_OWNER_ID:-}"
if [ -z "$OWNER_ID" ] && [ -f "$EFS/isol8-facts.json" ]; then
  OWNER_ID=$(jq -r '.owner_id // empty' "$EFS/isol8-facts.json" 2>/dev/null || true)
fi
[ -n "$OWNER_ID" ] || { log "FATAL: owner id unresolved (no ISOL8_OWNER_ID env, no isol8-facts.json)"; exit 1; }
export OWNER_ID
# The baked litestream config references $O6_LOCAL_ROOT so it always watches
# the SAME root this script restored/migrated into — a hardcoded path there
# would silently replicate nothing when the root is overridden.
export O6_LOCAL_ROOT="$LOCAL"
log "owner=$OWNER_ID bucket=$BUCKET local_root=$LOCAL"

mkdir -p "$LOCAL/state" "$LOCAL/agents"

# --- 1. Durable spine: stateDir-derived subdirs that must stay on EFS.
# Directory symlinks are safe under atomic file replaces inside them, and none
# of these contain SQLite, so the journal-mode probe (which classifies only
# database paths) never sees them. skills/npm/git/tools keep clawhub skills
# and plugin npm/git installs durable — the scanners and installers resolve
# them through the state root, which is now local.
for d in credentials identity devices extensions skills npm git tools; do
  mkdir -p "$EFS/$d" || exit 1
  ln -sfn "$EFS/$d" "$LOCAL/$d" || exit 1
done
# ~/.openclaw/.env is a documented owner surface loaded from the state root.
# Symlink rather than copy: a one-way copy makes EFS authoritative forever, so
# any runtime write lands on ephemeral disk and is silently reverted by the
# next task. Through the symlink, reads resolve and in-place writes stay
# durable. (An atomic tmp+rename writer would replace the link with a local
# file and lose durability for that surface — .env is not Litestream-covered
# either way; the symlink is strictly better than the copy.)
ln -sfn "$EFS/.env" "$LOCAL/.env" || exit 1

# --- 2. Restore every replicated DB from S3. Explicit CLI restores (rather
# than replicate's restore-if-db-not-exists, which covers only path-form
# entries and runs no integrity check) so EVERY DB — including the dynamic
# per-agent set — restores through one integrity-checked path.
KEYS=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$OWNER_ID/" \
        --query 'Contents[].Key' --output text 2>/tmp/o6-s3-err) || {
  log "FATAL: S3 enumeration failed: $(cat /tmp/o6-s3-err 2>/dev/null)"; exit 1; }
[ "$KEYS" = "None" ] && KEYS=""

# S3 object keys are <owner>/<db-rel-path>/ltx/<level>/<minTXID>-<maxTXID>.ltx
# — VERIFIED by running litestream v0.5.15 against this exact config, not
# inferred: there IS a literal "ltx/" segment, and levels are plain decimal
# (0, 1, 9), not zero-padded hex. Anchor on the "ltx/" segment and strip the
# trailing three components. Getting this wrong is the worst failure in the
# script: no key matches, DBS comes back empty, and a container with a
# perfectly good replica silently takes the migration/cold-start path.
DBS=""
if [ -n "$KEYS" ]; then
  DBS=$(printf '%s\n' $KEYS | sed -n "s#^$OWNER_ID/\(.*\)/ltx/[^/]*/[^/]*\$#\1#p" | sort -u)
  # Objects exist but none parsed => litestream's key layout moved under us.
  # Continuing would cold-start on top of a live replica and then snapshot the
  # empty database FORWARD over it. Crash instead.
  [ -n "$DBS" ] || {
    log "FATAL: S3 prefix $OWNER_ID/ has objects but none match <db>/ltx/<level>/<file>; refusing to continue"
    exit 1
  }
fi

# Migration source, in precedence order:
#   $EFS/{state,agents}              — first cutover boot (live EFS state)
#   $EFS/pre-o6-archive/{state,agents} — a PRIOR boot archived them but the
#     task died before Litestream durably replicated anything. Without this
#     arm the replacement task finds no replica and no live EFS state, takes
#     the cold-start path, and the owner's entire history silently rots in an
#     ignored archive. Re-migrating from the archive is idempotent and leaves
#     the archive in place.
#
# The archive arm is BOUNDED by a fill-pending marker written at archive time.
# Without that bound the frozen archive would be consulted on every boot
# forever, so an agent deleted AFTER cutover — whose replica prefix the backend
# deliberately deleted — would be resurrected from the archive on the next task
# replacement and start replicating again, silently undoing the deletion. The
# marker is cleared as soon as S3 is proven to hold everything the archive
# does, which is normally the very next boot.
ARCHIVE="$EFS/pre-o6-archive"
FILL_MARKER="$ARCHIVE/.o6-fill-pending"

# Close the fill window as soon as the replica covers every database the
# archive holds: from then on the archive is redundant and post-cutover agent
# deletions are honored instead of being undone.
if [ -f "$FILL_MARKER" ]; then
  _missing=0
  for _adb in "$ARCHIVE"/state/openclaw.sqlite "$ARCHIVE"/state/openclaw-quarantine.sqlite \
              "$ARCHIVE"/agents/*/agent/openclaw-agent.sqlite; do
    [ -f "$_adb" ] || continue
    _rel=${_adb#"$ARCHIVE"/}
    case "$DBS" in
      *"$_rel"*) ;;
      *) _missing=1 ;;
    esac
  done
  if [ "$_missing" = "0" ]; then
    rm -f "$FILL_MARKER"
    log "replica covers the full archive — fill window closed"
  fi
fi

ARCHIVE="$EFS/pre-o6-archive"
FILL_MARKER="$ARCHIVE/.o6-fill-pending"
# Sources are ADDITIVE, not alternatives: a task killed between the two archive
# moves leaves one subtree live on EFS and the other already archived, so
# picking a single source would silently drop whichever half is in the other
# place. cp -an makes applying both idempotent (a restored replica always wins).
FILL_EFS=0
FILL_ARCHIVE=0
if [ -d "$EFS/state" ] || [ -d "$EFS/agents" ]; then
  FILL_EFS=1
fi
if [ -f "$FILL_MARKER" ]; then
  if [ -d "$ARCHIVE/state" ] || [ -d "$ARCHIVE/agents" ]; then
    FILL_ARCHIVE=1
  fi
fi

o6_fill_from() {
  _src=$1
  for _d in state agents; do
    if [ -d "$_src/$_d" ]; then
      cp -an "$_src/$_d/." "$LOCAL/$_d/" || return 1
    fi
  done
  return 0
}

# sqlite3 wrapper that tolerates ONLY a missing table. Any other failure
# (SQLITE_BUSY, I/O error, schema mismatch) must abort the migration: the DB
# would still pass integrity_check while its absolute markers still point at
# the about-to-be-archived EFS paths, so history would resolve to a moved-away
# location and read as empty.
o6_sqlite() {
  _db=$1; _sql=$2
  if _err=$(sqlite3 "$_db" "$_sql" 2>&1); then
    return 0
  fi
  case "$_err" in
    *"no such table"*) log "warn: $(basename "$_db"): $_err" ;;
    *) log "FATAL: sqlite failed on $_db: $_err"; return 1 ;;
  esac
}

# Restore every replicated DB first. This is NOT exclusive with the migration
# below: a task that died mid-first-upload leaves SOME databases in S3 while
# the rest exist only under the migration source, so treating the two as
# alternatives would permanently strand those agents' history. Restore what
# S3 has, then let the migration fill in whatever is still missing locally.
if [ -n "$DBS" ]; then
  for rel in $DBS; do
    log "restore $rel"
    mkdir -p "$LOCAL/$(dirname "$rel")" || exit 1
    litestream restore -if-db-not-exists -if-replica-exists \
      -integrity-check quick \
      -o "$LOCAL/$rel" "s3://$BUCKET/$OWNER_ID/$rel" || {
        log "FATAL: restore failed for $rel"; exit 1; }
  done
fi

if [ "$FILL_EFS" = "1" ] || [ "$FILL_ARCHIVE" = "1" ]; then
  # --- 3. Cutover migration: fills every database the restore above did not
  # produce. `cp -n` never clobbers a restored replica (which is always the
  # more recent copy), so this is a fill-in, not an overwrite.
  # The single-writer deployment config guarantees no concurrent writer, and
  # on EFS these DBs run rollback-journal (no -wal sidecars), so a plain copy
  # is consistent. The EFS originals are archived (mv, not deleted) only
  # after the copies pass integrity_check.
  log "cutover migration (live_efs=$FILL_EFS archive=$FILL_ARCHIVE)"
  [ "$FILL_EFS" = "1" ] && { o6_fill_from "$EFS" || exit 1; }
  [ "$FILL_ARCHIVE" = "1" ] && { o6_fill_from "$ARCHIVE" || exit 1; }
  :
  # Scoped path rewrite — ONLY the two relocated subtrees. Session markers
  # ("sqlite:<agent>:<session>:<absPath>" inside session_entries.entry_json)
  # take precedence over config, and the state DB registers agent DBs by
  # absolute path; both would otherwise point readers at the archived EFS
  # paths (recreating empty DBs there — history reads as empty). Workspace
  # paths also live under the state root and must NOT be rewritten, hence
  # prefix-scoped replaces, never a blanket one. The FROM prefixes are always
  # the canonical EFS roots even when re-migrating from the archive: the
  # markers inside those DBs were written before the archive move.
  for db in "$LOCAL"/agents/*/agent/openclaw-agent.sqlite; do
    [ -f "$db" ] || continue
    o6_sqlite "$db" "UPDATE session_entries SET entry_json = replace(replace(entry_json, '$EFS/agents/', '$LOCAL/agents/'), '$EFS/state/', '$LOCAL/state/') WHERE entry_json LIKE '%$EFS/agents/%' OR entry_json LIKE '%$EFS/state/%';" || exit 1
  done
  if [ -f "$LOCAL/state/openclaw.sqlite" ]; then
    o6_sqlite "$LOCAL/state/openclaw.sqlite" "UPDATE agent_databases SET path = replace(path, '$EFS/agents/', '$LOCAL/agents/') WHERE path LIKE '$EFS/agents/%';" || exit 1
  fi
  # Integrity gate before the EFS originals are archived.
  for db in "$LOCAL/state/openclaw.sqlite" "$LOCAL/state/openclaw-quarantine.sqlite" \
            "$LOCAL"/agents/*/agent/openclaw-agent.sqlite; do
    [ -f "$db" ] || continue
    ok=$(sqlite3 "$db" "PRAGMA integrity_check;" | head -1)
    [ "$ok" = "ok" ] || { log "FATAL: integrity_check failed for $db: $ok"; exit 1; }
  done
  # A stale legacy workspace-state.json would re-trip the migration gate the
  # native-seed heal used to clear; the state DB is present by construction
  # at this point, which is exactly the heal's own guard condition. The marker
  # lives at workspace-<agent>/.openclaw/workspace-state.json.
  rm -f "$EFS"/workspace-*/.openclaw/workspace-state.json 2>/dev/null || true
  # Archive the live EFS originals. The recovery marker is armed BEFORE the
  # first destructive move: a task killed between the two moves must still be
  # recoverable, and without the marker the replacement would ignore whatever
  # was already archived.
  if [ "$FILL_EFS" = "1" ]; then
    mkdir -p "$ARCHIVE" || exit 1
    : > "$FILL_MARKER" || exit 1
    for d in state agents; do
      if [ -d "$EFS/$d" ]; then mv "$EFS/$d" "$ARCHIVE/$d" || exit 1; fi
    done
    log "migration complete; EFS originals archived at pre-o6-archive/ (fill window armed)"
  else
    log "migration complete; filled in from archive (archive left in place)"
  fi
elif [ -z "$DBS" ]; then
  log "cold start: no replica, no EFS state — fresh container"
fi

# --- 4. Clear-auth sentinel (written by the backend when it cannot reach the
# container-local store). Applied BEFORE gateway start so a freshly staged
# credentials/oauth.json re-imports (the import fires only when the persisted
# store row is absent). Missing DB or table is benign: a fresh store has no
# row, so the staged file imports on its own.
SENTINEL="$EFS/o6-clear-auth-profiles.json"
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
    db="$LOCAL/agents/$agent/agent/openclaw-agent.sqlite"
    if [ -f "$db" ]; then
      if _serr=$(sqlite3 "$db" "DELETE FROM auth_profile_store WHERE store_key = 'primary';" 2>&1); then
        log "sentinel: cleared auth row for agent $agent"
      else
        case "$_serr" in
          *"no such table"*) log "sentinel: no auth table for agent $agent (benign)" ;;
          *)
            # An I/O error, corruption, or schema mismatch. Consuming the
            # sentinel here would leave the stale primary row shadowing the
            # freshly staged oauth.json with no retry request on disk, so the
            # owner's reconnect would silently keep using the spent token.
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

# --- 5. Hand off. litestream supervises the app: SIGTERM is forwarded to the
# child (the gateway, via exec in the app script), then every DB final-syncs
# to S3 — but ONLY when the child exits cleanly, so the gateway must exit 0
# on SIGTERM. Config is env-expanded by litestream ($OWNER_ID exported above,
# $LITESTREAM_S3_BUCKET from the task def).
log "starting litestream replicate (supervising doctor+gateway)"
exec litestream replicate -config "$LITESTREAM_CONFIG" -exec "$APP_CMD"
