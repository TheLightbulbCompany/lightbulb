# Maintaining the `paperclip` skill

Engineer-facing. This lived inside SKILL.md as an HTML comment, which
meant every agent on the fleet paid for it on every load — the comment
is invisible in rendered markdown but not to a model reading the file.
Moved out so the skill body carries only what an agent needs.

This tree is the source of the Hermes image's bundled skills, and its only
consumer: `apps/infra/hermes/build-skills.sh` copies it, swaps a handful of
tokens (`/app/skills/` -> `/opt/hermes-skills/`, `exec` -> `terminal`,
`~/.openclaw/lightbulb-facts.json` -> `$HERMES_HOME/lightbulb-facts.json`,
`skill_workshop` -> `skills`), and drops the engineer-facing MAINTENANCE.md files. There is no
overlay: this tree is the only copy, so write Hermes wording here directly when a
token swap cannot express it (a per-file overlay drifted silently and shipped
stale text until it was deleted). Hermes tool names are anchored to its
`CONFIGURABLE_TOOLSETS`
(`terminal`, `file`, `web`, `memory`, `skills`, `browser`, `secrets`) plus our
own plugin tools in `apps/infra/hermes/plugins/lightbulb/__init__.py`
(`send_room_message`, `hire_suggestion`, `lightbulb_local_sessions`).

ISOL8-LOCAL EDITS (deliberate; re-apply on re-vendor).
  This skill is vendored from the Paperclip skills-catalog, but carries
  Lightbulb-local changes that upstream does not have. If you re-vendor, re-apply
  each of these rather than letting them silently revert.

  Vendored from TheLightbulbCompany/paperclip skills/paperclip at 987520f9
  (the same commit as paperclip-version.json upstreamCommit — keep them equal;
  the same stamp also lives in a short HTML comment at the top of SKILL.md's
  body, right after the frontmatter, since a test asserts on SKILL.md's own
  text). To re-vendor: diff that path at the new commit against this file,
  take upstream's changes, and re-apply each edit below.

  SHAPE: this SKILL.md is deliberately THIN — the non-negotiables (auth, the
  write-path helper mandate, the assigned-create status rule, the critical
  rules, the monitor-vs-approval boundary, the board-only attention feed) plus
  a routing table into references/. Everything else lives in references/*.md
  and is read on demand. Each local edit below names where it now lives.

  1. Write-path helper mandate ("Writing to the Paperclip API" section + the
     write tables): every API write goes through /app/skills/paperclip/scripts/paperclip-api.sh
     (jq-built bodies); the raw-jq / inline-JSON escape hatches were removed,
     because hand-written bodies once produced a malformed payload like
     {body:} that silently failed the write and stalled the run.
  2. Run-id truthfulness ("Authentication" + "Run audit trail"):
     PAPERCLIP_RUN_ID exists ONLY on heartbeat-wake runs; webchat/interactive
     sessions have no run id, and the X-Paperclip-Run-Id header must be
     OMITTED there — never invented. Fabricated run ids fail requests.
  3. Board hygiene ("Writing to the Paperclip API"): never create probe/test
     issues on the owner's live board to debug API behavior; use --dry-run.
  4. Scripts: /app/skills/paperclip/scripts/paperclip-api.sh and /app/skills/paperclip/scripts/paperclip-issue-update.sh
     treat PAPERCLIP_RUN_ID as optional (header sent only when set), and
     paperclip-api.sh carries a Lightbulb-added top-level `create` subcommand,
     a `raw <METHOD> </api/...path>` escape hatch (jq-validated stdin
     body) so no write ever needs hand-rolled curl, and `update` field
     flags (--title/--priority/--assignee) so the webchat section's
     assign-to-start command actually works through the helper.
  5. ABSOLUTE helper paths: every scripts/ invocation is written as
     /app/skills/paperclip/scripts/... (the image bakes this skill at
     /app/skills/paperclip — Dockerfile Layer). The agent's working directory
     is its workspace, NOT the skill root, so relative `scripts/...` commands
     fail with "not found" and the first-run kickoff silently loses its board
     writes. Keep paths absolute when re-vendoring.
  6. EXPLICIT create status ("Creating work for someone else" + the create/
     subtask rows): paperclip-api.sh sends an explicit `backlog` when a create
     passes --assignee with no --status, instead of letting the server pick.
     Paperclip's own default promotes that case to `todo`
     (resolveCreateIssueStatusDefault), and assignee + non-backlog is exactly
     what dispatches an agent run — then a 30s reconciler re-dispatches it
     until it escalates to `blocked`. Upstream's docs are silent on this, so
     an agent filing a few assigned tasks in one heartbeat starts a run storm.
     Keep both the helper default and the doc wording on re-vendor.
  7. Webchat capability section (summarised in SKILL.md; full rules in
     references/heartbeat.md): upstream
     documents only the heartbeat worldview; Lightbulb owners drive agents from a
     live chat surface daily. Added after a first-run Friday, asked in webchat
     to adjust tasks it had just created, tried to checkout, got "Agent run id
     required", and relayed that to the owner — instead of just PATCHing the
     fields, or assigning + `todo` to start a real run.
  8. Company-memory and governance primitives: the deployed fork includes
     company search/extraction, run-scoped decisions with server-applied
     effects, own-agent ruleKey stats, and decision queues/triage. Dedicated
     helper commands encode their payloads and query strings. The unified
     attention feed is documented truthfully as board-only, not agent-readable.
  9. Issue monitors (references/monitors.md + the `monitor` helper
     subcommand + the api-reference monitor shapes; the monitor-vs-approval
     boundary is restated inline in SKILL.md): upstream ships the
     execution monitor but never documents it to agents, so the whole fleet
     defaulted to a `request_confirmation` card for waits a monitor handles
     natively — one such card sat 38 hours and spawned duplicate blocker
     issues. The section also pins the monitor-vs-approval boundary (a
     monitor is the WAIT, an approval is the AUTHORITY) so nothing reads a
     scheduled wait as permission to act — keep that boundary in SKILL.md
     itself, not only in the reference. The helper subcommand exists
     because `executionPolicy` is REPLACE-semantics upstream: a hand-built
     `raw PATCH` of just `{monitor}` silently wipes the issue's
     reviewer/approver stages. Keep all three on re-vendor.
  10. Rename-safe delegation targets (SKILL.md hiring rule +
      references/{api-reference,company-skills}.md): use an explicit assignee
      when supplied, otherwise resolve the root CEO by `role: ceo` and an empty
      `reportsTo`; never fall back to a literal agent name. Fresh Lightbulb
      workspaces no longer guarantee named Friday/Atlas identities.
  11. Monitors-for-promises sentence (references/monitors.md, first paragraph after
      the H1): ties a chat promise that waits on an external thing to a monitor.
      Lightbulb-local; upstream's monitors.md has no chat concept. Re-apply on
      re-vendor.
  12. Human-only board cards (references/interactions.md "Who may resolve it"
      bullet + the checkbox example; references/api-reference.md confirmation
      and checkbox examples; paperclip-api.sh `confirm`): every board/user
      confirmation carries `"resolverPolicy": "board_only"`. Paperclip
      v2026.916 (upstream 10d0555189) defaults resolverPolicy to "anyone", so
      without it another agent may accept a card meant for the owner. It is
      `board_only`, not the canonical `human_only`, on purpose: 916 maps the
      alias to human_only (packages/shared/src/constants.ts:316) and 813
      accepts only board_only/board_or_agents, so the skill works whichever of
      the Hermes image and the Paperclip pin rolls first. Switch to human_only
      once every env runs 916. Re-apply
      on re-vendor; paperclip-skill-primitives.test.ts pins the helper and the
      api-reference examples.
  13. CEO timer-wake board keeping (SKILL.md routing table's `ceo-heartbeat.md`
      row + references/ceo-heartbeat.md in full): the stock Paperclip fork
      ships a separate CEO-only `HEARTBEAT.md` (a different file from the
      generic heartbeat this skill already carries as references/heartbeat.md)
      that upstream hands only to instruction-bundle adapters
      (`supportsInstructionsBundle: true`) — `hermes_gateway` sets that false,
      so Paperclip never delivers it to our agents on its own.
      references/ceo-heartbeat.md vendors that file verbatim (own header names
      the exact upstream path/tag and the two inline edits: `$AGENT_HOME` ->
      `$HERMES_HOME`, and the "hire via `paperclip-create-agent`" line ->
      this skill's own Hiring rule, since we neither bundle that skill nor
      allow direct `/agents`/`/agent-hires` calls), then appends a
      Lightbulb-local "Board hygiene (timer wakes)" section (owner ruling
      2026-09-24: on a timer wake the CEO may close/cancel/nudge/flag stale
      board items, including cancelling human-created work, always with a
      one-line reason comment; bounded to 10 mutations/wake). The routing
      row's detection signal is the literal `- reason: heartbeat_timer` line
      Paperclip's `renderPaperclipWakePrompt` puts in the wake summary
      (`packages/adapter-utils/src/server-utils.ts:2346` on the fork) — NOT an
      env var; our bridge's native-run path (`apps/infra/hermes/bridge/turn.py`
      `_run_native_paperclip`) never sets `PAPERCLIP_WAKE_REASON`, only
      `PAPERCLIP_TASK_ID` when the wake is issue-scoped. Re-apply the header's
      two edits and the hygiene section on any re-vendor of the CEO file; do
      not let a diff silently drop them.
      Since the per-agent checklist (below), references/ceo-heartbeat.md is the
      FALLBACK for an empty checklist AND the CEO's seed: backend
      core/services/heartbeat_checklist.py `CEO_CHECKLIST` is this file below
      its HTML comment after build-skills.sh's `/app/skills/` swap, pinned by
      tests/unit/services/test_heartbeat_checklist.py. Edit both together.
      Existing workspaces keep the copy they were seeded with.
  13a. Per-agent heartbeat checklist (SKILL.md routing row
      `$HERMES_HOME/HEARTBEAT.md`): each agent's own `HEARTBEAT.md` at its
      Hermes profile root (`<owner>/hermes/<agent>/HEARTBEAT.md`), seeded
      only-if-absent by hermes_profiles.upload_profile (IfNoneMatch; CEO ->
      `CEO_CHECKLIST`, everyone else -> `SPECIALIST_CHECKLIST`), edited on the
      agent page's Schedule tab through routers/workspace_files.py (org admins
      only), and re-pulled by a warm microVM on the `.revision` bump (bridge
      sync.py M6-R51 recency walk). The timer interval/on-off shown beside it
      is the Paperclip agent's `runtimeConfig.heartbeat`; the CEO's on/off
      toggle is `POST /teams/agents/{id}/heartbeat` ->
      `nightly_ceo_opted_out` -> user_provision.arm_nightly_ceo.
  13b. Checkout-first board-hygiene run attribution
      (references/ceo-heartbeat.md's "Board hygiene (timer wakes)" section,
      2026-09-27): the deployed fork's cross-issue influence limiter (edit 18
      below) 403s every hygiene mutation on a timer wake, because that wake
      arrives with no source issue to check out
      (`cross_issue_influence_run_context_required`, refused since 2026-09-24).
      Owner ruling: keep the CEO's hygiene power and work with Paperclip's
      rule rather than patch the fork. Fix: the checklist's new "Checkout
      first" step finds-or-creates the CEO's own standing "Board hygiene"
      issue (assigned to the CEO; filing it needs no checkout) and checks it
      out before any mutation, so the mutations below become cross-issue
      writes on a held run (capped at Paperclip's 20/run, well above this
      checklist's own 10/wake budget, which stays the binding number); a
      failed checkout (a live run already holds the issue) skips every
      mutation that wake with no fallback write. The new "Close the loop"
      step comments a one-line summary on "Board hygiene" and leaves it
      `in_progress` -- never `done` -- for the next wake to reuse.
      `references/heartbeat.md`'s cross-issue section carries one pointer
      sentence at this pattern. CEO_CHECKLIST in
      core/services/heartbeat_checklist.py must mirror both new paragraphs
      (edit 13's pin still applies: keep the two files in lockstep). Re-apply
      on re-vendor, same as edit 13's header edits and hygiene section.
  14. (Removed 2026-09-24, owner ruling: the heartbeat only keeps the board
      moving.) The CEO daily owner brief that #3182 appended to
      references/ceo-heartbeat.md is gone from the checklist and its seed; the
      brief will be redesigned separately. lightbulb-post.sh, the agent-posts
      route and `owner_timezone` in lightbulb-facts.json stay.
  15. Routine output routing (references/routines.md "Where a routine's output
      goes" + "Starter routines"). The `Post results to:` line is the contract
      between routine descriptions and agents: `#name` -> `lightbulb-post.sh
      --room '#name'` (backend resolves `roomName` among the agent's own
      channels, `messaging.resolve_agent_post_room`), `@handle` -> `--handle`,
      `none`/absent -> no post. The starter list and its descriptions live in
      backend core/services/paperclip_provisioning.py `STARTER_ROUTINES`
      (seeded paused by `provision_org` via `agent_routine_provisioning
      .create_agent_routines(paused=True)`); rename a routine there and here
      together. Re-apply on re-vendor.
  16. Chat deliverables as documents (references/artifacts.md "A deliverable
      asked for in chat is a document too"). Points at the lightbulb skill's
      references/chat-vs-document.md, which owns the rule, the webchat-safe
      calls (create --status backlog, raw PUT /api/issues/:id/documents/:key,
      disposition) and the chat wording; the `#document-<key>` link it writes
      is what the web/iOS chat renders as a document card. Re-apply on re-vendor.
  17. Handoffs through issues (SKILL.md "Handoffs go through issues, not chat",
      under Delegating review tasks, right after the courier pattern): the
      room caps agent chains at two agent replies per human message
      (backend core/services/messaging.py AGENT_REPLY_CAP), so a chat handoff
      past it is never answered. Mirrors the lightbulb skill's "Teammates in a
      room" section; edit both together. Re-apply on re-vendor.
  18. Cross-issue influence limiter (SKILL.md webchat section +
      references/heartbeat.md's webchat bullet list + its "Cross-issue writes
      inside a heartbeat run" subsection): a same-company sibling of edit 7.
      REWRITE this entry on re-vendor, don't append a new one — it and its
      predecessor describe the same server boundary and stale wording next to
      current wording is how the incident below happened in the first place.
      Superseded 2026-09-27 by the fork's cross-issue influence limiter
      (paperclip#26, released v2026.916.1-isol8.3): from a room turn (no
      heartbeat run), a write to an issue assigned to you succeeds unless a
      live heartbeat run holds it checked out (`401`); a write to someone
      else's issue is refused `403 cross_issue_influence_run_context_required`
      (filing a new issue is still always allowed). Inside a heartbeat run the
      rule is NOT identical to webchat, unlike the prior wording here: checkout
      the target issue first, then writes to it pass AND writes to any OTHER
      issue are allowed too, capped at 20 cross-issue writes per run; any
      cross-issue write attempted before the run's first checkout is refused
      outright regardless of the cap, and a timer wake with no source issue
      (the CEO's `heartbeat_timer`) must checkout something before its first
      write. (Prior text, superseded: a 2026-09-26 prod incident found two
      agents in one room turn 403'd trying to PATCH a teammate's issue and
      narrated it as "this session/run lacks attribution" — the ownership
      framing was right, but "identically inside a heartbeat run" is what this
      release changes.) Keep the doc's framing that this is Paperclip's own
      boundary, not a Lightbulb/run-attribution limitation.
  19. Task watchdog helper (the `watchdog` helper subcommand +
      references/monitors.md "Surviving a Dropped Run" + SKILL.md write-table
      row): upstream documents no agent-facing way to arm a watchdog, and the
      earlier `raw PUT` snippet was copied as a POST once (404; only
      get/put/delete are registered). The helper pins the verb and defaults
      `agentId` to `$PAPERCLIP_AGENT_ID`. Humans arm the same thing from the
      Watchdog row on the web issue detail (Teams BFF
      `PUT`/`DELETE /teams/issues/{id}/watchdog`). Re-apply on re-vendor.
  20. Work modes (references/work-modes.md + SKILL.md routing-table row):
      upstream injects the Ask / Planning / Accepted-plan directives into the
      wake prompt (server/src/services/heartbeat.ts) but its skill never
      explains them. The reference restates those directives and the plan
      loop. Load-bearing detail: acceptance flips `workMode` back to
      `standard` only when the `request_confirmation` targets issue document
      key `plan` (server/src/services/issue-thread-interactions.ts,
      `acceptedPlanStartsExecution`); the `confirm` helper sets no target, so
      the reference sends agents to `raw POST`. People pick the mode in the
      web create dialog, the issue detail's Work mode row and the phone create
      sheet. Re-apply on re-vendor, and re-read the directive text at the new
      tag.
