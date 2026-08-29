#!/usr/bin/env bash
# Copy every DynamoDB table from the isol8-{env}- prefix to lightbulb-{env}-.
#
# This does NOT rename anything -- DynamoDB has no rename. It creates a table
# with the same key schema, GSIs, TTL and billing mode under the new name, then
# copies the items. The old table is left ALONE: prod tables are
# RemovalPolicy.RETAIN, and nothing here deletes.
#
# The cutover is a separate, single step: flip DYNAMODB_TABLE_PREFIX. Until
# that deploy, the backend keeps reading and writing the OLD tables, so this
# script is safe to run repeatedly ahead of time -- but items written after the
# copy will be missing from the new table, which is why --verify re-checks
# counts and why the real cutover wants a short write pause or a final re-run.
#
#   scripts/migrate-ddb-table-prefix.sh dev            # plan only
#   scripts/migrate-ddb-table-prefix.sh dev --apply
#   scripts/migrate-ddb-table-prefix.sh dev --verify
set -euo pipefail
ENV="${1:?usage: migrate-ddb-table-prefix.sh <dev|prod> [--apply|--verify]}"
MODE="${2:-plan}"
R=us-east-1
OLD="isol8-${ENV}-"
NEW="lightbulb-${ENV}-"

echo "env=$ENV  ${OLD}* -> ${NEW}*  mode=${MODE#--}"
aws dynamodb list-tables --region $R --output json > /tmp/.tabs.$$
trap 'rm -f /tmp/.tabs.$$' EXIT

python3 - "$OLD" "$NEW" "$MODE" "$R" /tmp/.tabs.$$ <<'PY'
import json, subprocess, sys
old, new, mode, region, path = sys.argv[1:6]
names = [t for t in json.load(open(path))["TableNames"] if t.startswith(old)]

def aws(*a):
    r = subprocess.run(["aws",*a,"--region",region,"--output","json"],
                       capture_output=True,text=True)
    return (json.loads(r.stdout) if r.stdout.strip() else {}) if r.returncode==0 else None

def desc(t):
    d = aws("dynamodb","describe-table","--table-name",t)
    return d["Table"] if d else None

def count(t):
    """REAL count via scan, never describe-table's ItemCount.

    ItemCount is refreshed roughly every six hours, so on a freshly written
    table it reads 0 -- which made --verify report every copied table as a
    MISMATCH even though the data was there. A verifier that lies in the
    optimistic direction is far worse, so this pays a scan per table; the
    volumes here are small by design."""
    tok, n = None, 0
    while True:
        a = ["dynamodb","scan","--table-name",t,"--select","COUNT"]
        if tok: a += ["--exclusive-start-key", json.dumps(tok)]
        page = aws(*a)
        if page is None: return None
        n += page.get("Count", 0)
        tok = page.get("LastEvaluatedKey")
        if not tok: return n

print(f"  source tables: {len(names)}")
plan=[]
for t in names:
    d = desc(t)
    if not d: print(f"    SKIP {t} (undescribable)"); continue
    tgt = new + t[len(old):]
    plan.append((t, tgt, d))

if mode == "--verify":
    bad=0
    for src,tgt,_ in plan:
        s,g = count(src), count(tgt)
        flag = "" if s==g else "   <-- MISMATCH"
        if s!=g: bad+=1
        print(f"    {src[:44]:46} {s} -> {tgt[:44]:46} {g}{flag}")
    print(f"\n  mismatched: {bad}")
    raise SystemExit(1 if bad else 0)

for src,tgt,d in plan:
    print(f"    {'COPY' if mode=='--apply' else 'would copy'} {src} -> {tgt}  ({d.get('ItemCount')} items)")

if mode != "--apply":
    print("\nPLAN ONLY — nothing created."); raise SystemExit(0)

made=copied=0
for src,tgt,d in plan:
    if desc(tgt) is None:
        args=["dynamodb","create-table","--table-name",tgt,
              "--attribute-definitions",json.dumps(d["AttributeDefinitions"]),
              "--key-schema",json.dumps(d["KeySchema"]),
              "--billing-mode","PAY_PER_REQUEST"]
        gsis=[{ "IndexName":g["IndexName"],"KeySchema":g["KeySchema"],
                "Projection":g["Projection"]} for g in d.get("GlobalSecondaryIndexes",[])]
        if gsis: args += ["--global-secondary-indexes",json.dumps(gsis)]
        if aws(*args) is None:
            print(f"    CREATE FAILED {tgt}"); raise SystemExit(1)
        made+=1
        subprocess.run(["aws","dynamodb","wait","table-exists","--table-name",tgt,
                        "--region",region],check=True)
    # copy items (scan + batch write); volumes here are small by design
    tok=None; n=0
    while True:
        a=["dynamodb","scan","--table-name",src]
        if tok: a += ["--exclusive-start-key",json.dumps(tok)]
        page=aws(*a)
        if page is None: print(f"    SCAN FAILED {src}"); raise SystemExit(1)
        items=page.get("Items",[])
        for i in range(0,len(items),25):
            batch={tgt:[{"PutRequest":{"Item":it}} for it in items[i:i+25]]}
            if aws("dynamodb","batch-write-item","--request-items",json.dumps(batch)) is None:
                print(f"    WRITE FAILED {tgt}"); raise SystemExit(1)
        n+=len(items)
        tok=page.get("LastEvaluatedKey")
        if not tok: break
    copied+=n
    print(f"    {src} -> {tgt}: {n} items")
print(f"\n  tables created: {made}   items copied: {copied}")
PY
