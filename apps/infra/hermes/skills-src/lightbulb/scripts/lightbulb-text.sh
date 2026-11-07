#!/usr/bin/env bash
# Lightbulb text CLI — reply to a customer who texted the business's number.
# All policy is decided by the backend; this only relays.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  lightbulb-text.sh send --reply <Reply-Key>               (text on stdin)
  lightbulb-text.sh set-business-phone <E.164>
Options:
  --as <agent_id>   act as this agent (default: $LIGHTBULB_AGENT_ID, else your workspace-<id> cwd)
Env (already set): OPENCLAW_HOOK_TOKEN, ISOL8_API_URL, ISOL8_OWNER_ID
EOF
}
die() { printf '%s\n' "$1" >&2; exit 1; }

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
cmd="$1"; shift
command -v jq >/dev/null || die 'Missing jq'; command -v curl >/dev/null || die 'Missing curl'
[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'
[[ -n "${ISOL8_OWNER_ID:-}" ]] || die 'Missing ISOL8_OWNER_ID.'

need_val() { [[ $# -ge 2 && -n "${2:-}" ]] || die "Option $1 needs a value."; }

agent_id="${LIGHTBULB_AGENT_ID:-${PAPERCLIP_AGENT_ID:-}}"
case "$PWD" in */workspace-*) [[ -n "$agent_id" ]] || agent_id="${PWD##*/workspace-}" ;; esac
reply=""; phone=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --as) need_val "$@"; agent_id="$2"; shift 2 ;;
    --reply) need_val "$@"; reply="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
    *) [[ "$cmd" == set-business-phone && -z "$phone" ]] || { usage >&2; exit 2; }; phone="$1"; shift ;;
  esac
done

base="${ISOL8_API_URL%/}/api/v1/agent-text/u/${ISOL8_OWNER_ID}"
auth=(-H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" -H 'Content-Type: application/json')

call() { # method path json -- returns only on success; every other answer dies with its reason
  local out code status
  out="$(curl -sS -w $'\n%{http_code}' -X "$1" "${auth[@]}" -d "$3" "$2")"
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  status="$(jq -r '.status // "unknown"' <<<"$out" 2>/dev/null || echo unknown)"
  case "$status" in
    sent|ok) ;;
    bad_phone) die 'Not saved: give the number as +1XXXXXXXXXX.' ;;
    bad_reply_key) die 'Not sent: that Reply-Key is not a text you received.' ;;
    human_thread) die 'Not sent: a person has taken this thread over.' ;;
    no_line) die 'Not sent: this workspace has no text number.' ;;
    disabled) die 'Not sent: texting is not configured.' ;;
    send_failed) die 'Not sent: the carrier declined it; try again later.' ;;
    *) [[ "$code" -ge 200 && "$code" -lt 300 ]] && die "Not sent (status: $status)."
       printf 'HTTP %s: %s\n' "$code" "$out" >&2; exit 1 ;;
  esac
}

case "$cmd" in
  send)
    [[ -n "$reply" ]] || { usage >&2; exit 2; }
    [[ -n "$agent_id" ]] || die 'Cannot tell which agent you are: pass --as <agent_id>.'
    body="$(cat)"; [[ -n "$body" ]] || die 'Empty text: pipe the message on stdin.'
    payload="$(jq -nc --arg a "$agent_id" --arg r "$reply" --arg t "$body" '{agent_id:$a, reply_key:$r, text:$t}')"
    call POST "$base/send" "$payload"
    echo 'Text sent.' ;;
  set-business-phone)
    [[ -n "$phone" ]] || { usage >&2; exit 2; }
    [[ -n "$agent_id" ]] || die 'Cannot tell which agent you are: pass --as <agent_id>.'
    call POST "$base/business-phone" "$(jq -nc --arg a "$agent_id" --arg p "$phone" '{agent_id:$a, phone:$p}')"
    echo 'Business phone saved.' ;;
  *) usage >&2; exit 2 ;;
esac
