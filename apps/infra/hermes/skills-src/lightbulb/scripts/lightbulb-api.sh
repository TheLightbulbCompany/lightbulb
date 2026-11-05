#!/usr/bin/env bash
# Lightbulb platform API — the one way to make a hook-token-authed call.
#
# WHY A SCRIPT: OpenClaw redacts `Authorization: Bearer <x>` on the
# session-append path (AUTHORIZATION_BEARER_REDACT_PATTERN), and that pattern
# is NOT in SHELL_REFERENCE_PRESERVING_PATTERN_SOURCES — the opaque-credential
# class `[^\s\\"',;&#?<>)}\]]+` does not exclude `$`, so a SHELL REFERENCE in
# the header is masked too. `$OPENCLAW_HOOK_TOKEN` is stored as `***`;
# `${OPENCLAW_HOOK_TOKEN:-}` as `***}`. The turn that emits the curl EXECUTES
# fine; the NEXT turn reads its own history, copies the masked form, and gets
# 401 "invalid hook token". Prod 2026-08-30: ~15 minutes of an owner's first
# run lost to that loop. A file on disk is executed, never round-tripped
# through the transcript — the same reason lightbulb-mail.sh always worked.
# Never tell an agent to inline this header; point it here.
#
# Prints the response body, then the HTTP status on its own last line (the
# shape the skill's status tables are written against). Exit 0 on any HTTP
# status — the CALLER reads the status line and decides; only a transport
# failure or a missing prerequisite exits non-zero.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  lightbulb-api.sh <METHOD> <path> [json-body]
    METHOD   GET | POST | PUT | PATCH | DELETE
    path     absolute path under the API, e.g. /api/v1/company/provisioning/u/$ISOL8_OWNER_ID/catalog
    json     request body (POST/PUT/PATCH); sent as application/json
Env (already set): OPENCLAW_HOOK_TOKEN, ISOL8_API_URL
Output: body, then the HTTP status on the last line.
EOF
}
die() { printf '%s\n' "$1" >&2; exit 1; }

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") usage >&2; exit 2 ;;
esac
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
method="$1"; path="$2"; body="${3:-}"
case "$method" in GET|POST|PUT|PATCH|DELETE) ;; *) die "Unsupported method: $method" ;; esac
[[ "$path" == /* ]] || die "path must start with / (got: $path)"
command -v curl >/dev/null || die 'Missing curl'
[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'

[[ "$path" != *//* ]] || die "empty path segment in $path — is ISOL8_OWNER_ID set?"
# Every hook-token route embeds the owner: /api/v1/<area>/u/<owner_id>/...
# (platform-info is the one public exception). Anything else — /api/v1/teams/*,
# /api/v1/container/*, ... — is the owner's BROWSER API: a Clerk session, never a
# container token, so it 401s by design. Refuse up front so the agent reads
# "wrong door", not "platform fault" (prod 2026-09-03: Friday hit /teams/routines
# through this script, read the 401 as a platform bug, and fell back to
# browsing the Teams UI).
case "$path" in
  */u/*|/api/v1/platform-info*) ;;
  *) die "owner-only route: $path takes the owner's browser session, not a container token. Hook-token routes look like /api/v1/<area>/u/\$ISOL8_OWNER_ID/...; Teams objects (routines, tasks, agents, projects) are managed through the Paperclip API (\$PAPERCLIP_API_URL). Room, member and search actions go through the lightbulb_workspace tool. Nothing to retry." ;;
esac
# A bodiless POST/PUT/PATCH 422s on FastAPI ("body: Field required"); send {}.
case "$method" in POST|PUT|PATCH) [[ -n "$body" ]] || body='{}' ;; esac
args=(-sS -w $'\n%{http_code}' -X "$method" -H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}")
# --data-raw, never -d: with -d a body starting with "@" is read as a FILE, and
# "@-" blocks on stdin under the pty exec until the tool timeout.
[[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' --data-raw "$body")
# The run this call comes from (set in room and task turns): a task run's provisioning ask becomes a Question on
# its task (spec 05 P1); every other route ignores it.
[[ -n "${LIGHTBULB_RUN_ID:-}" ]] && args+=(-H "X-Lightbulb-Run: ${LIGHTBULB_RUN_ID}")
# A task run's secret proves the run is the caller's own (checked like the runtime routes check it).
[[ -n "${LIGHTBULB_RUN_SECRET:-}" ]] && args+=(-H "X-Lightbulb-Run-Secret: ${LIGHTBULB_RUN_SECRET}")
curl "${args[@]}" "${ISOL8_API_URL%/}${path}"
