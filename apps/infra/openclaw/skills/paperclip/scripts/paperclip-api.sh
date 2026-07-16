#!/usr/bin/env bash

# Paperclip write-path CLI.
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
Paperclip write-path helper. Builds every request body safely with jq.

Usage:
  scripts/paperclip-api.sh <subcommand> [args] [--dry-run]

Global options:
  --dry-run     Build and print the JSON body (no HTTP request).
  --help, -h    Show this help.

Environment (required for live requests; not needed for --dry-run):
  PAPERCLIP_API_URL, PAPERCLIP_API_KEY
  PAPERCLIP_AGENT_ID is additionally required for `checkout`.
  PAPERCLIP_RUN_ID is optional: it exists only on heartbeat-wake runs, and the
  X-Paperclip-Run-Id header is sent only when it is set. Webchat/interactive
  sessions have no run id — the header is omitted; never invent one.

Subcommands (markdown / freeform text is read from stdin where noted):

  comment <issueId>
      POST /api/issues/{id}/comments   body: { body }
      Reads the markdown comment body from stdin (required).

  update <issueId> [--status STATUS] [--blocked-by "id1,id2"]
      PATCH /api/issues/{id}           body: { status?, comment?, blockedByIssueIds? }
      Reads an optional markdown comment from stdin. At least one of
      --status / --blocked-by / a stdin comment is required.
      STATUS: backlog|todo|in_progress|in_review|done|blocked|cancelled
      --blocked-by replaces the blocker set; pass --blocked-by "" to clear it.

  checkout <issueId> [--agent-id ID] [--expect "s1,s2,..."]
      POST /api/issues/{id}/checkout   body: { agentId, expectedStatuses }
      agentId defaults to $PAPERCLIP_AGENT_ID.
      expectedStatuses defaults to todo,backlog,blocked,in_review.

  release <issueId>
      POST /api/issues/{id}/release    (no body)

  subtask <companyId> --title T --parent <id> [--goal <id>] [--status S]
          [--priority P] [--assignee <agentId>] [--blocked-by "id1,id2"]
          [--inherit-workspace <issueId>] [--billing-code CODE]
      POST /api/companies/{companyId}/issues
      body: { title, parentId, goalId?, description?, status?, priority?,
              assigneeAgentId?, blockedByIssueIds?,
              inheritExecutionWorkspaceFromIssueId?, billingCode? }
      Reads an optional markdown description from stdin.

  create <companyId> --title T [--goal <id>] [--status S]
         [--priority P] [--assignee <agentId>] [--blocked-by "id1,id2"]
         [--inherit-workspace <issueId>] [--billing-code CODE]
      POST /api/companies/{companyId}/issues   (top-level issue; no parentId)
      body: { title, goalId?, description?, status?, priority?,
              assigneeAgentId?, blockedByIssueIds?,
              inheritExecutionWorkspaceFromIssueId?, billingCode? }
      Reads an optional markdown description from stdin.

  confirm <issueId> --prompt P [--title T] [--idempotency-key K]
          [--continuation none|wake_assignee|wake_assignee_on_accept]
          [--accept-label L] [--reject-label L] [--reject-requires-reason]
      POST /api/issues/{id}/interactions   kind: request_confirmation
      Builds payload.version=1 + payload.prompt for you (cannot be omitted).
      Reads optional details markdown from stdin (payload.detailsMarkdown).
      --continuation defaults to wake_assignee.

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

  raw <METHOD> </api/...path>
      Escape hatch for endpoints without a dedicated subcommand (board
      approvals, checkbox/questions/suggest-tasks interaction creates, issue
      documents, ...). METHOD: GET|POST|PATCH|PUT|DELETE. The path must start
      with /api/. Reads an optional complete JSON body from stdin — build it
      with jq -n (never hand-type inline JSON). The body is validated with jq
      before sending and rejected loudly if it does not parse.

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

  # X-Paperclip-Run-Id attributes the write to the current heartbeat run. A
  # run id exists only on heartbeat-wake runs; webchat/interactive sessions
  # have none, so the header is omitted entirely (never fabricate one).
  local run_id_header=()
  if [[ -n "${PAPERCLIP_RUN_ID:-}" ]]; then
    run_id_header=(-H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID")
  fi

  local response_file status_code
  response_file="$(mktemp)"
  if [[ "$has_payload" == "1" ]]; then
    status_code="$(
      curl -sS -X "$method" -w '%{http_code}' -o "$response_file" \
        "$url" \
        -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
        "${run_id_header[@]+"${run_id_header[@]}"}" \
        -H 'Content-Type: application/json' \
        --data-binary "$payload"
    )"
  else
    status_code="$(
      curl -sS -X "$method" -w '%{http_code}' -o "$response_file" \
        "$url" \
        -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
        "${run_id_header[@]+"${run_id_header[@]}"}"
    )"
  fi

  if [[ "$status_code" -lt 200 || "$status_code" -ge 300 ]]; then
    printf 'Paperclip API request failed (HTTP %s): %s %s\n' "$status_code" "$method" "$url" >&2
    cat "$response_file" >&2
    printf '\n' >&2
    rm -f "$response_file"
    exit 1
  fi

  cat "$response_file"
  printf '\n'
  rm -f "$response_file"
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
  local status=""
  local blocked_by="" blocked_set=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status) status="${2:-}"; shift 2 ;;
      --blocked-by) blocked_by="${2:-}"; blocked_set=1; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      *) die "update: unknown argument: $1" ;;
    esac
  done

  local comment
  comment="$(read_stdin)"
  [[ -n "$status" || -n "$comment" || "$blocked_set" == "1" ]] \
    || die 'update: pass --status, --blocked-by, and/or a stdin comment.'

  local payload
  payload="$(
    jq -nc \
      --arg status "$status" \
      --arg comment "$comment" \
      --arg blockedBy "$blocked_by" \
      --argjson blockedSet "$blocked_set" '
      (if $status == "" then {} else {status: $status} end)
      + (if $comment == "" then {} else {comment: $comment} end)
      + (if $blockedSet == 1
         then {blockedByIssueIds: ($blockedBy | split(",") | map(select(length > 0)))}
         else {}
         end)
    '
  )"
  send PATCH "$(api_base)/issues/$issue_id" "$payload"
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
  local inherit_workspace="" billing_code=""
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
      --dry-run) dry_run=1; shift ;;
      *) die "subtask: unknown argument: $1" ;;
    esac
  done

  [[ -n "$title" ]] || die 'subtask: --title is required.'
  [[ -n "$parent" ]] || printf 'subtask: warning — no --parent set; subtasks should set parentId.\n' >&2
  [[ -n "$goal" ]] || printf 'subtask: warning — no --goal set; subtasks should set goalId.\n' >&2

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
      --arg billingCode "$billing_code" '
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
    '
  )"
  send POST "$(api_base)/companies/$company_id/issues" "$payload"
}

cmd_create() {
  local company_id="${1:-}"
  shift || true
  [[ -n "$company_id" ]] || die 'create: missing <companyId>.'
  local title="" goal="" status="" priority="" assignee="" blocked_by=""
  local inherit_workspace="" billing_code=""
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
      --dry-run) dry_run=1; shift ;;
      *) die "create: unknown argument: $1" ;;
    esac
  done

  [[ -n "$title" ]] || die 'create: --title is required.'

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
      --arg billingCode "$billing_code" '
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
    '
  )"
  send POST "$(api_base)/companies/$company_id/issues" "$payload"
}

cmd_confirm() {
  local issue_id="${1:-}"
  shift || true
  [[ -n "$issue_id" ]] || die 'confirm: missing <issueId>.'
  local prompt="" title="" idempotency_key="" continuation="wake_assignee"
  local accept_label="" reject_label="" reject_requires_reason=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prompt) prompt="${2:-}"; shift 2 ;;
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

  local payload
  payload="$(
    jq -nc \
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
        continuationPolicy: $continuationPolicy,
        payload: (
          {version: 1, prompt: $prompt}
          + (if $details == "" then {} else {detailsMarkdown: $details} end)
          + (if $acceptLabel == "" then {} else {acceptLabel: $acceptLabel} end)
          + (if $rejectLabel == "" then {} else {rejectLabel: $rejectLabel} end)
          + (if $rejectRequiresReason then {rejectRequiresReason: true} else {} end)
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
    checkout) cmd_checkout "$@" ;;
    release)  cmd_release "$@" ;;
    subtask)  cmd_subtask "$@" ;;
    create)   cmd_create "$@" ;;
    confirm)  cmd_confirm "$@" ;;
    accept)   cmd_accept "$@" ;;
    reject)   cmd_reject "$@" ;;
    respond)  cmd_respond "$@" ;;
    raw)      cmd_raw "$@" ;;
    *)
      printf 'Unknown subcommand: %s\n\n' "$subcommand" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
