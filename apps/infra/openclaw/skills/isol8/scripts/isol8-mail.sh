#!/usr/bin/env bash

# Agent-email reply helper.
#
# Sends a reply to whoever emailed this agent. The reply-token carries the
# owner/thread identity server-side — there is no recipient argument, because
# the only valid recipient is the original sender. Builds the request body
# with `jq -nc --arg` (never hand-interpolated) so a reply containing quotes,
# newlines, or backslashes can never produce malformed JSON.

set -euo pipefail

usage() {
  cat <<'EOF'
Isol8 agent-email reply helper.

Usage:
  scripts/isol8-mail.sh <reply-token>

Reads the reply body (plain text) from stdin. Sends it to the original
sender of the email that carried <reply-token> — there is no way to choose
a different recipient.

Environment (required):
  OPENCLAW_HOOK_TOKEN   Bearer credential for the backend relay.
  ISOL8_API_URL         Backend base URL (e.g. https://api-dev.isol8.co).

Example:
  printf '%s' "Thanks -- I've booked that for Tuesday." | \
    scripts/isol8-mail.sh "$REPLY_TOKEN"
EOF
}

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

reply_token="$1"

command -v jq >/dev/null 2>&1 || die 'Missing required command: jq'
command -v curl >/dev/null 2>&1 || die 'Missing required command: curl'

[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'

body="$(cat)"
[[ -n "$body" ]] || die 'Reply body (stdin) is empty -- refusing to send an empty reply.'

payload="$(jq -nc --arg tok "$reply_token" --arg body "$body" '{reply_token: $tok, body_text: $body}')"

response="$(
  curl -sS -w $'\n%{http_code}' -X POST \
    "${ISOL8_API_URL%/}/api/v1/agent-mail/reply" \
    -H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "$payload"
)"

code="$(printf '%s' "$response" | tail -n1)"
out="$(printf '%s' "$response" | sed '$d')"

if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
  printf 'Sending reply failed (HTTP %s):\n%s\n' "$code" "$out" >&2
  exit 1
fi

# HTTP 200 alone does not mean the reply went out -- the relay returns 200
# with a JSON body whose `status` field can be "sent" (real success) or
# "no_address" / "disabled" / "error" (reply did NOT go out). Parse it, and
# treat an unparseable body (jq failure) as a failure too rather than
# crashing under `set -euo pipefail`.
status="$(printf '%s' "$out" | jq -r '.status // empty' 2>/dev/null || true)"

if [[ "$status" == "sent" ]]; then
  echo "Reply sent."
  exit 0
fi

printf 'Reply not sent (status: %s).\n' "${status:-unknown}" >&2
exit 1
