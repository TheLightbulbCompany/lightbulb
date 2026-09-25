# Maintaining the `lightbulb` skill

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

MAINTENANCE (Lightbulb engineers): every claim in this skill is anchored to
  code. This skill is the fleet's knowledge of the platform. It is SPLIT: a
  thin SKILL.md (routing table + non-negotiables) plus references/ read on
  demand. Each anchor below names the file that now carries the claim.
  - Capability inventory .... references/platform-primitives.md
                              <- routers/teams/issues.py (add_issue_comment wake) + core/services/paperclip_adapter_config.py;
                                 routers/ses_inbound.py (email wake — any authenticated sender while the row's
                                 `send_to_anyone` is on, the default; else allowlisted senders only) and
                                 core/services/agent_mail_send.py (the same flag gates `--to`/`--reply`);
                                 the image's gog binary (gmail settings watch = Pub/Sub)
  - Send a file in chat ..... references/platform-primitives.md ("Send a file in chat" row)
                              <- apps/infra/hermes/bridge/turn.py (MEDIA_PREFIX, MEDIA_CONTENT_TYPES,
                                 OUTBOUND_MEDIA_MAX_BYTES, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, _within: own profile or
                                 HERMES_HOME/outbox/, no dot parts).
  - App host for links ...... SKILL.md ("Links: use this workspace's own app host")
                              <- core/services/owner_facts.py (`app_url` = settings.FRONTEND_URL)
  - Workspace memory ........ references/platform-primitives.md ("Queryable company state" row)
                              <- apps/infra/hermes/plugins/memory/agentcore/__init__.py (one /owners/<owner>/
                                 namespace for every agent); bridge/sync.py normalize_profile_backends
                                 (on only while AGENTCORE_MEMORY_ID is set); ~2 min = the memory
                                 strategy's async extraction lag, not a code constant.
  - Browser live view ....... references/surfaces.md
                              <- apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx;
                                 apps/backend/core/services/browser_sessions.py; apps/backend/core/services/browser_control.py; apps/infra/hermes/Dockerfile (agent-browser pin).
  - Control Panel tabs ...... references/surfaces.md
  - Paired Mac browser ...... references/surfaces.md
                              <- apps/backend/core/services/{desktop_execution,desktop_browser}.py;
                                 apps/infra/hermes/plugins/lightbulb/desktop_browser.py;
                                 apps/desktop/src-tauri/src/local_mcp.rs;
                                 apps/frontend/src/components/desktop/LocalBrowserSetup.tsx.
  - Paired Mac execution .... references/platform-primitives.md
                              <- apps/infra/hermes/plugins/lightbulb/desktop.py; bridge/turn.py;
                                 apps/backend/routers/desktop_execution.py;
                                 apps/backend/core/services/{desktop_execution,desktop_pair,connection_service}.py;
                                 apps/frontend/src/components/desktop/{DesktopPermissionsGate,NodeStatusPill}.tsx.
  - Shared local sessions ... references/platform-primitives.md + references/surfaces.md
                              <- apps/infra/hermes/bridge/ + apps/infra/hermes/plugins/lightbulb/__init__.py
                                 (lightbulb_local_sessions); apps/backend/core/services/{local_session_grants,local_sessions,local_session_relay,openclaw_work}.py;
                                 apps/backend/core/repositories/local_sessions_repo.py; apps/backend/routers/messaging.py;
                                 apps/desktop/src-tauri/src/local_sessions/{workspace,transport}.rs;
                                 apps/frontend/src/components/messaging/{LocalSessionPanel,NativeSessionTranscript,RoomDirectory,SessionFocus}.tsx; apps/desktop/src-tauri/skills/lightbulb-session-sharing/SKILL.md; SKILL.md (sessions routing row).
                                 Optional adapter/tool availability requires the promoted Hermes image; docs are not deployment evidence.
  - Workspace search ........ references/surfaces.md
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (search button + shortcut label);
                                 apps/frontend/src/components/teams/command-palette/CommandPalette.tsx (the Conversations group,
                                 rendered first); components/messaging/roomCommands.ts (useRoomCommandResults — rooms then
                                 people, degrades to [] via useMessagingWorkspaceOptional when no messaging provider is mounted,
                                 e.g. InlineTeamsPanel.tsx)
  - Message actions ......... references/surfaces.md
                              <- apps/frontend/src/components/messaging/{RoomMessageItem,RoomMessageContent}.tsx (reactions,
                                 reply-in-thread, edit/delete, the menuExtras-gated "more" menu open to every member);
                                 RoomPanel.tsx renderMessage (pin/unpin against pinnedMessageIds, mark-unread-from-here
                                 gated `!inThread`); messageLinks.ts (copy-link permalink format);
                                 RoomPanel.tsx renderRun (muted "<Agent> finished · no reply" / "stopped" / "run failed"
                                 status line for a silent agent turn)
  - Conversation list ....... references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomDirectory.tsx (group order, Manage sections
                                 dialog, drag/keyboard section reorder, Leave confirm); RoomDirectoryRow.tsx (the ⋯ menu,
                                 Move to section); roomSections.ts (groupRooms — Starred · custom · Channels · Direct);
                                 sidebarPrefs.ts (per-browser collapse + sort)
  - Lightbulb channel ....... references/surfaces.md
                              <- apps/backend/core/services/messaging.py (ensure_vendor_room, on_org_member_joined,
                                 is_vendor_room / _deny_vendor_human_change); core/config.py VENDOR_ORG_ID;
                                 apps/frontend/src/components/messaging/{RoomDirectoryRow,RoomHeader,RoomPanel}.tsx (Shared marker, gates)
  - Composer ................ references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomComposer.tsx (typeaheads, formatting toolbar,
                                 drag-and-drop, up-arrow edit); composerText.ts (caret grammar); mentions.ts
                                 resolveTypedMentions (typed tokens resolve on send); respondHint.ts (must mirror
                                 core/services/messaging.py select_dispatch_targets); drafts.ts (lightbulb.messaging.draft| keys)
  - Room header ............. references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomHeader.tsx (topic edit, member facepile,
                                 star/mute, the pinned-count affordance, the ⋯ overflow's Copy link/Rename/Details/Leave);
                                 messageLinks.ts (roomPermalink copy-link format); RoomPanel.tsx (Details tab pinned count);
                                 RoomDetailsPanel.tsx (the Members/Pins/Files/Links/About tabs)
  - Inbox page .............. apps/frontend/src/components/teams/inbox/InboxPage.tsx (the three bands);
                              InboxNeedsYou/InboxHappeningNow/InboxRecently.tsx;
                              apps/frontend/src/components/workspace/panelRegistry.ts (INBOX_SECTIONS + the teams-activity alias)
  - Tasks groupings ......... apps/frontend/src/components/teams/issues/TasksList.tsx (TASKS_GROUP_BY / GROUP_OPTIONS);
                              panelRegistry.ts (the groupBy param; teams-goals/teams-projects are their own
                              TEAMS_LIST_KEYS panels, not aliases, since 2026-09-24)
                              <- apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Questions / secrets ..... references/surfaces.md
  - Integrations wiring ..... references/integrations.md
                              <- apps/backend/routers/integrations.py; core/services/hermes_profiles.py (translate_mcp_servers)
  - Marketplace flows ....... references/marketplace.md
                              <- apps/backend/routers/{marketplace_listings,marketplace_import}.py; core/services/{marketplace_service,agent_export,catalog_service,claude_code_import}.py
  - Starter team + roster ... references/company-and-plans.md
                              <- apps/backend/core/services/paperclip_provisioning.py (STARTER_ROSTER + provision_org);
                                 core/services/{agent_provenance,paperclip_agent_lifecycle}.py;
                                 core/repositories/paperclip_repo.py (starter_agent_ids)
  - Plans / providers ....... references/company-and-plans.md
                              <- apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py
  - Provisioning rail ....... references/money.md
                              <- apps/backend/routers/company_provisioning.py (container routes, refusal codes);
                                 core/services/provisioning_workflow.py (STATES); core/services/owner_facts.py (the `provisioning` facts list)
  - Skills (dirs/scope) ..... references/skills.md
                              <- apps/backend/core/services/hermes_profiles.py (SKILLS_DIR_LIGHTBULB,
                                 SKILLS_DIR_HERMES_BUNDLED, SKILLS_EXTERNAL_DIRS, the `skills` config block);
                                 scripts/reapply_hermes_skills_dirs.py; the image's /opt/hermes/tools/skills_tool.py
                                 (the Ambiguous-skill-name refusal);
                                 paperclip fork server/src/routes/agents.ts — listSkills exists for
                                 claude/codex/cursor/gemini/grok/opencode/pi but NOT hermes_gateway,
                                 which is why the company-skills route answers supported:false
  - Skills / self-learning .. references/marketplace.md
                              <- apps/frontend/src/components/control/panels/{SkillsTab,SkillProposalsPanel}.tsx
  - Connectable catalog ..... references/integrations.md
                              <- apps/backend/routers/platform_info.py (live GET /api/v1/platform-info);
                                 core/services/credentials/registry.py (all_providers);
                                 core/services/hermes_profiles.py (build_profile_env — the ISOL8_API_URL the runtime reads)
  - Microsoft 365 (Graph) ... references/integrations.md
                              <- apps/backend/core/services/credentials/providers.yaml (microsoft365 scopes);
                                 apps/backend/routers/runtime_rpc.py (runtime_credentials — the per-turn token key);
                                 bridge/turn.py (runtime_mcp_env — the MCP_<KEY>_ACCESS_TOKEN name)
  - Agent-started connect ... references/integrations.md
                              <- apps/backend/routers/connections.py (authorize_from_container — hook-token sibling of authorize;
                                 ISOL8_OWNER_ID + OPENCLAW_HOOK_TOKEN env in hermes_profiles.build_profile_env);
                                 /app/skills/lightbulb/scripts/lightbulb-connect.sh (the mint door — never an inline header, see the redaction note)
  - Connections door ........ references/integrations.md + references/surfaces.md + SKILL.md boundaries table
                              <- apps/frontend/src/components/control/panels/ConnectionsPanel.tsx (rail panel); apps/frontend/src/components/workspace/WorkspaceHeader.tsx (rail label)
  - Paperclip org model ..... references/company-and-plans.md
                              <- TheLightbulbCompany/paperclip fork: server/src/services/agent-permissions.ts,
                                 services/agents.ts (getChainOfCommand), adapters/registry.ts (hermes_gateway)
  - Launch competition ...... references/surfaces.md
                              <- apps/backend/routers/launch.py; core/services/launch_{phase,settings,service,referral,close,revenue}.py
  - Email ................... references/email.md
                              <- /app/skills/lightbulb/scripts/lightbulb-mail.sh + backend routers/agent_mail.py + core/services/agent_mail_send.py (one send door; policy table in its docstring)
  - Post outside a turn ..... references/platform-primitives.md ("Post outside a room turn") + SKILL.md routing row
                              <- /app/skills/lightbulb/scripts/lightbulb-post.sh + backend routers/messaging.py (post_agent_post,
                                 /internal/messaging/agent-posts; the GET agent-messages/.../receipts/{eventId} pre-check is what prints "Already posted:") + core/services/messaging.py (resolve_agent_post_room — the
                                 room > user/handle > issue creator > #general order; then create_agent_message)
  - Hook-token calls ........ SKILL.md (the non-negotiable) + references/money.md (the status tables)
                              <- /app/skills/lightbulb/scripts/lightbulb-api.sh — every OPENCLAW_HOOK_TOKEN call goes through a script on disk;
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (_register_redaction) masks that header including $VAR refs,
                                 so an agent-typed curl poisons its own next turn (401 loop)
                              <- apps/infra/hermes/bridge/ (the run payload's message {id, threadRootMessageId}) and
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (send_room_message's threadRootMessageId);
                                 paperclip skill references/monitors.md;
                                 spec docs/superpowers/specs/2026-09-16-proactive-agents-design.md (delete when shipped).
