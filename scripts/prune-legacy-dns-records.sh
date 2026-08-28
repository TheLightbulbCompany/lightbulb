#!/usr/bin/env bash
# Delete the LIGHTBULB records from the retired isol8.co zone, and nothing else.
#
# WHY THIS IS NOT "delete the hosted zone":
# isol8.co is NOT only ours. GooseTown is LIVE inside it -- goosetown.isol8.co
# serves HTTP 200, its ECS services are running, and `*.goosetown.isol8.co` is
# InUse=True on live CloudFront distributions. 9 of the zone's 37 records are
# GooseTown's. Deleting the zone takes down a separate product. So this prunes
# our records and leaves the zone standing.
#
# ORDER: run this only AFTER the prod promotion that carries the mail-retirement
# change. Until then CloudFormation still owns the SES identity and several of
# these records in isol8-{env}-ses-inbound, and hand-deleting them strands the
# stack. This script refuses to run while that stack still declares them.
#
#   scripts/prune-legacy-dns-records.sh            # dry run (default)
#   scripts/prune-legacy-dns-records.sh --apply
set -euo pipefail

ZONE_ID="${ZONE_ID:-Z09248243AOUC775CDUI4}"   # isol8.co
APPLY=false; [[ "${1:-}" == "--apply" ]] && APPLY=true

# Anything at or under these names belongs to another product. Never touch.
PROTECTED_SUFFIXES=("goosetown.isol8.co.")

echo "zone: $ZONE_ID  mode: $([[ $APPLY == true ]] && echo APPLY || echo DRY-RUN)"

# --- guard: CloudFormation must no longer own the mail records -------------
for env in prod dev; do
  if aws cloudformation describe-stack-resources --stack-name "isol8-${env}-ses-inbound" \
       --region us-east-1 --query "StackResources[?ResourceType=='AWS::SES::EmailIdentity'].PhysicalResourceId" \
       --output text 2>/dev/null | grep -q "isol8\.co"; then
    echo "REFUSING: isol8-${env}-ses-inbound still declares the isol8.co SES identity."
    echo "  Promote the mail-retirement change first and let CloudFormation delete it."
    exit 2
  fi
done

records=$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" --output json)

python3 - "$APPLY" "$ZONE_ID" <<'PY' <<<"$records"
import json, subprocess, sys
apply = sys.argv[1] == "true"; zone = sys.argv[2]
protected = ("goosetown.isol8.co.",)
rrs = json.load(sys.stdin)["ResourceRecordSets"]

keep, drop = [], []
for r in rrs:
    name, typ = r["Name"], r["Type"]
    if typ in ("NS", "SOA"):                       # zone apex plumbing
        keep.append((name, typ, "zone apex")); continue
    if any(name == p or name.endswith("." + p) or name.endswith(p) for p in protected):
        keep.append((name, typ, "GooseTown — LIVE")); continue
    drop.append(r)

print(f"\nKEEP ({len(keep)}):")
for n, t, why in keep: print(f"  {t:6} {n:52} {why}")
print(f"\nDELETE ({len(drop)}):")
for r in drop: print(f"  {r['Type']:6} {r['Name']}")

if not apply:
    print("\nDRY RUN — nothing deleted. Re-run with --apply.")
    sys.exit(0)
if not drop:
    print("\nnothing to delete."); sys.exit(0)

batch = {"Changes": [{"Action": "DELETE", "ResourceRecordSet": r} for r in drop]}
subprocess.run(["aws", "route53", "change-resource-record-sets",
                "--hosted-zone-id", zone, "--change-batch", json.dumps(batch)],
               check=True, stdout=subprocess.DEVNULL)
print(f"\ndeleted {len(drop)} records; zone and GooseTown left intact.")
PY
