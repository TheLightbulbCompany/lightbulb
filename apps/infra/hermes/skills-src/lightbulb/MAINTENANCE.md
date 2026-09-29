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
                                 ATTACHMENT_MAX_BYTES_BY_TYPE incl. video, MAX_ATTACHMENTS, _within: own profile or
                                 HERMES_HOME/outbox/ (copied into <profile>/outbox/), no dot parts; _note replaces an
                                 unsendable line; videos go S3-direct via _upload_attachment) + the room-turn system
                                 prompt in the same file; sync.py save_profile (only the profile dir persists).
  - Shell working folder .... SKILL.md (scripts non-negotiable: "your cwd is this conversation's own folder")
                              + references/email.md
                              <- apps/infra/hermes/bridge/turn.py (conversation_dir, _terminal_scope TERMINAL_CWD).
  - App host for links ...... SKILL.md ("Links: use this workspace's own app host")
                              <- core/services/owner_facts.py (`app_url` = settings.FRONTEND_URL)
  - Workspace memory ........ references/platform-primitives.md ("Queryable company state" row)
                              <- apps/infra/hermes/plugins/memory/agentcore/__init__.py (one /owners/<owner>/
                                 namespace for every agent); bridge/sync.py normalize_profile_backends
                                 (on only while AGENTCORE_MEMORY_ID is set); ~2 min = the memory
                                 strategy's async extraction lag, not a code constant.
  - Website logins .......... SKILL.md ("Type a website password" row) + references/surfaces.md (Browser row)
                              <- apps/infra/hermes/bridge/website_logins.py (vault store + ask callbacks);
                                 core/services/website_logins.py; frontend WebsiteLoginsSection (Connections).
  - Browser live view ....... references/surfaces.md
                              <- apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx;
                                 apps/backend/core/services/browser_sessions.py; apps/backend/core/services/browser_control.py; apps/infra/hermes/Dockerfile (agent-browser pin);
                                 apps/infra/hermes/bridge/browser_control.py (shared-screen yield; one lease for
                                 the bridge, released ~30 s after the last run browses) + bridge/cdp_mux.py (one
                                 AWS connection, each run its own tab, the run's tabs closed at its end).
                                 The shared-screen facts (no take-control/Done, autosave, fresh snapshot after a
                                 person's input, sign-in by the person or the vault) must match both.
  - Browser tool choice ..... SKILL.md ("references/surfaces.md § Browser" row) + references/surfaces.md
                              (Browser row: "Which tool", extensions, recording, downloads) +
                              references/platform-primitives.md (pointer)
                              <- Hermes's own browser_cdp / browser_dialog / computer_use: the bridge points
                                 every CDP client at the run's loopback endpoint (browser_control.install,
                                 cdp_mux) and computer_use's driver at apps/infra/hermes/cua_driver.py (AWS
                                 InvokeBrowser; HERMES_CUA_DRIVER_CMD in bridge/__init__.py); managed overlay
                                 browser.allow_private_urls (chrome://, file:// etc.);
                                 IAM: bedrock_aip.ensure_owner_browser_policy + container-stack
                                 BoundaryAgentCoreBrowserUse (InvokeBrowser). Download dir and file:// facts
                                 were measured live 2026-09-27.
  - computer_use procedure .. references/surfaces.md (Browser row: "Operating computer_use")
                              <- cua_driver.py returns no elements (no element index, ax is empty);
                                 captures reach the model as images because hermes_profiles never
                                 pins auxiliary.vision, for any provider (else Hermes describes each)
  - Browser extensions ...... references/surfaces.md (Browser row: agent installs from the Web Store via
                              computer_use, the ask route that keeps it, member adds, the helper's Save-login bar)
                              <- apps/backend/core/services/browser_extensions.py (Web Store fetch, helper
                                 zip, the ask); routes in routers/container.py (/browser/extensions*,
                                 /browser/helper/*); apps/backend/browser_helper/ (the in-browser helper);
                                 the ask's Activity item in core/services/activity.py; frontend
                                 BrowserExtensionsSection + WebsiteLoginsSection (Connections, and the
                                 Browser panel bar popovers in BrowserDockPanel).
  - Control Panel tabs ...... references/surfaces.md
  - Paired Mac browser ...... references/surfaces.md ("Browser on a paired Mac": use_browser routing for
                              localhost/LAN/"my Mac", the "Lightbulb agent" window, show only for a login/2FA/CAPTCHA
                              ask, Paperclip asks in the issue thread; "Paired Mac tools" + "Local browser setup" rows)
                              + references/platform-primitives.md (lightbulb_desktop paragraph)
                              <- apps/infra/hermes/bridge/browser_control.py (select_mac/select_cloud, the Mac's
                                 sync + save before run.ended, the §7 close messages); bridge/cdp_mux.py (named
                                 upstreams, Lightbulb.humanInput, Lightbulb.show); bridge/browser_state.py
                                 (source "mac": cookies + localStorage via a Fetch-answered background tab, vetting); apps/backend/core/services/{mac_browser,mac_cdp}.py
                                 (MESSAGES are the agent's verbatim errors);
                                 apps/backend/core/services/{desktop_execution,desktop_browser}.py;
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
  - Session Details dialog .. references/surfaces.md (status line, Details tabs, Access switches, footer)
                              <- apps/frontend/src/components/messaging/LocalSessionPanel.tsx (status line, Details + Pick up
                                 buttons); SessionDetails.tsx (About/Changes/Access/Linked work tabs, the Access switches,
                                 footer Copy link / Message owner / Pick up); SessionShareLinks.tsx (Share snapshot…);
                                 LocalSessionWork.tsx (Link existing work).
  - Session pick-up ......... references/surfaces.md ("Pick up" paragraph)
                              <- apps/frontend/src/components/messaging/SessionPickup.tsx (canPickUp: allowed + snapshot ready;
                                 desktop-only, browser copy); SessionDetails.tsx (the pick-up switch: needs sharing on,
                                 upload warning, 7-day retention, Codex not yet).
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
                                 star/mute, the pinned-count affordance, and the ⋯ overflow actions);
                                 RoomPanel.tsx (Add people picker, channel/chat authorization, DM-to-group conversion,
                                 current-member filtering, and Details tab pinned count); apps/mobile/src/components/room/
                                 RoomDetails.tsx + PickerSheet.tsx (mobile Add people flow); apps/backend/core/services/
                                 messaging.py (server authorization and in-place DM conversion); messageLinks.ts
                                 (roomPermalink copy-link format); RoomDetailsPanel.tsx (Members/Pins/Files/Links/About)
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
  - Software projects ....... references/software-projects.md + SKILL.md routing row
                              + the Engineering persona (paperclip_provisioning.py STARTER_CAPABILITIES["engineering"])
                              <- core/services/credentials/providers.yaml (github scopes + gh_cli; vercel_cli paste_token +
                                 skill_cli/VERCEL_TOKEN; aws aws_sts tiers, PowerUserAccess = no IAM; cloudflare/sentry/posthog
                                 actor: mcp); credentials/skills/vercel/SKILL.md (the pinned npx form);
                                 apps/infra/hermes/bridge/sync.py (no DELETE propagation, HOME_EXCLUDE_GLOBS .gitconfig,
                                 _REPRODUCIBLE_DIRS node_modules/.venv/.next/dist/build, save at end of turn);
                                 apps/infra/hermes/Dockerfile (global npm installs as root, then USER 1000; no dockerd);
                                 the provisioning-rail fallback = core/config.py STRIPE_PROVISIONING_ENABLED +
                                 routers/company_provisioning.py container_catalog (`provisioning_unavailable`);
                                 Hermes's bundled github / requesting-code-review skills (/opt/hermes/skills, the base image);
                                 PyPI package graphifyy (its `graphify` entry point; `graphify query --graph`,
                                 `graphify update . --no-cluster` = AST only), mirrored by this repo's own
                                 .github/workflows/code-graph.yml;
                                 the review routine = skills-src/paperclip/references/routines.md (an agent may create a
                                 routine assigned to itself; `schedule` trigger) + references/platform-primitives.md
                                 (Automations: a GitHub trigger added from the routine's own detail panel, owner-side).
  - Skills (dirs/scope) ..... references/skills.md
                              <- apps/backend/core/services/hermes_profiles.py (SKILLS_DIR_LIGHTBULB,
                                 SKILLS_DIR_HERMES_BUNDLED, SKILLS_EXTERNAL_DIRS, the `skills` config block);
                                 scripts/reapply_hermes_skills_dirs.py; the image's /opt/hermes/tools/skills_tool.py
                                 (the Ambiguous-skill-name refusal);
                                 paperclip fork server/src/routes/agents.ts — listSkills exists for
                                 claude/codex/cursor/gemini/grok/opencode/pi but NOT hermes_gateway,
                                 which is why the company-skills route answers supported:false
  - Skills / self-learning .. references/marketplace.md
                              <- apps/frontend/src/components/control/panels/SkillsTab.tsx
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
  - Teammates in a room ..... references/platform-primitives.md ("Teammates in a room") + SKILL.md non-negotiable + routing row
                              <- backend core/services/messaging.py (AGENT_REPLY_CAP, the hop on create_agent_message: human 0,
                                 run reply = trigger + 1, no-run post = 1; capped messages get no dispatch targets, mentions
                                 included, and one system "… paused after N replies" notice) + openclaw_work.py (Jev
                                 fallback wakes nobody on an agent-authored message); design 2026-09-25 agent conversation
  - Post outside a turn ..... references/platform-primitives.md ("Post outside a room turn") + SKILL.md routing row
                              <- /app/skills/lightbulb/scripts/lightbulb-post.sh + backend routers/messaging.py (post_agent_post,
                                 /internal/messaging/agent-posts; the GET agent-messages/.../receipts/{eventId} pre-check is what prints "Already posted:") + core/services/messaging.py (resolve_agent_post_room — the
                                 room > user/handle > issue creator > #general order; then create_agent_message)
  - Chat vs document ........ references/chat-vs-document.md + SKILL.md (non-negotiable + routing row)
                              <- paperclip fork server/src/routes/issues.ts (PUT /issues/:id/documents/:key:
                                 assertAgentIssueMutationAllowed — own/unassigned idle issue, no run id needed;
                                 lockedDocumentStrategy create_new_document, hence "link the returned key");
                                 packages/shared/src/validators/issue.ts issueDocumentKeySchema;
                                 apps/frontend/src/components/messaging/RoomDocumentCards.tsx +
                                 packages/shared/src/messaging/documentLinks.ts (the card the link becomes);
                                 plugins/lightbulb/__init__.py OWN_THREAD_REFUSAL (why no mid-turn acknowledgement)
  - Hook-token calls ........ SKILL.md (the non-negotiable) + references/money.md (the status tables)
                              <- /app/skills/lightbulb/scripts/lightbulb-api.sh — every OPENCLAW_HOOK_TOKEN call goes through a script on disk;
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (_register_redaction) masks that header including $VAR refs,
                                 so an agent-typed curl poisons its own next turn (401 loop)
                              <- apps/infra/hermes/bridge/ (the run payload's message {id, threadRootMessageId}) and
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (send_room_message's threadRootMessageId);
                                 paperclip skill references/monitors.md;
                                 spec docs/superpowers/specs/2026-09-16-proactive-agents-design.md (delete when shipped).
