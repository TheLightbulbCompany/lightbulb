#!/bin/sh
# Smoke harness for isol8-local-sqlite-boot.sh — stubs litestream/aws, drives the four
# boot scenarios against scratch dirs. Fails loudly on any wrong behavior.
set -eu
BOOT="$1"
WORK=$(mktemp -d)
STUBS="$WORK/stubs"; mkdir -p "$STUBS"

# ---- stubs ----
cat > "$STUBS/aws" <<'EOF'
#!/bin/sh
# emits $AWS_STUB_KEYS as the list-objects-v2 --output text payload
if [ "$1" = "s3" ]; then      # `aws s3 rm` (tombstone purge)
  echo "s3rm $*" >> "${S3RM_LOG:-/dev/null}"
  case "$*" in
    *--recursive*) [ "${AWS_STUB_PURGE_FAIL:-}" = "1" ] && exit 1 ;;
  esac
  exit 0
fi
[ "${AWS_STUB_FAIL:-}" = "1" ] && { echo "boom" >&2; exit 1; }
printf '%s' "${AWS_STUB_KEYS:-None}"
EOF
cat > "$STUBS/litestream" <<'EOF'
#!/bin/sh
echo "litestream $*" >> "${LS_LOG:?}"
case "$1" in
  restore)
    # honor -o <path>: create the "restored" DB so integrity paths exist
    out=""
    prev=""
    for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
    [ -n "$out" ] && { mkdir -p "$(dirname "$out")"; sqlite3 "$out" "CREATE TABLE IF NOT EXISTS t(x);"; }
    exit "${LS_RESTORE_EXIT:-0}"
    ;;
  replicate) exit 0 ;;
esac
EOF
cat > "$STUBS/stat" <<'EOF'
#!/bin/sh
# Emulates `stat -f -c %T <path>` as it behaves in the container. GNU stat is
# absent on macOS, so WITHOUT this stub every case fell through the mount fence
# via its old fail-open "unknown" branch — which is exactly the defect that let
# a prod container archive an owner's live databases. The harness must model
# the real filesystem, not accidentally disable the guard it is testing.
# STAT_STUB_NESTED=fail makes stat EXIT NONZERO, which is what drives the
# boot script's `|| echo ...` fallback — the path that used to fail open.
case "$*" in
  *"/state"|*"/agents")
    [ "${STAT_STUB_NESTED:-ext4}" = "fail" ] && exit 1
    echo "${STAT_STUB_NESTED:-ext4}" ;;
  *) echo "${STAT_STUB_OTHER:-nfs}" ;;
esac
EOF
chmod +x "$STUBS/aws" "$STUBS/litestream" "$STUBS/stat"
export PATH="$STUBS:$PATH"

# $2 = STATE_ROOT (the EFS mount as the container sees it; state/ + agents/ are
#      the nested ephemeral mounts, modelled here as ordinary empty dirs)
run_boot() {
  env ROOT_DIR="$1" sh -c '
    export ISOL8_STATE_ROOT="$ROOT_DIR" \
      LITESTREAM_S3_BUCKET=test-bucket ISOL8_OWNER_ID=org_smoke \
      ISOL8_LITESTREAM_CONFIG=/dev/null ISOL8_APP_CMD=/bin/true LS_LOG="$ROOT_DIR/../ls.log"
    exec sh "$0"
  ' "$BOOT"
}

fail() { echo "SMOKE FAIL: $*"; exit 1; }

# ---- 1. cold start ----
L="$WORK/t1/local"; mkdir -p "$L"
out=$(run_boot "$L" 2>&1) || fail "cold start exited nonzero: $out"
echo "$out" | grep -q "cold start" || fail "cold-start branch not taken"
# No symlinks in this design: nothing outside state/ and agents/ moves at all.
for d in credentials identity devices extensions skills npm git tools media logs; do
  [ -e "$L/$d" ] && fail "boot created $d — it should be untouched EFS"
done
grep -q "litestream replicate" "$WORK/t1/ls.log" || fail "replicate not exec'd"
echo "PASS 1 cold start"

# ---- 2. restore path ----
L="$WORK/t2/local"; mkdir -p "$L"
export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/0000/0000000000000001-0000000000000002.ltx	org_smoke/agents/agent-a1/agent/openclaw-agent.sqlite/0000/0000000000000001-0000000000000002.ltx	org_smoke/agents/agent-a1/agent/openclaw-agent.sqlite/0001/0000000000000001-0000000000000003.ltx"
out=$(run_boot "$L" 2>&1) || fail "restore run exited nonzero: $out"
unset AWS_STUB_KEYS
grep -q "restore -if-db-not-exists -if-replica-exists -integrity-check quick -o $L/state/openclaw.sqlite s3://test-bucket/org_smoke/state/openclaw.sqlite" "$WORK/t2/ls.log" || fail "state restore args"
grep -q "\-o $L/agents/agent-a1/agent/openclaw-agent.sqlite s3://test-bucket/org_smoke/agents/agent-a1/agent/openclaw-agent.sqlite" "$WORK/t2/ls.log" || fail "agent restore args"
[ "$(grep -c '^litestream restore' "$WORK/t2/ls.log")" = "2" ] || fail "expected exactly 2 restores (dedup levels)"
echo "PASS 2 restore"

# ---- 2b. enumeration failure is fatal ----
L="$WORK/t2b/local"; mkdir -p "$L"
if (export AWS_STUB_FAIL=1; run_boot "$L" >/dev/null 2>&1); then fail "enumeration failure did not exit nonzero"; fi
echo "PASS 2b enumeration fail-loud"

# ---- 2c. objects present but unparseable => FAIL LOUD (a silent cold start
# here would snapshot an empty DB forward over a live replica).
L="$WORK/t2c/local"; mkdir -p "$L"
# NOTE: "…/openclaw.sqlite/0000/1.ltx" is the REAL S3 shape and parses fine —
# use something that is genuinely not a replica object.
if (export AWS_STUB_KEYS="org_smoke/some-unexpected-object.bin"; run_boot "$L" >/dev/null 2>&1); then
  fail "unparseable S3 keys did not abort"
fi
echo "PASS 2c unparseable keys fail loud"

# ---- 2d. a tombstoned agent prefix is purged and never restored ----
L="$WORK/t2d/local"; mkdir -p "$L"
export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/0000/1-2.ltx	org_smoke/agents/gone/agent/openclaw-agent.sqlite/0000/1-2.ltx	org_smoke/agents/gone/.replica-deleted"
out=$(run_boot "$L" 2>&1) || fail "tombstone run exited nonzero: $out"
unset AWS_STUB_KEYS
echo "$out" | grep -q "tombstone: purging deleted agent prefix agents/gone" || fail "tombstone not honored: $out"
echo "$out" | grep -q "restore agents/gone" && fail "restored a tombstoned agent: $out"
echo "$out" | grep -q "restore state/openclaw.sqlite" || fail "live DB not restored alongside the tombstone: $out"
echo "PASS 2d tombstoned agent purged, not restored"

# ---- 2e. a prefix containing ONLY a tombstone (no LTX yet) must boot, not
# crash-loop on the "recognized nothing" guard.
L="$WORK/t2e/local"; mkdir -p "$L"
export AWS_STUB_KEYS="org_smoke/agents/justdeleted/.replica-deleted"
out=$(run_boot "$L" 2>&1) || fail "tombstone-only prefix crash-looped: $out"
unset AWS_STUB_KEYS
echo "$out" | grep -q "tombstone: purging deleted agent prefix agents/justdeleted" || fail "tombstone not processed: $out"
echo "$out" | grep -q "FATAL" && fail "tombstone-only prefix hit the recognized-nothing guard: $out"
echo "PASS 2e tombstone-only prefix boots"

# ---- 2g. a partial purge must KEEP the tombstone: objects are deleted before
# the marker, so a failure leaves the marker for the next boot to retry.
L="$WORK/t2g/local"; mkdir -p "$L"
export AWS_STUB_KEYS="org_smoke/agents/gone/.replica-deleted	org_smoke/agents/gone/agent/openclaw-agent.sqlite/0000/1-2.ltx"
export S3RM_LOG="$WORK/t2g/s3rm.log"
export AWS_STUB_PURGE_FAIL=1
out=$(run_boot "$L" 2>&1) || fail "partial-purge run exited nonzero: $out"
unset AWS_STUB_PURGE_FAIL
echo "$out" | grep -q "keeping the tombstone so the next boot retries" || fail "did not keep tombstone on purge failure: $out"
# the recursive call carries --exclude "*.replica-deleted", so match only the
# NON-recursive invocation that would actually remove the marker
grep -v -- "--recursive" "$S3RM_LOG" | grep -q "\.replica-deleted" && fail "deleted the marker despite a failed object purge"
unset S3RM_LOG AWS_STUB_KEYS
echo "PASS 2g partial purge keeps the tombstone"

# ---- 2i. a FAILED restore must still abort the boot now that restores run
# concurrently. `wait` with no argument discards every child's status, so the
# script collects per-PID; this case is what stops that regressing. Continuing
# past a missed restore lets OpenClaw create an empty DB that litestream then
# snapshots FORWARD over the live replica — the destruction the header warns of.
L="$WORK/t2i/local"; mkdir -p "$L"
export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/0000/1-2.ltx	org_smoke/agents/a1/agent/openclaw-agent.sqlite/0000/1-2.ltx"
export LS_RESTORE_EXIT=1
if out=$(run_boot "$L" 2>&1); then
  unset AWS_STUB_KEYS LS_RESTORE_EXIT
  fail "boot exited 0 despite a failed restore: $out"
fi
unset AWS_STUB_KEYS LS_RESTORE_EXIT
echo "$out" | grep -q "FATAL" || fail "failed restore did not log FATAL: $out"
echo "PASS 2i failed restore aborts the boot under concurrency"

# ---- 2h. the OTHER litestream key layout (file replicas use an extra "ltx/"
# segment). The parser must accept both — hard-coding either makes DBS empty,
# and an empty DBS against a live replica is the worst failure mode here.
L="$WORK/t2h/local"; mkdir -p "$L"
export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/ltx/0/1-2.ltx"
out=$(run_boot "$L" 2>&1) || fail "file-layout keys exited nonzero: $out"
unset AWS_STUB_KEYS
# EXACT, not a substring: the previous spelling matched
# "restore state/openclaw.sqlite/ltx" too, so this case stayed green for weeks
# while the parser silently produced the wrong path (the strip used '\$',
# a LITERAL dollar inside single quotes, not the end anchor).
echo "$out" | grep -qE "restore state/openclaw\.sqlite( |$)" || fail "file-layout key not parsed to a bare db path: $out"
echo "$out" | grep -q "restore state/openclaw.sqlite/ltx" && fail "file-layout key kept its ltx/ segment: $out"
echo "PASS 2h both litestream key layouts parse"

# ---- 4. sentinel ----
L="$WORK/t4/local"; mkdir -p "$L/agents/main/agent"
sqlite3 "$L/agents/main/agent/openclaw-agent.sqlite" "CREATE TABLE auth_profile_store(store_key TEXT, store_json TEXT); INSERT INTO auth_profile_store VALUES('primary','{}');"
echo '{"agent_ids":["main"],"requested_at":"now"}' > "$L/clear-auth-profiles.json"
out=$(run_boot "$L" 2>&1) || fail "sentinel run exited nonzero: $out"
rows=$(sqlite3 "$L/agents/main/agent/openclaw-agent.sqlite" "SELECT count(*) FROM auth_profile_store;")
[ "$rows" = "0" ] || fail "auth row not cleared"
[ ! -f "$L/clear-auth-profiles.json" ] || fail "sentinel not removed"
echo "PASS 4 sentinel"

# ---- 4b. sentinel with no DB is benign ----
L="$WORK/t4b/local"; mkdir -p "$L"
echo '{"agent_ids":["main"]}' > "$L/clear-auth-profiles.json"
out=$(run_boot "$L" 2>&1) || fail "sentinel-no-db exited nonzero: $out"
[ ! -f "$L/clear-auth-profiles.json" ] || fail "sentinel not consumed"
[ ! -f "$L/agents/main/agent/openclaw-agent.sqlite" ] || fail "junk DB created by sentinel apply"
echo "PASS 4b sentinel benign"

# ---- 4c. a non-benign sentinel failure must KEEP the sentinel (so the next
# boot retries) instead of silently consuming the retry request.
L="$WORK/t4c/local"; mkdir -p "$L/agents/main/agent"
printf 'not a database' > "$L/agents/main/agent/openclaw-agent.sqlite"
echo '{"agent_ids":["main"]}' > "$L/clear-auth-profiles.json"
out=$(run_boot "$L" 2>&1) || fail "sentinel-failure run exited nonzero: $out"
echo "$out" | grep -q "keeping sentinel" || fail "did not report keeping the sentinel: $out"
[ -f "$L/clear-auth-profiles.json" ] || fail "sentinel consumed despite a non-benign failure"
echo "PASS 4c sentinel retained on non-benign failure"

# ---- 4d. a sentinel naming a NON-default agent must be ignored: only main's
# row may be cleared (others merge from main; independent imports reuse the
# one-time refresh token -> refresh_token_reused fan-out).
L="$WORK/t4d/local"; mkdir -p "$L/agents/other/agent"
sqlite3 "$L/agents/other/agent/openclaw-agent.sqlite" "CREATE TABLE auth_profile_store(store_key TEXT, store_json TEXT); INSERT INTO auth_profile_store VALUES('primary','{}');"
echo '{"agent_ids":["other"]}' > "$L/clear-auth-profiles.json"
out=$(run_boot "$L" 2>&1) || fail "non-default sentinel run exited nonzero: $out"
rows=$(sqlite3 "$L/agents/other/agent/openclaw-agent.sqlite" "SELECT count(*) FROM auth_profile_store;")
[ "$rows" = "1" ] || fail "non-default agent auth row was cleared"
echo "$out" | grep -q "ignoring non-default agent" || fail "no ignore log: $out"
echo "PASS 4d non-default sentinel ignored"

# ---- 5. the mount-order fence: if state/ or agents/ ends up on the network
# filesystem (ECS mounts in array order, so a reordering shadows the ephemeral
# volumes), boot must FAIL rather than silently run SQLite on NFS at ~95ms.
# Simulated by pointing the check at a path the harness reports as nfs — we
# cannot mount NFS here, so assert the guard's shape by stubbing `stat`.
L="$WORK/t5/local"; mkdir -p "$L"
export STAT_STUB_NESTED=nfs4
if out=$(run_boot "$L" 2>&1); then
  unset STAT_STUB_NESTED
  fail "boot did not abort when state/ was on nfs: $out"
fi
unset STAT_STUB_NESTED
echo "$out" | grep -q "expected local disk" || fail "wrong abort reason: $out"
echo "PASS 5 mount-order fence aborts on nfs"

# ---- 5b. the fence must FAIL CLOSED when the filesystem cannot be identified.
# The original spelling denied nfs/smb/cifs and fell back to "unknown", which
# matched nothing and PASSED — so any stat failure sailed through. That is the
# defect that let a prod container archive an owner's live databases out from
# under itself while coming up empty.
L="$WORK/t5b/local"; mkdir -p "$L"
export STAT_STUB_NESTED=fail
if out=$(run_boot "$L" 2>&1); then
  unset STAT_STUB_NESTED
  fail "boot continued when the filesystem could not be identified: $out"
fi
unset STAT_STUB_NESTED
echo "$out" | grep -q "expected local disk" || fail "wrong abort reason: $out"
echo "PASS 5b fence fails CLOSED on an unidentifiable filesystem"

echo "ALL SMOKE TESTS PASSED"
