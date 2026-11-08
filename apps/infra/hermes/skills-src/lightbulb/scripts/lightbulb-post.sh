#!/usr/bin/env bash
# Post into a Lightbulb room when you are NOT in a room turn (a Paperclip run, heartbeat,
# routine). Inside a room turn, reply normally / use the room tools instead.
# All policy (which rooms you may post to) is decided by the backend; this only relays.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: lightbulb-post.sh [--feed | --room <roomId> | --user <userId> | --handle <handle>] [--issue <issueId>]
                         [--why "<reason>"] [--as <agent_id>] [--event-id <id>]   (message body on stdin)
Target, first that applies:
  --room    that room (you must already be a member); a room id, or '#name' for the
            channel of that name you are in
  --feed    the workspace feed (Activity); see references/feed.md
  --user / --handle   that workspace member's DM with you (created if missing)
  --issue   accepted and ignored (older routines pass it); the post goes to #general
  (none)    the workspace's #general channel
Options:
  --why "<reason>"  "Why I created this post": one or two plain sentences, <= 280 characters.
                    Required with --feed: a feed post without it is refused.
  --as <agent_id>   post as this agent (default: $LIGHTBULB_AGENT_ID, else your workspace-<id> cwd)
  --event-id <id>   idempotency key; a retry with the same key replays instead of doubling.
                    Default: a hash of agent + target + body.
Output: "Posted to room <id>." for a new message; "Already posted: ..." (exit 0, nothing
sent) when this event id was posted before, whatever the body.
Env (already set): OPENCLAW_HOOK_TOKEN, ISOL8_API_URL, ISOL8_OWNER_ID
USAGE
}
die() { printf '%s\n' "$1" >&2; exit 1; }

command -v jq >/dev/null || die 'Missing jq'; command -v curl >/dev/null || die 'Missing curl'
[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'
[[ -n "${ISOL8_OWNER_ID:-}" ]] || die 'Missing ISOL8_OWNER_ID.'

need_val() { [[ $# -ge 2 && -n "${2:-}" ]] || die "Option $1 needs a value."; }

agent_id="${LIGHTBULB_AGENT_ID:-${PAPERCLIP_AGENT_ID:-}}"
case "$PWD" in */workspace-*) [[ -n "$agent_id" ]] || agent_id="${PWD##*/workspace-}" ;; esac
room=""; user=""; handle=""; issue=""; event_id=""; feed=""; why=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --room) need_val "$@"; room="$2"; shift 2 ;;
    --feed) feed=1; shift ;;
    --why) need_val "$@"; why="$2"; shift 2 ;;
    --user) need_val "$@"; user="$2"; shift 2 ;;
    --handle) need_val "$@"; handle="$2"; shift 2 ;;
    --issue) need_val "$@"; issue="$2"; shift 2 ;;
    --as) need_val "$@"; agent_id="$2"; shift 2 ;;
    --event-id) need_val "$@"; event_id="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$agent_id" ]] || die 'Cannot tell which agent you are: pass --as <agent_id>.'
n=0; for t in "$feed" "$room" "$user" "$handle"; do [[ -n "$t" ]] && n=$((n+1)); done
[[ $n -le 1 ]] || die 'Pick at most one of --feed, --room, --user, --handle.'
[[ -z "$feed" || -n "${why//[[:space:]]/}" ]] || { usage >&2; die '--feed needs --why "<why it matters>".'; }
body="$(cat)"; [[ -n "${body//[[:space:]]/}" ]] || die 'Empty body: pipe the message text on stdin.'

sha() { if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi | cut -c1-32; }
# Event ids are owner-wide on the backend, so every key is scoped to the posting agent.
[[ -n "$event_id" ]] || event_id="$(printf '%s\n%s\n%s\n%s\n%s\n%s%s' "$agent_id" "${feed:+feed}$room" "$user" "$handle" "$issue" "$body" "${why:+$'\n'why:$why}" | sha)"
event_id="post:${agent_id}:${event_id}"

api="${ISOL8_API_URL%/}/api/v1/internal/messaging"
seen="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" \
  "${api}/agent-messages/${ISOL8_OWNER_ID}/receipts/$(jq -rn --arg e "$event_id" '$e|@uri')")" || seen=""
if [[ "$seen" == 200 ]]; then printf 'Already posted: event %s was sent before; nothing new sent.\n' "$event_id"; exit 0; fi

payload="$(jq -nc --arg e "$event_id" --arg a "$agent_id" --arg t "$body" \
  --arg room "$room" --arg user "$user" --arg handle "$handle" --arg issue "$issue" \
  --arg feed "$feed" --arg why "$why" \
  '{eventId:$e, agentId:$a, text:$t}
   + (if $feed != "" then {feed:true} else {} end)
   + (if $why != "" then {postReason:$why} else {} end)
   + (if $room == "" then {} elif ($room|startswith("#")) then {roomName:$room} else {roomId:$room} end)
   + (if $user != "" then {userId:$user} else {} end)
   + (if $handle != "" then {handle:$handle} else {} end)
   + (if $issue != "" then {issueId:$issue} else {} end)')"
out="$(curl -sS -w $'\n%{http_code}' -X POST \
  -H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" -H 'Content-Type: application/json' \
  -d "$payload" "${api}/agent-posts/${ISOL8_OWNER_ID}")"
code="${out##*$'\n'}"; out="${out%$'\n'*}"
[[ "$code" -ge 200 && "$code" -lt 300 ]] || die "Not posted (HTTP $code): $(jq -r '.message // .detail // .' <<<"$out" 2>/dev/null || printf '%s' "$out")"
printf 'Posted to room %s.\n' "$(jq -r '.roomId' <<<"$out")"
