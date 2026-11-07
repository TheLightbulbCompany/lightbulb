#!/usr/bin/env bash
# The phone line's LiveKit SIP dispatch rule, from the checked-in JSON, so it is not a hand step.
# Idempotent: creates the rule named in the JSON, or replaces the one with that name.
#   LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... apps/huddle-agent/scripts/phone_dispatch_rule.sh [prod]
# The JSON is a SIPDispatchRuleInfo (`lk sip dispatch update` replaces with it; create wraps it as
# CreateSIPDispatchRuleRequest.dispatch_rule). Prints rule ids only, never credentials.
set -euo pipefail
env="${1:-prod}"
file="$(cd "$(dirname "$0")" && pwd)/phone_dispatch_rule.${env}.json"
[ -f "$file" ] || { echo "no $file" >&2; exit 1; }
if grep -q SET_ME "$file"; then
  echo "set trunk_ids in $file to the LiveKit inbound trunk id (ST_...) first" >&2
  exit 1
fi
name="$(jq -r .name "$file")"
id="$(lk sip dispatch list --json | jq -r --arg n "$name" \
  '[.. | objects | select(.name? == $n) | (.sipDispatchRuleId // .sip_dispatch_rule_id)] | first // empty')"
if [ -n "$id" ]; then
  lk sip dispatch update --id "$id" "$file" >/dev/null
  echo "updated $name ($id)"
else
  jq '{dispatch_rule: .}' "$file" | lk sip dispatch create - >/dev/null
  echo "created $name"
fi
