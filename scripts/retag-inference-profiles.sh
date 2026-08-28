#!/usr/bin/env bash
# Step 2 of the ManagedBy rename: re-tag every owner Bedrock application
# inference profile from isol8-backend to lightbulb-backend.
#
# WHY THIS IS A SEPARATE STEP AND NOT PART OF A DEPLOY:
# The tag gates every mutate action on an AIP via aws:ResourceTag/ManagedBy.
# Re-tagging BEFORE the widened IAM condition is live locks the backend out of
# the profile it just re-tagged. So this REFUSES to run until it can see
# `lightbulb-backend` in the deployed service-stack template.
#
#   scripts/retag-inference-profiles.sh dev            # dry run
#   scripts/retag-inference-profiles.sh dev --apply
#   scripts/retag-inference-profiles.sh prod --apply
set -euo pipefail

ENV="${1:?usage: retag-inference-profiles.sh <dev|prod> [--apply]}"
APPLY=false; [[ "${2:-}" == "--apply" ]] && APPLY=true
R=us-east-1
OLD=isol8-backend
NEW=lightbulb-backend

echo "env=$ENV  mode=$([[ $APPLY == true ]] && echo APPLY || echo DRY-RUN)"

# --- gate: the widened condition must already be deployed -------------------
# NOT `aws ... | grep -q`: grep -q exits on the FIRST match, which SIGPIPEs
# aws, and `set -o pipefail` then reports the pipeline as failed -- so the gate
# failed precisely BECAUSE the string was present. Capture, then match.
DEPLOYED_POLICY=$(aws cloudformation get-template --stack-name "isol8-${ENV}-service" \
  --region $R --output json 2>/dev/null || true)
if [[ "$DEPLOYED_POLICY" != *"$NEW"* ]]; then
  echo "REFUSING: isol8-${ENV}-service does not yet accept ManagedBy=${NEW}."
  echo "  Deploy the widened IAM condition first, or re-tagging locks the"
  echo "  backend out of every profile it re-tags."
  exit 2
fi
echo "  gate ok: deployed policy accepts ${NEW}"

aws bedrock list-inference-profiles --region $R --type-equals APPLICATION --output json \
  > /tmp/.aips.$$
trap 'rm -f /tmp/.aips.$$' EXIT

python3 - "$ENV" "$APPLY" "$OLD" "$NEW" /tmp/.aips.$$ <<'PY'
import json, subprocess, sys
env, apply_s, old, new, path = sys.argv[1:6]
apply = apply_s == "true"
profs = [p for p in json.load(open(path))["inferenceProfileSummaries"]
         if p["inferenceProfileName"].startswith(f"isol8-{env}-")]

def tags_of(arn):
    out = subprocess.run(["aws","bedrock","list-tags-for-resource",
                          "--resource-arn",arn,"--region","us-east-1",
                          "--output","json"],capture_output=True,text=True)
    if out.returncode: return None
    return {t["key"]: t["value"] for t in json.loads(out.stdout).get("tags",[])}

todo, already, skip = [], [], []
for p in profs:
    t = tags_of(p["inferenceProfileArn"])
    if t is None:            skip.append((p["inferenceProfileName"], "unreadable tags"))
    elif t.get("ManagedBy") == new:  already.append(p["inferenceProfileName"])
    elif t.get("ManagedBy") == old:  todo.append(p)
    else:                    skip.append((p["inferenceProfileName"], f"ManagedBy={t.get('ManagedBy')!r}"))

print(f"  profiles={len(profs)}  to_retag={len(todo)}  already_new={len(already)}  skipped={len(skip)}")
for n, why in skip: print(f"    SKIP {n[:54]}  ({why})")
for p in todo:      print(f"    {'RETAG' if apply else 'would retag'} {p['inferenceProfileName'][:54]}")

if not apply:
    print("\nDRY RUN — nothing changed."); raise SystemExit(0)

done = 0
for p in todo:
    # Tag first, THEN verify: an AIP whose tag write silently failed must not be
    # reported as migrated.
    subprocess.run(["aws","bedrock","tag-resource",
                    "--resource-arn",p["inferenceProfileArn"],
                    "--tags",f"key=ManagedBy,value={new}",
                    "--region","us-east-1"],check=True,capture_output=True)
    if (tags_of(p["inferenceProfileArn"]) or {}).get("ManagedBy") != new:
        print(f"    VERIFY FAILED {p['inferenceProfileName']}"); raise SystemExit(1)
    done += 1
print(f"\n  retagged and verified: {done}")
PY
