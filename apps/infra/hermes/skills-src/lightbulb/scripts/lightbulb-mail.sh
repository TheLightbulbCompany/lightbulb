#!/usr/bin/env bash
# Isol8 agent-mail CLI — the team address, its inbox, and the one way to send.
# All policy (who you may email) is decided by the backend; this only relays.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  lightbulb-mail.sh address
  lightbulb-mail.sh inbox [--since ISO8601] [--limit N]
  lightbulb-mail.sh send --owner --subject "S"            (body on stdin)
  lightbulb-mail.sh send --to someone@example.com --subject "S"
  lightbulb-mail.sh send --reply <Reply-Key>               (body on stdin)
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
to=""; subject=""; reply=""; owner=false; since=""; limit="20"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --as) need_val "$@"; agent_id="$2"; shift 2 ;;
    --to) need_val "$@"; to="$2"; shift 2 ;;
    --subject) need_val "$@"; subject="$2"; shift 2 ;;
    --reply) need_val "$@"; reply="$2"; shift 2 ;;
    --owner) owner=true; shift ;;
    --since) need_val "$@"; since="$2"; shift 2 ;;
    --limit) need_val "$@"; limit="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$agent_id" ]] || die 'Cannot tell which agent you are: pass --as <agent_id>.'

base="${ISOL8_API_URL%/}/api/v1/agent-mail/u/${ISOL8_OWNER_ID}"
auth=(-H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" -H 'Content-Type: application/json')

call() { # method path [json]
  local out code
  out="$(curl -sS -w $'\n%{http_code}' -X "$1" "${auth[@]}" ${3:+-d "$3"} "$2")"
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  [[ "$code" -ge 200 && "$code" -lt 300 ]] || { printf 'HTTP %s: %s\n' "$code" "$out" >&2; exit 1; }
  printf '%s' "$out"
}

case "$cmd" in
  address)
    res="$(call POST "$base/address" "$(jq -nc --arg a "$agent_id" '{agent_id:$a}')")"
    addr="$(jq -r '.address // empty' <<<"$res")"
    [[ -n "$addr" ]] || die "No address yet ($(jq -r '.reason // "unknown"' <<<"$res")). Name your company first, then retry."
    printf '%s\n' "$addr" ;;
  inbox)
    q="agent_id=$(jq -rn --arg a "$agent_id" '$a|@uri')&limit=$(jq -rn --arg l "$limit" '$l|@uri')"
    [[ -n "$since" ]] && q="$q&since=$(jq -rn --arg s "$since" '$s|@uri')"
    res="$(call GET "$base/inbox?$q")"
    jq -r 'if (.messages|length)==0 then "Inbox empty (address: \(.address // "none yet"))."
           else .messages[] | "=== \(.received_at)  from: \(.from)\nsubject: \(.subject)\nReply-Key: \(.key)\n\(.text)\n" end' <<<"$res" ;;
  send)
    body="$(cat)"; [[ -n "$body" ]] || die 'Empty body: pipe the email text on stdin.'
    n=0; $owner && n=$((n+1)); [[ -n "$to" ]] && n=$((n+1)); [[ -n "$reply" ]] && n=$((n+1))
    [[ $n -eq 1 ]] || die 'Pick exactly one of --owner, --to, --reply.'
    [[ -n "$reply" || -n "$subject" ]] || die '--subject is required.'
    payload="$(jq -nc --arg a "$agent_id" --arg s "$subject" --arg b "$body" --arg t "$to" --arg r "$reply" \
      '{agent_id:$a, subject:$s, body_text:$b} + (if $t != "" then {to:$t} else {} end) + (if $r != "" then {reply_key:$r} else {} end)')"
    res="$(call POST "$base/send" "$payload")"
    status="$(jq -r '.status // "unknown"' <<<"$res")"
    case "$status" in
      sent|already_sent) echo "Email $status." ;;
      forbidden_recipient) die 'Not sent: your owner has not allowed you to email people other than them (this includes replying to senders who are not on your wake list). Ask them to enable it in Control → Agents → Team email.' ;;
      outbound_disabled) die 'Not sent: outbound email is paused for this account (a recipient reported spam). Your owner can re-enable it in Control → Agents → Team email.' ;;
      front_desk_agent) die 'Not sent: the front desk does not send email; reply by text.' ;;
      rate_limited) die 'Not sent: daily email limit reached. Try tomorrow.' ;;
      no_address) die 'Not sent: the team has no address yet — name your company, then run: lightbulb-mail.sh address' ;;
      bad_reply_key) die 'Not sent: that Reply-Key is not one of your messages.' ;;
      *) die "Not sent (status: $status)." ;;
    esac ;;
  *) usage >&2; exit 2 ;;
esac
