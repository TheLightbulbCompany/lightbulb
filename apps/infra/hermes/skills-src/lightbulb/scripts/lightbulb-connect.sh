#!/usr/bin/env bash
# Lightbulb integrations CLI — mint a one-click provider sign-in link.
#
# WHY THIS FILE EXISTS, rather than the agent curl'ing the endpoint directly:
# OpenClaw's transcript redaction masks the credential in an Authorization
# header (AUTHORIZATION_BEARER_REDACT_PATTERN), and that pattern is NOT in
# SHELL_REFERENCE_PRESERVING_PATTERN_SOURCES — so a SHELL REFERENCE inside the
# header is masked too, not just a literal secret. The opaque-credential class
# is `[^\s\\"',;&#?<>)}\]]+`, which does not exclude `$`. A turn that emits
#     curl -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN" .../authorize
# EXECUTES fine, but is PERSISTED as `Bearer ***`. On the next turn the agent
# reads its own history, copies the masked form, and sends a literal `***` —
# which the backend rejects 401 "invalid hook token"
# (core/services/container_auth.py::_require_hook_token). Prod 2026-08-30: an owner
# lost ~15 minutes of first run to that loop, then wrote throwaway Python
# trying to route around a filter it had misdiagnosed as an input guard.
# Assignment form (FOO=$FOO) is preserved; the header form is not.
#
# The token never entering the agent's TYPED TEXT is what fixes it: a
# pre-existing file on disk is executed, never round-tripped through the
# transcript — the same reason lightbulb-mail.sh has always worked. Keep it
# that way; do not tell an agent to inline this header anywhere.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  lightbulb-connect.sh link <provider>    print the sign-in URL for a one-click provider
Env (already set): OPENCLAW_HOOK_TOKEN, ISOL8_API_URL, ISOL8_OWNER_ID
EOF
}
die() { printf '%s\n' "$1" >&2; exit 1; }

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
cmd="$1"; shift
case "$cmd" in
  -h|--help) usage; exit 0 ;;
  link) ;;
  *) usage >&2; exit 2 ;;
esac
command -v jq >/dev/null || die 'Missing jq'; command -v curl >/dev/null || die 'Missing curl'

[[ $# -ge 1 && -n "${1:-}" ]] || die 'Usage: lightbulb-connect.sh link <provider>'
# A catalog slug only: anything else is spliced raw into the URL path and either
# reaches a different route or makes curl exit 3 with no HTTP status line.
[[ "$1" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "provider must be a catalog slug (got: $1)"
[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'
[[ -n "${ISOL8_OWNER_ID:-}" ]] || die 'Missing ISOL8_OWNER_ID.'

out="$(curl -sS -w $'\n%{http_code}' \
  -H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" \
  "${ISOL8_API_URL%/}/api/v1/connections/u/${ISOL8_OWNER_ID}/$1/authorize")"
code="${out##*$'\n'}"; body="${out%$'\n'*}"
[[ "$code" -ge 200 && "$code" -lt 300 ]] || die "HTTP $code from authorize for '$1': $body"

# Capture, then print: `jq -e` on a null field writes "null" to stdout before
# exiting 1, and a caller using $(...) would otherwise receive the string null.
url="$(printf '%s' "$body" | jq -er '.authorize_url' 2>/dev/null)" \
  || die "authorize for '$1' returned 2xx but no authorize_url: $body"
printf '%s\n' "$url"
