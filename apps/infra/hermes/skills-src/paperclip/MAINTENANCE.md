# Maintaining the `paperclip` skill

Engineer-facing. This lived inside SKILL.md as an HTML comment, which
meant every agent on the fleet paid for it on every load — the comment
is invisible in rendered markdown but not to a model reading the file.
Moved out so the skill body carries only what an agent needs.

This tree is the source of the Hermes image's bundled skills, and its only
consumer: `apps/infra/hermes/build-skills.sh` copies it, swaps a handful of
tokens (`/app/skills/` -> `/opt/hermes-skills/`, `exec` -> `terminal`,
`~/.openclaw/lightbulb-facts.json` -> `$HERMES_HOME/lightbulb-facts.json`,
`skill_workshop` -> `skills`), drops the engineer-facing MAINTENANCE.md files, and
overlays `apps/infra/hermes/skills-overlay/` for the few files whose content
genuinely differs there (no ACP coding harness). An overlaid file is the one the image
actually ships, so an edit here that names a tool, a path, or a capability
must land in the overlay too. Hermes tool names are anchored to its
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
  14. CEO daily owner brief (references/ceo-heartbeat.md "Daily owner brief
      (timer wakes)", appended after Board hygiene; SKILL.md's ceo-heartbeat
      row names it). Local 08:00 in `owner_timezone` from lightbulb-facts.json
      <- backend core/services/owner_facts.py `_lookup_owner_timezone` (the
      Clerk org creator's `user_repo.get_timezone`, "UTC" if unresolvable).
      Once-a-day is `lightbulb-post.sh --event-id brief-<date>`; the email-only-
      on-a-fresh-post rule matches the script's `Posted to room` vs
      `Already posted:` output (the receipt pre-check in
      skills-src/lightbulb/scripts/lightbulb-post.sh). Change that output and
      this section together. Re-apply on re-vendor, like the hygiene section.
