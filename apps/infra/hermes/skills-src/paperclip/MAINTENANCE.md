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

  Vendored from TheLightbulbCompany/paperclip skills/paperclip at 987520f9.
  The stamp is the LAST VENDORED commit, not the deployed fork commit: a fork
  bump does not re-vendor, so it can lag paperclip-version.json upstreamCommit
  (deployed 4dc88143 = v2026.1012.0-isol8.1, where upstream's skill differs
  in api-reference.md, artifacts.md, issue-documents.md and the
  artifact-upload script). Update it only when you re-vendor. The same stamp
  also lives in a short HTML comment at the top of SKILL.md's body, right
  after the frontmatter, since a test asserts on SKILL.md's own text. To
  re-vendor: diff that path from the stamp to the deployed commit, take
  upstream's changes, and re-apply each edit below. Behaviour changes that
  must not wait for a re-vendor are edited here directly (v2026.1012.0:
  @-mentions are context only and wake no one).

  SHAPE: this SKILL.md is deliberately THIN — the non-negotiables (auth, the
  write-path helper mandate, the assigned-create status rule, the critical
  rules, the monitor-vs-approval boundary, the board-only attention feed) plus
  a routing table into references/. Everything else lives in references/*.md
  and is read on demand. Each local edit below names where it now lives.
  Budget: keep SKILL.md under ~7.5k chars — `skill_view` puts all of it in
  history on every load, while Hermes's skill index shows only the first 57
  chars of the front-matter `description` (keep the trigger there).
  Vendored sections moved out of SKILL.md on 2026-10-08 (upstream keeps them
  inline; on re-vendor, put upstream's changes in these files, not back in
  SKILL.md): the helper write table -> references/hot-routes.md "Helper
  subcommands"; "Delegating review tasks" + the full handoff/escalation/
  send-it-back/workspace-continuity/unassigned-follow-up rules ->
  references/delegation.md; "Managing A User's Inbox" -> references/inbox.md;
  "MCP Tool Approval Gates" -> references/interactions.md; "Standalone
  Decisions" -> references/attention-decisions-search.md; "Monitors and
  Watchers" -> references/monitors.md; "Proposing Credentials Safely" +
  "Reading Granted Secrets" -> references/secrets.md; the comment-wake payload
  paragraph -> references/heartbeat.md Step 6; manual local CLI mode ->
  references/workflows.md.

  1. Write-path helper mandate ("Writing to the Paperclip API" section in
     SKILL.md; the full table is references/hot-routes.md "Helper
     subcommands"): every API write goes through /app/skills/paperclip/scripts/paperclip-api.sh
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
  6. Create status (SKILL.md "Creating work for someone else" + the
     create/subtask rows in references/hot-routes.md): paperclip-api.sh sends
     an explicit `todo` when a create passes --assignee with no --status — the
     stock default (resolveCreateIssueStatusDefault), sent explicitly in one
     POST. Owner ruling
     2026-10-18: asked-for work starts; `backlog` is for parking on purpose.
     It replaced a `backlog` default that left tasks filed from chat sitting
     unstarted. Keep the helper default and the doc wording on re-vendor.
  6a. "Telling the person about task work" (SKILL.md; heartbeat.md webchat
     bullet; the CEO checklist's "Room posts" paragraph, mirrored in
     backend core/services/heartbeat_checklist.py): owner ruling 2026-10-18 —
     no progress posts in rooms; a task is mentioned when done, when it needs
     the person, or when asked. A task filed from a room reports back from its
     OWN run, one message to the room in its description's From section
     (lightbulb-post.sh --room; item 24); self-started work posts one result
     message. Lightbulb-only.
  7. Webchat capability section (summarised in SKILL.md; full rules in
     references/heartbeat.md): upstream
     documents only the heartbeat worldview; Lightbulb owners drive agents from a
     live chat surface daily. Added after a first-run Friday, asked in webchat
     to adjust tasks it had just created, tried to checkout, got "Agent run id
     required", and relayed that to the owner — instead of just PATCHing the
     fields. The "work you take on in a room stays in that room's
     conversation" rule in it is edit 24's; keep the two together.
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
  16. Chat deliverables (references/artifacts.md). A deliverable asked for in
      chat goes back as a file attached to the reply; an issue document only
      when the work already is a task (founder ruling 2026-10-28: no task just
      to hold a chat deliverable). Points at the lightbulb skill's
      references/chat-vs-document.md, which owns the rule; for task-backed work
      the raw PUT /api/issues/:id/documents/:key and the `#document-<key>` link
      (rendered as a document card in web/iOS chat) still apply. Do not restore
      upstream's create-a-task-for-the-deliverable flow. Re-apply on re-vendor.
  17. Handoffs through issues (SKILL.md Critical Rules "Handoffs go through
      issues, not chat" + its full form in references/delegation.md): the
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
      references/monitors.md "Surviving a Dropped Run" + the watchdog row in
      references/hot-routes.md "Helper subcommands"): upstream documents no agent-facing way to arm a watchdog, and the
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
      `acceptedPlanStartsExecution`), so the reference files the card with
      `confirm --plan` (entry 21). People pick the mode in the web create
      dialog, the issue detail's Work mode row and the phone create sheet.
      Re-apply on re-vendor, and re-read the directive text at the new tag.
  21. `confirm --plan` (the `confirm` helper subcommand +
      references/{work-modes,comment-style,artifacts}.md): a plain `confirm`
      card carries no `payload.target`, so accepting it never set
      `acceptedPlanStartsExecution` and a planning-mode issue stayed in
      planning after its plan was approved. `--plan` GETs
      `/api/issues/{id}/documents/plan` and sets `payload.target` to
      `{type: "issue_document", key: "plan", revisionId: <latestRevisionId>,
      issueId, documentId, revisionNumber}` — `revisionId` is required by
      `requestConfirmationIssueDocumentTargetSchema`
      (packages/shared/src/validators/issue.ts) and upstream 422s one that is
      not the document's latest, which is why the helper reads it instead of
      taking it as an argument. Every plan-approval instruction in the
      references must say `confirm --plan`. Re-apply on re-vendor.
  22. Cases routing row (SKILL.md routing table, `references/cases.md` row):
      the row no longer says "Disabled on this instance". `enableCases` is
      applied by the backend at startup
      (apps/backend `core/services/paperclip_instance_flags.py`, `DESIRED_FLAGS`),
      and the owner-facing surface is the Teams Cases panels (frontend components/teams/cases/,
      BFF routers/teams/cases.py). references/cases.md still tells an agent to
      stop and report on a 403, which covers an environment where the flag is
      off. If Cases is ever switched off again, restore the warning here.
  23. External objects (references/external-objects.md + its routing-table row
      in SKILL.md + the reworded first sentence of SKILL.md "State external
      facts only from a fresh check"). GitHub PR / issue URLs written on an
      issue are tracked by Paperclip when the instance flag
      `experimental.enableExternalObjects` is on, and Teams shows their state
      (apps/frontend .../teams/issues/external-objects/). The flag is applied
      by the backend at startup (apps/backend
      `core/services/paperclip_instance_flags.py`), after it has synced every
      workspace's GitHub token into Paperclip; a start whose sync did not
      finish clean leaves the flag as it was. The reference
      documents exactly two agent routes, both verified in the fork's
      server/src/routes/issues.ts: `GET /api/issues/:id/external-objects`
      (issue read access) and `POST /api/issues/:id/external-objects/refresh`
      (`issue:mutate`; it does NOT force — an object inside its ~5 minute
      window answers `reason: "backoff"`). There is NO create route: a test
      (apps/infra/test/paperclip-skill-primitives.test.ts) fails if the
      reference grows one. The snapshot never replaces the fresh-check rule.
      Re-apply on re-vendor.
  24. Task work runs in the task's own session (SKILL.md webchat summary +
      references/heartbeat.md's "Answer and do quick work in the room turn
      yourself; a task is only for ..." bullet + lightbulb references/chat-vs-document.md "How" + the
      helper's usage text). Owner ruling 2026-10-18 ("the task is the durable
      record ... the work shouldn't really be done in the room"), reversing
      2026-10-04's "one conversation per piece of work". That design (after
      ISOA-680's duplicate PR #3712) had the helper record issue -> room
      conversation in `$HERMES_HOME/lightbulb-issue-bindings/<issueId>.json`
      and the bridge route Paperclip's wakes into the room conversation; a
      room turn records no disposition, so Paperclip's liveness repair re-ran
      the task twice and blocked it (ISOA-860). Now: a room turn files the
      task (assigned, `todo`, one POST) with full context and stops; every
      Paperclip run is stock, in the issue's own session
      (bridge/turn.py `_run_paperclip_session`); the helper's `create`/
      `subtask` append a `## From` section from a room session (room id from
      LIGHTBULB_ROOM_SESSION_KEY / HERMES_SESSION_KEY, message link from
      LIGHTBULB_ROOM_MESSAGE_ID, both exported by bridge `_room_turn_env`;
      ideas.py work_description's From style), and the task's run posts done /
      needs-you there. Old binding files on disk are inert.
      Re-apply on re-vendor.
  25. A person's decision is an approval (SKILL.md Critical Rules "A person's
      decision is an approval" + the interactions.md routing row; detail in
      references/interactions.md "When a person must decide", which replaced
      upstream's "Requesting Board Approval" section; references/api-reference.md
      "CEO strategy approval" points at it; references/heartbeat.md's
      blocked-on-a-human bullet carries a pointer). Observed dev 2026-10-14
      (Walker Studio): asked twice for "an approval in Needs you", the CEO filed
      a task assigned to the person instead, and other agents asked in chat; the
      approvals list stayed empty, so the setup list's "Answer your first
      approval" step (apps/frontend OnboardingBand.tsx, any answered approval)
      could not be done. The raw `POST /api/companies/:id/approvals` + jq
      commands that first replaced it were not used either (dev 2026-10-14,
      image h-2026.10.14-f8eebfc: "Reply 'Plan approved' here", no approval),
      so the section now names ONE tool, `request_approval`
      (apps/infra/hermes/plugins/lightbulb/__init__.py _request_approval; also
      SKILL.md's rule, hot-routes.md's first "Anything else" row,
      api-reference.md "CEO strategy approval", heartbeat.md's blocked-on-a-
      human bullet). It calls fork server/src/routes/approvals.ts with the
      agent's own key (agents may create from a room turn; `issueIds` are
      UUIDs per createApprovalSchema, so it resolves WAL-6 through GET
      /api/issues/:id, whose router.param accepts identifiers), then PATCHes
      each linked issue to in_review (the linked approval is the review path).
      It binds nothing (item 24): the approval's wake runs in the issue's own
      session, and the in_review PATCH carries the run's id only when called
      from a Paperclip run. `approve_ceo_strategy` is
      the ROOT CEO's first-plan decision (a first message dispatched to a
      specialist gets `request_board_approval` instead: onboarding_brief.py
      is_root_ceo picks the brief's decision by role, never by name): no server side effect on approve beyond the
      requester wake, and the BFF renders title/summary/plan
      (core/services/activity.py, ApprovalPayload.tsx CeoStrategyPayload). The
      old api-reference line "must be approved before you can move tasks to
      in_progress" was dropped: the fork enforces no such gate. The first-run
      contract mirrors backend core/services/onboarding_brief.py; edit both
      together. Re-apply on re-vendor.
  26. What the Library shows (references/artifacts.md first paragraph + the
      artifacts.md routing row): the Library (web panel teams-artifacts,
      ArtifactsPanel.tsx -> BFF /teams/artifacts -> fork
      server/src/services/company-artifacts.ts) lists agent-written issue
      documents (minus SYSTEM_ISSUE_DOCUMENT_KEYS), work products and
      attachments only. Observed dev 2026-10-14: Growth said "the full doc is
      WAL-4's issue thread" and the Library said "Nothing made yet".
      Re-apply on re-vendor.
  27. Comment style is for issues only (references/comment-style.md, the
      paragraph under the heading): a chat message (room reply, DM,
      lightbulb-post.sh) is never shaped like an issue comment. It pairs with
      bridge/turn.py CHAT_STYLE. Observed prod 2026-10-15: Pulse posted
      status-line, heading and table reports to GTM Strategy. Re-apply on
      re-vendor.
  28. Blocked or unsure goes to ask_person; counts come from the board
      (SKILL.md Critical Rules, the two lines after "A person's decision is an
      approval"; references/interactions.md "When you need an answer, not a
      sign-off: ask_person"; references/heartbeat.md "If the block needs a human
      decision", which replaced "Say so in a comment ... the comment is what gets
      a human's attention"). The tools are
      apps/infra/hermes/plugins/lightbulb/__init__.py _ask_person (a Paperclip
      ask_user_questions interaction, wake_assignee, optionally parking the
      issue blocked self-owned). The count rule points at hot-routes.md's
      list route; a lightbulb_board wrapper tool (#4495) was removed 30 Oct:
      it called the same route with the same key. Observed
      2026-10-07..28 (docs/research/2026-10-30-production-agents/G1): blocked
      agents' comments reached nobody (ISOA-867 8 days, ISOA-752), and an agent
      stated "only 3 open cards" with 55 open. Test:
      tests/test_ask_and_count_skill.py. Re-apply on re-vendor.
