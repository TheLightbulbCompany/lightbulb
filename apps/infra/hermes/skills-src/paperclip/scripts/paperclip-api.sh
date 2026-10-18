#!/usr/bin/env bash

# Paperclip coordination CLI.
#
# Every Paperclip API call that *writes* (comments, status patches, checkout,
# issues, subtasks, issue-thread interactions) must go through this helper instead of a
# hand-written `curl ... jq` / inline-JSON one-liner. The model only passes
# plain arguments and stdin; this script builds the JSON body with
# `jq -nc --arg` so strings (markdown bodies, prompts, reasons) are always
# JSON-safe. A hand-built body once produced a malformed payload like
# `{body:}` (jq syntax error from `{body:}`) that silently failed the write and
# stalled the run forever — this helper makes that class of failure impossible.

set -euo pipefail

usage() {
  cat <<'EOF'
Paperclip coordination helper. Builds request bodies and query strings safely.

Usage:
  scripts/paperclip-api.sh <subcommand> [args] [--dry-run]

Global options:
  --dry-run     Print the JSON body or encoded read URL (no HTTP request).
  --help, -h    Show this help.

Environment (required for live requests; not needed for --dry-run):
  PAPERCLIP_API_URL, PAPERCLIP_API_KEY
  PAPERCLIP_AGENT_ID is additionally required for `checkout`.
  PAPERCLIP_RUN_ID is optional: it exists only on heartbeat-wake runs, and the
  X-Paperclip-Run-Id header is sent only when it is set. Webchat/interactive
  sessions have no run id — the header is omitted; never invent one. (In a
  room turn the header also carries a Paperclip run the bridge attached to
  this conversation for that issue.) A create/update/checkout from a room
  turn records the issue as this conversation's: Paperclip's wakes for it
  come back to this conversation.

Subcommands (markdown / freeform text is read from stdin where noted):

  comment <issueId>
      POST /api/issues/{id}/comments   body: { body }
      Reads the markdown comment body from stdin (required).

  update <issueId> [--status STATUS] [--title T] [--priority P]
         [--assignee <agentId>] [--blocked-by "id1,id2"]
      PATCH /api/issues/{id}           body: { status?, title?, priority?,
                                              assigneeAgentId?, comment?,
                                              blockedByIssueIds? }
      Reads an optional markdown comment from stdin. At least one flag or a
      stdin comment is required.
      STATUS: backlog|todo|in_progress|in_review|done|blocked|cancelled
      --blocked-by replaces the blocker set; pass --blocked-by "" to clear it.
      --assignee + --status todo STARTS work: it dispatches a heartbeat run
      for the assignee; for an issue a room conversation owns, that run joins
      the conversation (see the skill's "Webchat and interactive sessions").

  monitor <issueId> --at ISO8601 [--service NAME] [--notes TEXT]
          [--timeout-at ISO8601] [--max-attempts N] [--recovery POLICY]
  monitor <issueId> --clear
      PATCH /api/issues/{id}  body: { executionPolicy: { ..., monitor } }
      Wait for an EXTERNAL thing (a PR to merge, a build to finish, a reply to
      land) without a human round-trip: Paperclip re-checks at --at and wakes
      the assignee. Prefer this over a request_confirmation card that only
      asks someone to tell you the thing happened.
      RECOVERY (when the wait times out or runs out of attempts; default
      wake_owner): wake_owner | create_recovery_issue | escalate_to_board
      PRECONDITIONS (else upstream 422s): the issue must be in_progress or
      in_review AND assigned to an agent (not to a human user).
      ONE-SHOT: when it fires, the monitor is consumed — nextCheckAt is
      cleared. Reschedule if you still need to wait.
      Name what you are watching in --notes; there is deliberately no --ref
      flag because upstream redacts externalRef at write time.
      Reads the issue's current executionPolicy and MERGES the monitor onto it
      — executionPolicy is REPLACE-semantics upstream, so sending the monitor
      alone would wipe the issue's reviewer/approver stages.
      A monitor is the WAIT, never the AUTHORITY: it does not entitle you to
      act on what it observes. Keep the approval gate you already needed.

  watchdog <issueId> [--agent-id ID] [--instructions TEXT]
  watchdog <issueId> --clear
      PUT /api/issues/{id}/watchdog      body: { agentId, instructions? }
      DELETE /api/issues/{id}/watchdog   (--clear; no body)
      Arm the issue's task watchdog: durable state that watches the issue's
      subtree and, when work stops with no live path, opens a "Watchdog
      review" child issue for the named agent and wakes it. One per issue;
      arming again replaces it. Arm one whenever you schedule a monitor.
      agentId defaults to $PAPERCLIP_AGENT_ID. --instructions is what the
      woken agent reads: say what to re-check and how to resume.

  checkout <issueId> [--agent-id ID] [--expect "s1,s2,..."]
      POST /api/issues/{id}/checkout   body: { agentId, expectedStatuses }
      agentId defaults to $PAPERCLIP_AGENT_ID.
      expectedStatuses defaults to todo,backlog,blocked,in_review.

  release <issueId>
      POST /api/issues/{id}/release    (no body)

  subtask <companyId> --title T --parent <id> [--goal <id>] [--status S]
          [--priority P] [--assignee <agentId>] [--blocked-by "id1,id2"]
          [--inherit-workspace <issueId>] [--billing-code CODE]
          [--project <id>] [--assignee-user <userId>]
      POST /api/companies/{companyId}/issues
      body: { title, parentId, goalId?, description?, status?, priority?,
              assigneeAgentId?, blockedByIssueIds?,
              inheritExecutionWorkspaceFromIssueId?, billingCode?,
              projectId?, assigneeUserId? }
      Reads an optional markdown description from stdin.
      STATUS: backlog|todo|in_progress|in_review|done|blocked|cancelled
      With --assignee and no --status, sends an EXPLICIT backlog (see
      "Assigning work" at the bottom) — pass --status todo to start it now.

  create <companyId> --title T [--goal <id>] [--status S]
         [--priority P] [--assignee <agentId>] [--blocked-by "id1,id2"]
         [--inherit-workspace <issueId>] [--billing-code CODE]
         [--project <id>] [--assignee-user <userId>]
      POST /api/companies/{companyId}/issues   (top-level issue; no parentId)
      body: { title, goalId?, description?, status?, priority?,
              assigneeAgentId?, blockedByIssueIds?,
              inheritExecutionWorkspaceFromIssueId?, billingCode?,
              projectId?, assigneeUserId? }
      Reads an optional markdown description from stdin.
      STATUS: backlog|todo|in_progress|in_review|done|blocked|cancelled
      With --assignee and no --status, sends an EXPLICIT backlog (see
      "Assigning work" at the bottom) — pass --status todo to start it now.

  confirm <issueId> --prompt P [--plan] [--title T] [--idempotency-key K]
          [--continuation none|wake_assignee|wake_assignee_on_accept]
          [--accept-label L] [--reject-label L] [--reject-requires-reason]
      POST /api/issues/{id}/interactions   kind: request_confirmation
      Builds payload.version=1 + payload.prompt for you (cannot be omitted).
      Reads optional details markdown from stdin (payload.detailsMarkdown).
      --continuation defaults to wake_assignee.
      --plan binds the card to the issue's `plan` document: it reads the
      document's latest revision and sets payload.target to it. Use it for
      every plan approval — accepting a plan-bound card is what switches a
      planning-mode issue back to standard and starts execution. Write the
      plan first; a later plan revision makes the card stale.

  accept <issueId> <interactionId> [--option ID]... [--client-key KEY]...
      POST /api/issues/{id}/interactions/{interactionId}/accept
      body: { selectedOptionIds?, selectedClientKeys? }
      --option   selects a request_checkbox_confirmation option (repeatable).
      --client-key selects a suggest_tasks proposal (repeatable).
      With no flags the body is {} (request_confirmation needs nothing else).

  reject <issueId> <interactionId> [--reason R]
      POST /api/issues/{id}/interactions/{interactionId}/reject
      body: { reason? }

  respond <issueId> <interactionId> [--summary MD]
      POST /api/issues/{id}/interactions/{interactionId}/respond
      body: { answers, summaryMarkdown? }
      Reads the ask_user_questions answers as JSON on stdin (required). The
      JSON is validated before sending; a malformed answers array fails loudly
      here instead of silently at the API. Pass either the answers array, e.g.
        [{"questionId":"q1","optionIds":["o1"]}]
      or a full object { "answers": [...], "summaryMarkdown": "..." }.

  decision <companyId> --title T --rule-key K --idempotency-key K
           [--expires-at ISO] [--continuation none|wake_origin_agent]
      POST /api/companies/{companyId}/decisions
      Reads a jq-built object with `body`, `options`, and optional `inputs` or
      `metadata` from stdin. The helper supplies the title, stable ruleKey,
      idempotency key, expiry, and continuation policy. Live calls require a
      real PAPERCLIP_RUN_ID because decisions are run-scoped.

  decision-stats <companyId> [--since ISO]
      GET /api/companies/{companyId}/decisions/stats?groupBy=ruleKey
      Agents are always server-scoped to their own originAgentId.

  decision-get <decisionId>
      GET /api/decisions/{decisionId}
      Origin agents use this after wake to verify every effect execution.

  search <companyId> --query Q [--scope S] [--status CSV] [--priority CSV]
         [--assignee-agent ID|null] [--project ID] [--label ID]
         [--updated-within D|--updated-after ISO] [--sort S]
         [--limit N] [--offset N]
      GET /api/companies/{companyId}/search with safely encoded filters.

  extract <companyId> --contains TEXT [--kind literal|url] [--scope S]
          [--status CSV] [--updated-within D|--updated-after ISO]
          [--matches-per-issue N] [--limit N] [--offset N]
      GET /api/companies/{companyId}/search/extract.

  queue-list <companyId>
      GET /api/companies/{companyId}/decision-queues.

  queue-items <companyId> --key K
      GET /api/companies/{companyId}/decision-queues/{key}/items.

  queue-create <companyId> --key K --title T [--description D]
               [--retention-days N]
      POST /api/companies/{companyId}/decision-queues.

  queue-add <companyId> --key K --source-kind K --source-id ID
      POST /api/companies/{companyId}/decision-queues/{key}/items.

  triage <companyId> --source-kind K --source-id ID
         [--decide-by today|this_week|whenever|YYYY-MM-DD|--clear-decide-by]
         [--snoozed-until ISO|--clear-snooze]
      PUT /api/companies/{companyId}/decision-triage/{kind}/{id}.

  raw <METHOD> </api/...path>
      Escape hatch for endpoints without a dedicated subcommand
      (checkbox/questions/suggest-tasks interaction creates, issue documents,
      ...). A person's decision is the request_approval tool, never this. METHOD: GET|POST|PATCH|PUT|DELETE. The path must start
      with /api/. Reads an optional complete JSON body from stdin — build it
      with jq -n (never hand-type inline JSON). The body is validated with jq
      before sending and rejected loudly if it does not parse.

Assigning work (create / subtask):
  An issue that has an assignee AND a status other than `backlog` DISPATCHES
  AN AGENT RUN the moment it is created, and a 30s server-side reconciler
  re-dispatches it until it escalates to `blocked`. Paperclip's own create
  default is not neutral about this: omit `status` while passing an assignee
  and the server silently promotes the issue to `todo` — i.e. it starts.
  So this helper sends an explicit `backlog` for `--assignee` + no `--status`.
  Filing work to someone and starting it right now are different acts, and the
  second one spends money — ask for it with `--status todo`, never by omission.
  Creates with no assignee are unchanged: `status` stays omitted.
  `--assignee-user` files a task for a person (it shows in their Inbox); it
  never starts a run.

Examples:
  scripts/paperclip-api.sh comment "$PAPERCLIP_TASK_ID" <<'MD'
  ## Update

  - Investigated the failing path
  - Root cause was a stale cache key
  MD

  scripts/paperclip-api.sh update "$PAPERCLIP_TASK_ID" --status done <<'MD'
  Fixed and verified.
  MD

  scripts/paperclip-api.sh checkout "$PAPERCLIP_TASK_ID"
  scripts/paperclip-api.sh confirm "$PAPERCLIP_TASK_ID" --prompt "Accept this plan?" <<'MD'
  Review the plan document before accepting.
  MD

  # File assigned work WITHOUT starting it (the default):
  scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" --title "Ship pricing page" \
    --assignee "<agentId>" --status backlog

  # Assign AND start it right now (deliberate — this spends money):
  scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" --title "Hotfix checkout 500" \
    --assignee "<agentId>" --status todo
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

read_stdin() {
  # Echo piped stdin (preserving newlines); empty string when nothing is piped.
  if [[ ! -t 0 ]]; then
    cat
  fi
}

require_live_env() {
  if [[ -z "${PAPERCLIP_API_URL:-}" || -z "${PAPERCLIP_API_KEY:-}" ]]; then
    die 'Missing PAPERCLIP_API_URL or PAPERCLIP_API_KEY.'
  fi
}

# Paperclip's ISSUE_STATUSES enum (packages/shared/src/constants.ts).
ISSUE_STATUSES="backlog todo in_progress in_review done blocked cancelled"

require_valid_status() {
  # require_valid_status <subcommand> <status>
  local cmd="$1" status="$2"
  case " $ISSUE_STATUSES " in
    *" $status "*) return 0 ;;
  esac
  die "$cmd: invalid --status '$status' (expected one of: ${ISSUE_STATUSES// /, })."
}

# resolve_create_status <subcommand> <status> <assignee>
#
# Echoes the status to put in a create/subtask body ("" = omit the field).
#
# Stock Paperclip's default (`resolveCreateIssueStatusDefault`): an assigned
# create with no status is `todo`, so the assignee starts on it; an unassigned
# one is `backlog`. This sends that `todo` EXPLICITLY rather than omitting it,
# so a room-session create still takes the bind-then-start split below.
# `--status backlog` remains the way to file assigned work for later.
resolve_create_status() {
  local cmd="$1" status="$2" assignee="$3"

  if [[ -n "$status" ]]; then
    require_valid_status "$cmd" "$status"
    printf '%s' "$status"
    return 0
  fi

  if [[ -n "$assignee" ]]; then
    printf 'todo'
    return 0
  fi

  printf ''
}

api_base() {
  local base="${PAPERCLIP_API_URL:-}"
  printf '%s/api' "${base%/}"
}

# send METHOD URL [PAYLOAD]
# In --dry-run, prints the payload (or "(no request body)") and returns.
# Otherwise POSTs/PATCHes and fails loudly on any non-2xx status.
send() {
  local method="$1"
  local url="$2"
  local has_payload=0
  local payload=""
  if [[ $# -ge 3 ]]; then
    has_payload=1
    payload="$3"
  fi

  if [[ "$dry_run" == "1" ]]; then
    if [[ "$has_payload" == "1" ]]; then
      printf '%s\n' "$payload"
    else
      printf '(no request body)\n'
    fi
    return 0
  fi

  require_live_env
  require_command curl

  if in_room_conversation && [[ "$has_payload" == "1" && "$method" == "POST" && "$url" =~ /api/companies/[^/?]+/issues$ ]]; then
    local wake_status
    wake_status="$(jq -r 'if (.assigneeAgentId // "") != "" and (.status // "todo") != "backlog"
                          then (.status // "todo") else empty end' <<<"$payload")"
    if [[ -n "$wake_status" ]]; then
      create_bound_then_start "$url" "$payload" "$wake_status"
      return 0
    fi
  fi

  local bound_id="" bound_new=0
  if in_room_conversation && [[ ("$method" == "PATCH" && "$url" =~ /api/issues/[^/?]+$)
                                || ("$method" == "POST" && "$url" =~ /api/issues/[^/?]+/checkout$) ]]; then
    # Bind BEFORE the request that can wake the assignee, so Paperclip's wake
    # can never reach the bridge ahead of the binding. Undone if the write fails.
    bound_id="$(issue_uuid "$url")"
    if [[ -n "$bound_id" ]]; then
      [[ -e "$(binding_path "$bound_id")" ]] || bound_new=1
      write_binding "$bound_id"
    fi
  fi

  if [[ "$has_payload" == "1" ]]; then
    http_call "$method" "$url" "$payload"
  else
    http_call "$method" "$url"
  fi
  if ! http_ok; then
    [[ "$bound_new" == "1" ]] && rm -f "$(binding_path "$bound_id")"
    fail_http "$method" "$url"
  fi

  [[ "$method" == "GET" ]] || record_issue_conversation "$method" "$url" "$HTTP_RESPONSE"
  cat "$HTTP_RESPONSE"
  printf '\n'
  rm -f "$HTTP_RESPONSE"
}

# http_call METHOD URL [PAYLOAD]: sets HTTP_STATUS and HTTP_RESPONSE (a temp
# file the caller removes).
#
# X-Paperclip-Run-Id attributes the write to the current heartbeat run. A
# run id exists only on heartbeat-wake runs; webchat/interactive sessions
# have none, so the header is omitted entirely (never fabricate one) --
# unless the Lightbulb bridge attached Paperclip's run for this issue to
# this conversation (attached_run_id).
HTTP_STATUS=""
HTTP_RESPONSE=""
http_call() {
  local method="$1" url="$2"
  local run_id="${PAPERCLIP_RUN_ID:-}"
  [[ -n "$run_id" ]] || run_id="$(attached_run_id "$url")"
  local run_id_header=()
  if [[ -n "$run_id" ]]; then
    run_id_header=(-H "X-Paperclip-Run-Id: $run_id")
  fi
  HTTP_RESPONSE="$(mktemp)"
  if [[ $# -ge 3 ]]; then
    HTTP_STATUS="$(
      curl -sS -X "$method" -w '%{http_code}' -o "$HTTP_RESPONSE" \
        "$url" \
        -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
        "${run_id_header[@]+"${run_id_header[@]}"}" \
        -H 'Content-Type: application/json' \
        --data-binary "$3"
    )"
  else
    HTTP_STATUS="$(
      curl -sS -X "$method" -w '%{http_code}' -o "$HTTP_RESPONSE" \
        "$url" \
        -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
        "${run_id_header[@]+"${run_id_header[@]}"}"
    )"
  fi
}

http_ok() {
  [[ "$HTTP_STATUS" =~ ^[0-9]+$ && "$HTTP_STATUS" -ge 200 && "$HTTP_STATUS" -lt 300 ]]
}

fail_http() {
  printf 'Paperclip API request failed (HTTP %s): %s %s\n' "$HTTP_STATUS" "$1" "$2" >&2
  cat "$HTTP_RESPONSE" >&2
  printf '\n' >&2
  rm -f "$HTTP_RESPONSE"
  exit 1
}

# The Lightbulb bridge runs both room turns and Paperclip's runs. When a room
# conversation creates, updates or checks out an issue, this records that the
# issue belongs to that conversation, and the bridge lands Paperclip's later
# wakes for it there instead of in a fresh session that redoes the work. Only
# a room conversation records; an issue that goes done/cancelled drops its
# record. The room is LIGHTBULB_ROOM_SESSION_KEY when the bridge set it (a call,
# or a turn that ran beside its busy room in its own session), else
# HERMES_SESSION_KEY.
#
# The record must exist BEFORE any write that can wake the assignee, or the
# wake can reach the bridge first and start a separate session. So an issue
# PATCH or checkout binds before it is sent, and an assigned non-backlog
# create is split: create as `backlog` (Paperclip never wakes for backlog),
# bind the returned id, then PATCH to the asked-for status.
ROOM_SESSION_RE='^agent:[^:]+:lightbulb:channel:[0-9a-f-]{36}$'
ISSUE_ID_RE='^[A-Za-z0-9-]{1,128}$'
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

room_session_key() {
  printf '%s' "${LIGHTBULB_ROOM_SESSION_KEY:-${HERMES_SESSION_KEY:-}}"
}

in_room_conversation() {
  [[ -n "${HERMES_HOME:-}" && "$(room_session_key)" =~ $ROOM_SESSION_RE ]]
}

binding_path() {
  printf '%s/lightbulb-issue-bindings/%s.json' "$HERMES_HOME" "$1"
}

# write_binding ID: `boundAt` (epoch seconds, refreshed on every write) is
# what the bridge ages the binding by -- a file's mtime is reset by every
# profile restore.
write_binding() {
  local path
  path="$(binding_path "$1")"
  if ! { mkdir -p "${path%/*}" \
         && jq -nc --arg key "$(room_session_key)" --arg origin "${HERMES_SESSION_KEY:-}" \
              --argjson at "$(date +%s)" \
              '{sessionKey: $key, boundAt: $at} + (if $origin != $key then {origin: $origin} else {} end)' \
              >"$path"; }; then
    printf 'note: could not record this issue for this conversation\n' >&2
  fi
  return 0
}

# issue_uuid URL: the issue's id for an /api/issues/<id>... URL. An identifier
# (ISOA-680) is resolved with a GET, which never wakes anyone; empty if that
# fails (the write then goes ahead unbound before it, bound after it).
issue_uuid() {
  local id="${1#*/api/issues/}"
  id="${id%%/*}"
  id="${id%%\?*}"
  if [[ "$id" =~ $UUID_RE ]]; then
    printf '%s' "$id"
    return 0
  fi
  [[ "$id" =~ $ISSUE_ID_RE ]] || return 0
  http_call GET "$(api_base)/issues/$id"
  if http_ok; then
    jq -r '.id // empty' <"$HTTP_RESPONSE" 2>/dev/null | grep -E "$UUID_RE" || true
  fi
  rm -f "$HTTP_RESPONSE"
  return 0
}

# create_bound_then_start URL PAYLOAD STATUS: the split create described above.
create_bound_then_start() {
  local url="$1" payload="$2" wake_status="$3" id
  http_call POST "$url" "$(jq -c '.status = "backlog"' <<<"$payload")"
  http_ok || fail_http POST "$url"
  id="$(jq -r '.id // empty' <"$HTTP_RESPONSE" 2>/dev/null || true)"
  if [[ ! "$id" =~ $ISSUE_ID_RE ]]; then
    printf 'create: Paperclip returned no issue id; the issue was filed as backlog.\n' >&2
    cat "$HTTP_RESPONSE"
    printf '\n'
    rm -f "$HTTP_RESPONSE"
    exit 1
  fi
  rm -f "$HTTP_RESPONSE"
  write_binding "$id"
  local patch_url
  patch_url="$(api_base)/issues/$id"
  http_call PATCH "$patch_url" "$(jq -nc --arg status "$wake_status" '{status: $status}')"
  if ! http_ok; then
    rm -f "$(binding_path "$id")"
    printf 'create: issue %s was filed as backlog, but moving it to %s failed.\n' "$id" "$wake_status" >&2
    fail_http PATCH "$patch_url"
  fi
  record_issue_conversation PATCH "$patch_url" "$HTTP_RESPONSE"
  cat "$HTTP_RESPONSE"
  printf '\n'
  rm -f "$HTTP_RESPONSE"
}

# After a successful write: refresh the binding from the response's own id (a
# create/PATCH/checkout answers with the issue), or drop it when the issue
# went done/cancelled. Never fails the write.
record_issue_conversation() {
  local method="$1" url="$2" response_file="$3"
  in_room_conversation || return 0
  [[ ("$method" == "POST" && "$url" =~ /api/companies/[^/?]+/issues$)
     || ("$method" == "PATCH" && "$url" =~ /api/issues/[^/?]+$)
     || ("$method" == "POST" && "$url" =~ /api/issues/[^/?]+/checkout$) ]] || return 0
  local id status
  id="$(jq -r '.id // empty' <"$response_file" 2>/dev/null)" || return 0
  status="$(jq -r '.status // empty' <"$response_file" 2>/dev/null)" || return 0
  [[ "$id" =~ $ISSUE_ID_RE ]] || return 0
  if [[ "$status" == "done" || "$status" == "cancelled" ]]; then
    rm -f "$(binding_path "$id")"
  else
    write_binding "$id"
  fi
  return 0
}

# attached_run_id URL: the run id the bridge published for the issue this URL
# addresses (/api/issues/<id>/...) while it holds Paperclip's run for that
# issue in this agent's room conversation; empty otherwise.
attached_run_id() {
  local id="${1#*/api/issues/}" run=""
  [[ -n "${HERMES_HOME:-}" && "$id" != "$1" ]] || return 0
  id="${id%%/*}"
  id="${id%%\?*}"
  [[ "$id" =~ $ISSUE_ID_RE && -r "$HERMES_HOME/.lightbulb-turn/paperclip-run.$id" ]] || return 0
  run="$(<"$HERMES_HOME/.lightbulb-turn/paperclip-run.$id")"
  [[ "$run" =~ ^[A-Za-z0-9_-]{1,128}$ ]] && printf '%s' "$run"
  return 0
}

# Read-only commands print their fully encoded URL in --dry-run mode. The
# generic send() keeps its historical payload-only dry-run contract for writes.
send_read() {
  local url="$1"
  if [[ "$dry_run" == "1" ]]; then
    printf 'GET %s\n' "$url"
    return 0
  fi
  send GET "$url"
}

urlencode() {
  jq -rn --arg value "$1" '$value | @uri'
}

append_query() {
  local url="$1" key="$2" value="$3" separator="?"
  [[ "$url" == *\?* ]] && separator="&"
  printf '%s%s%s=%s' "$url" "$separator" "$(urlencode "$key")" "$(urlencode "$value")"
}

require_integer_between() {
  local command="$1" field="$2" value="$3" minimum="$4" maximum="$5"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$command: $field must be an integer."
  local numeric=$((10#$value))
  (( numeric >= minimum && numeric <= maximum )) \
    || die "$command: $field must be between $minimum and $maximum."
}

require_choice() {
  local command="$1" field="$2" value="$3" choices="$4"
  local option
  local -a allowed=()
  IFS=',' read -r -a allowed <<<"$choices"
  for option in "${allowed[@]}"; do
    [[ "$value" == "$option" ]] && return 0
  done
  die "$command: $field must be one of: $choices."
}

cmd_comment() {
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'comment: missing <issueId>.'
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      *) die "comment: unknown argument: $1" ;;
    esac
  done

  local body
  body="$(read_stdin)"
  [[ -n "$body" ]] || die 'comment: a markdown comment body is required on stdin.'

  local payload
  payload="$(jq -nc --arg body "$body" '{body: $body}')"
  send POST "$(api_base)/issues/$issue_id/comments" "$payload"
}

cmd_update() {
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'update: missing <issueId>.'
  local status="" title="" priority="" assignee=""
  local blocked_by="" blocked_set=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status) status="${2:-}"; shift 2 ;;
      --title) title="${2:-}"; shift 2 ;;
      --priority) priority="${2:-}"; shift 2 ;;
      --assignee) assignee="${2:-}"; shift 2 ;;
      --blocked-by) blocked_by="${2:-}"; blocked_set=1; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "update: unknown argument: $1" ;;
    esac
  done

  local comment
  comment="$(read_stdin)"
  [[ -n "$status" || -n "$title" || -n "$priority" || -n "$assignee" || -n "$comment" || "$blocked_set" == "1" ]] \
    || die 'update: pass --status, --title, --priority, --assignee, --blocked-by, and/or a stdin comment.'

  local payload
  payload="$(
    jq -nc \
      --arg status "$status" \
      --arg title "$title" \
      --arg priority "$priority" \
      --arg assignee "$assignee" \
      --arg comment "$comment" \
      --arg blockedBy "$blocked_by" \
      --argjson blockedSet "$blocked_set" '
      (if $status == "" then {} else {status: $status} end)
      + (if $title == "" then {} else {title: $title} end)
      + (if $priority == "" then {} else {priority: $priority} end)
      + (if $assignee == "" then {} else {assigneeAgentId: $assignee} end)
      + (if $comment == "" then {} else {comment: $comment} end)
      + (if $blockedSet == 1
         then {blockedByIssueIds: ($blockedBy | split(",") | map(select(length > 0)))}
         else {}
         end)
    '
  )"
  send PATCH "$(api_base)/issues/$issue_id" "$payload"
}

# Paperclip's ISSUE_EXECUTION_MONITOR_RECOVERY_POLICIES enum
# (packages/shared/src/constants.ts).
MONITOR_RECOVERY_POLICIES="wake_owner,create_recovery_issue,escalate_to_board"

cmd_monitor() {
  # Schedule (or clear) an issue's execution monitor — the native "wait for an
  # external thing" primitive. Paperclip re-checks at nextCheckAt and wakes the
  # assignee, so the waiting costs no human round-trip.
  #
  # This needs its own subcommand rather than `raw PATCH` because
  # executionPolicy has REPLACE semantics upstream: PATCHing
  # {executionPolicy: {monitor: ...}} on its own silently WIPES the issue's
  # reviewer/approver stages and every other policy field. So this reads the
  # current policy first and merges the monitor onto it — the same
  # read-modify-write the board UI does before it sends a monitor edit.
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'monitor: missing <issueId>.'
  # No --ref flag on purpose: upstream runs externalRef through
  # redactIssueMonitorExternalRef at WRITE time, so any value sent is replaced
  # with the literal "[redacted]" before it is ever stored. Offering the flag
  # would silently discard whatever the agent put in it. The watched thing
  # belongs in --notes, which is persisted verbatim (500 chars).
  local at="" service="" notes="" timeout_at="" max_attempts="" recovery=""
  local clear=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --at) at="${2:-}"; shift 2 ;;
      --service) service="${2:-}"; shift 2 ;;
      --notes) notes="${2:-}"; shift 2 ;;
      --timeout-at) timeout_at="${2:-}"; shift 2 ;;
      --max-attempts) max_attempts="${2:-}"; shift 2 ;;
      --recovery) recovery="${2:-}"; shift 2 ;;
      --clear) clear=1; shift ;;
      --dry-run) dry_run=1; shift ;;
      *) die "monitor: unknown argument: $1" ;;
    esac
  done

  if [[ "$clear" == "1" ]]; then
    if [[ -n "$at$service$notes$timeout_at$max_attempts$recovery" ]]; then
      die 'monitor: --clear takes no other monitor flags.'
    fi
  else
    [[ -n "$at" ]] || die 'monitor: --at <ISO8601> is required (or pass --clear).'
    if [[ -n "$max_attempts" ]]; then
      require_integer_between monitor --max-attempts "$max_attempts" 1 100
    fi
    if [[ -n "$recovery" ]]; then
      require_choice monitor --recovery "$recovery" "$MONITOR_RECOVERY_POLICIES"
    fi
  fi

  # Merge base: the issue's CURRENT executionPolicy. Without this read the
  # PATCH would drop the stages — see the REPLACE-semantics note above.
  local existing='null'
  if [[ "$dry_run" == "1" ]]; then
    printf 'monitor: --dry-run skips the read, so the merge base below is empty; a live call preserves existing stages.\n' >&2
  else
    require_live_env
    require_command jq
    existing="$(send GET "$(api_base)/issues/$issue_id" | jq -c '.executionPolicy // null')"
  fi

  local payload
  payload="$(
    jq -nc \
      --argjson existing "$existing" \
      --argjson clear "$clear" \
      --arg at "$at" \
      --arg service "$service" \
      --arg notes "$notes" \
      --arg timeoutAt "$timeout_at" \
      --arg maxAttempts "$max_attempts" \
      --arg recovery "$recovery" '
      def opt(key; value): if value == "" then {} else {(key): value} end;
      (($existing // {}) | if type == "object" then . else {} end) as $policy
      | (if $clear == 1 then null
         else ($policy.monitor // {})
              + {nextCheckAt: $at, kind: "external_service"}
              + opt("serviceName"; $service)
              + opt("notes"; $notes)
              + opt("timeoutAt"; $timeoutAt)
              + opt("recoveryPolicy"; $recovery)
              + (if $maxAttempts == "" then {} else {maxAttempts: ($maxAttempts | tonumber)} end)
         end) as $monitor
      | {executionPolicy: ($policy + {monitor: $monitor})}
    '
  )"
  send PATCH "$(api_base)/issues/$issue_id" "$payload"
}

cmd_watchdog() {
  # Arm (or remove) the issue's task watchdog. PUT, not POST: only
  # get/put/delete are registered for /issues/:id/watchdog upstream.
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'watchdog: missing <issueId>.'
  local agent_id="${PAPERCLIP_AGENT_ID:-}" instructions="" clear=0 has_flags=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent-id) agent_id="${2:-}"; has_flags=1; shift 2 ;;
      --instructions) instructions="${2:-}"; has_flags=1; shift 2 ;;
      --clear) clear=1; shift ;;
      --dry-run) dry_run=1; shift ;;
      *) die "watchdog: unknown argument: $1" ;;
    esac
  done

  if [[ "$clear" == "1" ]]; then
    [[ "$has_flags" == "0" ]] || die 'watchdog: --clear takes no other watchdog flags.'
    send DELETE "$(api_base)/issues/$issue_id/watchdog"
    return
  fi

  if [[ "$dry_run" != "1" && -z "$agent_id" ]]; then
    die 'watchdog: missing agent id. Set PAPERCLIP_AGENT_ID or pass --agent-id.'
  fi

  local payload
  payload="$(
    jq -nc --arg agentId "$agent_id" --arg instructions "$instructions" '
      {agentId: $agentId}
      + (if $instructions == "" then {} else {instructions: $instructions} end)
    '
  )"
  send PUT "$(api_base)/issues/$issue_id/watchdog" "$payload"
}

cmd_checkout() {
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'checkout: missing <issueId>.'
  local agent_id="${PAPERCLIP_AGENT_ID:-}"
  local expect="todo,backlog,blocked,in_review"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent-id) agent_id="${2:-}"; shift 2 ;;
      --expect) expect="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "checkout: unknown argument: $1" ;;
    esac
  done

  if [[ "$dry_run" != "1" && -z "$agent_id" ]]; then
    die 'checkout: missing agent id. Set PAPERCLIP_AGENT_ID or pass --agent-id.'
  fi

  local payload
  payload="$(
    jq -nc --arg agentId "$agent_id" --arg expect "$expect" '
      {
        agentId: $agentId,
        expectedStatuses: ($expect | split(",") | map(select(length > 0)))
      }
    '
  )"
  send POST "$(api_base)/issues/$issue_id/checkout" "$payload"
}

cmd_release() {
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'release: missing <issueId>.'
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      *) die "release: unknown argument: $1" ;;
    esac
  done
  send POST "$(api_base)/issues/$issue_id/release"
}

cmd_subtask() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'subtask: missing <companyId>.'
  local title="" parent="" goal="" status="" priority="" assignee="" blocked_by=""
  local inherit_workspace="" billing_code="" project="" assignee_user=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title) title="${2:-}"; shift 2 ;;
      --parent) parent="${2:-}"; shift 2 ;;
      --goal) goal="${2:-}"; shift 2 ;;
      --status) status="${2:-}"; shift 2 ;;
      --priority) priority="${2:-}"; shift 2 ;;
      --assignee) assignee="${2:-}"; shift 2 ;;
      --blocked-by) blocked_by="${2:-}"; shift 2 ;;
      --inherit-workspace) inherit_workspace="${2:-}"; shift 2 ;;
      --billing-code) billing_code="${2:-}"; shift 2 ;;
      --project) project="${2:-}"; shift 2 ;;
      --assignee-user) assignee_user="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "subtask: unknown argument: $1" ;;
    esac
  done

  [[ -n "$title" ]] || die 'subtask: --title is required.'
  [[ -n "$parent" ]] || printf 'subtask: warning — no --parent set; subtasks should set parentId.\n' >&2
  [[ -n "$goal" ]] || printf 'subtask: warning — no --goal set; subtasks should set goalId.\n' >&2

  # A person's task never starts a run, so only an agent assignee forces backlog.
  if [[ -n "$assignee_user" && -z "$assignee" ]]; then
    status="${status:-todo}"
  else
    status="$(resolve_create_status subtask "$status" "$assignee")"
  fi

  local description
  description="$(read_stdin)"

  local payload
  payload="$(
    jq -nc \
      --arg title "$title" \
      --arg parentId "$parent" \
      --arg goalId "$goal" \
      --arg description "$description" \
      --arg status "$status" \
      --arg priority "$priority" \
      --arg assigneeAgentId "$assignee" \
      --arg blockedBy "$blocked_by" \
      --arg inheritWorkspace "$inherit_workspace" \
      --arg billingCode "$billing_code" \
      --arg projectId "$project" \
      --arg assigneeUserId "$assignee_user" '
      {title: $title}
      + (if $parentId == "" then {} else {parentId: $parentId} end)
      + (if $goalId == "" then {} else {goalId: $goalId} end)
      + (if $description == "" then {} else {description: $description} end)
      + (if $status == "" then {} else {status: $status} end)
      + (if $priority == "" then {} else {priority: $priority} end)
      + (if $assigneeAgentId == "" then {} else {assigneeAgentId: $assigneeAgentId} end)
      + (if $blockedBy == ""
         then {}
         else {blockedByIssueIds: ($blockedBy | split(",") | map(select(length > 0)))}
         end)
      + (if $inheritWorkspace == "" then {} else {inheritExecutionWorkspaceFromIssueId: $inheritWorkspace} end)
      + (if $billingCode == "" then {} else {billingCode: $billingCode} end)
      + (if $projectId == "" then {} else {projectId: $projectId} end)
      + (if $assigneeUserId == "" then {} else {assigneeUserId: $assigneeUserId} end)
    '
  )"
  send POST "$(api_base)/companies/$company_id/issues" "$payload"
}

cmd_create() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'create: missing <companyId>.'
  local title="" goal="" status="" priority="" assignee="" blocked_by=""
  local inherit_workspace="" billing_code="" project="" assignee_user=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title) title="${2:-}"; shift 2 ;;
      --goal) goal="${2:-}"; shift 2 ;;
      --status) status="${2:-}"; shift 2 ;;
      --priority) priority="${2:-}"; shift 2 ;;
      --assignee) assignee="${2:-}"; shift 2 ;;
      --blocked-by) blocked_by="${2:-}"; shift 2 ;;
      --inherit-workspace) inherit_workspace="${2:-}"; shift 2 ;;
      --billing-code) billing_code="${2:-}"; shift 2 ;;
      --project) project="${2:-}"; shift 2 ;;
      --assignee-user) assignee_user="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "create: unknown argument: $1" ;;
    esac
  done

  [[ -n "$title" ]] || die 'create: --title is required.'

  # A person's task never starts a run, so only an agent assignee forces backlog.
  if [[ -n "$assignee_user" && -z "$assignee" ]]; then
    status="${status:-todo}"
  else
    status="$(resolve_create_status create "$status" "$assignee")"
  fi

  local description
  description="$(read_stdin)"

  local payload
  payload="$(
    jq -nc \
      --arg title "$title" \
      --arg goalId "$goal" \
      --arg description "$description" \
      --arg status "$status" \
      --arg priority "$priority" \
      --arg assigneeAgentId "$assignee" \
      --arg blockedBy "$blocked_by" \
      --arg inheritWorkspace "$inherit_workspace" \
      --arg billingCode "$billing_code" \
      --arg projectId "$project" \
      --arg assigneeUserId "$assignee_user" '
      {title: $title}
      + (if $goalId == "" then {} else {goalId: $goalId} end)
      + (if $description == "" then {} else {description: $description} end)
      + (if $status == "" then {} else {status: $status} end)
      + (if $priority == "" then {} else {priority: $priority} end)
      + (if $assigneeAgentId == "" then {} else {assigneeAgentId: $assigneeAgentId} end)
      + (if $blockedBy == ""
         then {}
         else {blockedByIssueIds: ($blockedBy | split(",") | map(select(length > 0)))}
         end)
      + (if $inheritWorkspace == "" then {} else {inheritExecutionWorkspaceFromIssueId: $inheritWorkspace} end)
      + (if $billingCode == "" then {} else {billingCode: $billingCode} end)
      + (if $projectId == "" then {} else {projectId: $projectId} end)
      + (if $assigneeUserId == "" then {} else {assigneeUserId: $assigneeUserId} end)
    '
  )"
  send POST "$(api_base)/companies/$company_id/issues" "$payload"
}

cmd_confirm() {
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'confirm: missing <issueId>.'
  local prompt="" title="" idempotency_key="" continuation="wake_assignee"
  local accept_label="" reject_label="" reject_requires_reason=0 plan=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prompt) prompt="${2:-}"; shift 2 ;;
      --plan) plan=1; shift ;;
      --title) title="${2:-}"; shift 2 ;;
      --idempotency-key) idempotency_key="${2:-}"; shift 2 ;;
      --continuation) continuation="${2:-}"; shift 2 ;;
      --accept-label) accept_label="${2:-}"; shift 2 ;;
      --reject-label) reject_label="${2:-}"; shift 2 ;;
      --reject-requires-reason) reject_requires_reason=1; shift ;;
      --dry-run) dry_run=1; shift ;;
      *) die "confirm: unknown argument: $1" ;;
    esac
  done

  [[ -n "$prompt" ]] || die 'confirm: --prompt is required.'

  local details
  details="$(read_stdin)"

  # --plan: bind the card to the plan document's CURRENT revision. Upstream
  # only treats an accepted confirmation as "start executing the plan" (and
  # flips a planning-mode issue back to standard) when payload.target is
  # {type: "issue_document", key: "plan", revisionId}, and it 422s a
  # revisionId that is not the document's latest. So read it here rather than
  # trusting one the caller remembered.
  local target='null'
  if [[ "$plan" == "1" ]]; then
    if [[ "$dry_run" == "1" ]]; then
      printf 'confirm: --dry-run skips the plan read, so revisionId below is a placeholder; a live call sends the latest revision.\n' >&2
      target='{"type":"issue_document","key":"plan","revisionId":"<latest plan revision id>"}'
    else
      require_live_env
      require_command jq
      target="$(
        send GET "$(api_base)/issues/$issue_id/documents/plan" | jq -c '
          if (.latestRevisionId // "") == "" then error("no revision") else . end
          | {type: "issue_document", key: "plan", revisionId: .latestRevisionId}
            + (if .issueId then {issueId: .issueId} else {} end)
            + (if .id then {documentId: .id} else {} end)
            + (if .latestRevisionNumber then {revisionNumber: .latestRevisionNumber} else {} end)
        '
      )" || die 'confirm: --plan needs a plan document with a revision. Write the plan first (PUT /api/issues/{id}/documents/plan).'
    fi
  fi

  local payload
  payload="$(
    jq -nc \
      --argjson target "$target" \
      --arg prompt "$prompt" \
      --arg title "$title" \
      --arg idempotencyKey "$idempotency_key" \
      --arg continuationPolicy "$continuation" \
      --arg details "$details" \
      --arg acceptLabel "$accept_label" \
      --arg rejectLabel "$reject_label" \
      --argjson rejectRequiresReason "$( [[ "$reject_requires_reason" == "1" ]] && echo true || echo false )" '
      {
        kind: "request_confirmation",
        resolverPolicy: "board_only",
        continuationPolicy: $continuationPolicy,
        payload: (
          {version: 1, prompt: $prompt}
          + (if $details == "" then {} else {detailsMarkdown: $details} end)
          + (if $acceptLabel == "" then {} else {acceptLabel: $acceptLabel} end)
          + (if $rejectLabel == "" then {} else {rejectLabel: $rejectLabel} end)
          + (if $rejectRequiresReason then {rejectRequiresReason: true} else {} end)
          + (if $target == null then {} else {target: $target} end)
        )
      }
      + (if $title == "" then {} else {title: $title} end)
      + (if $idempotencyKey == "" then {} else {idempotencyKey: $idempotencyKey} end)
    '
  )"
  send POST "$(api_base)/issues/$issue_id/interactions" "$payload"
}

cmd_accept() {
  local issue_id="${1:-}"
  local interaction_id="${2:-}"
  shift 2 2>/dev/null || true
  [[ -n "$issue_id" && -n "$interaction_id" ]] || die 'accept: missing <issueId> <interactionId>.'
  local options=() client_keys=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --option) options+=("${2:-}"); shift 2 ;;
      --client-key) client_keys+=("${2:-}"); shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "accept: unknown argument: $1" ;;
    esac
  done

  local options_json client_keys_json
  options_json="$(printf '%s\n' "${options[@]+"${options[@]}"}" | jq -R . | jq -sc 'map(select(length > 0))')"
  client_keys_json="$(printf '%s\n' "${client_keys[@]+"${client_keys[@]}"}" | jq -R . | jq -sc 'map(select(length > 0))')"

  local payload
  payload="$(
    jq -nc --argjson options "$options_json" --argjson clientKeys "$client_keys_json" '
      {}
      + (if ($options | length) > 0 then {selectedOptionIds: $options} else {} end)
      + (if ($clientKeys | length) > 0 then {selectedClientKeys: $clientKeys} else {} end)
    '
  )"
  send POST "$(api_base)/issues/$issue_id/interactions/$interaction_id/accept" "$payload"
}

cmd_reject() {
  local issue_id="${1:-}"
  local interaction_id="${2:-}"
  shift 2 2>/dev/null || true
  [[ -n "$issue_id" && -n "$interaction_id" ]] || die 'reject: missing <issueId> <interactionId>.'
  local reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reason) reason="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "reject: unknown argument: $1" ;;
    esac
  done

  local payload
  payload="$(
    jq -nc --arg reason "$reason" '
      (if $reason == "" then {} else {reason: $reason} end)
    '
  )"
  send POST "$(api_base)/issues/$issue_id/interactions/$interaction_id/reject" "$payload"
}

cmd_respond() {
  local issue_id="${1:-}"
  local interaction_id="${2:-}"
  shift 2 2>/dev/null || true
  [[ -n "$issue_id" && -n "$interaction_id" ]] || die 'respond: missing <issueId> <interactionId>.'
  local summary=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --summary) summary="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "respond: unknown argument: $1" ;;
    esac
  done

  local answers_raw
  answers_raw="$(read_stdin)"
  [[ -n "$answers_raw" ]] || die 'respond: the ask_user_questions answers JSON is required on stdin.'

  # jq parses + validates the answers JSON; a malformed array fails here loudly
  # rather than silently at the API. Accept either a bare answers array or a
  # full { answers, summaryMarkdown } object.
  local payload
  if ! payload="$(
    jq -c --arg summary "$summary" '
      (if type == "array" then {answers: .}
       elif type == "object" and has("answers") then .
       else error("respond: stdin must be an answers array or an object with an \"answers\" key")
       end)
      + (if $summary == "" then {} else {summaryMarkdown: $summary} end)
    ' <<<"$answers_raw"
  )"; then
    die 'respond: stdin is not valid answers JSON.'
  fi
  send POST "$(api_base)/issues/$issue_id/interactions/$interaction_id/respond" "$payload"
}

cmd_decision() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'decision: missing <companyId>.'
  local title="" rule_key="" idempotency_key="" expires_at="" continuation="wake_origin_agent"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title) title="${2:-}"; shift 2 ;;
      --rule-key) rule_key="${2:-}"; shift 2 ;;
      --idempotency-key) idempotency_key="${2:-}"; shift 2 ;;
      --expires-at) expires_at="${2:-}"; shift 2 ;;
      --continuation) continuation="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "decision: unknown argument: $1" ;;
    esac
  done

  [[ -n "$title" ]] || die 'decision: --title is required.'
  [[ -n "$rule_key" ]] || die 'decision: --rule-key is required.'
  [[ ${#rule_key} -le 240 ]] || die 'decision: --rule-key must be at most 240 characters.'
  [[ "$rule_key" =~ ^[a-z][a-z0-9]*([._-][a-z0-9]+)+$ ]] \
    || die 'decision: --rule-key must be a stable lowercase dotted/dashed key such as support.refund.'
  [[ -n "$idempotency_key" ]] || die 'decision: --idempotency-key is required.'
  require_choice decision --continuation "$continuation" "none,wake_origin_agent"
  if [[ "$dry_run" != "1" && -z "${PAPERCLIP_RUN_ID:-}" ]]; then
    die 'decision: PAPERCLIP_RUN_ID is required for a live run-scoped decision; never invent one.'
  fi

  local spec
  spec="$(read_stdin)"
  [[ -n "$spec" ]] || die 'decision: a jq-built {body, options, inputs?, metadata?} object is required on stdin.'

  local payload
  if ! payload="$(
    jq -ce \
      --arg title "$title" \
      --arg ruleKey "$rule_key" \
      --arg idempotencyKey "$idempotency_key" \
      --arg expiresAt "$expires_at" \
      --arg continuationPolicy "$continuation" '
        if type != "object" then error("decision spec must be an object")
        elif ((keys - ["body", "inputs", "metadata", "options"]) | length) > 0
          then error("decision spec may contain only body, options, inputs, and metadata")
        elif (.body | type) != "string" then error("decision spec body must be a string")
        elif (.options | type) != "array" or (.options | length) < 1
          then error("decision spec options must be a non-empty array")
        else .
          + {
              title: $title,
              ruleKey: $ruleKey,
              idempotencyKey: $idempotencyKey,
              continuationPolicy: $continuationPolicy
            }
          + (if $expiresAt == "" then {} else {expiresAt: $expiresAt} end)
        end
      ' <<<"$spec"
  )"; then
    die 'decision: stdin is not a valid decision spec; build it with jq -n.'
  fi
  send POST "$(api_base)/companies/$company_id/decisions" "$payload"
}

cmd_decision_stats() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'decision-stats: missing <companyId>.'
  local since=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --since) since="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "decision-stats: unknown argument: $1" ;;
    esac
  done
  local url
  url="$(api_base)/companies/$company_id/decisions/stats"
  url="$(append_query "$url" groupBy ruleKey)"
  [[ -z "$since" ]] || url="$(append_query "$url" since "$since")"
  send_read "$url"
}

cmd_decision_get() {
  local decision_id="${1:-}"
  shift || true
  [[ -n "$decision_id" ]] || die 'decision-get: missing <decisionId>.'
  while [[ $# -gt 0 ]]; do
    case "$1" in --dry-run) dry_run=1; shift ;; *) die "decision-get: unknown argument: $1" ;; esac
  done
  send_read "$(api_base)/decisions/$(urlencode "$decision_id")"
}

cmd_search() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'search: missing <companyId>.'
  local query="" scope="" status="" priority="" assignee_agent="" project="" label=""
  local updated_within="" updated_after="" sort="" limit="" offset=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --query) query="${2:-}"; shift 2 ;;
      --scope) scope="${2:-}"; shift 2 ;;
      --status) status="${2:-}"; shift 2 ;;
      --priority) priority="${2:-}"; shift 2 ;;
      --assignee-agent) assignee_agent="${2:-}"; shift 2 ;;
      --project) project="${2:-}"; shift 2 ;;
      --label) label="${2:-}"; shift 2 ;;
      --updated-within) updated_within="${2:-}"; shift 2 ;;
      --updated-after) updated_after="${2:-}"; shift 2 ;;
      --sort) sort="${2:-}"; shift 2 ;;
      --limit) limit="${2:-}"; shift 2 ;;
      --offset) offset="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "search: unknown argument: $1" ;;
    esac
  done
  [[ -n "$query" ]] || die 'search: --query is required; use a bounded company-memory query.'
  [[ -z "$updated_within" || -z "$updated_after" ]] \
    || die 'search: --updated-within and --updated-after are mutually exclusive.'
  [[ -z "$scope" ]] || require_choice search --scope "$scope" "all,issues,comments,documents,artifacts,agents,projects"
  [[ -z "$sort" ]] || require_choice search --sort "$sort" "relevance,updated,created,priority"
  [[ -z "$limit" ]] || require_integer_between search --limit "$limit" 1 50
  [[ -z "$offset" ]] || require_integer_between search --offset "$offset" 0 200

  local url
  url="$(api_base)/companies/$company_id/search"
  url="$(append_query "$url" q "$query")"
  local pair key value
  for pair in \
    "scope=$scope" "status=$status" "priority=$priority" \
    "assigneeAgentId=$assignee_agent" "projectId=$project" "labelId=$label" \
    "updatedWithin=$updated_within" "updatedAfter=$updated_after" \
    "sort=$sort" "limit=$limit" "offset=$offset"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    [[ -z "$value" ]] || url="$(append_query "$url" "$key" "$value")"
  done
  send_read "$url"
}

cmd_extract() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'extract: missing <companyId>.'
  local contains="" kind="" scope="" status="" updated_within="" updated_after=""
  local matches_per_issue="" limit="" offset=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --contains) contains="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      --scope) scope="${2:-}"; shift 2 ;;
      --status) status="${2:-}"; shift 2 ;;
      --updated-within) updated_within="${2:-}"; shift 2 ;;
      --updated-after) updated_after="${2:-}"; shift 2 ;;
      --matches-per-issue) matches_per_issue="${2:-}"; shift 2 ;;
      --limit) limit="${2:-}"; shift 2 ;;
      --offset) offset="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "extract: unknown argument: $1" ;;
    esac
  done
  [[ ${#contains} -ge 2 ]] || die 'extract: --contains must be at least two characters.'
  [[ -z "$updated_within" || -z "$updated_after" ]] \
    || die 'extract: --updated-within and --updated-after are mutually exclusive.'
  [[ -z "$kind" ]] || require_choice extract --kind "$kind" "literal,url"
  [[ -z "$scope" ]] || require_choice extract --scope "$scope" "all,issues,comments,documents"
  [[ -z "$matches_per_issue" ]] || require_integer_between extract --matches-per-issue "$matches_per_issue" 1 200
  [[ -z "$limit" ]] || require_integer_between extract --limit "$limit" 1 200
  [[ -z "$offset" ]] || require_integer_between extract --offset "$offset" 0 5000

  local url
  url="$(api_base)/companies/$company_id/search/extract"
  url="$(append_query "$url" contains "$contains")"
  local pair key value
  for pair in \
    "kind=$kind" "scope=$scope" "status=$status" \
    "updatedWithin=$updated_within" "updatedAfter=$updated_after" \
    "matchesPerIssue=$matches_per_issue" "limit=$limit" "offset=$offset"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    [[ -z "$value" ]] || url="$(append_query "$url" "$key" "$value")"
  done
  send_read "$url"
}

cmd_queue_list() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'queue-list: missing <companyId>.'
  while [[ $# -gt 0 ]]; do
    case "$1" in --dry-run) dry_run=1; shift ;; *) die "queue-list: unknown argument: $1" ;; esac
  done
  send_read "$(api_base)/companies/$company_id/decision-queues"
}

cmd_queue_items() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'queue-items: missing <companyId>.'
  local key=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --key) key="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "queue-items: unknown argument: $1" ;;
    esac
  done
  [[ -n "$key" ]] || die 'queue-items: --key is required.'
  send_read "$(api_base)/companies/$company_id/decision-queues/$(urlencode "$key")/items"
}

cmd_queue_create() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'queue-create: missing <companyId>.'
  local key="" title="" description="" retention_days=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --key) key="${2:-}"; shift 2 ;;
      --title) title="${2:-}"; shift 2 ;;
      --description) description="${2:-}"; shift 2 ;;
      --retention-days) retention_days="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "queue-create: unknown argument: $1" ;;
    esac
  done
  [[ "$key" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || die 'queue-create: --key must be lowercase kebab-case.'
  [[ -n "$title" ]] || die 'queue-create: --title is required.'
  [[ -z "$retention_days" ]] || require_integer_between queue-create --retention-days "$retention_days" 1 3650
  local payload
  payload="$(jq -nc \
    --arg key "$key" --arg title "$title" --arg description "$description" --arg retentionDays "$retention_days" '
      {key: $key, title: $title}
      + (if $description == "" then {} else {description: $description} end)
      + (if $retentionDays == "" then {} else {retentionDays: ($retentionDays | tonumber)} end)
    ')"
  send POST "$(api_base)/companies/$company_id/decision-queues" "$payload"
}

cmd_queue_add() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'queue-add: missing <companyId>.'
  local key="" source_kind="" source_id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --key) key="${2:-}"; shift 2 ;;
      --source-kind) source_kind="${2:-}"; shift 2 ;;
      --source-id) source_id="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "queue-add: unknown argument: $1" ;;
    esac
  done
  [[ -n "$key" && -n "$source_kind" && -n "$source_id" ]] \
    || die 'queue-add: --key, --source-kind, and --source-id are required.'
  require_choice queue-add --source-kind "$source_kind" \
    "approval,decision,issue_thread_interaction,join_request,recovery_action,productivity_review,blocker_attention,review,failed_run,budget_alert,agent_error_alert"
  local payload
  payload="$(jq -nc --arg sourceKind "$source_kind" --arg sourceId "$source_id" \
    '{sourceKind: $sourceKind, sourceId: $sourceId}')"
  send POST "$(api_base)/companies/$company_id/decision-queues/$(urlencode "$key")/items" "$payload"
}

cmd_triage() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'triage: missing <companyId>.'
  local source_kind="" source_id="" decide_by="" snoozed_until=""
  local decide_by_set=0 snooze_set=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-kind) source_kind="${2:-}"; shift 2 ;;
      --source-id) source_id="${2:-}"; shift 2 ;;
      --decide-by) decide_by="${2:-}"; decide_by_set=1; shift 2 ;;
      --clear-decide-by) decide_by=""; decide_by_set=1; shift ;;
      --snoozed-until) snoozed_until="${2:-}"; snooze_set=1; shift 2 ;;
      --clear-snooze) snoozed_until=""; snooze_set=1; shift ;;
      --dry-run) dry_run=1; shift ;;
      *) die "triage: unknown argument: $1" ;;
    esac
  done
  [[ -n "$source_kind" && -n "$source_id" ]] \
    || die 'triage: --source-kind and --source-id are required.'
  (( decide_by_set == 1 || snooze_set == 1 )) \
    || die 'triage: set or clear at least one deadline/snooze field.'
  require_choice triage --source-kind "$source_kind" \
    "approval,decision,issue_thread_interaction,join_request,recovery_action,productivity_review,blocker_attention,review,failed_run,budget_alert,agent_error_alert"
  if [[ "$decide_by_set" == "1" && -n "$decide_by" ]]; then
    [[ "$decide_by" =~ ^(today|this_week|whenever|[0-9]{4}-[0-9]{2}-[0-9]{2})$ ]] \
      || die 'triage: --decide-by must be today, this_week, whenever, or YYYY-MM-DD.'
  fi
  local payload
  payload="$(jq -nc \
    --arg decideBy "$decide_by" --arg snoozedUntil "$snoozed_until" \
    --argjson decideBySet "$decide_by_set" --argjson snoozeSet "$snooze_set" '
      (if $decideBySet == 1 then {decideBy: (if $decideBy == "" then null else $decideBy end)} else {} end)
      + (if $snoozeSet == 1 then {snoozedUntil: (if $snoozedUntil == "" then null else $snoozedUntil end)} else {} end)
    ')"
  send PUT "$(api_base)/companies/$company_id/decision-triage/$(urlencode "$source_kind")/$(urlencode "$source_id")" "$payload"
}

cmd_raw() {
  local method="${1:-}"
  local path="${2:-}"
  shift 2 2>/dev/null || true
  [[ -n "$method" && -n "$path" ]] || die 'raw: missing <METHOD> </api/...path>.'
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      *) die "raw: unknown argument: $1" ;;
    esac
  done

  method="$(printf '%s' "$method" | tr '[:lower:]' '[:upper:]')"
  case "$method" in
    GET|POST|PATCH|PUT|DELETE) ;;
    *) die "raw: unsupported method: $method (use GET|POST|PATCH|PUT|DELETE)." ;;
  esac
  [[ "$path" == /api/* ]] || die 'raw: path must start with /api/ (e.g. /api/issues/{id}/documents/plan).'

  local url="${PAPERCLIP_API_URL:-}"
  url="${url%/}$path"

  local body
  body="$(read_stdin)"
  if [[ -z "$body" ]]; then
    send "$method" "$url"
    return
  fi

  # jq parses + validates (and compacts) the stdin body; a hand-mangled JSON
  # string fails here loudly instead of silently at the API.
  local payload jq_err
  if ! payload="$(printf '%s' "$body" | jq -c . 2>&1)"; then
    jq_err="$payload"
    printf 'raw: stdin is not valid JSON — build the body with jq -n, never by hand:\n%s\n' "$jq_err" >&2
    exit 1
  fi
  send "$method" "$url" "$payload"
}

dry_run=0

main() {
  require_command jq

  if [[ $# -eq 0 ]]; then
    usage >&2
    exit 1
  fi

  case "$1" in
    --help|-h|help)
      usage
      exit 0
      ;;
  esac

  local subcommand="$1"
  shift

  case "$subcommand" in
    comment)  cmd_comment "$@" ;;
    update)   cmd_update "$@" ;;
    monitor)  cmd_monitor "$@" ;;
    watchdog) cmd_watchdog "$@" ;;
    checkout) cmd_checkout "$@" ;;
    release)  cmd_release "$@" ;;
    subtask)  cmd_subtask "$@" ;;
    create)   cmd_create "$@" ;;
    confirm)  cmd_confirm "$@" ;;
    accept)   cmd_accept "$@" ;;
    reject)   cmd_reject "$@" ;;
    respond)  cmd_respond "$@" ;;
    decision) cmd_decision "$@" ;;
    decision-stats) cmd_decision_stats "$@" ;;
    decision-get) cmd_decision_get "$@" ;;
    search) cmd_search "$@" ;;
    extract) cmd_extract "$@" ;;
    queue-list) cmd_queue_list "$@" ;;
    queue-items) cmd_queue_items "$@" ;;
    queue-create) cmd_queue_create "$@" ;;
    queue-add) cmd_queue_add "$@" ;;
    triage) cmd_triage "$@" ;;
    raw)      cmd_raw "$@" ;;
    *)
      printf 'Unknown subcommand: %s\n\n' "$subcommand" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
