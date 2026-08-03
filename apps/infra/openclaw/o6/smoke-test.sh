#!/bin/sh
# Smoke harness for isol8-o6-boot.sh — stubs litestream/aws, drives the four
# boot scenarios against scratch dirs. Fails loudly on any wrong behavior.
set -eu
BOOT="$1"
WORK=$(mktemp -d)
STUBS="$WORK/stubs"; mkdir -p "$STUBS"

# ---- stubs ----
cat > "$STUBS/aws" <<'EOF'
#!/bin/sh
# emits $AWS_STUB_KEYS as the list-objects-v2 --output text payload
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
chmod +x "$STUBS/aws" "$STUBS/litestream"
export PATH="$STUBS:$PATH"

run_boot() {
  env EFS_DIR="$1" LOCAL_DIR="$2" sh -c '
    export O6_EFS_ROOT="$EFS_DIR" O6_LOCAL_ROOT="$LOCAL_DIR" \
      LITESTREAM_S3_BUCKET=test-bucket ISOL8_OWNER_ID=org_smoke \
      O6_LITESTREAM_CONFIG=/dev/null O6_APP_CMD=/bin/true LS_LOG="$LOCAL_DIR/../ls.log"
    exec sh "$0"
  ' "$BOOT"
}

fail() { echo "SMOKE FAIL: $*"; exit 1; }

# ---- 1. cold start ----
E="$WORK/t1/efs"; L="$WORK/t1/local"; mkdir -p "$E" "$L"
out=$(run_boot "$E" "$L" 2>&1) || fail "cold start exited nonzero: $out"
echo "$out" | grep -q "cold start" || fail "cold-start branch not taken"
[ -L "$L/credentials" ] && [ "$(readlink "$L/credentials")" = "$E/credentials" ] || fail "credentials symlink"
for d in identity devices extensions skills npm git tools; do [ -L "$L/$d" ] || fail "missing symlink $d"; done
grep -q "litestream replicate" "$WORK/t1/ls.log" || fail "replicate not exec'd"
echo "PASS 1 cold start"

# ---- 2. restore path ----
E="$WORK/t2/efs"; L="$WORK/t2/local"; mkdir -p "$E" "$L"
export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/ltx/0/0000000000000001-0000000000000002.ltx	org_smoke/agents/agent-a1/agent/openclaw-agent.sqlite/ltx/0/0000000000000001-0000000000000002.ltx	org_smoke/agents/agent-a1/agent/openclaw-agent.sqlite/ltx/9/0000000000000001-0000000000000003.ltx"
out=$(run_boot "$E" "$L" 2>&1) || fail "restore run exited nonzero: $out"
unset AWS_STUB_KEYS
grep -q "restore -if-db-not-exists -if-replica-exists -integrity-check quick -o $L/state/openclaw.sqlite s3://test-bucket/org_smoke/state/openclaw.sqlite" "$WORK/t2/ls.log" || fail "state restore args"
grep -q "\-o $L/agents/agent-a1/agent/openclaw-agent.sqlite s3://test-bucket/org_smoke/agents/agent-a1/agent/openclaw-agent.sqlite" "$WORK/t2/ls.log" || fail "agent restore args"
[ "$(grep -c '^litestream restore' "$WORK/t2/ls.log")" = "2" ] || fail "expected exactly 2 restores (dedup levels)"
echo "PASS 2 restore"

# ---- 2b. enumeration failure is fatal ----
E="$WORK/t2b/efs"; L="$WORK/t2b/local"; mkdir -p "$E" "$L"
if (export AWS_STUB_FAIL=1; run_boot "$E" "$L" >/dev/null 2>&1); then fail "enumeration failure did not exit nonzero"; fi
echo "PASS 2b enumeration fail-loud"

# ---- 2c. objects present but unparseable => FAIL LOUD (a silent cold start
# here would snapshot an empty DB forward over a live replica).
E="$WORK/t2c/efs"; L="$WORK/t2c/local"; mkdir -p "$E" "$L"
if (export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/0000/1.ltx"; run_boot "$E" "$L" >/dev/null 2>&1); then
  fail "unparseable S3 keys did not abort"
fi
echo "PASS 2c unparseable keys fail loud"

# ---- 3. migration path ----
E="$WORK/t3/efs"; L="$WORK/t3/local"; mkdir -p "$E/state" "$E/agents/main/agent" "$E/workspace-main"
sqlite3 "$E/state/openclaw.sqlite" "CREATE TABLE agent_databases(agent_id TEXT, path TEXT); INSERT INTO agent_databases VALUES('main','$E/agents/main/agent/openclaw-agent.sqlite');"
sqlite3 "$E/agents/main/agent/openclaw-agent.sqlite" "CREATE TABLE session_entries(id TEXT, entry_json TEXT); \
  INSERT INTO session_entries VALUES('s1','{\"sessionFile\":\"sqlite:main:s1:$E/agents/main/agent/openclaw-agent.sqlite\",\"cwd\":\"$E/workspace-main\"}');"
mkdir -p "$E/workspace-main/.openclaw"
echo "seed" > "$E/workspace-main/.openclaw/workspace-state.json"
mkdir -p "$L"
out=$(run_boot "$E" "$L" 2>&1) || fail "migration exited nonzero: $out"
echo "$out" | grep -q "cutover migration from $E" || fail "migration branch not taken: $out"
mk=$(sqlite3 "$L/agents/main/agent/openclaw-agent.sqlite" "SELECT entry_json FROM session_entries;")
echo "$mk" | grep -q "sqlite:main:s1:$L/agents/main/agent/openclaw-agent.sqlite" || fail "marker not rewritten: $mk"
echo "$mk" | grep -q "\"cwd\":\"$E/workspace-main\"" || fail "workspace path was wrongly rewritten: $mk"
reg=$(sqlite3 "$L/state/openclaw.sqlite" "SELECT path FROM agent_databases;")
[ "$reg" = "$L/agents/main/agent/openclaw-agent.sqlite" ] || fail "agent_databases not rewritten: $reg"
[ -d "$E/pre-o6-archive/state" ] && [ -d "$E/pre-o6-archive/agents" ] || fail "EFS originals not archived"
[ ! -d "$E/state" ] && [ ! -d "$E/agents" ] || fail "EFS hot dirs still present"
[ ! -f "$E/workspace-main/.openclaw/workspace-state.json" ] || fail "nested legacy workspace-state.json not removed"
echo "PASS 3 migration"

# ---- 3b. archive fallback: a prior boot archived EFS state but died before
# any replica landed. The replacement must re-migrate FROM the archive, not
# cold-start into an empty container.
E="$WORK/t3b/efs"; L="$WORK/t3b/local"; mkdir -p "$E/pre-o6-archive/state" "$E/pre-o6-archive/agents/main/agent" "$L"
sqlite3 "$E/pre-o6-archive/state/openclaw.sqlite" "CREATE TABLE agent_databases(agent_id TEXT, path TEXT); INSERT INTO agent_databases VALUES('main','$E/agents/main/agent/openclaw-agent.sqlite');"
sqlite3 "$E/pre-o6-archive/agents/main/agent/openclaw-agent.sqlite" "CREATE TABLE session_entries(id TEXT, entry_json TEXT); \
  INSERT INTO session_entries VALUES('s1','{\"sessionFile\":\"sqlite:main:s1:$E/agents/main/agent/openclaw-agent.sqlite\"}');"
out=$(run_boot "$E" "$L" 2>&1) || fail "archive fallback exited nonzero: $out"
echo "$out" | grep -q "cutover migration from $E/pre-o6-archive" || fail "archive branch not taken: $out"
[ -f "$L/agents/main/agent/openclaw-agent.sqlite" ] || fail "archive DB not restored to local"
mk=$(sqlite3 "$L/agents/main/agent/openclaw-agent.sqlite" "SELECT entry_json FROM session_entries;")
echo "$mk" | grep -q "sqlite:main:s1:$L/agents/main/agent/openclaw-agent.sqlite" || fail "archive-path marker not rewritten to configured local root: $mk"
[ -d "$E/pre-o6-archive/state" ] || fail "archive was consumed instead of left in place"
echo "PASS 3b archive fallback"

# ---- 3c. a non-missing-table sqlite failure must ABORT the cutover, leaving
# the EFS originals unarchived (silently archiving them while markers still
# point at EFS is the data-loss shape).
E="$WORK/t3c/efs"; L="$WORK/t3c/local"; mkdir -p "$E/state" "$E/agents/main/agent" "$L"
sqlite3 "$E/state/openclaw.sqlite" "CREATE TABLE agent_databases(agent_id TEXT, path TEXT);"
# A corrupt agent DB: the rewrite fails with "file is not a database", which
# is NOT "no such table". Deterministic regardless of UID — the deployed task
# runs as root, where a chmod-based failure would simply be bypassed.
printf 'this is not a sqlite database at all' > "$E/agents/main/agent/openclaw-agent.sqlite"
if out=$(run_boot "$E" "$L" 2>&1); then
  fail "corrupt-DB rewrite did not abort: $out"
fi
echo "$out" | grep -q "FATAL: sqlite failed" || fail "abort did not report a sqlite failure: $out"
[ -d "$E/state" ] && [ -d "$E/agents" ] || fail "EFS originals archived despite failed rewrite"
echo "PASS 3c rewrite failure aborts cutover"

# ---- 3d. partial replica + archive must MERGE (not be exclusive): a task that
# died mid-first-upload leaves some DBs in S3 and the rest only in the archive.
E="$WORK/t3d/efs"; L="$WORK/t3d/local"; mkdir -p "$E/pre-o6-archive/agents/main/agent" "$L"
sqlite3 "$E/pre-o6-archive/agents/main/agent/openclaw-agent.sqlite" "CREATE TABLE session_entries(id TEXT, entry_json TEXT); INSERT INTO session_entries VALUES('s1','{}');"
export AWS_STUB_KEYS="org_smoke/state/openclaw.sqlite/ltx/0/0000000000000001-0000000000000002.ltx"
out=$(run_boot "$E" "$L" 2>&1) || fail "partial-merge run exited nonzero: $out"
unset AWS_STUB_KEYS
[ -f "$L/state/openclaw.sqlite" ] || fail "replicated state DB not restored"
[ -f "$L/agents/main/agent/openclaw-agent.sqlite" ] || fail "archive-only agent DB was stranded by the partial replica"
echo "PASS 3d partial replica merges with archive"

# ---- 4. sentinel ----
E="$WORK/t4/efs"; L="$WORK/t4/local"; mkdir -p "$E" "$L/agents/main/agent"
sqlite3 "$L/agents/main/agent/openclaw-agent.sqlite" "CREATE TABLE auth_profile_store(store_key TEXT, store_json TEXT); INSERT INTO auth_profile_store VALUES('primary','{}');"
echo '{"agent_ids":["main"],"requested_at":"now"}' > "$E/o6-clear-auth-profiles.json"
out=$(run_boot "$E" "$L" 2>&1) || fail "sentinel run exited nonzero: $out"
rows=$(sqlite3 "$L/agents/main/agent/openclaw-agent.sqlite" "SELECT count(*) FROM auth_profile_store;")
[ "$rows" = "0" ] || fail "auth row not cleared"
[ ! -f "$E/o6-clear-auth-profiles.json" ] || fail "sentinel not removed"
echo "PASS 4 sentinel"

# ---- 4b. sentinel with no DB is benign ----
E="$WORK/t4b/efs"; L="$WORK/t4b/local"; mkdir -p "$E" "$L"
echo '{"agent_ids":["main"]}' > "$E/o6-clear-auth-profiles.json"
out=$(run_boot "$E" "$L" 2>&1) || fail "sentinel-no-db exited nonzero: $out"
[ ! -f "$E/o6-clear-auth-profiles.json" ] || fail "sentinel not consumed"
[ ! -f "$L/agents/main/agent/openclaw-agent.sqlite" ] || fail "junk DB created by sentinel apply"
echo "PASS 4b sentinel benign"

echo "ALL SMOKE TESTS PASSED"
