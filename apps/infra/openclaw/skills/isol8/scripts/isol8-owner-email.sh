#!/usr/bin/env bash

# Owner-email helper.
#
# Sends an email to THIS container's owner. There is deliberately no recipient
# argument: the backend resolves the owner from the authenticated container, so
# this can only ever mail your own owner and nothing here can redirect it.
#
# Your FIRST send is what creates your email address — the backend mints it
# from your company's current name at that moment. So name the company before
# you send, or you will be stuck with whatever it is called now.

set -euo pipefail

usage() {
  cat <<'EOF'
Isol8 owner-email helper.

Usage:
  /app/skills/isol8/scripts/isol8-owner-email.sh --subject "Subject line" [--purpose first_contact|routine]

Reads the plain-text body from stdin.

Options:
  --subject   Required. The email subject.
  --purpose   Optional, default first_contact. Reporting label only — it does
              not decide anything server-side.

Environment (required, both already set in your container):
  OPENCLAW_HOOK_TOKEN   Bearer credential for the backend.
  ISOL8_API_URL         Backend base URL.

Example:
  printf '%s' "Hi -- here's what I found." | \
    /app/skills/isol8/scripts/isol8-owner-email.sh --subject "I've been reading up on you"
EOF
}

die() { printf '%s\n' "$1" >&2; exit 1; }

subject=""
purpose="first_contact"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject) subject="${2:-}"; shift 2 ;;
    --purpose) purpose="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ -n "$subject" ]] || { usage >&2; exit 2; }

command -v jq >/dev/null 2>&1 || die 'Missing required command: jq'
command -v curl >/dev/null 2>&1 || die 'Missing required command: curl'
[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'

facts="${HOME}/.openclaw/isol8-facts.json"
[[ -f "$facts" ]] || die "Missing ${facts}"

enabled="$(jq -r '.email_enabled // false' "$facts")"
[[ "$enabled" == "true" ]] || die 'Email is not available for this owner (email_enabled is false).'

owner_id="$(jq -r '.owner_id // empty' "$facts")"
[[ -n "$owner_id" ]] || die "No owner_id in ${facts}"

body="$(cat)"
[[ -n "$body" ]] || die 'Empty body: pipe the email text on stdin.'

# Built with `jq -nc --arg`, never hand-interpolated, so a body containing
# quotes, newlines, or backslashes cannot produce malformed JSON.
payload="$(jq -nc --arg p "$purpose" --arg s "$subject" --arg b "$body" \
  '{purpose:$p, subject:$s, body_text:$b}')"

curl -fsS -X POST \
  -H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "$payload" \
  "${ISOL8_API_URL}/api/v1/agent-mail/send-to-owner/u/${owner_id}"
